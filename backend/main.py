import asyncio
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pydantic import BaseModel, field_validator, model_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings

logger = logging.getLogger("dashhq")

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Dash HQ API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Backup net: any route that raises something we didn't explicitly plan for
# (a third-party API changing shape, a network hiccup mid-request, etc.)
# lands here instead of crashing the function or leaking a raw traceback.
# HTTPException and validation errors already have their own clean handling
# in FastAPI, so this only catches genuinely unexpected failures.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "Something went wrong. Please try again."})

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        "http://localhost:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

DISCORD_API = "https://discord.com/api/v10"


def _oauth_url() -> str:
    scopes = "identify" if settings.discord_bot_token else "identify guilds"
    params = {
        "client_id": settings.discord_client_id,
        "redirect_uri": settings.discord_redirect_uri,
        "response_type": "code",
        "scope": scopes,
    }
    return f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"


def _avatar_url(user: dict) -> str:
    if user.get("avatar"):
        return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=128"
    idx = (int(user["id"]) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"


def _member_avatar_url(guild_id: str, user: dict, member: dict) -> str:
    """Prefer the citizen's server-specific (guild) avatar/pfp, fetched via
    the bot, over their global Discord avatar — matches what members actually
    see of each other inside the server, not their profile elsewhere."""
    guild_avatar = member.get("avatar")
    if guild_avatar:
        return f"https://cdn.discordapp.com/guilds/{guild_id}/users/{user['id']}/avatars/{guild_avatar}.png?size=128"
    return _avatar_url(user)


@app.get("/auth/discord")
async def discord_login():
    return RedirectResponse(_oauth_url())


@app.get("/auth/discord/callback")
@limiter.limit("10/minute")
async def discord_callback(request: Request, code: str = None, error: str = None):
    portal = f"{settings.frontend_url}/"

    if error or not code:
        return RedirectResponse(f"{portal}?error=access_denied")

    try:
        return await _discord_callback_flow(code)
    except Exception:
        # Any surprise here (Discord API hiccup, unexpected response shape)
        # should bounce the user back to the site with a clear error state,
        # not strand them on a raw JSON crash page mid-login.
        logger.exception("Discord OAuth callback failed")
        return RedirectResponse(f"{portal}?error=server_error")


async def _discord_callback_flow(code: str) -> RedirectResponse:
    portal = f"{settings.frontend_url}/"
    async with httpx.AsyncClient() as client:
        # 1. Exchange code for access token
        token_res = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": settings.discord_client_id,
                "client_secret": settings.discord_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.discord_redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_res.status_code != 200:
            return RedirectResponse(f"{portal}?error=token_failed")

        access_token = token_res.json()["access_token"]

        # 2. Fetch Discord user identity
        user_res = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_res.status_code != 200:
            return RedirectResponse(f"{portal}?error=user_fetch_failed")

        user = user_res.json()
        user_id = user["id"]

        # 3. Check guild membership + Citizen role
        is_member = False
        tier = "CITIZEN"
        nick = None
        joined_year = None
        avatar_url = _avatar_url(user)

        if settings.discord_bot_token:
            member_res = await client.get(
                f"{DISCORD_API}/guilds/{settings.discord_guild_id}/members/{user_id}",
                headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            )
            if member_res.status_code == 200:
                m = member_res.json()
                roles = m.get("roles", [])
                nick = m.get("nick")
                raw_joined = m.get("joined_at", "")
                joined_year = raw_joined[:4] if raw_joined else None
                # Pull pfp + details from the bot's guild member record, not
                # the OAuth identity — reflects the real in-server profile.
                avatar_url = _member_avatar_url(settings.discord_guild_id, user, m)

                # Membership requires holding the Citizen role, not just guild presence.
                is_member = (
                    settings.citizen_role_id in roles
                    if settings.citizen_role_id
                    else True
                )
        else:
            guilds_res = await client.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if guilds_res.status_code == 200:
                guilds = guilds_res.json()
                is_member = any(g["id"] == settings.discord_guild_id for g in guilds)

        display_name = nick or user.get("global_name") or user["username"]

        payload = {
            "sub": user_id,
            "display_name": display_name.upper(),
            "handle": f"@{user['username']}",
            "avatar": avatar_url,
            "is_member": is_member,
            "tier": tier,
            "joined": joined_year or "—",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }

        token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
        return RedirectResponse(f"{portal}?token={token}")


@app.get("/auth/me")
async def auth_me(token: str = Query(...)):
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def _fetch_all_guild_members(client: httpx.AsyncClient) -> list[dict]:
    members = []
    after = "0"
    while True:
        res = await client.get(
            f"{DISCORD_API}/guilds/{settings.discord_guild_id}/members",
            params={"limit": 1000, "after": after},
            headers={"Authorization": f"Bot {settings.discord_bot_token}"},
        )
        res.raise_for_status()
        page = res.json()
        if not page:
            break
        members.extend(page)
        after = page[-1]["user"]["id"]
        if len(page) < 1000:
            break
    return members


def _member_row(m: dict) -> dict:
    user = m["user"]
    roles = m.get("roles", [])
    nick = m.get("nick")
    display_name = nick or user.get("global_name") or user["username"]
    return {
        "discord_id": user["id"],
        "username": user["username"],
        "global_name": user.get("global_name"),
        "nickname": nick,
        "display_name": display_name,
        "avatar_url": _member_avatar_url(settings.discord_guild_id, user, m),
        "roles": roles,
        "tier": "CITIZEN",
        "joined_at": m.get("joined_at"),
        "is_active": True,
        "left_at": None,
    }


async def _supabase_upsert_members(client: httpx.AsyncClient, rows: list[dict]) -> None:
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        res = await client.post(
            f"{settings.supabase_url}/rest/v1/members",
            headers=headers,
            json=batch,
        )
        res.raise_for_status()


async def _supabase_mark_departed(client: httpx.AsyncClient, run_started_at: str) -> None:
    # Any row still marked active that this run didn't touch has left the guild.
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    res = await client.patch(
        f"{settings.supabase_url}/rest/v1/members",
        headers=headers,
        params={"is_active": "eq.true", "updated_at": f"lt.{run_started_at}"},
        json={"is_active": False, "left_at": datetime.now(timezone.utc).isoformat()},
    )
    res.raise_for_status()


@app.get("/cron/sync-members")
async def sync_members(request: Request):
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    run_started_at = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=30) as client:
        guild_members = await _fetch_all_guild_members(client)
        rows = [_member_row(m) for m in guild_members if not m["user"].get("bot")]
        await _supabase_upsert_members(client, rows)
        await _supabase_mark_departed(client, run_started_at)

    return {"synced": len(rows), "run_started_at": run_started_at}


