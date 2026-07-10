import asyncio
import re
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Dash HQ API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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


def _application_embed(app_row: dict, status: str = "pending", reviewer: str = None) -> dict:
    color = {"pending": 0x1B42FF, "accepted": 0x10B981, "declined": 0xEF4444}[status]
    footer = f"Application ID: {app_row['id']}"
    if status != "pending":
        icon = "✅" if status == "accepted" else "❌"
        footer = f"{icon} {status.capitalize()} by {reviewer} · {footer}"
    return {
        "title": f"New Citizenship Application — {app_row['name']}",
        "color": color,
        "fields": [
            {"name": "Name / Alias", "value": _trunc(app_row["name"]), "inline": True},
            {"name": "X Profile", "value": _trunc(app_row["x_profile"]), "inline": True},
            {"name": "Intro & Role", "value": _trunc(app_row["intro"]), "inline": False},
            {"name": "Communities", "value": _trunc(app_row["communities"]), "inline": False},
            {"name": "Adding Value", "value": _trunc(app_row["value"]), "inline": False},
        ],
        "footer": {"text": footer},
    }


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
        # we don't want a Discord hiccup to lose someone's submission.
        if settings.discord_bot_token and settings.discord_applications_channel_id:
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
                await client.patch(
                    f"{settings.supabase_url}/rest/v1/applications",
                    headers=_supabase_headers(prefer="return=minimal"),
                    params={"id": f"eq.{saved['id']}"},
                    json={
                        "discord_message_id": msg["id"],
                        "discord_channel_id": settings.discord_applications_channel_id,
                    },
                )

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

    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(),
            params={"id": f"eq.{app_id}", "select": "*"},
        )
        rows = res.json()
        if not rows:
            return {"type": 4, "data": {"content": "Application not found.", "flags": 64}}
        application = rows[0]

        await client.patch(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(prefer="return=minimal"),
            params={"id": f"eq.{app_id}"},
            json={
                "status": status,
                "reviewed_by": reviewer,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    # Accept/Decline buttons are done their job and go away, but the X
    # profile link stays on the message permanently so the team can always
    # reach the applicant, whatever the decision.
    updated_components = [{"type": 1, "components": [_x_profile_button(application["x_profile"])]}]

    return {
        "type": 7,  # UPDATE_MESSAGE — edits the original message in place
        "data": {
            "embeds": [_application_embed(application, status=status, reviewer=reviewer)],
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
        except httpx.HTTPError:
            pass  # fall through — serve whatever's cached, even if stale
    return {i: _PRICE_CACHE[i][1] for i in ids if i in _PRICE_CACHE}


@app.get("/toolkit/gas")
@limiter.limit("60/minute")
async def toolkit_gas(request: Request, chain: str = Query("ethereum")):
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
                coins = res.json().get("coins") or []
                exact = next((c for c in coins if (c.get("symbol") or "").upper() == sym), None)
                pick = exact or (coins[0] if coins else None)
                if pick:
                    _COIN_ID_CACHE[sym] = pick["id"]

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


# honeypot.is only simulates EVM chains — there is no free equivalent for
# non-EVM chains (Solana etc.), so the rug checker is intentionally EVM-only.
ALLOWED_CHAIN_IDS = {1, 56, 137, 42161, 10, 8453, 43114}


class RugCheckIn(BaseModel):
    address: str
    chain_id: int = 1

    @field_validator("address")
    @classmethod
    def _valid_address(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^0x[a-fA-F0-9]{40}$", v):
            raise ValueError("must be a valid EVM contract address (0x...)")
        return v

    @field_validator("chain_id")
    @classmethod
    def _valid_chain(cls, v: int) -> int:
        if v not in ALLOWED_CHAIN_IDS:
            raise ValueError("unsupported chain")
        return v


@app.post("/toolkit/rug-check")
@limiter.limit("20/minute")
async def rug_check(request: Request, payload: RugCheckIn):
    # Proxied server-side (rather than called from the browser) so the free
    # honeypot.is API isn't hit by an uncontrolled client fan-out, and so it
    # can share the same slowapi rate limiting as the rest of the API.
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(
                "https://api.honeypot.is/v2/IsHoneypot",
                params={"address": payload.address, "chainID": payload.chain_id},
            )
            res.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not reach the honeypot scanner")

    data = res.json()
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


@app.get("/health")
async def health():
    return {"status": "ok"}