# ── Citizenship applications ─────────────────────────────────────────────────

X_PROFILE_RE = re.compile(
    r"^(https?://)?(www\.)?(x\.com|twitter\.com)/[A-Za-z0-9_]{2,}/?$|^@?[A-Za-z0-9_]{2,}$"
)


class ApplicationIn(BaseModel):
    name: str
    x: str
    intro: str
    communities: str
    value: str
    followedTeam: bool
    website: str = ""  # honeypot — real users never populate this

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("intro", "value")
    @classmethod
    def _min_8_words(cls, v: str) -> str:
        v = v.strip()
        if len(v.split()) < 8:
            raise ValueError("must be at least 8 words — give a real answer, not a one-liner")
        if len(v) > 600:
            raise ValueError("must be 600 characters or fewer — keep it concise")
        return v

    @field_validator("communities")
    @classmethod
    def _min_2_words(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 600:
            raise ValueError("must be 600 characters or fewer — keep it concise")
        if len(v.split()) < 2:
            raise ValueError("list at least one real community")
        return v

    @field_validator("x")
    @classmethod
    def _valid_x_profile(cls, v: str) -> str:
        v = v.strip()
        if not X_PROFILE_RE.match(v):
            raise ValueError("must be a valid X profile link or handle")
        # Normalize to a full URL — a bare handle like "@name" isn't a valid
        # link target, so without this the Discord follow-up's clickable
        # profile link would silently fail to render as clickable.
        if v.startswith("http://") or v.startswith("https://"):
            return v
        return f"https://x.com/{v.lstrip('@')}"

    @field_validator("followedTeam")
    @classmethod
    def _must_have_followed(cls, v: bool) -> bool:
        if not v:
            raise ValueError("must confirm following the team")
        return v


def _supabase_headers(prefer: str = "return=representation") -> dict:
    return {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def _trunc(s: str, n: int = 1000) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _x_profile_button(x_profile: str) -> dict:
    # A Link-style button (style 5) — Discord opens the URL directly on
    # click, no interaction/custom_id involved. Kept in the message's
    # components permanently, including after Accept/Decline replaces the
    # other buttons, so the team can always reach the applicant's profile.
    return {"type": 2, "style": 5, "label": "View X Profile", "url": x_profile}


def _application_embed(app_row: dict, status: str = "pending", reviewer: str = None, invite_url: str = None) -> dict:
    color = {"pending": 0x1B42FF, "accepted": 0x10B981, "declined": 0xEF4444}[status]
    footer = f"Application ID: {app_row['id']}"
    if status != "pending":
        icon = "✅" if status == "accepted" else "❌"
        footer = f"{icon} {status.capitalize()} by {reviewer} · {footer}"
    fields = [
        {"name": "Name / Alias", "value": _trunc(app_row["name"]), "inline": True},
        {"name": "X Profile", "value": _trunc(app_row["x_profile"]), "inline": True},
        {"name": "Intro & Role", "value": _trunc(app_row["intro"]), "inline": False},
        {"name": "Communities", "value": _trunc(app_row["communities"]), "inline": False},
        {"name": "Adding Value", "value": _trunc(app_row["value"]), "inline": False},
    ]
    if invite_url:
        # Plain text, not a button, so it can just be selected and copied
        # straight out of the embed to hand to the applicant.
        fields.append({"name": "Invite Link (one-time use)", "value": invite_url, "inline": False})
    return {
        "title": f"New Citizenship Application — {app_row['name']}",
        "color": color,
        "fields": fields,
        "footer": {"text": footer},
    }


async def _create_one_time_invite(client: httpx.AsyncClient) -> str | None:
    # Single-use, single-person invite for a freshly accepted applicant.
    # Discord disables the code itself once max_uses is hit — no cleanup
    # needed on our end. Not fatal if this fails (missing permission, channel
    # not configured, etc.) — the accept action itself should still succeed.
    if not settings.discord_invite_channel_id:
        return None
    try:
        res = await client.post(
            f"{DISCORD_API}/channels/{settings.discord_invite_channel_id}/invites",
            headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            json={"max_age": 604800, "max_uses": 1, "unique": True},  # 7-day window, one use
        )
        res.raise_for_status()
        code = res.json()["code"]
        return f"https://discord.gg/{code}"
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("Failed to create one-time Discord invite")
        return None


async def _discord_post_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict, json_body: dict, max_retries: int = 3
) -> httpx.Response:
    # A burst of applications can hit Discord's per-route rate limit. Rather
    # than silently dropping the team notification, back off for exactly as
    # long as Discord asks (Retry-After) and try again, a few times, before
    # giving up — the application itself is already safely saved regardless.
    for attempt in range(max_retries + 1):
        res = await client.post(url, headers=headers, json=json_body)
        if res.status_code != 429 or attempt == max_retries:
            return res
        retry_after = float(res.headers.get("Retry-After") or res.json().get("retry_after", 1))
        await asyncio.sleep(min(retry_after, 10))
    return res


@app.post("/applications")
@limiter.limit("5/hour")
async def submit_application(request: Request, application: ApplicationIn):
    if application.website:
        # Honeypot tripped — pretend success without saving or notifying anyone,
        # so scripted submitters don't learn to adapt.
        return {"status": "received"}

    row = {
        "name": application.name,
        "x_profile": application.x,
        "intro": application.intro,
        "communities": application.communities,
        "value": application.value,
        "followed_team": application.followedTeam,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(),
            json=row,
        )
        res.raise_for_status()
        saved = res.json()[0]

        # Notify the team via a bot message with Accept/Decline buttons. If this
        # fails for any reason, the application is still safely saved above —
        # we don't want a Discord hiccup to lose someone's submission, or to
        # make the applicant see an error when their submission went through.
        if settings.discord_bot_token and settings.discord_applications_channel_id:
            try:
                components = [{
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 3, "label": "Accept", "custom_id": f"accept:{saved['id']}"},
                        {"type": 2, "style": 4, "label": "Decline", "custom_id": f"decline:{saved['id']}"},
                        _x_profile_button(saved["x_profile"]),
                    ],
                }]
                msg_res = await _discord_post_with_retry(
                    client,
                    f"{DISCORD_API}/channels/{settings.discord_applications_channel_id}/messages",
                    {"Authorization": f"Bot {settings.discord_bot_token}"},
                    {"embeds": [_application_embed(saved)], "components": components},
                )
                if msg_res.status_code < 300:
                    msg = msg_res.json()
                    patch_res = await client.patch(
                        f"{settings.supabase_url}/rest/v1/applications",
                        headers=_supabase_headers(prefer="return=minimal"),
                        params={"id": f"eq.{saved['id']}"},
                        json={
                            "discord_message_id": msg["id"],
                            "discord_channel_id": settings.discord_applications_channel_id,
                        },
                    )
                    patch_res.raise_for_status()
            except (httpx.HTTPError, KeyError, ValueError):
                logger.exception("Discord notification failed for application %s", saved.get("id"))

    return {"status": "received"}


def _verify_discord_signature(signature: str, timestamp: str, body: bytes) -> bool:
    if not settings.discord_public_key or not signature or not timestamp:
        return False
    try:
        verify_key = VerifyKey(bytes.fromhex(settings.discord_public_key))
        verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, ValueError):
        return False


@app.post("/discord/interactions")
async def discord_interactions(request: Request):
    body = await request.body()
    signature = request.headers.get("x-signature-ed25519", "")
    timestamp = request.headers.get("x-signature-timestamp", "")

    if not _verify_discord_signature(signature, timestamp, body):
        raise HTTPException(status_code=401, detail="Invalid request signature")

    payload = await request.json()
    itype = payload.get("type")

    if itype == 1:  # PING — Discord sends this to validate the endpoint URL
        return {"type": 1}

    if itype != 3:  # not a message-component (button) interaction
        return {"type": 4, "data": {"content": "Unsupported interaction.", "flags": 64}}

    custom_id = payload.get("data", {}).get("custom_id", "")
    action, _, app_id = custom_id.partition(":")
    if action not in ("accept", "decline") or not app_id:
        return {"type": 4, "data": {"content": "Unrecognized action.", "flags": 64}}

    member_user = payload.get("member", {}).get("user", {})
    reviewer = member_user.get("global_name") or member_user.get("username", "someone")
    status = "accepted" if action == "accept" else "declined"

    # A Supabase hiccup here shouldn't surface as a broken interaction with
    # no readable message — fall back to a clear ephemeral reply so the mod
    # knows to just click the button again, instead of Discord showing
    # "This interaction failed" with no explanation.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(
                f"{settings.supabase_url}/rest/v1/applications",
                headers=_supabase_headers(),
                params={"id": f"eq.{app_id}", "select": "*"},
            )
            res.raise_for_status()
            rows = res.json()
            if not rows:
                return {"type": 4, "data": {"content": "Application not found.", "flags": 64}}
            application = rows[0]

            patch_res = await client.patch(
                f"{settings.supabase_url}/rest/v1/applications",
                headers=_supabase_headers(prefer="return=minimal"),
                params={"id": f"eq.{app_id}"},
                json={
                    "status": status,
                    "reviewed_by": reviewer,
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            patch_res.raise_for_status()

            invite_url = None
            if status == "accepted" and settings.discord_bot_token:
                invite_url = await _create_one_time_invite(client)
    except (httpx.HTTPError, KeyError, IndexError):
        logger.exception("Discord interaction failed for application %s", app_id)
        return {"type": 4, "data": {"content": "Something went wrong saving that — please try the button again.", "flags": 64}}

    # Accept/Decline buttons are done their job and go away, but the X
    # profile link stays on the message permanently so the team can always
    # reach the applicant, whatever the decision.
    updated_components = [{"type": 1, "components": [_x_profile_button(application["x_profile"])]}]

    return {
        "type": 7,  # UPDATE_MESSAGE — edits the original message in place
        "data": {
            "embeds": [_application_embed(application, status=status, reviewer=reviewer, invite_url=invite_url)],
            "components": updated_components,
        },
    }


# Most public RPC endpoints don't send CORS headers (they're built for
# server/wallet use, not raw browser fetch), so gas price has to be proxied
# server-side rather than called directly from the client like the other tools.
GAS_CHAINS = {
    "ethereum": {"rpc": "https://ethereum-rpc.publicnode.com", "coingecko_id": "ethereum"},
    "bsc": {"rpc": "https://bsc-rpc.publicnode.com", "coingecko_id": "binancecoin"},
    "polygon": {"rpc": "https://polygon-bor-rpc.publicnode.com", "coingecko_id": "matic-network"},
    "arbitrum": {"rpc": "https://arbitrum-one-rpc.publicnode.com", "coingecko_id": "ethereum"},
    "optimism": {"rpc": "https://optimism-rpc.publicnode.com", "coingecko_id": "ethereum"},
    "base": {"rpc": "https://base-rpc.publicnode.com", "coingecko_id": "ethereum"},
    "avalanche": {"rpc": "https://avalanche-c-chain-rpc.publicnode.com", "coingecko_id": "avalanche-2"},
}


# Warm-instance cache: coingecko id -> (fetched_at, {usd, usd_24h_change}).
# CoinGecko's free API has a real rate limit that a burst of concurrent
# citizens (or rapid chain/tool switching) can trip together — short-lived
# caching means those requests reuse one upstream call, and a rate-limit
# hiccup serves the last known price instead of an error.
_PRICE_CACHE: dict[str, tuple[float, dict]] = {}
_PRICE_TTL = 20  # seconds
_CACHE_MAX_SIZE = 500  # generous for this app's real traffic; just a backstop


def _cap_cache(cache: dict) -> None:
    # A warm serverless instance can live for a while — this is a cheap
    # backstop against unbounded growth, not a real eviction policy. A full
    # reset is fine since every entry is trivially re-fetchable.
    if len(cache) > _CACHE_MAX_SIZE:
        cache.clear()


async def _get_coingecko_prices(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
    now = time.time()
    stale = [i for i in ids if i not in _PRICE_CACHE or now - _PRICE_CACHE[i][0] > _PRICE_TTL]
    if stale:
        try:
            res = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": ",".join(sorted(set(stale))),
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                },
            )
            res.raise_for_status()
            for coin_id, d in res.json().items():
                if d and "usd" in d:
                    _PRICE_CACHE[coin_id] = (now, d)
            _cap_cache(_PRICE_CACHE)
        except httpx.HTTPError:
            pass  # fall through — serve whatever's cached, even if stale
    return {i: _PRICE_CACHE[i][1] for i in ids if i in _PRICE_CACHE}


# Solana has no "gas price" in the EVM sense — the base network fee is a
# fixed protocol constant (5000 lamports/signature), and the variable part
# is a priority fee (micro-lamports per compute unit) that recent blocks
# actually paid. Handled as its own branch below rather than forced into
# the eth_gasPrice-shaped GAS_CHAINS map.
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOLANA_BASE_FEE_LAMPORTS = 5000


async def _fetch_solana_priority_fees(client: httpx.AsyncClient) -> dict | None:
    try:
        res = await client.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getRecentPrioritizationFees", "params": []},
        )
        res.raise_for_status()
        fees = sorted(r["prioritizationFee"] for r in res.json().get("result", []))
        if not fees:
            return None
        return {
            "slow": fees[int(len(fees) * 0.25)],
            "avg": fees[len(fees) // 2],
            "fast": fees[int(len(fees) * 0.9)],
        }
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        return None


@app.get("/toolkit/gas")
@limiter.limit("60/minute")
async def toolkit_gas(request: Request, chain: str = Query("ethereum")):
    if chain == "solana":
        async with httpx.AsyncClient(timeout=10) as client:
            fees = await _fetch_solana_priority_fees(client)
            prices = await _get_coingecko_prices(client, ["solana"])
            native_data = prices.get("solana")
        return {
            "gwei": None,
            "native_usd": native_data["usd"] if native_data else None,
            "solana_fees": fees,
            "solana_base_fee_lamports": SOLANA_BASE_FEE_LAMPORTS,
        }

    if chain not in GAS_CHAINS:
        raise HTTPException(status_code=400, detail="unsupported chain")
    cfg = GAS_CHAINS[chain]

    async with httpx.AsyncClient(timeout=10) as client:
        gwei = None
        try:
            rpc_res = await client.post(
                cfg["rpc"],
                json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1},
                headers={"Content-Type": "application/json"},
            )
            rpc_res.raise_for_status()
            gwei = int(rpc_res.json()["result"], 16) / 1e9
        except (httpx.HTTPError, KeyError, ValueError):
            pass

        prices = await _get_coingecko_prices(client, [cfg["coingecko_id"]])
        native_data = prices.get(cfg["coingecko_id"])
        native_usd = native_data["usd"] if native_data else None

    return {"gwei": gwei, "native_usd": native_usd}


# Warm-instance cache: symbol -> coingecko id. Resolving a symbol via /search
# is the slow part; once resolved it never changes, so reuse it across
# requests for the lifetime of this serverless instance.
_COIN_ID_CACHE: dict[str, str] = {}


@app.get("/toolkit/ticker")
@limiter.limit("60/minute")
async def toolkit_ticker(request: Request, symbols: str = Query(...)):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:20]
    if not syms:
        return {}

    async with httpx.AsyncClient(timeout=10) as client:
        # Resolve any symbols we haven't seen before (one /search call each,
        # only for cache misses) — everything else piggybacks on the cache.
        to_resolve = [s for s in syms if s not in _COIN_ID_CACHE]
        if to_resolve:
            resolve_tasks = [
                client.get("https://api.coingecko.com/api/v3/search", params={"query": s})
                for s in to_resolve
            ]
            results = await asyncio.gather(*resolve_tasks, return_exceptions=True)
            for sym, res in zip(to_resolve, results):
                if isinstance(res, Exception) or res.status_code != 200:
                    continue
                try:
                    coins = res.json().get("coins") or []
                except ValueError:
                    continue  # malformed body for this one symbol — skip it, don't fail the batch
                exact = next((c for c in coins if (c.get("symbol") or "").upper() == sym), None)
                pick = exact or (coins[0] if coins else None)
                if pick:
                    _COIN_ID_CACHE[sym] = pick["id"]
            _cap_cache(_COIN_ID_CACHE)

        ids_by_symbol = {s: _COIN_ID_CACHE[s] for s in syms if s in _COIN_ID_CACHE}
        out: dict[str, dict] = {s: {"error": True} for s in syms}
        if not ids_by_symbol:
            return out

        prices = await _get_coingecko_prices(client, list(ids_by_symbol.values()))
        for sym, coin_id in ids_by_symbol.items():
            d = prices.get(coin_id)
            if d and "usd" in d:
                out[sym] = {"price": d["usd"], "chg": d.get("usd_24h_change") or 0}

    return out


# honeypot.is simulates EVM chains; Solana has no equivalent buy/sell
# simulator, so it's checked separately via rugcheck.xyz's public API
# (mint/freeze authority, liquidity, and known risk flags instead of a tax
# simulation) — a different kind of check, not a lesser one.
ALLOWED_CHAIN_IDS = {1, 56, 137, 42161, 10, 8453, 43114}
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class RugCheckIn(BaseModel):
    address: str
    chain_id: str = "1"  # numeric-string EVM chain id, or the literal "solana"

    @model_validator(mode="after")
    def _validate(self) -> "RugCheckIn":
        self.address = self.address.strip()
        if self.chain_id == "solana":
            if not SOLANA_MINT_RE.match(self.address):
                raise ValueError("must be a valid Solana token mint address")
        else:
            if self.chain_id not in {str(c) for c in ALLOWED_CHAIN_IDS}:
                raise ValueError("unsupported chain")
            if not re.match(r"^0x[a-fA-F0-9]{40}$", self.address):
                raise ValueError("must be a valid EVM contract address (0x...)")
        return self


async def _rug_check_evm(client: httpx.AsyncClient, address: str, chain_id: str) -> dict:
    try:
        res = await client.get(
            "https://api.honeypot.is/v2/IsHoneypot",
            params={"address": address, "chainID": int(chain_id)},
        )
        res.raise_for_status()
        data = res.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Could not reach the honeypot scanner")

    honeypot = data.get("honeypotResult") or {}
    simulation = data.get("simulationResult") or {}
    contract = data.get("contractCode") or {}
    pair = data.get("pair") or {}
    summary = data.get("summary") or {}

    is_honeypot = bool(honeypot.get("isHoneypot"))
    buy_tax = simulation.get("buyTax")
    sell_tax = simulation.get("sellTax")
    open_source = contract.get("openSource")
    liquidity = pair.get("liquidity") if isinstance(pair, dict) else None

    checks = [
        {"label": "Not flagged as a honeypot", "pass": not is_honeypot},
        {"label": "Contract source is verified/open", "pass": bool(open_source)},
        {"label": "Buy tax under 10%", "pass": buy_tax is None or buy_tax < 10},
        {"label": "Sell tax under 10%", "pass": sell_tax is None or sell_tax < 10},
        {"label": "Has active liquidity", "pass": bool(liquidity) and liquidity > 0},
    ]

    risk = (summary.get("risk") or "").lower()
    if is_honeypot or risk == "high" or (sell_tax is not None and sell_tax >= 50):
        level, label = "high", "High Risk"
    elif risk == "medium" or not open_source or (sell_tax is not None and sell_tax >= 10):
        level, label = "medium", "Caution"
    else:
        level, label = "low", "Looks Clean"

    return {"level": level, "label": label, "checks": checks}


async def _rug_check_solana(client: httpx.AsyncClient, mint: str) -> dict:
    try:
        res = await client.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        if res.status_code == 400:
            raise HTTPException(status_code=400, detail="That doesn't look like a real Solana token mint")
        res.raise_for_status()
        data = res.json()
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not reach the Solana risk scanner")
    except ValueError:
        raise HTTPException(status_code=502, detail="Could not reach the Solana risk scanner")

    rugged = bool(data.get("rugged"))
    score = data.get("score_normalised") or 0
    risks = data.get("risks") or []
    has_danger = any((r.get("level") or "").lower() in ("danger", "high") for r in risks)
    mint_authority = data.get("mintAuthority")
    freeze_authority = data.get("freezeAuthority")
    liquidity = data.get("totalMarketLiquidity") or 0

    checks = [
        {"label": "Not flagged as rugged", "pass": not rugged},
        {"label": "Mint authority renounced", "pass": mint_authority is None},
        {"label": "Freeze authority renounced", "pass": freeze_authority is None},
        {"label": "No high-severity risk flags", "pass": not has_danger},
        {"label": "Has active liquidity", "pass": liquidity > 0},
    ]

    if rugged or has_danger or score >= 60:
        level, label = "high", "High Risk"
    elif score >= 25 or mint_authority is not None or freeze_authority is not None:
        level, label = "medium", "Caution"
    else:
        level, label = "low", "Looks Clean"

    return {"level": level, "label": label, "checks": checks}


@app.post("/toolkit/rug-check")
@limiter.limit("20/minute")
async def rug_check(request: Request, payload: RugCheckIn):
    # Proxied server-side (rather than called from the browser) so these free
    # APIs aren't hit by an uncontrolled client fan-out, and so they share
    # the same slowapi rate limiting as the rest of the API.
    async with httpx.AsyncClient(timeout=10) as client:
        if payload.chain_id == "solana":
            return await _rug_check_solana(client, payload.address)
        return await _rug_check_evm(client, payload.address, payload.chain_id)


# ── OpenSea key management ────────────────────────────────────────────────
# OpenSea's "instant" API key (POST /api/v2/auth/keys, no signup) is free
# and keyless to obtain, but expires after 30 days *and OpenSea only allows
# minting one per hour, total, from this site's traffic*. An in-memory-only
# cache works fine for one warm serverless instance, but under a real
# traffic spike Vercel spins up several instances in parallel, each with
# its own empty cache — if each independently tries to mint a key on its
# first request, every one after the first gets hard-locked-out (429) for
# an hour, killing every NFT feature site-wide. Supabase is the shared
# backstop: check it before minting, and write to it after minting, so at
# most one instance across the whole fleet ever actually calls OpenSea.
_opensea_key: str | None = None
_opensea_key_expiry: float = 0
_OPENSEA_KEY_TABLE = "opensea_key_cache"


async def _supabase_read_opensea_key(client: httpx.AsyncClient) -> tuple[str, float] | None:
    if not (settings.supabase_url and settings.supabase_service_role_key):
        return None
    try:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/{_OPENSEA_KEY_TABLE}",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
            params={"id": "eq.1", "select": "api_key,expires_at"},
        )
        res.raise_for_status()
        rows = res.json()
        if not rows:
            return None
        expiry_ts = datetime.fromisoformat(rows[0]["expires_at"].replace("Z", "+00:00")).timestamp()
        if time.time() >= expiry_ts - 3600:
            return None
        return rows[0]["api_key"], expiry_ts
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None


async def _supabase_write_opensea_key(client: httpx.AsyncClient, api_key: str, expiry_ts: float) -> None:
    if not (settings.supabase_url and settings.supabase_service_role_key):
        return
    try:
        res = await client.post(
            f"{settings.supabase_url}/rest/v1/{_OPENSEA_KEY_TABLE}",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=[{"id": 1, "api_key": api_key, "expires_at": datetime.fromtimestamp(expiry_ts, tz=timezone.utc).isoformat()}],
        )
        res.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to persist OpenSea key to Supabase")


async def _get_opensea_key(client: httpx.AsyncClient) -> str | None:
    global _opensea_key, _opensea_key_expiry
    if _opensea_key and time.time() < _opensea_key_expiry - 3600:
        return _opensea_key

    shared = await _supabase_read_opensea_key(client)
    if shared:
        _opensea_key, _opensea_key_expiry = shared
        return _opensea_key

    try:
        res = await client.post("https://api.opensea.io/api/v2/auth/keys")
        res.raise_for_status()
        data = res.json()
        _opensea_key = data["api_key"]
        _opensea_key_expiry = time.time() + 29 * 24 * 3600
        await _supabase_write_opensea_key(client, _opensea_key, _opensea_key_expiry)
        return _opensea_key
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("Failed to obtain an OpenSea API key")
        # A concurrent instance may have just won this exact race and
        # already written a fresh key - one more shared-cache check before
        # giving up, so a burst of simultaneous cold starts doesn't turn
        # into every instance but one failing outright.
        shared = await _supabase_read_opensea_key(client)
        if shared:
            _opensea_key, _opensea_key_expiry = shared
            return _opensea_key
        return None


async def _opensea_get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict | None:
    key = await _get_opensea_key(client)
    if not key:
        return None
    try:
        res = await client.get(
            "https://api.opensea.io/api/v2" + path,
            params=params or {},
            headers={"X-API-KEY": key},
        )
        if res.status_code == 401:
            # Key was revoked/expired early — force a fresh one and retry once.
            global _opensea_key
            _opensea_key = None
            key = await _get_opensea_key(client)
            if not key:
                return None
            res = await client.get(
                "https://api.opensea.io/api/v2" + path,
                params=params or {},
                headers={"X-API-KEY": key},
            )
        res.raise_for_status()
        return res.json()
    except (httpx.HTTPError, ValueError):
        return None


# Search results come from OpenSea's lean /search endpoint, which doesn't
# include safelist_status/category/contracts/etc. - only the single-
# collection and listing endpoints do. Rather than run a scheduled job just
# to pre-fetch verification status (a real cron costs one of the very few
# free-tier slots for a cosmetic feature), this cache fills itself for free
# from traffic that already happens: every Discover tab load and every
# Watchlist add fetches the full shape for real collections. Once a
# collection has been seen that way, search results for it show accurate
# verified/category/etc. immediately, from any citizen's search, without
# a second API call - it just gets more complete the more the tool is used.
_collection_meta_cache: dict[str, tuple[float, dict]] = {}
_collection_meta_TTL = 6 * 3600


def _nft_collection_shape(c: dict, stats: dict | None) -> dict:
    total = (stats or {}).get("total") or {}
    intervals = {i.get("interval"): i for i in (stats or {}).get("intervals") or []}
    contracts = c.get("contracts") or []
    contract = contracts[0] if contracts else {}
    description = (c.get("description") or "").strip()
    slug = c.get("collection") or c.get("slug")
    # Present only in the full single-collection/listing shape, never in
    # the lean search shape - require all three so a partially-lean object
    # can't be mistaken for a full one.
    is_full_source = "safelist_status" in c and "category" in c and "contracts" in c
    shaped = {
        "slug": slug,
        "name": c.get("name") or "Unnamed collection",
        "image": c.get("image_url"),
        "floor": total.get("floor_price"),
        # Not every collection prices in ETH (WETH/USDC/a custom token all
        # show up here) - OpenSea reports which currency the collection's
        # figures are actually denominated in (floor and volume share it),
        # so display that instead of assuming ETH and mislabeling the number.
        "symbol": total.get("floor_price_symbol") or "ETH",
        "vol1d": (intervals.get("one_day") or {}).get("volume"),
        "vol7d": (intervals.get("seven_day") or {}).get("volume"),
        "vol30d": (intervals.get("thirty_day") or {}).get("volume"),
        "sales24h": (intervals.get("one_day") or {}).get("sales"),
        "owners": total.get("num_owners"),
        "openseaUrl": "https://opensea.io/collection/" + (slug or ""),
        # OpenSea's own safelist tiers: not_requested < requested < approved
        # < verified. Only "verified" gets the checkmark - that's OpenSea's
        # actual editorial verification, not a self-reported claim.
        "verified": c.get("safelist_status") == "verified",
        "category": c.get("category"),
        "description": description[:280] or None,
        "twitter": c.get("twitter_username"),
        "discord": c.get("discord_url"),
        "website": c.get("project_url"),
        "chain": contract.get("chain"),
        "contractAddress": contract.get("address"),
        "createdDate": c.get("created_date"),
    }
    if is_full_source and slug:
        _collection_meta_cache[slug] = (time.time(), shaped)
        _cap_cache(_collection_meta_cache)
    return shaped


def _enrich_with_cached_meta(shaped: dict) -> dict:
    slug = shaped.get("slug")
    if not slug or slug not in _collection_meta_cache:
        return shaped
    fetched_at, cached = _collection_meta_cache[slug]
    if time.time() - fetched_at > _collection_meta_TTL:
        del _collection_meta_cache[slug]
        return shaped
    for k in ("verified", "category", "description", "twitter", "discord", "website", "chain", "contractAddress", "createdDate"):
        shaped[k] = cached[k]
    return shaped


@app.get("/toolkit/nft-search")
@limiter.limit("40/minute")
async def nft_search(request: Request, q: str = Query(..., min_length=1, max_length=80)):
    async with httpx.AsyncClient(timeout=10) as client:
        data = await _opensea_get(client, "/search", {"query": q})
        if data is None:
            raise HTTPException(status_code=502, detail="Could not reach OpenSea right now")
        results = [
            r["collection"] for r in (data.get("results") or [])
            if r.get("type") == "collection" and r.get("collection")
        ][:12]
        # Search results don't include stats — fetch floor price for each in
        # parallel so the list is still useful at a glance, not just names.
        stats_tasks = [
            _opensea_get(client, f"/collections/{c.get('collection')}/stats")
            for c in results
        ]
        stats_list = await asyncio.gather(*stats_tasks, return_exceptions=True)
        out = []
        for c, stats in zip(results, stats_list):
            if isinstance(stats, Exception):
                stats = None
            out.append(_enrich_with_cached_meta(_nft_collection_shape(c, stats)))
        return {"results": out}


@app.get("/toolkit/nft-collection")
@limiter.limit("60/minute")
async def nft_collection(request: Request, slug: str = Query(..., min_length=1, max_length=120)):
    async with httpx.AsyncClient(timeout=10) as client:
        info = await _opensea_get(client, f"/collections/{slug}")
        stats = await _opensea_get(client, f"/collections/{slug}/stats")
        if info is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return _nft_collection_shape(info, stats)


@app.get("/toolkit/nft-discover")
@limiter.limit("40/minute")
async def nft_discover(request: Request, tab: str = Query("trending")):
    order_by = "seven_day_volume" if tab == "trending" else "created_date"
    async with httpx.AsyncClient(timeout=10) as client:
        data = await _opensea_get(client, "/collections", {"order_by": order_by, "limit": 20, "chain": "ethereum"})
        if data is None:
            raise HTTPException(status_code=502, detail="Could not reach OpenSea right now")
        collections = data.get("collections") or []
        # The listing endpoint doesn't include stats either — same parallel
        # stats fetch as search, capped to keep this snappy.
        subset = collections[:15]
        stats_tasks = [_opensea_get(client, f"/collections/{c.get('collection')}/stats") for c in subset]
        stats_list = await asyncio.gather(*stats_tasks, return_exceptions=True)
        out = []
        for c, stats in zip(subset, stats_list):
            if isinstance(stats, Exception):
                stats = None
            out.append(_nft_collection_shape(c, stats))
        return {"results": out}


# ── Wallet X-Ray ─────────────────────────────────────────────────────────
# Real, keyless on-chain data from Blockscout's public API (balance, token
# holdings with live USD pricing, transaction/transfer counts) plus real NFT
# holdings from OpenSea. The composite "score" is an explicitly-labeled
# heuristic (the UI calls it that) built entirely from these real numbers —
# no fabricated/random values, unlike a hash-based mock.
_XRAY_TIERS = [
    {"min": 0, "emoji": "🦐", "name": "Shrimp", "color": "#8A9BBF", "flavor": "Just getting started on-chain — every whale began here."},
    {"min": 14, "emoji": "🦀", "name": "Crab", "color": "#5A6A8A", "flavor": "Building a position, one transaction at a time."},
    {"min": 28, "emoji": "🐙", "name": "Octopus", "color": "#22D3EE", "flavor": "Dabbling across a few chains and protocols."},
    {"min": 42, "emoji": "🐟", "name": "Fish", "color": "#5B9BF8", "flavor": "An established, well-diversified retail wallet."},
    {"min": 58, "emoji": "🐬", "name": "Dolphin", "color": "#4D72FF", "flavor": "A serious, well-rounded on-chain presence."},
    {"min": 72, "emoji": "🦈", "name": "Shark", "color": "#1B42FF", "flavor": "A high-roller with real depth across the board."},
    {"min": 85, "emoji": "🐋", "name": "Whale", "color": "#F59E0B", "flavor": "Moves markets. Deep holdings, deep history."},
    {"min": 94, "emoji": "🐳", "name": "Humpback", "color": "#F59E0B", "flavor": "Apex on-chain presence — the top of the curve."},
]


def _xray_tier_for(score: float) -> dict:
    tier = _XRAY_TIERS[0]
    for t in _XRAY_TIERS:
        if score >= t["min"]:
            tier = t
    return tier


def _xray_next_tier(score: float) -> dict | None:
    for t in _XRAY_TIERS:
        if t["min"] > score:
            return t
    return None


def _log_score(value: float, floor: float, ceiling: float) -> float:
    import math
    if value <= floor:
        return 0.0
    if value >= ceiling:
        return 100.0
    return max(0.0, min(100.0, (math.log10(value) - math.log10(max(floor, 1e-9))) / (math.log10(ceiling) - math.log10(max(floor, 1e-9))) * 100))


@app.get("/toolkit/wallet-xray")
@limiter.limit("20/minute")
async def wallet_xray(request: Request, address: str = Query(..., min_length=3, max_length=100)):
    raw = address.strip()
    async with httpx.AsyncClient(timeout=12) as client:
        addr = raw
        ens_name = None
        if raw.lower().endswith(".eth"):
            try:
                r = await client.get("https://api.ensideas.com/ens/resolve/" + raw)
                d = r.json()
                if d and d.get("address"):
                    addr = d["address"]
                    ens_name = raw
            except (httpx.HTTPError, ValueError):
                pass
        if not re.match(r"^0x[a-fA-F0-9]{40}$", addr):
            raise HTTPException(status_code=400, detail="Could not resolve that address or ENS name")

        # Blockscout's free public API occasionally times out or hiccups on a
        # single request under load — retrying once before giving up avoids
        # silently reporting "0" (which reads as a confirmed empty wallet)
        # when the real cause was just a dropped request.
        async def _get_with_retry(url: str, timeout: float = 15):
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    res = await client.get(url, timeout=timeout)
                    res.raise_for_status()
                    return res.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last_exc = exc
            logger.exception("Blockscout request failed after retry: %s", url, exc_info=last_exc)
            return None

        info = await _get_with_retry(f"https://eth.blockscout.com/api/v2/addresses/{addr}")
        if info is None:
            raise HTTPException(status_code=502, detail="Could not reach the chain explorer right now")

        if not ens_name and info.get("ens_domain_name"):
            ens_name = info["ens_domain_name"]

        counters = await _get_with_retry(f"https://eth.blockscout.com/api/v2/addresses/{addr}/counters")
        counters_ok = counters is not None
        counters = counters or {}

        try:
            # A handful of real wallets (exchange hot wallets, very old/active
            # EOAs) hold thousands of tokens - mostly spam airdrops, but the
            # response itself can be large enough to need more than the
            # shared client timeout to fully download.
            tok_res = await client.get(
                f"https://eth.blockscout.com/api/v2/addresses/{addr}/token-balances",
                timeout=25,
            )
            tok_res.raise_for_status()
            tok_json = tok_res.json()
            token_balances = tok_json if isinstance(tok_json, list) else []
        except (httpx.HTTPError, ValueError):
            logger.exception("Failed to fetch token balances for %s", addr)
            token_balances = []

        nft_collections: list[dict] = []
        try:
            key = await _get_opensea_key(client)
            if key:
                nft_res = await client.get(
                    f"https://api.opensea.io/api/v2/chain/ethereum/account/{addr}/nfts",
                    params={"limit": 50},
                    headers={"X-API-KEY": key},
                )
                if nft_res.status_code == 200:
                    nfts = nft_res.json().get("nfts") or []
                    seen: dict[str, dict] = {}
                    for n in nfts:
                        slug = n.get("collection") or "unknown"
                        seen.setdefault(slug, {"name": slug, "count": 0, "image": n.get("image_url")})
                        seen[slug]["count"] += 1
                    nft_collections = list(seen.values())
        except (httpx.HTTPError, ValueError):
            pass

        # X-Ray's scoring is Ethereum-mainnet-only (Blockscout's per-chain
        # split means aggregating full history everywhere isn't free/cheap
        # to do well) - but silently showing an Ethereum-only net worth with
        # no hint that the wallet may hold real value elsewhere reads as
        # simply wrong for anyone whose activity is mostly on an L2. A cheap
        # native-balance presence check across the same public RPCs the Gas
        # Tracker already uses is enough to flag "also active elsewhere"
        # without pretending to give a full multi-chain accounting.
        async def _chain_has_balance(chain_key: str, cfg: dict) -> str | None:
            try:
                res = await client.post(
                    cfg["rpc"],
                    json={"jsonrpc": "2.0", "method": "eth_getBalance", "params": [addr, "latest"], "id": 1},
                    timeout=6,
                )
                res.raise_for_status()
                result = res.json().get("result")
                if result and int(result, 16) > 0:
                    return chain_key
            except (httpx.HTTPError, ValueError, TypeError):
                pass
            return None

        other_chain_results = await asyncio.gather(
            *[_chain_has_balance(k, cfg) for k, cfg in GAS_CHAINS.items() if k != "ethereum"]
        )
        _CHAIN_LABELS = {"bsc": "BNB Chain", "polygon": "Polygon", "arbitrum": "Arbitrum", "optimism": "Optimism", "base": "Base", "avalanche": "Avalanche"}
        other_chains = [_CHAIN_LABELS.get(k, k) for k in other_chain_results if k]

    eth_balance = int(info.get("coin_balance") or 0) / 1e18
    eth_price = float(info.get("exchange_rate") or 0)
    eth_usd = eth_balance * eth_price

    token_usd_total = 0.0
    fungible_tokens = 0
    for tb in token_balances:
        tok = tb.get("token") or {}
        if tok.get("type") != "ERC-20":
            continue
        try:
            decimals = int(tok.get("decimals") or 0)
            raw_value = int(tb.get("value") or 0)
            rate = float(tok.get("exchange_rate") or 0)
            if rate <= 0:
                continue
            qty = raw_value / (10 ** decimals)
            token_usd_total += qty * rate
            fungible_tokens += 1
        except (TypeError, ValueError):
            continue

    net_worth_usd = eth_usd + token_usd_total
    tx_count = int(counters.get("transactions_count") or 0)
    transfer_count = int(counters.get("token_transfers_count") or 0)
    nft_collection_count = len(nft_collections)
    nft_item_count = sum(c["count"] for c in nft_collections)

    net_worth_score = _log_score(net_worth_usd, 10, 2_000_000)
    diversity_score = min(100.0, fungible_tokens * 6)
    nft_score = min(100.0, nft_collection_count * 10)
    if counters_ok:
        experience_score = _log_score(tx_count, 1, 20000)
        defi_score = _log_score(transfer_count, 1, 50000)
        conviction_score = min(100.0, (fungible_tokens + nft_collection_count) * 100 / max(tx_count, 1) * 20)
    else:
        # The counters endpoint failed even after a retry — tx_count and
        # transfer_count are both 0 here, but that's "unknown", not a
        # confirmed empty wallet. Scoring them as literal zeros would both
        # understate the composite and, worse, spike conviction_score to a
        # false 100 (it divides by tx_count). Drop these three sub-scores
        # out of the average entirely rather than report a wrong number.
        experience_score = None
        defi_score = None
        conviction_score = None

    # Weighted average over whichever sub-scores actually have real data —
    # if counters failed, experience/defi are excluded and the remaining
    # weights (net worth/diversity/NFT) are rescaled to still sum to 1,
    # instead of treating the missing scores as zeros.
    weighted = [(net_worth_score, 0.40), (diversity_score, 0.15), (nft_score, 0.10)]
    if counters_ok:
        weighted += [(experience_score, 0.25), (defi_score, 0.10)]
    weight_total = sum(w for _, w in weighted)
    composite = round(sum(s * w for s, w in weighted) / weight_total)
    composite = max(1, min(99, composite))

    tier = _xray_tier_for(composite)
    next_tier = _xray_next_tier(composite)

    defi_for_archetype = defi_score or 0
    if composite >= 90:
        archetype = "The Apex Operator"
    elif nft_score + defi_for_archetype >= 100 and net_worth_score > 40:
        archetype = "The Blue-Chip Accumulator"
    elif counters_ok and tx_count < 200 and net_worth_usd > 5000:
        archetype = "The Diamond-Handed Holder"
    elif nft_score + defi_for_archetype >= 110:
        archetype = "The Degen Explorer"
    elif counters_ok and tx_count > 2500:
        archetype = "The Serial Flipper"
    else:
        archetype = "The Fresh Signal"

    return {
        "address": addr,
        "ensName": ens_name,
        "composite": composite,
        "tier": tier,
        "nextTier": next_tier,
        "archetype": archetype,
        "countersOk": counters_ok,
        "subs": [
            {"k": "Net Worth", "v": round(net_worth_score)},
            {"k": "Experience", "v": round(experience_score) if experience_score is not None else None},
            {"k": "Diversity", "v": round(diversity_score)},
            {"k": "NFT Footprint", "v": round(nft_score)},
            {"k": "DeFi Footprint", "v": round(defi_score) if defi_score is not None else None},
            {"k": "Conviction", "v": round(conviction_score) if conviction_score is not None else None},
        ],
        "crypto": {
            "netWorthUsd": round(net_worth_usd),
            "distinctTokens": fungible_tokens,
            "ethBalance": round(eth_balance, 4),
            "otherChains": other_chains,
        },
        "nft": {
            "collections": nft_collection_count,
            "items": nft_item_count,
            "top": sorted(nft_collections, key=lambda c: -c["count"])[:6],
        },
        "defi": {
            "tokenTransfers": transfer_count if counters_ok else None,
        },
        "behavior": {
            "txCount": tx_count if counters_ok else None,
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
