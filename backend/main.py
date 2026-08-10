import asyncio
import base64
import hashlib
import json
import logging
import random
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

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

import pnl_card
from config import settings
from register_commands import COMMANDS as TOOLKIT_BOT_COMMANDS

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
            "joined": joined_year or "-",
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


# ── X (Twitter) OAuth — identity for the application flow ───────────────────
# Same JWT-based pattern as Discord auth above, but with one extra wrinkle:
# X's OAuth 2.0 requires PKCE, and this deployment has no server-side session
# store to hold the code_verifier between the redirect out and the callback
# coming back. So the verifier (plus which flow triggered it - starting a
# fresh application vs checking an existing one) travels round-trip inside
# the "state" param itself, signed so it can't be tampered with in transit.
X_API = "https://api.twitter.com/2"


def _x_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _redirect_with_params(base_url: str, **params) -> RedirectResponse:
    parts = urllib.parse.urlsplit(base_url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query.update({k: v for k, v in params.items() if v is not None})
    return RedirectResponse(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query))))


@app.get("/auth/x")
async def x_login(intent: str = Query("apply")):
    if intent not in ("apply", "status"):
        intent = "apply"
    verifier, challenge = _x_pkce_pair()
    state = jwt.encode(
        {"cv": verifier, "intent": intent, "exp": int(time.time()) + 600},
        settings.jwt_secret,
        algorithm="HS256",
    )
    params = {
        "response_type": "code",
        "client_id": settings.x_client_id,
        "redirect_uri": settings.x_redirect_uri,
        "scope": "users.read tweet.read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(f"https://twitter.com/i/oauth2/authorize?{urllib.parse.urlencode(params)}")


@app.get("/auth/x/callback")
@limiter.limit("10/minute")
async def x_callback(request: Request, code: str = None, state: str = None, error: str = None):
    apply_page = f"{settings.frontend_url}/apply"
    status_page = f"{settings.frontend_url}/apply?view=status"

    if error or not code or not state:
        return _redirect_with_params(apply_page, xerror="access_denied")

    try:
        state_payload = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
        verifier = state_payload["cv"]
        intent = state_payload.get("intent", "apply")
    except jwt.InvalidTokenError:
        return _redirect_with_params(apply_page, xerror="bad_state")

    landing = status_page if intent == "status" else apply_page

    try:
        async with httpx.AsyncClient() as client:
            token_res = await client.post(
                f"{X_API}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.x_redirect_uri,
                    "code_verifier": verifier,
                    "client_id": settings.x_client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=(settings.x_client_id, settings.x_client_secret),
            )
            if token_res.status_code != 200:
                return _redirect_with_params(landing, xerror="token_failed")

            access_token = token_res.json()["access_token"]

            user_res = await client.get(
                f"{X_API}/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_res.status_code != 200:
                return _redirect_with_params(landing, xerror="user_fetch_failed")

            x_user = user_res.json()["data"]
            x_user_id = x_user["id"]
            x_username = x_user["username"]

        if intent == "status":
            status_token = jwt.encode(
                {"x_user_id": x_user_id, "iat": int(time.time()), "exp": int(time.time()) + 600},
                settings.jwt_secret,
                algorithm="HS256",
            )
            return _redirect_with_params(status_page, token=status_token)

        # intent == apply: re-applying is only allowed once the most recent
        # application on file for this X account has been declined.
        async with httpx.AsyncClient(timeout=15) as client:
            existing_res = await client.get(
                f"{settings.supabase_url}/rest/v1/applications",
                headers=_supabase_headers(),
                params={
                    "x_user_id": f"eq.{x_user_id}",
                    "select": "status",
                    "order": "submitted_at.desc",
                    "limit": 1,
                },
            )
            existing_res.raise_for_status()
            existing = existing_res.json()

        if existing and existing[0]["status"] in ("pending", "accepted"):
            return _redirect_with_params(
                apply_page, xerror="already_applied", status=existing[0]["status"], handle=x_username
            )

        apply_token = jwt.encode(
            {
                "x_user_id": x_user_id,
                "x_username": x_username,
                "intent": "apply",
                "iat": int(time.time()),
                "exp": int(time.time()) + 1800,
            },
            settings.jwt_secret,
            algorithm="HS256",
        )
        return _redirect_with_params(apply_page, token=apply_token)
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("X OAuth callback failed")
        return _redirect_with_params(landing, xerror="server_error")


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


@app.get("/cron/register-discord-commands")
async def register_discord_commands(request: Request):
    # One-time (and re-run-whenever-the-command-list-changes) setup action,
    # not a real schedule — reuses the cron auth pattern since it's the
    # same "server action gated by a shared secret" shape, and needing to
    # go through Vercel's dashboard to trigger it is exactly the point:
    # nobody without access to the deployed environment can register or
    # overwrite the bot's commands.
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not (settings.discord_bot_token and settings.discord_client_id and settings.discord_guild_id):
        raise HTTPException(status_code=500, detail="Discord bot env vars not fully configured")

    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.put(
            f"{DISCORD_API}/applications/{settings.discord_client_id}/guilds/{settings.discord_guild_id}/commands",
            headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            json=TOOLKIT_BOT_COMMANDS,
        )
        res.raise_for_status()
        registered = res.json()

    return {"registered": len(registered), "commands": [c["name"] for c in registered]}


@app.get("/cron/test-dm")
async def test_dm(request: Request, user_id: str = Query(..., min_length=1, max_length=32)):
    # Admin-only preview tool, same shared-secret gate as every other
    # /cron endpoint - lets a real DM be sent on demand to check how an
    # alert actually renders in Discord, without waiting for a live poll
    # cycle to trigger one for real.
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not settings.discord_bot_token:
        raise HTTPException(status_code=500, detail="Discord bot env vars not fully configured")

    sample_c = {"name": "Azuki", "slug": "azuki", "symbol": "ETH", "floor": 0.0289, "openseaUrl": "https://opensea.io/collection/azuki"}
    embed = _price_target_embed(sample_c, target=0.03, direction="below", loop_alert=False)
    embed["footer"]["text"] += " · Test DM, not a real alert"
    async with httpx.AsyncClient(timeout=10) as client:
        delivered = await _discord_dm(client, user_id, embed, content=f"🎯 {embed['title']} (this is a test DM)")
    return {"delivered": delivered}


@app.get("/cron/purge-channel")
async def purge_channel(request: Request, channel_id: str = Query(..., min_length=1, max_length=32), max_batches: int = Query(8, ge=1, le=20)):
    # Admin-only, same shared-secret gate as every other /cron endpoint -
    # wipes messages from a channel. Bulk-delete only works on messages
    # under 14 days old (a hard Discord API limit), so anything older
    # falls back to one-by-one deletion.
    #
    # Bounded to max_batches per call on purpose - a channel with
    # hundreds of messages plus Discord's per-route rate limiting can
    # blow past Vercel's function timeout if it tries to loop until
    # empty in one request (this happened on the first version). Each
    # call processes a fixed amount of work and reports whether more is
    # left via "done", so a caller loops by re-invoking until done=true
    # instead of one request doing unbounded work.
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not settings.discord_bot_token:
        raise HTTPException(status_code=500, detail="Discord bot env vars not fully configured")

    async def _delete_one(client: httpx.AsyncClient, mid: str) -> tuple[bool, str]:
        # Respects Discord's 429 by reading the actual retry_after it
        # hands back instead of guessing a delay - a fixed sleep either
        # wastes time waiting longer than needed or, worse, still isn't
        # long enough and gets 429'd again anyway.
        for attempt in range(4):
            res = await client.delete(f"{DISCORD_API}/channels/{channel_id}/messages/{mid}", headers=headers)
            if res.status_code < 300:
                return True, ""
            if res.status_code == 429:
                try:
                    retry_after = float(res.json().get("retry_after", 1.0))
                except (ValueError, TypeError):
                    retry_after = 1.0
                await asyncio.sleep(retry_after + 0.1)
                continue
            return False, f"delete {mid}: {res.status_code}"
        return False, f"delete {mid}: gave up after repeated 429s"

    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    deleted_bulk, deleted_single, errors = 0, 0, []
    bulk_forbidden = False
    done = False
    start = time.time()
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(max_batches):
            if time.time() - start > 45:
                # Leave headroom under Vercel's 60s limit rather than risk
                # a 504 mid-batch - the caller just re-invokes for more.
                break
            res = await client.get(f"{DISCORD_API}/channels/{channel_id}/messages", headers=headers, params={"limit": 100})
            if res.status_code >= 300:
                errors.append(f"fetch: {res.status_code} {res.text[:200]}")
                break
            batch = res.json()
            if not batch:
                done = True
                break
            ids = [m["id"] for m in batch]
            if len(ids) >= 2 and not bulk_forbidden:
                bulk_res = await client.post(f"{DISCORD_API}/channels/{channel_id}/messages/bulk-delete", headers=headers, json={"messages": ids})
                if bulk_res.status_code == 403:
                    # Bot is missing Manage Messages - bulk-delete always
                    # needs it regardless of who authored the messages, so
                    # retrying per-batch is pointless; drop to per-message
                    # deletes (which don't need it for the bot's own
                    # messages) for the rest of this run.
                    bulk_forbidden = True
                    errors.append("bulk-delete: 403 Forbidden - bot needs the Manage Messages permission")
                elif bulk_res.status_code >= 300:
                    errors.append(f"bulk-delete: {bulk_res.status_code} {bulk_res.text[:200]}")
                else:
                    deleted_bulk += len(ids)
                    continue
            for mid in ids:
                ok, err = await _delete_one(client, mid)
                if ok:
                    deleted_single += 1
                else:
                    errors.append(err)
                await asyncio.sleep(0.3)  # stay comfortably under Discord's per-channel delete rate limit
            if len(batch) < 100:
                done = True
                break

    return {"channel_id": channel_id, "deleted_bulk": deleted_bulk, "deleted_single": deleted_single, "errors": errors[:20], "error_count": len(errors), "done": done}


@app.get("/cron/notify")
async def notify(request: Request, message: str = Query(..., min_length=1, max_length=1500), level: str = Query("error", pattern="^(error|warning|info)$")):
    # Proactive CI/ops alerting - GitHub's own default is a failure email
    # to whoever pushed, easy to miss. Same shared-secret gate as every
    # other /cron endpoint. Safe no-op if no alert channel is configured,
    # so this never becomes a hard dependency for CI to pass.
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not settings.discord_ops_alert_channel_id:
        return {"configured": False, "posted": False}

    icon = {"error": "🚨", "warning": "⚠️", "info": "ℹ️"}[level]
    color = {"error": EMBED_COLOR_BAD, "warning": EMBED_COLOR_WARN, "info": EMBED_COLOR}[level]
    embed = {
        "title": f"{icon} CI/Ops Alert",
        "description": message[:4000],
        "color": color,
        "footer": {"text": "Dash HQ Toolkit · CI/CD"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        posted = await _post_channel_message(client, settings.discord_ops_alert_channel_id, embed)
    return {"configured": True, "posted": posted}


@app.get("/cron/test-monitor-channel")
async def test_monitor_channel(request: Request, channel_id: str = Query(None, min_length=1, max_length=32)):
    # Verifies a channel is actually postable end-to-end - env var
    # presence alone isn't proof it's non-empty or that the bot has
    # permission there (Sensitive vars are write-only, so `vercel env
    # pull` can't confirm the stored value). Defaults to
    # DISCORD_NFT_MONITOR_CHANNEL_ID; pass ?channel_id=... to check any
    # other configured channel (e.g. DISCORD_NFT_SCOPE_CHANNEL_ID)
    # through the same mechanism instead of a one-off endpoint each time.
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    target = channel_id or settings.discord_nft_monitor_channel_id
    if not target:
        return {"configured": False, "posted": False, "detail": "No channel_id given and DISCORD_NFT_MONITOR_CHANNEL_ID is empty/unset"}
    embed = {"title": "🎯 Test post - channel wiring check", "description": "If you can see this, the channel is correctly configured and the bot can post here.", "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}
    async with httpx.AsyncClient(timeout=10) as client:
        posted = await _post_channel_message(client, target, embed, content="🎯 Channel wiring check (test post)")
    return {"configured": True, "channel_id": target, "posted": posted}


# ── Pidgin AutoMod setup (one-time / re-run-on-change) ──────────────────────
# English-only enforcement in #general via Discord's native AutoMod - free,
# no persistent bot connection needed. Everything else in this backend is
# reachable only through Discord's Interactions webhook (slash commands,
# buttons, selects), which never fires for regular chat messages; scanning
# every message ourselves would need a permanent Gateway connection, which
# is exactly the always-on-worker cost this dodges. Trade-off: keyword
# matching, not real language detection - it will miss some Pidgin and can
# occasionally false-positive on a borderline phrase. Not registered
# anywhere in TOOLKIT_TOOLS/dashboard on purpose - this is meant to be
# invisible to regular members, unlike every other feature in this file.
PIDGIN_KEYWORDS = [
    "abeg", "wahala", "wetin", "dey", "abi", "sabi", "comot", "waka",
    "gbege", "katakata", "jare", "omo", "palava", "shakara", "kolo",
    "gist", "ehen", "biko", "japa", "sapa", "oga", "walahi", "sha",
    "how far", "no wahala", "e go better", "na so", "wetin dey happen",
    "i no know", "dis one", "chop money", "make we", "you dey mad",
    "wetin be", "na him", "no vex", "abeg no", "e don do",
]
_PIDGIN_RULE_NAME = "English-only #general (Pidgin filter)"
_PIDGIN_EXEMPT_ROLE_NAME = "Pidgin Exempt"


async def _get_or_create_pidgin_exempt_role(client: httpx.AsyncClient, headers: dict) -> str:
    # A citizen holding this role is skipped by the AutoMod rule entirely
    # (Discord AutoMod supports exempt_roles same as exempt_channels) - the
    # mechanism mods use to turn the timeout off for one specific person
    # without touching the rule itself. Looked up by name and created if
    # missing, so no extra env var/manual setup step is needed.
    roles_res = await client.get(f"{DISCORD_API}/guilds/{settings.discord_guild_id}/roles", headers=headers)
    roles_res.raise_for_status()
    existing = next((r for r in roles_res.json() if r["name"] == _PIDGIN_EXEMPT_ROLE_NAME), None)
    if existing:
        return existing["id"]
    create_res = await client.post(
        f"{DISCORD_API}/guilds/{settings.discord_guild_id}/roles",
        headers=headers,
        json={"name": _PIDGIN_EXEMPT_ROLE_NAME, "mentionable": False, "hoist": False},
    )
    create_res.raise_for_status()
    return create_res.json()["id"]


@app.get("/cron/setup-pidgin-automod")
async def setup_pidgin_automod(request: Request):
    # Same guarded-setup-action pattern as /cron/register-discord-commands -
    # re-run whenever PIDGIN_KEYWORDS changes; safe to call repeatedly since
    # it patches the existing rule by name instead of duplicating it.
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not (settings.discord_bot_token and settings.discord_guild_id and settings.discord_general_channel_id):
        raise HTTPException(status_code=500, detail="Discord bot env vars not fully configured")

    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        channels_res = await client.get(f"{DISCORD_API}/guilds/{settings.discord_guild_id}/channels", headers=headers)
        channels_res.raise_for_status()
        # Only text-capable channel types can ever trigger a MESSAGE_SEND
        # AutoMod rule in the first place - voice channels (2), categories
        # (4), and stage channels (13) never need exempting. Filtering to
        # just the types that matter also keeps the list under Discord's
        # hard cap of 50 exempt_channels per rule, which a server with
        # many voice/category channels can otherwise exceed even though
        # its actual TEXT channel count is well under 50.
        TEXTUAL_CHANNEL_TYPES = {0, 5, 15}  # GUILD_TEXT, GUILD_ANNOUNCEMENT, GUILD_FORUM
        all_channel_ids = [c["id"] for c in channels_res.json() if c.get("type") in TEXTUAL_CHANNEL_TYPES]
        # Discord AutoMod has no "only apply in these channels" allowlist,
        # only exempt_channels - exempting everything except #general is
        # how enforcement stays scoped to just that one channel (so
        # #lifestyle-chat and everywhere else is unaffected).
        exempt_channels = [cid for cid in all_channel_ids if cid != settings.discord_general_channel_id]
        channels_truncated = len(exempt_channels) > 50
        if channels_truncated:
            # Still over the cap even after filtering to text channels -
            # truncate rather than fail outright. Any channel past the
            # 50th falls back to being enforced too (better than the
            # whole feature not working; flagged in the response so it's
            # visible, not silent).
            exempt_channels = exempt_channels[:50]
        # Creating a role needs Manage Roles specifically (separate from
        # Manage Server, which is all AutoMod rule management itself
        # needs) - the bot may not have that yet. Don't let the whole
        # rule setup fail just because this one bonus feature isn't
        # grantable right now; the core English-only enforcement matters
        # more than the mod-exemption toggle, and this can be retried
        # (re-running this endpoint is always safe) once Manage Roles is
        # added.
        try:
            exempt_role_id = await _get_or_create_pidgin_exempt_role(client, headers)
            exempt_role_warning = None
        except httpx.HTTPStatusError as e:
            logger.warning("Could not create/find Pidgin Exempt role (bot likely missing Manage Roles): %s", e)
            exempt_role_id = None
            exempt_role_warning = "Could not create the 'Pidgin Exempt' role - give the bot Manage Roles permission, then re-run this endpoint to enable /pidgin-exempt."

        base_actions = [
            {"type": 1, "metadata": {"custom_message": "🚫 Blocked: that looked like Nigerian Pidgin, and #general is English-only — that's why you've been timed out for 10 minutes. Other languages (including Pidgin) are welcome in the lifestyle chat!"}},
            {"type": 3, "metadata": {"duration_seconds": 600}},
        ]
        alert_action = None
        if settings.discord_automod_alert_channel_id:
            alert_action = {"type": 2, "metadata": {"channel_id": settings.discord_automod_alert_channel_id}}

        existing_res = await client.get(f"{DISCORD_API}/guilds/{settings.discord_guild_id}/auto-moderation/rules", headers=headers)
        existing_res.raise_for_status()
        existing = next((r for r in existing_res.json() if r["name"] == _PIDGIN_RULE_NAME), None)

        def _build_body(actions: list) -> dict:
            return {
                "name": _PIDGIN_RULE_NAME,
                "event_type": 1,
                "trigger_type": 1,
                "trigger_metadata": {"keyword_filter": PIDGIN_KEYWORDS},
                "actions": actions,
                "enabled": True,
                "exempt_channels": exempt_channels,
                "exempt_roles": [exempt_role_id] if exempt_role_id else [],
            }

        async def _submit(actions: list):
            body = _build_body(actions)
            if existing:
                return await client.patch(
                    f"{DISCORD_API}/guilds/{settings.discord_guild_id}/auto-moderation/rules/{existing['id']}",
                    headers=headers, json=body,
                )
            return await client.post(
                f"{DISCORD_API}/guilds/{settings.discord_guild_id}/auto-moderation/rules",
                headers=headers, json=body,
            )

        actions = base_actions + ([alert_action] if alert_action else [])
        res = await _submit(actions)
        alert_warning = None
        if res.status_code >= 300 and alert_action and "AUTO_MODERATION_CHANNEL_FLAG_ACTION_ACCESS" in res.text:
            # Bot lacks View/Send Messages in the alert channel - drop just
            # that action and retry, same "don't block core enforcement
            # over a bonus feature" approach as the exempt-role fallback.
            logger.warning("Could not use AutoMod alert channel (bot likely missing View/Send Messages there): %s", res.text[:300])
            alert_warning = "Could not post AutoMod alerts to the configured channel - give the bot View Channel + Send Messages permission there, then re-run this endpoint to enable alerts."
            actions = base_actions
            res = await _submit(actions)
        if res.status_code >= 300:
            raise HTTPException(status_code=502, detail=f"Discord AutoMod setup failed: {res.status_code} {res.text[:300]}")
        rule = res.json()

    warnings = [w for w in (exempt_role_warning, alert_warning) if w]
    return {
        "rule_id": rule["id"],
        "action": "updated" if existing else "created",
        "keywords": len(PIDGIN_KEYWORDS),
        "exempt_channels": len(exempt_channels),
        "exempt_role_id": exempt_role_id,
        "exempt_role_name": _PIDGIN_EXEMPT_ROLE_NAME,
        "alert_channel_enabled": alert_action is not None and alert_warning is None,
        "warning": " | ".join(warnings) if warnings else None,
    }


async def _handle_pidgin_exempt_command(payload: dict) -> dict:
    # Hidden from regular members via default_member_permissions on the
    # command itself (same mechanism /history uses), but that's a Discord
    # Integrations setting a server admin could loosen later - this check
    # is the real enforcement so add/remove stay mod-only regardless.
    if not _is_team_member(payload):
        return {"type": 4, "data": {"content": "Mods only.", "flags": 64}}
    sub_options = (payload.get("data") or {}).get("options") or []
    if not sub_options:
        return {"type": 4, "data": {"content": "Use `/pidgin-exempt add` or `/pidgin-exempt remove`.", "flags": 64}}
    sub = sub_options[0]
    sub_name = sub.get("name")
    sub_opts = {o["name"]: o.get("value") for o in (sub.get("options") or [])}
    target_user_id = sub_opts.get("user")
    if not target_user_id:
        return {"type": 4, "data": {"content": "Specify a user.", "flags": 64}}
    if not (settings.discord_bot_token and settings.discord_guild_id):
        return {"type": 4, "data": {"content": "Discord bot env vars not fully configured.", "flags": 64}}

    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            role_id = await _get_or_create_pidgin_exempt_role(client, headers)
            url = f"{DISCORD_API}/guilds/{settings.discord_guild_id}/members/{target_user_id}/roles/{role_id}"
            cleared_timeout = False
            if sub_name == "add":
                res = await client.put(url, headers=headers)
                if res.status_code < 300:
                    # A mod reaching for /pidgin-exempt add is almost always
                    # doing it to rescue someone currently timed out, not
                    # just to pre-approve them for later - so lift any
                    # active timeout in the same step instead of making
                    # them also go find the member and remove it manually.
                    member_url = f"{DISCORD_API}/guilds/{settings.discord_guild_id}/members/{target_user_id}"
                    clear_res = await client.patch(member_url, headers=headers, json={"communication_disabled_until": None})
                    cleared_timeout = clear_res.status_code < 300
            elif sub_name == "remove":
                res = await client.delete(url, headers=headers)
            else:
                return {"type": 4, "data": {"content": "Unknown subcommand.", "flags": 64}}
            if res.status_code >= 300:
                return {"type": 4, "data": {"content": f"Discord API error: {res.status_code} {res.text[:200]}", "flags": 64}}
        except httpx.HTTPError:
            logger.exception("pidgin-exempt role update failed")
            return {"type": 4, "data": {"content": "Something went wrong talking to Discord.", "flags": 64}}

    if sub_name == "add":
        suffix = " and their active timeout was lifted." if cleared_timeout else " (no active timeout to lift)."
        content = f"✅ <@{target_user_id}> is now exempt from the English-only #general timeout{suffix}"
    else:
        content = f"✅ <@{target_user_id}> is no longer exempt from the English-only #general timeout."
    return {"type": 4, "data": {"content": content, "flags": 64}}


# ── Citizenship applications ─────────────────────────────────────────────────

class ApplicationIn(BaseModel):
    name: str
    token: str  # X identity token from the /auth/x?intent=apply flow
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
            raise ValueError("must be at least 8 words, give a real answer, not a one-liner")
        if len(v) > 600:
            raise ValueError("must be 600 characters or fewer, keep it concise")
        return v

    @field_validator("communities")
    @classmethod
    def _min_2_words(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 600:
            raise ValueError("must be 600 characters or fewer, keep it concise")
        if len(v.split()) < 2:
            raise ValueError("list at least one real community")
        return v

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


def _x_profile_button(x_username: str) -> dict:
    # A Link-style button (style 5) — Discord opens the URL directly on
    # click, no interaction/custom_id involved. Kept in the message's
    # components permanently, including after Accept/Decline replaces the
    # other buttons, so the team can always reach the applicant's profile.
    return {"type": 2, "style": 5, "label": "View X Profile", "url": f"https://x.com/{x_username}"}


def _application_embed(
    app_row: dict, status: str = "pending", reviewer: str = None, invite_url: str = None, decline_reason: str = None
) -> dict:
    color = {"pending": 0x1B42FF, "accepted": 0x10B981, "declined": 0xEF4444}[status]
    footer = f"Application ID: {app_row['id']}"
    if status != "pending":
        icon = "✅" if status == "accepted" else "❌"
        footer = f"{icon} {status.capitalize()} by {reviewer} · {footer}"
    fields = [
        {"name": "Name / Alias", "value": _trunc(app_row["name"]), "inline": True},
        {"name": "X Profile", "value": f"@{app_row['x_username']}", "inline": True},
        {"name": "Intro & Role", "value": _trunc(app_row["intro"]), "inline": False},
        {"name": "Communities", "value": _trunc(app_row["communities"]), "inline": False},
        {"name": "Adding Value", "value": _trunc(app_row["value"]), "inline": False},
    ]
    if decline_reason:
        fields.append({"name": "Reason for Declining", "value": _trunc(decline_reason, 300), "inline": False})
    if invite_url:
        # Plain text, not a button, so it can just be selected and copied
        # straight out of the embed to hand to the applicant.
        fields.append({"name": "Invite Link (one-time use)", "value": invite_url, "inline": False})
    return {
        "title": f"New Citizenship Application: {app_row['name']}",
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
            json={"max_age": 0, "max_uses": 1, "unique": True},  # never expires by time, one use
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

    try:
        identity = jwt.decode(application.token, settings.jwt_secret, algorithms=["HS256"])
        if identity.get("intent") != "apply":
            raise jwt.InvalidTokenError("wrong token intent")
        x_user_id = identity["x_user_id"]
        x_username = identity["x_username"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Your X connection expired, please connect again.")
    except (jwt.InvalidTokenError, KeyError):
        raise HTTPException(status_code=401, detail="Could not verify your X connection, please connect again.")

    row = {
        "name": application.name,
        "x_user_id": x_user_id,
        "x_username": x_username,
        "intro": application.intro,
        "communities": application.communities,
        "value": application.value,
        "followed_team": application.followedTeam,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        # Re-check the dedup rule at submit time too, not just at the OAuth
        # redirect - closes the gap where someone connects X, then opens a
        # second tab and submits twice before the first submission lands.
        existing_res = await client.get(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(),
            params={
                "x_user_id": f"eq.{x_user_id}",
                "select": "status",
                "order": "submitted_at.desc",
                "limit": 1,
            },
        )
        existing_res.raise_for_status()
        existing = existing_res.json()
        if existing and existing[0]["status"] in ("pending", "accepted"):
            raise HTTPException(status_code=409, detail="You already have an application on file. Check your status instead.")

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
                        _x_profile_button(saved["x_username"]),
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


@app.get("/applications/status")
@limiter.limit("20/minute")
async def application_status(request: Request, token: str = Query(...)):
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        x_user_id = payload["x_user_id"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="This link expired, please connect again.")
    except (jwt.InvalidTokenError, KeyError):
        raise HTTPException(status_code=401, detail="Could not verify your X connection, please connect again.")

    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(),
            params={
                "x_user_id": f"eq.{x_user_id}",
                "select": "name,x_username,status,decline_reason,invite_url,submitted_at,reviewed_at",
                "order": "submitted_at.desc",
                "limit": 1,
            },
        )
        res.raise_for_status()
        rows = res.json()

    if not rows:
        return {"found": False}

    row = rows[0]
    return {
        "found": True,
        "name": row["name"],
        "x_username": row["x_username"],
        "status": row["status"],
        "decline_reason": row.get("decline_reason"),
        "invite_url": row.get("invite_url") if row["status"] == "accepted" else None,
        "submitted_at": row["submitted_at"],
        "reviewed_at": row.get("reviewed_at"),
    }


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

    # Safety net: whatever this specific interaction ends up doing, a bug
    # anywhere in that path must never leave Discord with no response at
    # all ("This interaction failed", visible to whoever ran the command) -
    # always fall back to a clean, ephemeral, generic error instead of a
    # raw exception bubbling up to FastAPI's default handler.
    try:
        return await _dispatch_interaction(payload, itype)
    except Exception:
        logger.exception("Unhandled error dispatching Discord interaction (type=%s)", itype)
        return {"type": 4, "data": {"content": "⚠️ Something went wrong running that. Please try again in a moment.", "flags": 64}}


async def _dispatch_interaction(payload: dict, itype) -> dict:
    if itype == 2:  # APPLICATION_COMMAND — a /slash command
        cmd_name = (payload.get("data") or {}).get("name")
        if cmd_name == "history":
            return await _handle_history_command(payload)
        if cmd_name == "pidgin-exempt":
            return await _handle_pidgin_exempt_command(payload)
        return await _handle_toolkit_command(payload)

    member_user = payload.get("member", {}).get("user", {})
    reviewer = member_user.get("global_name") or member_user.get("username", "someone")

    if itype == 5:  # MODAL_SUBMIT — the decline-reason box was just submitted
        custom_id = payload.get("data", {}).get("custom_id", "")
        action, _, app_id = custom_id.partition(":")
        if action != "declinereason" or not app_id:
            return {"type": 4, "data": {"content": "Unrecognized submission.", "flags": 64}}
        reason = ""
        for row in payload.get("data", {}).get("components", []):
            for comp in row.get("components", []):
                if comp.get("custom_id") == "reason":
                    reason = comp.get("value", "").strip()
        return await _finalize_review(app_id, "declined", reviewer, decline_reason=reason[:300])

    if itype != 3:  # not a message-component (button) interaction
        return {"type": 4, "data": {"content": "Unsupported interaction.", "flags": 64}}

    if payload.get("data", {}).get("custom_id") == "toolkit_select":
        return await _handle_toolkit_select(payload)

    history_id = payload.get("data", {}).get("custom_id", "")
    if history_id.startswith("monitor_select:"):
        return await _handle_monitor_select(payload)
    if history_id.startswith("history_select:"):
        return await _handle_history_select(payload)
    if history_id.startswith("history_page:") or history_id.startswith("history_filter:"):
        return await _handle_history_page(payload)
    if history_id == "history_clear_prompt":
        return await _handle_history_clear_prompt(payload)
    if history_id == "history_clear_confirm":
        return await _handle_history_clear_confirm(payload)
    if history_id == "history_clear_cancel":
        return await _handle_history_clear_cancel(payload)

    custom_id = payload.get("data", {}).get("custom_id", "")
    action, _, app_id = custom_id.partition(":")
    if action not in ("accept", "decline") or not app_id:
        return {"type": 4, "data": {"content": "Unrecognized action.", "flags": 64}}

    if action == "decline":
        # Open a modal to collect why, instead of declining blind - the
        # applicant sees this reason on their status page later, and the
        # team should always be leaving one.
        return {
            "type": 9,  # MODAL
            "data": {
                "custom_id": f"declinereason:{app_id}",
                "title": "Decline Application",
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4,  # TEXT_INPUT
                        "custom_id": "reason",
                        "style": 2,  # paragraph
                        "label": "Reason (shown to the applicant)",
                        "max_length": 300,
                        "min_length": 1,
                        "required": True,
                        "placeholder": "e.g. Private X account, we need to see your activity",
                    }],
                }],
            },
        }

    return await _finalize_review(app_id, "accepted", reviewer)


async def _finalize_review(app_id: str, status: str, reviewer: str, decline_reason: str = None) -> dict:
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

            invite_url = None
            if status == "accepted" and settings.discord_bot_token:
                invite_url = await _create_one_time_invite(client)

            patch_json = {
                "status": status,
                "reviewed_by": reviewer,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
            if decline_reason is not None:
                patch_json["decline_reason"] = decline_reason
            if invite_url:
                patch_json["invite_url"] = invite_url

            patch_res = await client.patch(
                f"{settings.supabase_url}/rest/v1/applications",
                headers=_supabase_headers(prefer="return=minimal"),
                params={"id": f"eq.{app_id}"},
                json=patch_json,
            )
            patch_res.raise_for_status()
    except (httpx.HTTPError, KeyError, IndexError):
        logger.exception("Discord interaction failed for application %s", app_id)
        return {"type": 4, "data": {"content": "Something went wrong saving that. Please try the button again.", "flags": 64}}

    # Accept/Decline buttons are done their job and go away, but the X
    # profile link stays on the message permanently so the team can always
    # reach the applicant, whatever the decision.
    updated_components = [{"type": 1, "components": [_x_profile_button(application["x_username"])]}]

    return {
        "type": 7,  # UPDATE_MESSAGE — edits the original message in place
        "data": {
            "embeds": [_application_embed(
                application, status=status, reviewer=reviewer, invite_url=invite_url, decline_reason=decline_reason
            )],
            "components": updated_components,
        },
    }


# ── /history — team-only application archive, browsable in Discord ──────────
# Registered with default_member_permissions requiring Manage Server (see
# register_commands.py), so regular citizens never see this command at all.
# _is_team_member is a second, server-side check on top of that in case a
# server admin ever loosens the command's visibility in Integrations settings.
#
# Why this looks the way it does: Discord requires an interaction response
# within 3 seconds. A cold serverless start plus a Supabase round trip can
# blow past that on its own - and Mangum (the ASGI-to-Lambda adapter this
# backend runs on) makes FastAPI's normal BackgroundTasks useless for fixing
# it, because Mangum's Lambda handler blocks on the *entire* ASGI response
# cycle, background tasks included, before it hands anything back to the
# caller (confirmed against Mangum's own HTTPCycle source: it awaits the
# whole app() call, background task and all, before building the Lambda
# response). So a "deferred ack now, background work after" pattern doesn't
# actually respond any faster on this platform - the client still waits for
# both.
#
# The fix that actually works: every slow handler below does zero DB work
# itself. It fires a real, independent HTTP request to this same backend's
# own /discord/history-worker endpoint (a genuinely separate Vercel/Lambda
# invocation, not a task tied to this one), then immediately returns
# Discord's deferred-response type - which this invocation can do in
# milliseconds since it never touches Supabase. The short client-side
# timeout on that dispatch call exists only so *this* request doesn't sit
# around waiting for the worker's slower response; once the worker's own
# invocation has been accepted by Vercel's routing layer, it keeps running
# to completion on its own regardless of what this caller does afterward.
# The worker then PATCHes the real content into Discord's follow-up message
# endpoint once it's ready, with up to 15 minutes to do it in instead of 3
# seconds.
HISTORY_PAGE_SIZE = 20
HISTORY_KEEP_LIMIT = 50
STATUS_EMOJI = {"pending": "🕓", "accepted": "✅", "declined": "❌"}


def _is_team_member(payload: dict) -> bool:
    try:
        perms = int((payload.get("member") or {}).get("permissions", "0"))
    except (TypeError, ValueError):
        return False
    return bool(perms & 0x20)  # MANAGE_GUILD


async def _dispatch_history_worker(action: str, **kwargs) -> None:
    body = {"action": action, **kwargs}
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            await client.post(
                f"{settings.frontend_url}/discord/history-worker",
                json=body,
                headers={"X-Internal-Secret": settings.cron_secret},
            )
    except (httpx.TimeoutException, httpx.HTTPError):
        # Expected in the common case: this only needs to wait long enough
        # for the request to be dispatched, not for the worker's own
        # (slower) DB work to finish. See the module note above.
        pass


async def _discord_followup_patch(token: str, data: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.patch(
                f"{DISCORD_API}/webhooks/{settings.discord_client_id}/{token}/messages/@original",
                json=data,
            )
            if res.status_code >= 400:
                logger.error("Discord follow-up rejected: %s %s", res.status_code, res.text)
            res.raise_for_status()
    except (httpx.HTTPError, KeyError, ValueError):
        logger.exception("Discord follow-up failed for /history interaction")


async def _history_fetch(client: httpx.AsyncClient, status_filter: str, offset: int) -> tuple[list[dict], int]:
    params = {
        "select": "id,name,x_username,status,submitted_at,reviewed_by",
        "order": "submitted_at.desc",
        "limit": HISTORY_PAGE_SIZE,
        "offset": offset,
    }
    if status_filter and status_filter != "all":
        params["status"] = f"eq.{status_filter}"
    res = await client.get(
        f"{settings.supabase_url}/rest/v1/applications",
        headers={**_supabase_headers(), "Prefer": "count=exact"},
        params=params,
    )
    res.raise_for_status()
    rows = res.json()
    range_total = res.headers.get("content-range", "").split("/")[-1]
    total = int(range_total) if range_total.isdigit() else len(rows)
    return rows, total


async def _history_resolved_count(client: httpx.AsyncClient) -> int:
    # Only resolved (accepted/declined) applications ever get cleared, so
    # Clear Old's visibility and threshold are based on this count, not the
    # all-statuses total - a server with 60 pending and 3 resolved
    # applications has nothing worth clearing yet.
    res = await client.get(
        f"{settings.supabase_url}/rest/v1/applications",
        headers={**_supabase_headers(), "Prefer": "count=exact"},
        params={"select": "id", "status": "neq.pending", "limit": 1},
    )
    res.raise_for_status()
    range_total = res.headers.get("content-range", "").split("/")[-1]
    return int(range_total) if range_total.isdigit() else 0


def _history_list_embed(rows: list[dict], total: int, status_filter: str, resolved_total: int) -> dict:
    if not rows:
        desc = "No applications match this filter."
    else:
        lines = []
        for r in rows:
            emoji = STATUS_EMOJI.get(r["status"], "•")
            when = (r.get("submitted_at") or "")[:10]
            lines.append(f"{emoji} **{r['name']}** (@{r['x_username']}) · {r['status']} · {when}")
        desc = "\n".join(lines)
    label = {"all": "All", "pending": "Pending", "accepted": "Accepted", "declined": "Declined"}.get(status_filter, "All")
    note = "Select a name below for full details. Use Prev/Next to page through the rest."
    if resolved_total > HISTORY_KEEP_LIMIT:
        note += (
            f" Clear Old permanently deletes every resolved application except the {HISTORY_KEEP_LIMIT} most "
            "recent - it cannot be undone, and pending applications are never affected."
        )
    return {
        "title": f"📋 Application History: {label}",
        "description": desc,
        "color": EMBED_COLOR,
        "footer": {"text": f"{total} total. {note}"},
    }


def _history_components(rows: list[dict], status_filter: str, offset: int, resolved_total: int) -> list[dict]:
    components = []
    if rows:
        options = [
            {
                "label": r["name"][:100],
                "value": r["id"],
                "description": f"@{r['x_username']} · {r['status']}"[:100],
                "emoji": {"name": STATUS_EMOJI.get(r["status"], "•")},
            }
            for r in rows
        ]
        components.append({
            "type": 1,
            "components": [{
                "type": 3,  # SELECT_MENU
                "custom_id": f"history_select:{status_filter}:{offset}",
                "placeholder": "View an applicant's full details",
                "options": options,
            }],
        })
    nav_row = {
        "type": 1,
        "components": [
            {
                "type": 2, "style": 2, "label": "◀ Prev",
                "custom_id": f"history_page:{status_filter}:{max(0, offset - HISTORY_PAGE_SIZE)}",
                "disabled": offset <= 0,
            },
            {
                "type": 2, "style": 2, "label": "Next ▶",
                "custom_id": f"history_page:{status_filter}:{offset + HISTORY_PAGE_SIZE}",
                "disabled": len(rows) < HISTORY_PAGE_SIZE,
            },
        ],
    }
    if resolved_total > HISTORY_KEEP_LIMIT:
        nav_row["components"].append({
            "type": 2, "style": 4, "label": f"🗑 Clear Old (keep {HISTORY_KEEP_LIMIT})",
            "custom_id": "history_clear_prompt",
        })
    components.append(nav_row)
    # One-click filter row so pending requests are always a single tap
    # away, without needing to re-run /history with a status option.
    filter_defs = [("all", "All"), ("pending", "🕓 Pending"), ("accepted", "✅ Accepted"), ("declined", "❌ Declined")]
    components.append({
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 1 if status_filter == key else 2,  # highlight the active filter
                "label": label,
                "custom_id": f"history_filter:{key}:0",
                "disabled": status_filter == key,
            }
            for key, label in filter_defs
        ],
    })
    return components


async def _history_list_response(status_filter: str, offset: int) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        rows, total = await _history_fetch(client, status_filter, offset)
        resolved_total = await _history_resolved_count(client)
    return {
        "embeds": [_history_list_embed(rows, total, status_filter, resolved_total)],
        "components": _history_components(rows, status_filter, offset, resolved_total),
    }


async def _handle_history_clear_prompt(payload: dict) -> dict:
    # No DB work here - just showing the confirm dialog - so this responds
    # directly rather than round-tripping through the worker.
    if not _is_team_member(payload):
        return {"type": 4, "data": {"content": "This command is for team members only.", "flags": 64}}
    return {
        "type": 4,
        "data": {
            "content": (
                f"**This will permanently delete every resolved (accepted/declined) application except the "
                f"{HISTORY_KEEP_LIMIT} most recent.** Pending applications are never touched, no matter how old. "
                "This cannot be undone: declined reasons, invite links, and reviewer history for anything older "
                "will be gone for good. Are you sure?"
            ),
            "flags": 64,
            "components": [{
                "type": 1,
                "components": [
                    {"type": 2, "style": 4, "label": "Confirm Delete", "custom_id": "history_clear_confirm"},
                    {"type": 2, "style": 2, "label": "Cancel", "custom_id": "history_clear_cancel"},
                ],
            }],
        },
    }


async def _handle_history_clear_cancel(payload: dict) -> dict:
    return {"type": 7, "data": {"content": "Cancelled, nothing was deleted.", "components": []}}


def _history_detail_embed(app_row: dict) -> dict:
    status = app_row["status"]
    color = {"pending": 0x1B42FF, "accepted": 0x10B981, "declined": 0xEF4444}[status]
    fields = [
        {"name": "X Profile", "value": f"@{app_row['x_username']}", "inline": True},
        {"name": "Status", "value": f"{STATUS_EMOJI.get(status, '')} {status.capitalize()}", "inline": True},
        {"name": "Submitted", "value": (app_row.get("submitted_at") or "")[:10], "inline": True},
        {"name": "Intro & Role", "value": _trunc(app_row["intro"]), "inline": False},
        {"name": "Communities", "value": _trunc(app_row["communities"]), "inline": False},
        {"name": "Adding Value", "value": _trunc(app_row["value"]), "inline": False},
    ]
    if app_row.get("reviewed_by"):
        fields.append({"name": "Reviewed By", "value": app_row["reviewed_by"], "inline": True})
    if app_row.get("reviewed_at"):
        fields.append({"name": "Reviewed At", "value": app_row["reviewed_at"][:10], "inline": True})
    if app_row.get("decline_reason"):
        fields.append({"name": "Decline Reason", "value": _trunc(app_row["decline_reason"], 300), "inline": False})
    if app_row.get("invite_url"):
        fields.append({"name": "Invite Link", "value": app_row["invite_url"], "inline": False})
    return {
        "title": app_row["name"],
        "color": color,
        "fields": fields,
        "footer": {"text": f"Application ID: {app_row['id']}"},
    }


def _history_detail_components(app_row: dict) -> list[dict]:
    # Pending applications get real Accept/Decline buttons right here, not
    # just a read-only view - reuses the exact same accept:/decline: custom
    # IDs the original applications-channel message uses, so it's the same
    # tested code path either way. Accepted/declined ones are read-only,
    # nothing left to action.
    row = [_x_profile_button(app_row["x_username"])]
    if app_row["status"] == "pending":
        row = [
            {"type": 2, "style": 3, "label": "Accept", "custom_id": f"accept:{app_row['id']}"},
            {"type": 2, "style": 4, "label": "Decline", "custom_id": f"decline:{app_row['id']}"},
        ] + row
    return [{"type": 1, "components": row}]


async def _handle_history_command(payload: dict) -> dict:
    if not _is_team_member(payload):
        return {"type": 4, "data": {"content": "This command is for team members only.", "flags": 64}}
    # Tied to the applications channel specifically, not just gated by
    # permission - even a team member typing this from #general gets
    # pointed back to the right place instead of getting a result out of
    # context. The permission check above is what actually keeps citizens
    # out; this is about where, not who.
    if (
        settings.discord_applications_channel_id
        and payload.get("channel_id") != settings.discord_applications_channel_id
    ):
        return {
            "type": 4,
            "data": {
                "content": f"Use this in <#{settings.discord_applications_channel_id}> instead.",
                "flags": 64,
            },
        }
    status_filter = _cmd_options(payload).get("status") or "all"
    await _dispatch_history_worker("list", token=payload["token"], status_filter=status_filter, offset=0)
    return {"type": 5, "data": {"flags": 64}}  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, ephemeral


async def _handle_history_select(payload: dict) -> dict:
    if not _is_team_member(payload):
        return {"type": 4, "data": {"content": "This command is for team members only.", "flags": 64}}
    values = (payload.get("data") or {}).get("values") or []
    app_id = values[0] if values else None
    if not app_id:
        return {"type": 4, "data": {"content": "Nothing selected.", "flags": 64}}
    await _dispatch_history_worker("select", token=payload["token"], app_id=app_id)
    return {"type": 5, "data": {"flags": 64}}  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, ephemeral


async def _handle_history_page(payload: dict) -> dict:
    if not _is_team_member(payload):
        return {"type": 4, "data": {"content": "This command is for team members only.", "flags": 64}}
    custom_id = (payload.get("data") or {}).get("custom_id", "")
    _, _, rest = custom_id.partition(":")
    status_filter, _, offset_str = rest.partition(":")
    offset = int(offset_str) if offset_str.isdigit() else 0
    await _dispatch_history_worker("list", token=payload["token"], status_filter=status_filter, offset=offset)
    return {"type": 6}  # DEFERRED_UPDATE_MESSAGE — edits the list message once ready


async def _handle_history_clear_confirm(payload: dict) -> dict:
    if not _is_team_member(payload):
        return {"type": 4, "data": {"content": "This command is for team members only.", "flags": 64}}
    await _dispatch_history_worker("clear_confirm", token=payload["token"])
    return {"type": 6}  # DEFERRED_UPDATE_MESSAGE — edits the confirm prompt once the delete finishes


async def _history_clear_confirm_run(token: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        # Only resolved applications ever count toward the keep-limit or get
        # deleted - a pending one that's been sitting a while is still live
        # work the team hasn't acted on yet, not clutter, so it's excluded
        # from this query entirely and also re-excluded (belt and suspenders)
        # directly on the delete call below.
        keep_res = await client.get(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(),
            params={"select": "id", "status": "neq.pending", "order": "submitted_at.desc", "limit": HISTORY_KEEP_LIMIT},
        )
        keep_res.raise_for_status()
        keep_ids = [r["id"] for r in keep_res.json()]
        if not keep_ids:
            await _discord_followup_patch(token, {"content": "Nothing to clear.", "components": []})
            return

        del_res = await client.delete(
            f"{settings.supabase_url}/rest/v1/applications",
            headers=_supabase_headers(prefer="return=representation"),
            params={"id": f"not.in.({','.join(keep_ids)})", "status": "neq.pending"},
        )
        del_res.raise_for_status()
        deleted_count = len(del_res.json())

    await _discord_followup_patch(token, {
        "content": f"Deleted {deleted_count} old application{'s' if deleted_count != 1 else ''}. The {HISTORY_KEEP_LIMIT} most recent resolved applications stay on file, and every pending one was left untouched. Run `/history` again to see the updated list.",
        "components": [],
    })


@app.post("/discord/history-worker")
async def discord_history_worker(request: Request):
    # Not reachable from Discord directly - only this backend's own
    # /history handlers call it, authenticated with the same shared secret
    # the /cron/* endpoints use. See the big comment above the /history
    # section for why this exists as a separate endpoint at all.
    if request.headers.get("X-Internal-Secret") != settings.cron_secret:
        raise HTTPException(status_code=401, detail="unauthorized")

    body = await request.json()
    action = body.get("action")
    token = body.get("token")

    try:
        if action == "list":
            # No "flags" here - the edit-message endpoint doesn't accept it
            # (ephemeral was already locked in by the original deferred
            # response), and Discord 400s the whole request if it's present.
            resp = await _history_list_response(body.get("status_filter") or "all", body.get("offset") or 0)
            await _discord_followup_patch(token, resp)
        elif action == "select":
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(
                    f"{settings.supabase_url}/rest/v1/applications",
                    headers=_supabase_headers(),
                    params={"id": f"eq.{body.get('app_id')}", "select": "*"},
                )
                res.raise_for_status()
                rows = res.json()
            if not rows:
                await _discord_followup_patch(token, {"content": "Application not found."})
            else:
                await _discord_followup_patch(token, {
                    "embeds": [_history_detail_embed(rows[0])],
                    "components": _history_detail_components(rows[0]),
                })
        elif action == "clear_confirm":
            await _history_clear_confirm_run(token)
        else:
            await _discord_followup_patch(token, {"content": "Unrecognized request."})
    except Exception:
        logger.exception("history worker failed for action %s", action)
        if token:
            await _discord_followup_patch(token, {"content": "Something went wrong loading that. Please try again.", "components": []})

    return {"ok": True}


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
    "robinhood": {"rpc": "https://rpc.mainnet.chain.robinhood.com", "coingecko_id": "ethereum"},
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


async def _parse_eth_amount(client: httpx.AsyncClient, raw, field_label: str = "amount") -> float:
    # Accepts a plain ETH number ("0.03"), or a USD amount ("$50", "50usd",
    # "50 USD") which gets converted at the live rate - so every price
    # input across the bots takes whichever unit is easier to think in,
    # not just raw ETH.
    s = str(raw or "").strip().lower().replace(",", "").replace(" ", "")
    if not s:
        raise HTTPException(status_code=400, detail=f"Missing {field_label}.")
    is_usd = s.startswith("$") or s.endswith("usd") or s.endswith("dollars")
    s = s.lstrip("$")
    for suffix in ("dollars", "usd", "eth"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    try:
        value = float(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f'Couldn\'t read "{raw}" as a price - try something like `0.03` or `$50`.')
    if value < 0:
        raise HTTPException(status_code=400, detail=f"{field_label.capitalize()} can't be negative.")
    if not is_usd:
        return value
    prices = await _get_coingecko_prices(client, ["ethereum"])
    eth_usd = (prices.get("ethereum") or {}).get("usd")
    if not eth_usd:
        raise HTTPException(status_code=502, detail="Could not fetch a live ETH price to convert USD right now - try an ETH amount instead.")
    return value / eth_usd


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


async def _gas_core(chain: str) -> dict:
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


@app.get("/toolkit/gas")
@limiter.limit("60/minute")
async def toolkit_gas(request: Request, chain: str = Query("ethereum")):
    return await _gas_core(chain)


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


async def _rug_check_core(address: str, chain_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        if chain_id == "solana":
            return await _rug_check_solana(client, address)
        return await _rug_check_evm(client, address, chain_id)


@app.post("/toolkit/rug-check")
@limiter.limit("20/minute")
async def rug_check(request: Request, payload: RugCheckIn):
    # Proxied server-side (rather than called from the browser) so these free
    # APIs aren't hit by an uncontrolled client fan-out, and so they share
    # the same slowapi rate limiting as the rest of the API.
    return await _rug_check_core(payload.address, payload.chain_id)


# ── CA Scanner (Discord) ──────────────────────────────────────────────────
# The web CA Scanner calls DexScreener directly from the browser (its CORS
# is open, no proxying needed there). The Discord bot has no browser, so
# this is the same lookup done server-side for /scan.
_CHAIN_DISPLAY_NAMES = {
    "ethereum": "Ethereum", "bsc": "BNB Chain", "polygon": "Polygon", "arbitrum": "Arbitrum",
    "optimism": "Optimism", "base": "Base", "avalanche": "Avalanche", "solana": "Solana",
    "robinhood": "Robinhood Chain",
}


async def _ca_scan_core(address: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
            res.raise_for_status()
            data = res.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=502, detail="Could not reach the scanner right now")

    pairs = data.get("pairs") or []
    if not pairs:
        raise HTTPException(status_code=404, detail="No pools found for that address on any indexed chain")

    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    info = best.get("info") or {}
    pc = best.get("priceChange") or {}
    chain_id = best.get("chainId", "")
    return {
        "name": (best.get("baseToken") or {}).get("name") or "Unknown token",
        "symbol": (best.get("baseToken") or {}).get("symbol") or "",
        "chain": _CHAIN_DISPLAY_NAMES.get(chain_id, chain_id.title()),
        "dex": best.get("dexId") or "",
        "priceUsd": float(best["priceUsd"]) if best.get("priceUsd") else None,
        "change24h": pc.get("h24"),
        "marketCap": best.get("marketCap") or best.get("fdv"),
        "liquidityUsd": (best.get("liquidity") or {}).get("usd"),
        "volume24h": (best.get("volume") or {}).get("h24"),
        "imageUrl": info.get("imageUrl"),
        "url": best.get("url"),
    }


# ── New Pair Scanner (Discord) ────────────────────────────────────────────
# Same GeckoTerminal endpoint the web Pairs tool polls client-side every
# 45s. A slash command is a one-shot snapshot rather than a live feed, so
# this returns the freshest handful at call time.
_PAIRS_CHAIN_DISPLAY = {
    "eth": "Ethereum", "bsc": "BNB Chain", "polygon_pos": "Polygon", "arbitrum": "Arbitrum",
    "optimism": "Optimism", "base": "Base", "avax": "Avalanche", "solana": "Solana",
    "robinhood": "Robinhood Chain",
}


async def _pairs_core(chain: str, limit: int = 5) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(f"https://api.geckoterminal.com/api/v2/networks/{chain}/new_pools?page=1")
            res.raise_for_status()
            data = res.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=502, detail="Could not reach the pair scanner right now")

    out = []
    for p in (data.get("data") or [])[:limit]:
        a = p.get("attributes", {})
        pool_addr = a.get("address") or p.get("id", "").split("_")[-1]
        out.append({
            "name": a.get("name"),
            "liquidityUsd": float(a["reserve_in_usd"]) if a.get("reserve_in_usd") else None,
            "createdAt": a.get("pool_created_at"),
            "url": f"https://www.geckoterminal.com/{chain}/pools/{pool_addr}",
        })
    return out


# ── Wallet Card (Discord) ─────────────────────────────────────────────────
# Same ENS resolve/reverse-resolve as the web Wallet Card tool. The QR code
# itself needs no server-side work — api.qrserver.com generates one from a
# plain GET URL, which Discord can just embed directly as an image.
async def _wallet_card_core(raw: str) -> dict:
    raw = raw.strip()
    addr = raw
    ens_name = None
    async with httpx.AsyncClient(timeout=10) as client:
        if raw.lower().endswith(".eth"):
            try:
                r = await client.get("https://api.ensideas.com/ens/resolve/" + raw)
                d = r.json()
                if d and d.get("address"):
                    addr = d["address"]
                    ens_name = raw
            except (httpx.HTTPError, ValueError):
                addr = ""
        elif re.match(r"^0x[a-fA-F0-9]{40}$", raw):
            try:
                r = await client.get("https://api.ensideas.com/ens/resolve/" + raw)
                d = r.json()
                if d and d.get("name"):
                    ens_name = d["name"]
            except (httpx.HTTPError, ValueError):
                pass
    if not addr:
        raise HTTPException(status_code=400, detail="Could not resolve that address or ENS name")
    return {
        "address": addr,
        "ensName": ens_name,
        "qrUrl": "https://api.qrserver.com/v1/create-qr-code/?size=300x300&margin=8&data=" + urllib.parse.quote(addr),
    }


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


async def _get_opensea_key(client: httpx.AsyncClient, force_refresh: bool = False) -> str | None:
    global _opensea_key, _opensea_key_expiry
    if not force_refresh and _opensea_key and time.time() < _opensea_key_expiry - 3600:
        return _opensea_key

    if not force_refresh:
        shared = await _supabase_read_opensea_key(client)
        if shared:
            _opensea_key, _opensea_key_expiry = shared
            return _opensea_key

    # force_refresh skips both caches on purpose - a caller only sets it
    # after OpenSea itself has just 401'd the "current" key, so re-reading
    # the same shared Supabase row here would hand back that identical
    # known-bad key and guarantee a second 401 on the retry.
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
            # Key was revoked/expired early — force a genuinely fresh one
            # (bypassing the shared cache, which still holds this same bad
            # key) and retry once.
            global _opensea_key
            _opensea_key = None
            key = await _get_opensea_key(client, force_refresh=True)
            if not key:
                return None
            res = await client.get(
                "https://api.opensea.io/api/v2" + path,
                params=params or {},
                headers={"X-API-KEY": key},
            )
        if res.status_code == 429:
            # Ground-truth signal that OpenSea's free-tier rate limit has
            # actually been hit right now - recorded so NFT Scope's
            # dynamic slot/scan-budget sizing can automatically back off,
            # instead of relying on a manually-tuned guess at what's
            # "safe." Best-effort: a failed write here must never break
            # the request already in flight.
            try:
                await _nft_alert_state_set(client, "__opensea__", "rate_limited", 0)
            except httpx.HTTPError:
                pass
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
    pricing = c.get("pricing_currencies") or {}
    listing_currency = pricing.get("listing_currency") or {}
    offer_currency = pricing.get("offer_currency") or {}
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
        "volTotal": total.get("volume"),
        "sales24h": (intervals.get("one_day") or {}).get("sales"),
        "owners": total.get("num_owners"),
        "totalSupply": c.get("total_supply"),
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
        # OpenSea reports the live USD rate for whatever currency each figure
        # is actually denominated in - real conversion, not an ETH assumption.
        "floorUsd": (total.get("floor_price") * float(listing_currency["usd_price"]))
        if total.get("floor_price") is not None and listing_currency.get("usd_price") else None,
        "listingUsdRate": float(listing_currency["usd_price"]) if listing_currency.get("usd_price") else None,
        "offerSymbol": offer_currency.get("symbol"),
        "offerUsdRate": float(offer_currency["usd_price"]) if offer_currency.get("usd_price") else None,
        "offerDecimals": offer_currency.get("decimals"),
    }
    if is_full_source and slug:
        _collection_meta_cache[slug] = (time.time(), shaped)
        _cap_cache(_collection_meta_cache)
    return shaped


async def _opensea_get_top_offer(client: httpx.AsyncClient, slug: str) -> int | None:
    # Returns the top offer's raw integer amount, in the offer currency's
    # smallest unit (matches offerDecimals from _nft_collection_shape) -
    # OpenSea returns offers already sorted highest-first.
    data = await _opensea_get(client, f"/offers/collection/{slug}")
    offers = (data or {}).get("offers") or []
    if not offers:
        return None
    try:
        return int(offers[0]["protocol_data"]["parameters"]["offer"][0]["startAmount"])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


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


_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Chains OpenSea's contract-lookup endpoint is tried against, in priority
# order, when a member searches by contract address instead of a name.
# ethereum/base/robinhood first (the chains this community actually mints
# on, matching _NFT_SCOPE_CHAINS), then the rest of what OpenSea covers
# as bonus reach.
_NFT_CONTRACT_LOOKUP_CHAINS = ["ethereum", "base", "robinhood", "matic", "arbitrum", "optimism", "avalanche"]


async def _nft_resolve_by_contract(client: httpx.AsyncClient, address: str) -> dict | None:
    # OpenSea's contract endpoint is chain-scoped and there's no
    # "search all chains" variant, so try the candidates in parallel and
    # keep whichever one actually maps to a real collection.
    contract_tasks = [_opensea_get(client, f"/chain/{chain}/contract/{address}") for chain in _NFT_CONTRACT_LOOKUP_CHAINS]
    contract_results = await asyncio.gather(*contract_tasks, return_exceptions=True)
    slug = None
    for res in contract_results:
        if isinstance(res, Exception) or not res:
            continue
        if res.get("collection"):
            slug = res["collection"]
            break
    if not slug:
        return None
    info = await _opensea_get(client, f"/collections/{slug}")
    if not info:
        return None
    stats = await _opensea_get(client, f"/collections/{slug}/stats")
    return _nft_collection_shape(info, stats)


async def _nft_search_core(q: str) -> list[dict]:
    q = (q or "").strip()
    async with httpx.AsyncClient(timeout=10) as client:
        if _EVM_ADDRESS_RE.match(q):
            direct = await _nft_resolve_by_contract(client, q)
            return [_enrich_with_cached_meta(direct)] if direct else []
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
        return out


@app.get("/toolkit/nft-search")
@limiter.limit("40/minute")
async def nft_search(request: Request, q: str = Query(..., min_length=1, max_length=80)):
    return {"results": await _nft_search_core(q)}


async def _nft_collection_core(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        info = await _opensea_get(client, f"/collections/{slug}")
        stats = await _opensea_get(client, f"/collections/{slug}/stats")
        if info is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return _nft_collection_shape(info, stats)


@app.get("/toolkit/nft-collection")
@limiter.limit("60/minute")
async def nft_collection(request: Request, slug: str = Query(..., min_length=1, max_length=120)):
    return await _nft_collection_core(slug)


# ── Discord /watchlist — per-citizen NFT watchlist, persisted in Supabase ──
async def _discord_watchlist_add(discord_user_id: str, slug: str) -> dict:
    # Confirm the collection is real before saving a slug nobody can look
    # up later - matches the web tool only ever adding from real search
    # results, never an arbitrary typed string.
    collection = await _nft_collection_core(slug)
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(
            f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
            headers=_supabase_headers(prefer="resolution=merge-duplicates,return=minimal"),
            json=[{"discord_user_id": discord_user_id, "slug": collection["slug"]}],
        )
        res.raise_for_status()
    return collection


async def _discord_watchlist_remove(discord_user_id: str, slug: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.delete(
            f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
            headers=_supabase_headers(prefer="return=minimal"),
            params={"discord_user_id": f"eq.{discord_user_id}", "slug": f"eq.{slug}"},
        )
        res.raise_for_status()


async def _discord_watchlist_clear(discord_user_id: str) -> int:
    # A lightweight slug-only count first, not the full _discord_watchlist_list
    # (which fetches each collection's live OpenSea data via
    # asyncio.gather) - clearing doesn't need any of that, just how many
    # rows existed, so this stays a single cheap Supabase round trip.
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
            headers=_supabase_headers(),
            params={"discord_user_id": f"eq.{discord_user_id}", "select": "slug"},
        )
        res.raise_for_status()
        count = len(res.json())
        if count == 0:
            return 0
        del_res = await client.delete(
            f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
            headers=_supabase_headers(prefer="return=minimal"),
            params={"discord_user_id": f"eq.{discord_user_id}"},
        )
        del_res.raise_for_status()
        return count


async def _discord_watchlist_list(discord_user_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
            headers=_supabase_headers(),
            params={"discord_user_id": f"eq.{discord_user_id}", "select": "slug", "order": "added_at.desc"},
        )
        res.raise_for_status()
        slugs = [row["slug"] for row in res.json()]
        if not slugs:
            return []
        results = await asyncio.gather(*[_nft_collection_core(s) for s in slugs], return_exceptions=True)
        return [r for r in results if not isinstance(r, Exception)]


# ── NFT alerts & mint radar (Discord notification bot) ─────────────────────
# Powers /cron/nft-poll, hit every 5 minutes by a free GitHub Actions
# schedule (Vercel's own cron is once-a-day on this project's plan). Two
# jobs share one poll cycle: (1) diff each watchlisted collection's latest
# stats against its own snapshot history to catch supply cuts, volume
# spikes and sweeps, and (2) scan recently-created collections for new
# mints, scored by an explicit on-chain-only checklist so citizens can see
# exactly why something was flagged - not a black-box number. Everything
# here runs on the same free OpenSea "instant" key and Supabase project
# already used by the rest of the toolkit; no paid API involved.
_NFT_ALERT_COOLDOWN_SECONDS = 3600  # don't re-alert the same collection/type within an hour
_NFT_FLOOR_CHANGE_THRESHOLD_PCT = 8.0  # minimum floor move (either direction) worth alerting on
# arbitrum/optimism/avalanche are already proven-working OpenSea chain
# slugs elsewhere in this codebase (_NFT_CONTRACT_LOOKUP_CHAINS) - adding
# them here covers three more entire, independent NFT ecosystems this
# scan previously never touched at all.
_NFT_SCOPE_CHAINS = ["ethereum", "base", "polygon", "robinhood", "arbitrum", "optimism", "avalanche"]


async def _nft_store_snapshot(client: httpx.AsyncClient, c: dict) -> None:
    await client.post(
        f"{settings.supabase_url}/rest/v1/nft_snapshot_history",
        headers=_supabase_headers(prefer="return=minimal"),
        json=[{
            "slug": c["slug"],
            "chain": c.get("chain"),
            "floor": c.get("floor"),
            "symbol": c.get("symbol"),
            "volume_1d": c.get("vol1d"),
            "sales_1d": c.get("sales24h"),
            "volume_total": c.get("volTotal"),
            "owners": c.get("owners"),
            "total_supply": c.get("totalSupply"),
        }],
    )


async def _nft_recent_snapshots(client: httpx.AsyncClient, slug: str, limit: int = 50) -> list[dict]:
    res = await client.get(
        f"{settings.supabase_url}/rest/v1/nft_snapshot_history",
        headers=_supabase_headers(),
        params={"slug": f"eq.{slug}", "select": "*", "order": "captured_at.desc", "limit": str(limit)},
    )
    res.raise_for_status()
    return res.json()


async def _nft_alert_state_get(client: httpx.AsyncClient, slug: str, alert_type: str) -> dict | None:
    res = await client.get(
        f"{settings.supabase_url}/rest/v1/nft_alert_state",
        headers=_supabase_headers(),
        params={"slug": f"eq.{slug}", "alert_type": f"eq.{alert_type}", "select": "*", "limit": "1"},
    )
    res.raise_for_status()
    rows = res.json()
    return rows[0] if rows else None


async def _nft_alert_state_set(client: httpx.AsyncClient, slug: str, alert_type: str, value: float) -> None:
    await client.post(
        f"{settings.supabase_url}/rest/v1/nft_alert_state",
        headers=_supabase_headers(prefer="resolution=merge-duplicates,return=minimal"),
        json=[{
            "slug": slug,
            "alert_type": alert_type,
            "last_alerted_at": datetime.now(timezone.utc).isoformat(),
            "last_value": value,
        }],
    )


def _nft_alert_cooled_down(state: dict | None, cooldown_seconds: int = _NFT_ALERT_COOLDOWN_SECONDS) -> bool:
    if not state or not state.get("last_alerted_at"):
        return True
    last = datetime.fromisoformat(state["last_alerted_at"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - last).total_seconds() > cooldown_seconds


async def _post_nft_alert(client: httpx.AsyncClient, channel_id: str, embed: dict) -> bool:
    # Callers use this return value to decide whether to record alert state /
    # mark a mint as seen - a failed post (bad channel id, missing bot
    # permission, etc.) must not be treated as delivered, or that alert is
    # silently lost forever instead of retried next cycle.
    if not channel_id or not settings.discord_bot_token:
        return False
    try:
        res = await _discord_post_with_retry(
            client,
            f"{DISCORD_API}/channels/{channel_id}/messages",
            {"Authorization": f"Bot {settings.discord_bot_token}"},
            {"embeds": [embed]},
        )
        if res.status_code >= 300:
            logger.error("NFT alert post to channel %s failed: %s %s", channel_id, res.status_code, res.text[:300])
            return False
        return True
    except httpx.HTTPError:
        logger.exception("Failed to post NFT alert to Discord")
        return False


def _nft_alert_footer(c: dict) -> dict:
    return {"text": f"{TOOLKIT_FOOTER['text']} · {c.get('chain', '').title() or 'NFT'} Alert"}


def _supply_cut_embed(c: dict, prev_supply: int) -> dict:
    return {
        "title": f"✂️ Supply Cut — {c['name']}",
        "url": c.get("openseaUrl"),
        "description": f"Total supply dropped from **{prev_supply:,}** to **{c['totalSupply']:,}**.",
        "color": EMBED_COLOR_GOOD,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": _nft_alert_footer(c),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _volume_spike_embed(c: dict, avg: float) -> dict:
    return {
        "title": f"📈 Volume Spike — {c['name']}",
        "url": c.get("openseaUrl"),
        "description": (
            f"24h volume is **{c['vol1d']:.2f} {c['symbol']}**, "
            f"vs a recent average of **{avg:.2f} {c['symbol']}**."
        ),
        "color": EMBED_COLOR_WARN,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": _nft_alert_footer(c),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _floor_change_embed(c: dict, prev_floor: float, pct: float) -> dict:
    up = pct >= 0
    return {
        "title": f"{'📈' if up else '📉'} Floor {'Up' if up else 'Down'} — {c['name']}",
        "url": c.get("openseaUrl"),
        "description": (
            f"Floor moved from **{prev_floor:.4f} {c['symbol']}** to **{c['floor']:.4f} {c['symbol']}** "
            f"({pct:+.1f}%)."
        ),
        "color": EMBED_COLOR_GOOD if up else EMBED_COLOR_BAD,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": _nft_alert_footer(c),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _mint_progress_embed(c: dict, prev_supply: int) -> dict:
    return {
        "title": f"🌱 Mint Progress — {c['name']}",
        "url": c.get("openseaUrl"),
        "description": f"Total supply grew from **{prev_supply:,}** to **{c['totalSupply']:,}**.",
        "color": EMBED_COLOR_GOOD,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": _nft_alert_footer(c),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _price_target_embed(c: dict, target: float, direction: str, loop_alert: bool = False) -> dict:
    verb = "dropped to or below" if direction == "below" else "risen to or above"
    symbol = c.get("symbol") or "ETH"
    footer_note = "Price Target · Looping (re-alerts hourly while true)" if loop_alert else "Price Target · One-time"
    return {
        "title": f"🎯 {c['name']} hit your target price",
        "url": c.get("openseaUrl"),
        "description": f"Floor has {verb} **{target:.4f} {symbol}** — currently **{c['floor']:.4f} {symbol}**.",
        "color": EMBED_COLOR_GOOD if direction == "below" else EMBED_COLOR,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": {"text": f"{TOOLKIT_FOOTER['text']} · {footer_note}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _check_price_alerts(client: httpx.AsyncClient, slug: str, c: dict) -> None:
    floor = c.get("floor")
    if floor is None:
        return
    try:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/nft_price_alerts",
            headers=_supabase_headers(),
            params={"slug": f"eq.{slug}", "select": "discord_user_id,target_price,direction,loop_alert,last_alerted_at"},
        )
        res.raise_for_status()
        rows = res.json()
    except httpx.HTTPError:
        # Table may not exist yet if schema.sql hasn't been re-run.
        return

    fired_user_ids = []
    for row in rows:
        target = row["target_price"]
        direction = row["direction"]
        hit = (floor <= target) if direction == "below" else (floor >= target)
        if not hit:
            continue
        is_loop = bool(row.get("loop_alert"))
        # Loop alerts re-fire on the same hour cooldown as every other
        # /monitor event type instead of every single poll cycle, for as
        # long as the floor keeps satisfying the condition.
        if is_loop and not _nft_alert_cooled_down(row):
            continue
        embed = _price_target_embed(c, target, direction, loop_alert=is_loop)
        delivered = await _discord_dm(client, row["discord_user_id"], embed, content=f"🎯 {embed['title']}")
        if not delivered:
            continue
        fired_user_ids.append(row["discord_user_id"])
        match_params = {"discord_user_id": f"eq.{row['discord_user_id']}", "slug": f"eq.{slug}", "target_price": f"eq.{target}"}
        if is_loop:
            await client.patch(
                f"{settings.supabase_url}/rest/v1/nft_price_alerts",
                headers=_supabase_headers(prefer="return=minimal"),
                params=match_params,
                json={"last_alerted_at": datetime.now(timezone.utc).isoformat()},
            )
        else:
            # One-shot by design - a specific price target is a single
            # moment someone's waiting for. Only clear it once the DM
            # actually lands, so a closed-DMs failure retries next poll
            # instead of silently vanishing.
            await client.delete(
                f"{settings.supabase_url}/rest/v1/nft_price_alerts",
                headers=_supabase_headers(prefer="return=minimal"),
                params=match_params,
            )

    # Same public+tag treatment as every other /monitor event type - post
    # once per slug per poll cycle (not once per person) so several
    # citizens hitting their target on the same collection at the same
    # time show up as one call-out, not a spam of near-identical posts.
    if fired_user_ids and settings.discord_nft_monitor_channel_id:
        symbol = c.get("symbol") or "ETH"
        mentions = " ".join(f"<@{uid}>" for uid in dict.fromkeys(fired_user_ids))
        public_embed = {
            "title": f"🎯 Price target hit — {c['name']}",
            "url": c.get("openseaUrl"),
            "description": f"Floor is now **{floor:.4f} {symbol}**, hitting the target for the citizens tagged below.",
            "color": EMBED_COLOR_GOOD,
            "thumbnail": {"url": c["image"]} if c.get("image") else None,
            "footer": {"text": f"{TOOLKIT_FOOTER['text']} · Price Target"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await _post_channel_message(client, settings.discord_nft_monitor_channel_id, public_embed, content=f"🎯 Price target hit on **{c['name']}** — {mentions}")


def _sweep_embed(c: dict, sweep: dict) -> dict:
    return {
        "title": f"🧹 Possible Sweep — {c['name']}",
        "url": c.get("openseaUrl"),
        "description": (
            f"**{sweep['count']}** sales in the last {sweep['windowMinutes']} min, "
            f"by only **{sweep['uniqueBuyers']}** unique wallet(s) from **{sweep['uniqueSellers']}** "
            f"different sellers — **{sweep['totalPaid']:.2f} {sweep['symbol']}** total."
        ),
        "color": EMBED_COLOR_BAD,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": _nft_alert_footer(c),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_recent_sale_events(client: httpx.AsyncClient, slug: str, window_seconds: int, limit: int = 50) -> list[dict]:
    data = await _opensea_get(client, f"/events/collection/{slug}", {"event_type": "sale", "limit": limit})
    events = (data or {}).get("asset_events") or []
    now = time.time()
    return [e for e in events if now - (e.get("event_timestamp") or 0) <= window_seconds]


def _analyze_wash_trading(events: list[dict]) -> dict:
    # A free-data wash-trade heuristic built from raw sale events - not
    # one check but a small toolkit, each catching a different way real
    # trading can be faked without needing a paid third-party wash-trade
    # API:
    #   (a) direct self-trade - same wallet as buyer and seller on one sale
    #   (b) reciprocal ping-pong - two wallets flipping the same position
    #       back and forth
    #   (c) closed trading cluster - a small set of wallets that ONLY
    #       ever trade with each other, never an outside counterparty
    #       (catches 3+ wallet cycles that (a)/(b) alone would miss)
    #   (d) low token diversity - the same one or two tokens changing
    #       hands repeatedly instead of distinct items actually moving
    reasons = []
    buyers = {e["buyer"] for e in events if e.get("buyer")}
    sellers = {e["seller"] for e in events if e.get("seller")}

    self_trades = [e for e in events if e.get("buyer") and e.get("buyer") == e.get("seller")]
    if self_trades:
        reasons.append(f"{len(self_trades)} sale(s) had the same wallet as buyer and seller")

    edges = {(e["seller"], e["buyer"]) for e in events if e.get("buyer") and e.get("seller") and e["buyer"] != e["seller"]}
    reciprocal_pairs = {tuple(sorted(pair)) for pair in edges if (pair[1], pair[0]) in edges}
    if reciprocal_pairs:
        reasons.append(f"{len(reciprocal_pairs)} wallet pair(s) traded back and forth with each other")

    # A wallet that shows up as BOTH a buyer (some sale) and a seller
    # (another sale) within the same window is the real tell of
    # recirculation - a legitimate sweep has purely-buying whales and
    # purely-selling holders, so this is empty for real activity no
    # matter how few distinct sellers there are. Without this check,
    # "closed" below is trivially true for any small wallet count, since
    # counterparties are always built from the same event set they're
    # compared against.
    all_wallets = buyers | sellers
    recirculating = buyers & sellers
    if recirculating and 2 <= len(all_wallets) <= 6 and len(events) >= 4:
        counterparties: dict[str, set] = {}
        for e in events:
            b, s = e.get("buyer"), e.get("seller")
            if not b or not s:
                continue
            counterparties.setdefault(b, set()).add(s)
            counterparties.setdefault(s, set()).add(b)
        if counterparties and all(cps <= all_wallets for cps in counterparties.values()):
            reasons.append(f"All {len(all_wallets)} wallet(s) involved only ever traded with each other, never an outside buyer/seller")

    token_ids = [e["nft"]["identifier"] for e in events if isinstance(e.get("nft"), dict) and e["nft"].get("identifier") is not None]
    if token_ids:
        unique_tokens = len(set(token_ids))
        if unique_tokens <= max(1, len(token_ids) // 3):
            reasons.append(f"Only {unique_tokens} distinct token(s) changed hands across {len(token_ids)} sales - repeatedly flipped, not organic spread")

    return {"suspicious": bool(reasons), "reasons": reasons, "unique_buyers": len(buyers), "unique_sellers": len(sellers)}


async def _detect_sweep(
    client: httpx.AsyncClient, slug: str, window_seconds: int = 900, min_sales: int = 5,
    max_buyer_ratio: float = 0.4, min_seller_ratio: float = 0.3,
) -> dict | None:
    # A real sweep: several sales in a short window, concentrated in very
    # few buyer wallets, bought from MANY DIFFERENT sellers (the existing
    # holders whose floor listings just got scooped). Wash trading can
    # fake the same "few buyers, many sales" shape without a real sweep
    # ever happening - _analyze_wash_trading screens that out instead of
    # presenting it as a bullish signal.
    recent = await _fetch_recent_sale_events(client, slug, window_seconds)
    if len(recent) < min_sales:
        return None
    buyers = {e["buyer"] for e in recent if e.get("buyer")}
    sellers = {e["seller"] for e in recent if e.get("seller")}
    if not buyers or len(buyers) / len(recent) > max_buyer_ratio:
        return None
    if not sellers or len(sellers) / len(recent) < min_seller_ratio:
        return None
    if _analyze_wash_trading(recent)["suspicious"]:
        return None
    payments = [e["payment"] for e in recent if e.get("payment")]
    if not payments:
        return None
    decimals = payments[0].get("decimals", 18)
    total_paid = sum(int(p["quantity"]) for p in payments) / (10 ** decimals)
    return {
        "count": len(recent),
        "uniqueBuyers": len(buyers),
        "uniqueSellers": len(sellers),
        "totalPaid": total_paid,
        "symbol": payments[0].get("symbol", "ETH"),
        "windowMinutes": window_seconds // 60,
    }


async def _nft_poll_tracked_slugs(client: httpx.AsyncClient) -> list[str]:
    # The tracked set is the union of every citizen's personal /watchlist
    # AND everything anyone has a /monitor subscription on - a collection
    # nobody watchlisted but someone specifically subscribed to (via
    # /monitor) still needs to be polled, or its subscriptions would just
    # sit there and never fire.
    watchlist_res = await client.get(
        f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
        headers=_supabase_headers(),
        params={"select": "slug"},
    )
    watchlist_res.raise_for_status()
    slugs = {row["slug"] for row in watchlist_res.json()}

    try:
        sub_res = await client.get(
            f"{settings.supabase_url}/rest/v1/nft_watch_subscriptions",
            headers=_supabase_headers(),
            params={"select": "slug"},
        )
        sub_res.raise_for_status()
        slugs |= {row["slug"] for row in sub_res.json()}
    except httpx.HTTPError:
        # Table may not exist yet if schema.sql hasn't been re-run - the
        # watchlist-only set is still a valid fallback.
        pass

    try:
        price_res = await client.get(
            f"{settings.supabase_url}/rest/v1/nft_price_alerts",
            headers=_supabase_headers(),
            params={"select": "slug"},
        )
        price_res.raise_for_status()
        slugs |= {row["slug"] for row in price_res.json()}
    except httpx.HTTPError:
        pass

    return sorted(slugs)


async def _nft_watch_subscribers(client: httpx.AsyncClient, slug: str, event_type: str) -> list[str]:
    try:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/nft_watch_subscriptions",
            headers=_supabase_headers(),
            params={"slug": f"eq.{slug}", "event_type": f"eq.{event_type}", "select": "discord_user_id"},
        )
        res.raise_for_status()
        return [row["discord_user_id"] for row in res.json()]
    except httpx.HTTPError:
        return []


async def _discord_dm(client: httpx.AsyncClient, discord_user_id: str, embed: dict, content: str | None = None) -> bool:
    if not settings.discord_bot_token:
        return False
    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    try:
        dm_res = await client.post(f"{DISCORD_API}/users/@me/channels", headers=headers, json={"recipient_id": discord_user_id})
        if dm_res.status_code >= 300:
            logger.error("Could not open DM with %s: %s %s", discord_user_id, dm_res.status_code, dm_res.text[:200])
            return False
        channel_id = dm_res.json()["id"]
        # An embed-only DM often renders as a blank push notification on
        # mobile (the OS preview reads message.content, not the embed) -
        # actual "content" text is what makes this land as a real ping
        # instead of a silent message the citizen only sees if they
        # happen to open Discord.
        body = {"embeds": [embed]}
        if content:
            body["content"] = content
        msg_res = await _discord_post_with_retry(client, f"{DISCORD_API}/channels/{channel_id}/messages", headers, body)
        return msg_res.status_code < 300
    except (httpx.HTTPError, KeyError):
        # Most common cause: the user has DMs from server members disabled.
        # Not fatal - the public channel post already carries the alert.
        return False


async def _post_channel_message(client: httpx.AsyncClient, channel_id: str, embed: dict, content: str | None = None) -> bool:
    if not channel_id or not settings.discord_bot_token:
        return False
    try:
        body = {"embeds": [embed]}
        if content:
            body["content"] = content
        res = await _discord_post_with_retry(
            client, f"{DISCORD_API}/channels/{channel_id}/messages",
            {"Authorization": f"Bot {settings.discord_bot_token}"}, body,
        )
        if res.status_code >= 300:
            logger.error("Monitor public post to channel %s failed: %s %s", channel_id, res.status_code, res.text[:300])
            return False
        return True
    except httpx.HTTPError:
        logger.exception("Failed to post /monitor alert publicly")
        return False


async def _dm_watchlist_owners(client: httpx.AsyncClient, slug: str, embed: dict) -> None:
    # /watchlist has no per-event-type granularity like /monitor does -
    # adding a collection means "keep me posted on everything for this
    # one," so any alert that fires for it DMs everyone who added it,
    # not just posts to the shared public channel.
    try:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/discord_nft_watchlist",
            headers=_supabase_headers(),
            params={"slug": f"eq.{slug}", "select": "discord_user_id"},
        )
        res.raise_for_status()
        owner_ids = [row["discord_user_id"] for row in res.json()]
    except httpx.HTTPError:
        return
    if not owner_ids:
        return
    ping = f"🔔 {embed['title']}" if embed.get("title") else "🔔 A collection on your watchlist just updated."
    for user_id in owner_ids:
        await _discord_dm(client, user_id, embed, content=ping)


async def _dm_subscribers(client: httpx.AsyncClient, slug: str, event_type: str, embed: dict) -> None:
    subscriber_ids = await _nft_watch_subscribers(client, slug, event_type)
    if not subscriber_ids:
        return
    ping = f"🔔 {embed['title']}" if embed.get("title") else "🔔 An NFT collection you're monitoring just updated."
    for user_id in subscriber_ids:
        await _discord_dm(client, user_id, embed, content=ping)

    # Also call out publicly in the shared monitor channel, tagging
    # everyone who /monitor'd this collection for this event - stays
    # visible even if someone's DMs are closed, and lets the rest of the
    # server see who's tracking what instead of it being silently private.
    if settings.discord_nft_monitor_channel_id:
        mentions = " ".join(f"<@{uid}>" for uid in dict.fromkeys(subscriber_ids))
        await _post_channel_message(client, settings.discord_nft_monitor_channel_id, embed, content=f"{ping} — {mentions}")


# ── /monitor — personal per-collection, per-event DM subscriptions ─────────
_NFT_MONITOR_EVENTS = [
    {"label": "Floor Price Up", "value": "floor_up", "emoji": "📈", "description": f"Alert when floor rises ≥{_NFT_FLOOR_CHANGE_THRESHOLD_PCT:.0f}%"},
    {"label": "Floor Price Down", "value": "floor_down", "emoji": "📉", "description": f"Alert when floor drops ≥{_NFT_FLOOR_CHANGE_THRESHOLD_PCT:.0f}%"},
    {"label": "Supply Cut / Burns", "value": "supply_cut", "emoji": "✂️", "description": "Alert when total supply decreases"},
    {"label": "Mint Progress", "value": "mint_progress", "emoji": "🌱", "description": "Alert when total supply increases"},
    {"label": "Sweep Detected", "value": "sweep", "emoji": "🧹", "description": "Alert on concentrated buying"},
    {"label": "Volume Spike", "value": "volume_spike", "emoji": "📊", "description": "Alert when 24h volume spikes"},
]
_NFT_MONITOR_LABELS = {e["value"]: e["label"] for e in _NFT_MONITOR_EVENTS}


def _monitor_select_component(slug: str) -> dict:
    return {
        "type": 1,
        "components": [{
            "type": 3,
            "custom_id": f"monitor_select:{slug}",
            "placeholder": "Choose what to be DM'd about…",
            "min_values": 0,
            "max_values": len(_NFT_MONITOR_EVENTS),
            "options": [
                {"label": e["label"], "value": e["value"], "description": e["description"], "emoji": {"name": e["emoji"]}}
                for e in _NFT_MONITOR_EVENTS
            ],
        }],
    }


async def _nft_monitor_list(discord_user_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(
            f"{settings.supabase_url}/rest/v1/nft_watch_subscriptions",
            headers=_supabase_headers(),
            params={"discord_user_id": f"eq.{discord_user_id}", "select": "slug,event_type", "order": "slug.asc"},
        )
        res.raise_for_status()
        return res.json()


async def _nft_price_alerts_list(discord_user_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            res = await client.get(
                f"{settings.supabase_url}/rest/v1/nft_price_alerts",
                headers=_supabase_headers(),
                params={"discord_user_id": f"eq.{discord_user_id}", "select": "slug,target_price,direction,loop_alert", "order": "slug.asc"},
            )
            res.raise_for_status()
            return res.json()
        except httpx.HTTPError:
            return []


async def _cmd_monitor_response(payload: dict, discord_user_id: str) -> dict:
    sub_options = (payload.get("data") or {}).get("options") or []
    if not sub_options:
        embed = {"title": "Missing subcommand", "description": "Use `/monitor set`, `list`, or `clear`.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
        return {"embeds": [_clean_embed(embed)], "flags": 64}
    sub = sub_options[0]
    sub_name = sub.get("name")
    sub_opts = {o["name"]: o.get("value") for o in (sub.get("options") or [])}

    if sub_name == "list":
        rows = await _nft_monitor_list(discord_user_id)
        price_rows = await _nft_price_alerts_list(discord_user_id)
        if not rows and not price_rows:
            embed = {"title": "🔔 Your Monitor Subscriptions", "description": "Nothing yet. Try `/monitor set` or `/monitor price`.", "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}
        else:
            by_slug: dict[str, list[str]] = {}
            for r in rows:
                by_slug.setdefault(r["slug"], []).append(_NFT_MONITOR_LABELS.get(r["event_type"], r["event_type"]))
            for r in price_rows:
                verb = "≤" if r["direction"] == "below" else "≥"
                loop_tag = " 🔁" if r.get("loop_alert") else ""
                by_slug.setdefault(r["slug"], []).append(f"🎯 Price target {verb} {r['target_price']:.4f} ETH{loop_tag}")
            lines = [f"**{slug}**: " + ", ".join(labels) for slug, labels in by_slug.items()]
            embed = {"title": "🔔 Your Monitor Subscriptions", "description": "\n".join(lines), "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}
        return {"embeds": [_clean_embed(embed)], "flags": 64}

    query = (sub_opts.get("collection") or "").strip()
    if not query:
        embed = {"title": "Missing collection", "description": "Provide a collection name.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
        return {"embeds": [_clean_embed(embed)], "flags": 64}

    matches = await _nft_search_core(query)
    if not matches:
        embed = {"title": "No collections found", "description": f'No OpenSea results for "{query}".', "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
        return {"embeds": [_clean_embed(embed)], "flags": 64}
    c = matches[0]

    if sub_name == "clear":
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(
                f"{settings.supabase_url}/rest/v1/nft_watch_subscriptions",
                headers=_supabase_headers(prefer="return=minimal"),
                params={"discord_user_id": f"eq.{discord_user_id}", "slug": f"eq.{c['slug']}"},
            )
            try:
                await client.delete(
                    f"{settings.supabase_url}/rest/v1/nft_price_alerts",
                    headers=_supabase_headers(prefer="return=minimal"),
                    params={"discord_user_id": f"eq.{discord_user_id}", "slug": f"eq.{c['slug']}"},
                )
            except httpx.HTTPError:
                pass
        embed = {"title": f"🔕 Cleared: {c['name']}", "description": "You won't get any /monitor alerts for this collection.", "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}
        return {"embeds": [_clean_embed(embed)], "flags": 64}

    if sub_name == "set":
        embed = {
            "title": f"🔔 Monitor: {c['name']}",
            "description": "Choose what you want to be personally DM'd about for this collection. Selecting nothing clears it.",
            "color": EMBED_COLOR,
            "thumbnail": {"url": c["image"]} if c.get("image") else None,
            "footer": TOOLKIT_FOOTER,
        }
        return {"embeds": [_clean_embed(embed)], "components": [_monitor_select_component(c["slug"])], "flags": 64}

    if sub_name == "price":
        floor = c.get("floor")
        if floor is None:
            embed = {"title": "No floor data", "description": f"{c['name']} doesn't have a floor price to compare against right now.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
            return {"embeds": [_clean_embed(embed)], "flags": 64}

        raw_target = sub_opts.get("target")
        raw_percent = sub_opts.get("percent")
        loop_alert = bool(sub_opts.get("loop"))
        async with httpx.AsyncClient(timeout=10) as client:
            if raw_target is not None:
                # Accepts a plain ETH number or a USD amount ("$50") -
                # converted at the live rate.
                try:
                    target = await _parse_eth_amount(client, raw_target, "target")
                except HTTPException as exc:
                    embed = {"title": "Invalid target", "description": exc.detail, "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
                    return {"embeds": [_clean_embed(embed)], "flags": 64}
            elif raw_percent is not None:
                # Same shortcut as the reference tool's -50%/-25%/+50%/+100%
                # quick-pick buttons - a relative offset off the live floor
                # instead of typing an exact ETH number.
                try:
                    target = floor * (1 + float(raw_percent) / 100)
                except (TypeError, ValueError):
                    target = None
                if target is None or target <= 0:
                    embed = {"title": "Invalid percent", "description": "That works out to a target price of 0 or less - try a smaller drop, e.g. `percent:-50`.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
                    return {"embeds": [_clean_embed(embed)], "flags": 64}
            else:
                embed = {"title": "Missing target", "description": "Give either `target` (an exact price, ETH or `$USD`) or `percent` (e.g. `-50` for 50% below the current floor).", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
                return {"embeds": [_clean_embed(embed)], "flags": 64}

            direction = "below" if target <= floor else "above"
            res = await client.post(
                f"{settings.supabase_url}/rest/v1/nft_price_alerts",
                headers=_supabase_headers(prefer="resolution=merge-duplicates,return=minimal"),
                json=[{
                    "discord_user_id": discord_user_id, "slug": c["slug"], "target_price": target,
                    "direction": direction, "loop_alert": loop_alert,
                }],
            )
            if res.status_code >= 300:
                embed = {"title": "Couldn't save price alert", "description": f"`{res.status_code}` - has `schema.sql` been re-run since `/monitor price` was added?", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
                return {"embeds": [_clean_embed(embed)], "flags": 64}

            symbol = c.get("symbol") or "ETH"
            verb = "drops to or below" if direction == "below" else "rises to or above"
            lifecycle = "Loops - re-alerts roughly hourly for as long as it stays true." if loop_alert else "Fires once, then clears itself."
            embed = {
                "title": f"🎯 Price target set: {c['name']}",
                "description": f"<@{discord_user_id}> will get a DM once the floor {verb} **{target:.4f} {symbol}** (currently {floor:.4f} {symbol}). {lifecycle}",
                "color": EMBED_COLOR_GOOD,
                "thumbnail": {"url": c["image"]} if c.get("image") else None,
                "footer": TOOLKIT_FOOTER,
            }
            # /monitor works from anywhere, but the announcement always
            # lands in the dedicated nft-intel channel specifically -
            # same "DMs stay private, the fact of it is public, in one
            # consistent place" pattern as the rest of /monitor - instead
            # of showing up in whatever channel happened to be typed in.
            if settings.discord_nft_monitor_channel_id:
                await _post_channel_message(client, settings.discord_nft_monitor_channel_id, embed)
        ack = {"title": "🎯 Price target set", "description": f"Posted in <#{settings.discord_nft_monitor_channel_id}>." if settings.discord_nft_monitor_channel_id else "Saved.", "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}
        return {"embeds": [_clean_embed(ack)], "flags": 64}

    embed = {"title": "Unknown subcommand", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
    return {"embeds": [_clean_embed(embed)], "flags": 64}


async def _handle_monitor_select(payload: dict) -> dict:
    if not _is_citizen(payload):
        return {"type": 4, "data": {"content": "This is reserved for verified Dash HQ citizens.", "flags": 64}}
    custom_id = (payload.get("data") or {}).get("custom_id", "")
    _, _, slug = custom_id.partition(":")
    selected = (payload.get("data") or {}).get("values") or []
    member_user = (payload.get("member") or {}).get("user") or {}
    discord_user_id = member_user.get("id", "")
    if not slug or not discord_user_id:
        return {"type": 4, "data": {"content": "Something went wrong - try `/monitor set` again.", "flags": 64}}

    async with httpx.AsyncClient(timeout=10) as client:
        # Replace-based: this menu always sets the exact set of events for
        # this collection, not additive - clear then (re)insert.
        await client.delete(
            f"{settings.supabase_url}/rest/v1/nft_watch_subscriptions",
            headers=_supabase_headers(prefer="return=minimal"),
            params={"discord_user_id": f"eq.{discord_user_id}", "slug": f"eq.{slug}"},
        )
        if selected:
            await client.post(
                f"{settings.supabase_url}/rest/v1/nft_watch_subscriptions",
                headers=_supabase_headers(prefer="resolution=merge-duplicates,return=minimal"),
                json=[{"discord_user_id": discord_user_id, "slug": slug, "event_type": e} for e in selected],
            )

    if selected:
        labels = [_NFT_MONITOR_LABELS.get(e, e) for e in selected]
        desc = "You'll get a DM when any of these happen:\n" + "\n".join(f"• {l}" for l in labels)
    else:
        desc = "Cleared - you won't get any /monitor alerts for this collection."
    embed = {"title": "🔔 Monitor settings saved", "description": desc, "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}

    # The picker itself stays private (it's an interactive control - a
    # public select menu could let anyone else fiddle with someone else's
    # subscriptions), but what got saved is announced publicly, same
    # "DMs stay private, the fact of it is public" pattern as /monitor
    # price - other members can see who's watching what.
    if settings.discord_nft_monitor_channel_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                c_name = slug
                try:
                    c = await _nft_collection_core(slug)
                    c_name = c.get("name") or slug
                except HTTPException:
                    pass
                if selected:
                    labels_public = ", ".join(_NFT_MONITOR_LABELS.get(e, e) for e in selected)
                    public_desc = f"<@{discord_user_id}> is now watching **{c_name}** for: {labels_public}"
                else:
                    public_desc = f"<@{discord_user_id}> cleared their /monitor alerts for **{c_name}**"
                public_embed = {"title": "🔔 Monitor updated", "description": public_desc, "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}
                await _post_channel_message(client, settings.discord_nft_monitor_channel_id, public_embed)
        except httpx.HTTPError:
            logger.exception("Failed to post public /monitor set announcement")

    return {"type": 7, "data": {"embeds": [_clean_embed(embed)], "components": [_monitor_select_component(slug)]}}


async def _nft_poll_watchlist_alerts(client: httpx.AsyncClient) -> list[str]:
    slugs = await _nft_poll_tracked_slugs(client)
    alerted = []
    for slug in slugs:
        try:
            c = await _nft_collection_core(slug)
        except HTTPException:
            continue
        try:
            history = await _nft_recent_snapshots(client, slug, limit=50)
            await _nft_store_snapshot(client, c)

            if c.get("floor") is not None:
                await _check_price_alerts(client, slug, c)

            if history:
                prev = history[0]
                if (
                    c.get("totalSupply") is not None
                    and prev.get("total_supply") is not None
                    and c["totalSupply"] < prev["total_supply"]
                ):
                    state = await _nft_alert_state_get(client, slug, "supply_cut")
                    if not state or state.get("last_value") != c["totalSupply"]:
                        embed = _supply_cut_embed(c, prev["total_supply"])
                        delivered = await _post_nft_alert(client, settings.discord_nft_channel_id, embed)
                        if delivered:
                            await _nft_alert_state_set(client, slug, "supply_cut", c["totalSupply"])
                            await _dm_watchlist_owners(client, slug, embed)
                            await _dm_subscribers(client, slug, "supply_cut", embed)
                            alerted.append(f"{slug}:supply_cut")

                if (
                    c.get("totalSupply") is not None
                    and prev.get("total_supply") is not None
                    and c["totalSupply"] > prev["total_supply"]
                ):
                    state = await _nft_alert_state_get(client, slug, "mint_progress")
                    if not state or state.get("last_value") != c["totalSupply"]:
                        embed = _mint_progress_embed(c, prev["total_supply"])
                        delivered = await _post_nft_alert(client, settings.discord_nft_channel_id, embed)
                        if delivered:
                            await _nft_alert_state_set(client, slug, "mint_progress", c["totalSupply"])
                            await _dm_watchlist_owners(client, slug, embed)
                            await _dm_subscribers(client, slug, "mint_progress", embed)
                            alerted.append(f"{slug}:mint_progress")

                if (
                    c.get("floor") is not None
                    and prev.get("floor") is not None
                    and prev["floor"] > 0
                ):
                    pct = (c["floor"] - prev["floor"]) / prev["floor"] * 100
                    if abs(pct) >= _NFT_FLOOR_CHANGE_THRESHOLD_PCT:
                        state = await _nft_alert_state_get(client, slug, "floor_change")
                        if _nft_alert_cooled_down(state):
                            embed = _floor_change_embed(c, prev["floor"], pct)
                            delivered = await _post_nft_alert(client, settings.discord_nft_channel_id, embed)
                            if delivered:
                                await _nft_alert_state_set(client, slug, "floor_change", c["floor"])
                                direction_event = "floor_up" if pct > 0 else "floor_down"
                                await _dm_watchlist_owners(client, slug, embed)
                                await _dm_subscribers(client, slug, direction_event, embed)
                                alerted.append(f"{slug}:{direction_event}")

                baseline = [h["volume_1d"] for h in history if h.get("volume_1d") is not None]
                if baseline and c.get("vol1d") is not None:
                    avg = sum(baseline) / len(baseline)
                    if avg > 0 and c["vol1d"] >= avg * 2.5:
                        state = await _nft_alert_state_get(client, slug, "volume_spike")
                        if _nft_alert_cooled_down(state):
                            # A spike is only worth a call-out if the trades
                            # behind it are real - same wash-trade module
                            # used for sweeps and NFT Scope, applied here
                            # too so an inflated number doesn't get
                            # presented as organic demand. Fails open on a
                            # fetch error rather than silently swallowing a
                            # real spike over an OpenSea hiccup.
                            wash_tainted = False
                            try:
                                recent_events = await _fetch_recent_sale_events(client, slug, window_seconds=86400)
                                wash_tainted = bool(recent_events) and _analyze_wash_trading(recent_events)["suspicious"]
                            except httpx.HTTPError:
                                pass
                            if not wash_tainted:
                                embed = _volume_spike_embed(c, avg)
                                delivered = await _post_nft_alert(client, settings.discord_nft_channel_id, embed)
                                if delivered:
                                    await _nft_alert_state_set(client, slug, "volume_spike", c["vol1d"])
                                    await _dm_watchlist_owners(client, slug, embed)
                                    await _dm_subscribers(client, slug, "volume_spike", embed)
                                    alerted.append(f"{slug}:volume_spike")

            sweep = await _detect_sweep(client, slug)
            if sweep:
                state = await _nft_alert_state_get(client, slug, "sweep")
                if _nft_alert_cooled_down(state):
                    embed = _sweep_embed(c, sweep)
                    delivered = await _post_nft_alert(client, settings.discord_nft_channel_id, embed)
                    if delivered:
                        await _nft_alert_state_set(client, slug, "sweep", sweep["count"])
                        await _dm_watchlist_owners(client, slug, embed)
                        await _dm_subscribers(client, slug, "sweep", embed)
                        alerted.append(f"{slug}:sweep")
        except (httpx.HTTPError, KeyError, ZeroDivisionError):
            logger.exception("nft-poll: alert check failed for %s", slug)
            continue
    return alerted


async def _nft_mint_radar_seen_has(client: httpx.AsyncClient, slug: str) -> bool:
    # Table name predates the "NFT Scope" rename - kept as-is to avoid an
    # unnecessary migration for a purely cosmetic reason.
    res = await client.get(
        f"{settings.supabase_url}/rest/v1/nft_mint_radar_seen",
        headers=_supabase_headers(),
        params={"slug": f"eq.{slug}", "select": "slug", "limit": "1"},
    )
    res.raise_for_status()
    return bool(res.json())


async def _nft_mint_radar_mark_seen(client: httpx.AsyncClient, slug: str) -> None:
    await client.post(
        f"{settings.supabase_url}/rest/v1/nft_mint_radar_seen",
        headers=_supabase_headers(prefer="resolution=merge-duplicates,return=minimal"),
        json=[{"slug": slug}],
    )


# ── NFT Scope — weighted signal model, not a pass/fail checklist ───────────
#
# Honest scope note (read before tuning): this is a heuristic model built
# from documented, widely-understood NFT market dynamics - real trading
# requires an independent third party to act, wash-traded/planted signals
# are cheap to fake individually but hard to fake ALL of at once, and a
# trend sustained across many snapshots is far more meaningful than a
# single noisy data point. It is NOT a model trained or backtested against
# a historical dataset of "which projects actually mooned" - no such
# labeled dataset exists in this system, and building one is out of scope
# for a free-tier bot. This is the strongest signal set achievable from
# free OpenSea data, not a crystal ball. Every post carries NFA.

# Three posting tiers, not a single pass/fail bar - every post is grouped
# by intensity/risk/certainty of the underlying signals, always framed as
# risk (never as a promise):
#   🟢 green  85-100  strongest signal set achievable from free data
#   🟡 yellow  70-84  solid signals, real but less complete
#   🔴 red     50-69  genuinely speculative - the "could still cook" tier,
#                     still verified clean (wash-trade + fake-offer gates
#                     apply to every tier), just thinner evidence
# Below 50, or anything blocked by a manipulation red flag, never posts -
# that's not a fourth tier, that's "not enough to say anything useful."
_NFT_SCOPE_RED_THRESHOLD = 50
_NFT_SCOPE_YELLOW_THRESHOLD = 70
_NFT_SCOPE_GREEN_THRESHOLD = 85
# Reserved per-pass, not pooled - ranking discovery by dollar volume
# structurally favors big blue-chips (one Moonbirds sale outweighs
# dozens of trades on a smaller collection in $ terms), which meant
# trending alone could fill the whole cap before fresh mints or
# currently-active smaller projects ever got a look. One slot each
# guarantees every discovery source gets a fair shot regardless of size.
#
# These are the BASE (safe, always-on) sizes. Auto-scaling on top of
# them - see _opensea_healthy and _nft_scope_pass_limits below - is what
# makes this self-sufficient long-term instead of a number that needs
# manual retuning every time real demand or OpenSea's actual rate limit
# changes: more genuinely-qualifying projects in one cycle raises the
# cap up to the surge ceiling automatically, and a real OpenSea 429
# drops everything back to base automatically, without a redeploy.
_NFT_SCOPE_FRESH_MAX_POSTS = 1
_NFT_SCOPE_TRENDING_MAX_POSTS = 1
_NFT_SCOPE_MOMENTUM_MAX_POSTS = 1
_NFT_SCOPE_SURGE_MAX_POSTS = 3  # per-pass ceiling when OpenSea is healthy AND demand is genuinely there
_NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS = 6   # ~30 min of history minimum before trusting a trend
_NFT_SCOPE_MOMENTUM_SCAN_LIMIT = 25  # base; doubles in surge mode, see _nft_scope_pass_limits
_NFT_SCOPE_TRENDING_LIMIT = 50  # per chain per source - wider discovery pool, more distinct candidates to round-robin over. This
# is a free widening (one listing call per source per chain either way, just a bigger `limit`
# param) - it does NOT cost extra evaluations. Combined with the post-cooldown skip above (which
# exits before spending any real evaluation budget on a candidate that already posted recently),
# a bigger pool means the same fixed eval budget below naturally reaches further down the ranked
# list each cycle instead of re-checking the same handful of already-posted names at the top.
# A pushed-down cooldown (candidates get re-judged far more often than
# the standard 1-hour alert cooldown, so nothing sits ignored just
# because it failed once) is only safe if worst-case cost per cycle is
# capped independent of it - otherwise a cycle where nothing clears the
# bar could burn through a large chunk of OpenSea's free-tier budget
# (600 req/hour, SHARED with every member's /nft, /scan, /rug command,
# not just this background poll) in one shot. _NFT_SCOPE_TRENDING_SCAN_BUDGET
# below is that cap - it bounds total candidates actually evaluated per
# cycle to a fixed, small, predictable number no matter how big the
# discovery pool is or how often it's rechecked. Neither of these
# touches any scoring or blocking rule - just how often and how much
# gets looked at.
_NFT_SCOPE_TRENDING_SCAN_COOLDOWN_SECONDS = 300  # 5 min - matches the poll cadence itself, as fast as a candidate can possibly be re-judged
_NFT_SCOPE_TRENDING_SCAN_BUDGET = 20  # base cap on candidates evaluated per cycle across ALL chains combined; doubles in surge mode
_NFT_SCOPE_RATE_LIMIT_BACKOFF_SECONDS = 1800  # 30 min conservative mode after a real OpenSea 429
# Deliberately separate from the scan cooldown above: re-SCANNING a
# candidate every 5 min keeps discovery fresh, but re-POSTING the same
# winner every 5 min just because it still clears the bar starves every
# other qualifying project out of the one reserved slot. This is the
# knob that actually forces rotation across "a ton of projects" instead
# of the same one or two repeatedly claiming the spot cycle after cycle.
_NFT_SCOPE_TRENDING_POST_COOLDOWN_SECONDS = 3600  # 1 hour - matches the momentum pass's own repost cadence


async def _opensea_healthy(client: httpx.AsyncClient) -> bool:
    # Ground truth, not a guess: checks whether OpenSea has actually
    # 429'd us recently (marker written directly inside _opensea_get).
    # Fails OPEN on a Supabase hiccup - a lookup error should never
    # itself force permanent throttling, since that would be strictly
    # worse than just using the safe base limits.
    try:
        state = await _nft_alert_state_get(client, "__opensea__", "rate_limited")
    except httpx.HTTPError:
        return True
    return _nft_alert_cooled_down(state, cooldown_seconds=_NFT_SCOPE_RATE_LIMIT_BACKOFF_SECONDS)


def _nft_scope_pass_limits(healthy: bool) -> dict:
    # The self-sufficient part: no fixed slot count to manually retune.
    # Healthy -> allowed to scale up to the surge ceiling if demand is
    # actually there (a pass still only uses what it finds real
    # qualifying candidates for - this raises the CEILING, it doesn't
    # force extra posts). Recently rate-limited -> drop straight back to
    # the safe base sizes, automatically, no redeploy needed either way.
    if healthy:
        return {
            "fresh_max": _NFT_SCOPE_SURGE_MAX_POSTS,
            "trending_max": _NFT_SCOPE_SURGE_MAX_POSTS,
            "momentum_max": _NFT_SCOPE_SURGE_MAX_POSTS,
            "trending_scan_budget": _NFT_SCOPE_TRENDING_SCAN_BUDGET * 2,
            "momentum_scan_limit": _NFT_SCOPE_MOMENTUM_SCAN_LIMIT * 2,
        }
    return {
        "fresh_max": _NFT_SCOPE_FRESH_MAX_POSTS,
        "trending_max": _NFT_SCOPE_TRENDING_MAX_POSTS,
        "momentum_max": _NFT_SCOPE_MOMENTUM_MAX_POSTS,
        "trending_scan_budget": _NFT_SCOPE_TRENDING_SCAN_BUDGET,
        "momentum_scan_limit": _NFT_SCOPE_MOMENTUM_SCAN_LIMIT,
    }


def _detect_fake_offer(c: dict, top_offer_amount: float | None) -> str | None:
    # A genuinely eager buyer wanting a collection badly enough to bid
    # multiples over floor would usually just buy AT floor instead of
    # leaving a phantom high bid nobody's filling - a huge offer/floor
    # gap with zero recorded sales AND zero volume is the classic "plant
    # a fake top offer to look hot" pattern. Deliberately conservative
    # (a single fake sale could still slip past this) - it catches the
    # cheap, common version of the trick, not every possible one.
    floor = c.get("floor")
    if top_offer_amount is None or not floor or floor <= 0:
        return None
    ratio = top_offer_amount / floor
    sales = c.get("sales24h") or 0
    vol1d = c.get("vol1d") or 0
    if ratio >= 4.0 and sales == 0 and vol1d <= 0:
        return f"Top offer is {ratio:.1f}x floor with zero recorded sales/volume backing it - reads like a planted offer, not real demand"
    return None


def _detect_abnormal_turnover(c: dict) -> str | None:
    # A large wallet-graph wash-trading operation (dozens+ of sybil
    # wallets, many different token IDs) can dodge the pairwise/cluster/
    # token-diversity checks in _analyze_wash_trading entirely, since
    # those are built to catch a SMALL colluding ring, not a big one.
    # But it can't hide the arithmetic: real collections essentially
    # never see a large fraction of their entire supply change hands in
    # a single day, and they don't see current holders each flip their
    # token multiple times in a day either. A collection where 24h sales
    # approach or exceed its own supply, or run far ahead of its current
    # owner count, is trading against itself no matter how many distinct
    # wallets are involved.
    sales = c.get("sales24h") or 0
    if sales <= 0:
        return None
    supply = c.get("totalSupply")
    if supply and supply > 0:
        turnover = sales / supply
        if turnover >= 0.4:
            return (
                f"{sales} sales against only {supply} total supply ({turnover:.0%} of the entire "
                "collection changed hands in 24h) - real collections don't turn over like this; "
                "this is the signature of scripted wash trading, not genuine demand"
            )
    owners = c.get("owners")
    if owners and owners > 0:
        flips_per_owner = sales / owners
        if flips_per_owner >= 5:
            return (
                f"{sales} sales across only {owners} current owners (~{flips_per_owner:.1f} flips per "
                "holder in 24h) - way beyond organic trading, looks like scripted churn among a small set of wallets"
            )
    return None


_NFT_SCOPE_BLUE_CHIP_OWNERS_THRESHOLD = 2500  # already-established collections everyone knows, not secondary-play alpha


def _detect_blue_chip(c: dict) -> str | None:
    # Owners count is chain-agnostic (unlike floor/volume, which are
    # priced in whatever native token that chain uses) and is the
    # cleanest signal for "this is already a widely-known, widely-held
    # collection" - the point of NFT Scope is surfacing plays nobody's
    # watching yet, not re-announcing CryptoPunks/BAYC/Pudgy Penguins
    # every time someone tracks them. A collection this broadly held
    # doesn't need an alpha call; it needs no introduction.
    owners = c.get("owners")
    if owners and owners >= _NFT_SCOPE_BLUE_CHIP_OWNERS_THRESHOLD:
        return (
            f"{owners} owners - already an established, widely-held collection, not an emerging "
            "secondary play; NFT Scope is for what's still under the radar"
        )
    return None


def _trend_up(values: list[float], min_len: int) -> tuple[bool, float]:
    # Requires a MAJORITY of recent consecutive moves to be upward, not
    # just an endpoint-to-endpoint comparison - a single spike surrounded
    # by noise shouldn't read as "trending." Shared by every dimension
    # _momentum_points checks (floor, volume, owners).
    if len(values) < min_len:
        return False, 0.0
    deltas = [values[i] - values[i + 1] for i in range(len(values) - 1)]
    if not deltas:
        return False, 0.0
    up = sum(1 for d in deltas if d > 0)
    oldest = values[-1]
    if up >= len(deltas) * 0.7 and oldest > 0 and values[0] > oldest * 1.05:
        return True, (values[0] - oldest) / oldest * 100
    return False, 0.0


def _momentum_points(c: dict, history: list[dict]) -> tuple[int, list[str]]:
    # history is newest-first (captured_at desc). A real secondary play
    # is backed by MULTIPLE aligned trends, not price moving alone - a
    # single large buy can push floor up on its own with nothing else
    # behind it. Checking volume and owner growth alongside price is
    # what tells a genuine build-up (broadening participation, new
    # buyers actually entering) apart from one trade making noise.
    reasons: list[str] = []
    points = 0
    min_len = _NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS

    floors = [h["floor"] for h in history if h.get("floor") is not None]
    if c.get("floor") is not None:
        floors = [c["floor"]] + floors
    floor_up, floor_pct = _trend_up(floors, min_len)
    if floor_up:
        points += 10
        reasons.append(f"Floor trending up {floor_pct:.0f}% over the last {len(floors)} snapshots, not just one spike")

    volumes = [h["volume_1d"] for h in history if h.get("volume_1d") is not None]
    if c.get("vol1d") is not None:
        volumes = [c["vol1d"]] + volumes
    vol_up, vol_pct = _trend_up(volumes, min_len)
    if vol_up:
        points += 5
        reasons.append(f"24h volume trending up {vol_pct:.0f}% across recent snapshots - broadening participation, not a one-off spike")

    owner_counts = [h["owners"] for h in history if h.get("owners") is not None]
    if c.get("owners") is not None:
        owner_counts = [c["owners"]] + owner_counts
    owners_up, owners_pct = _trend_up(owner_counts, min_len)
    if owners_up:
        points += 5
        reasons.append(f"Unique owners growing (+{owners_pct:.0f}% over the window) - new buyers actually entering, not just existing holders reshuffling")

    return points, reasons


def _nft_scope_score(c: dict, top_offer_amount: float | None, history: list[dict] | None = None, rapid_activity: dict | None = None) -> dict:
    supply = c.get("totalSupply")
    owners = c.get("owners")
    sales = c.get("sales24h") or 0
    vol1d = c.get("vol1d") or 0
    floor = c.get("floor") or 0

    reasons: list[str] = []
    red_flags: list[str] = []
    points = 0

    # Distribution health (0-25)
    if owners is not None and owners >= 5:
        points += 10
    if supply and owners and supply > 0:
        ratio = owners / supply
        if ratio >= 0.5:
            points += 15
            reasons.append(f"Healthy distribution - {owners} owners across {supply} supply ({ratio:.0%})")
        elif ratio >= 0.2:
            points += 10
            reasons.append(f"Solid distribution - {owners} owners across {supply} supply ({ratio:.0%})")
        elif ratio >= 0.1:
            points += 5
            reasons.append(f"Moderate distribution - {owners} owners across {supply} supply ({ratio:.0%})")
        else:
            red_flags.append(f"Concentrated ownership - only {ratio:.0%} owner-to-supply ratio")

    # Trading authenticity (0-25)
    if sales > 0:
        points += 15
        reasons.append(f"{sales} independently recorded sale(s) in the last 24h" + (f" moving {vol1d:.4f} {c.get('symbol') or 'ETH'}" if vol1d else ""))
    if floor > 0:
        points += 5
    fake_offer_reason = _detect_fake_offer(c, top_offer_amount)
    if fake_offer_reason:
        red_flags.append(fake_offer_reason)
    elif top_offer_amount is not None and floor > 0 and top_offer_amount >= floor:
        points += 5
        reasons.append("Top offer sitting at or above floor - genuine buy-side pressure")

    # Project substance (0-20)
    socials = [name for name, present in (("Twitter/X", c.get("twitter")), ("Discord", c.get("discord")), ("website", c.get("website"))) if present]
    if socials:
        points += 5
        reasons.append(f"Public presence: {', '.join(socials)} linked - not an anonymous drop with nowhere to be held accountable")
    if len(c.get("description") or "") >= 40:
        points += 5
        reasons.append("Has a real project description, not a blank listing")
    if c.get("category"):
        points += 5
        reasons.append(f"Listed under \"{c['category']}\" - a real declared category, not left blank")
    if c.get("verified"):
        points += 5
        reasons.append("OpenSea-verified collection")

    # Supply sanity (0-10)
    if supply is not None and 1 < supply <= 100_000:
        points += 10
        reasons.append(f"Supply of {supply} is in a sane range - not an infinite-mint or dust-supply setup")

    # Momentum (0-20, only when there's enough tracked history to trust)
    if history:
        momentum_points, momentum_reasons = _momentum_points(c, history)
        points += momentum_points
        reasons.extend(momentum_reasons)

    # Rapid activity (0-10) - an early, fast-reacting supplement to the
    # snapshot-based momentum above, not a replacement or a shortcut
    # around it. It only ever ADDS points on top of every other check
    # still running in full (turnover, fake-offer, mandatory real
    # activity, final wash-check) - a burst of trading never lowers the
    # bar, it can only raise a score that still has to clear every gate
    # on its own merits.
    if rapid_activity:
        points += 10
        reasons.append(
            f"🚀 {rapid_activity['count']} verified sale(s) in the last {rapid_activity['window_minutes']} min "
            f"from {rapid_activity['unique_buyers']} buyer(s)/{rapid_activity['unique_sellers']} seller(s) - "
            "activity accelerating right now, not just a 24h number catching up"
        )
        surge = rapid_activity.get("price_surge_pct")
        if surge is not None and surge >= _NFT_SCOPE_RAPID_SURGE_THRESHOLD_PCT:
            points += 5
            reasons.append(
                f"💥 Price climbed {surge:.0f}% within that same burst - volume AND price moving together, "
                "not just a busy but flat market"
            )
        if rapid_activity.get("is_sharp"):
            points += 5
            reasons.append(
                f"🔥 {rapid_activity['sharp_count']} of those sales landed in just the last "
                f"{rapid_activity['sharp_window_minutes']} min - this is happening right now, not winding down"
            )

    turnover_reason = _detect_abnormal_turnover(c)
    if turnover_reason:
        red_flags.append(turnover_reason)

    blue_chip_reason = _detect_blue_chip(c)
    if blue_chip_reason:
        red_flags.append(blue_chip_reason)

    # A detected fake offer, an abnormal supply/owner turnover, or an
    # already-established blue chip is a hard stop, not a minor
    # deduction - never recommend regardless of how high the rest of the
    # score is (the first two can otherwise produce a HIGH score, since
    # "lots of sales" and "many owners" are exactly what those numbers
    # look like from the outside; the third would score highest of all,
    # since a blue chip legitimately aces every soft signal).
    blocked = fake_offer_reason is not None or turnover_reason is not None or blue_chip_reason is not None

    # A description, social links, a category, even a listed floor price
    # cost a scammer nothing to fake and don't require a single other
    # person to act - independently verified trading (a real completed
    # sale, from a real spread of owners) is the one thing that can't be
    # written into a mint page. Without it there's no market validation
    # at all yet, no matter how polished everything else looks - this is
    # a hard requirement for every tier, not just a scoring contributor,
    # so a freshly-launched mint with $0 volume can never be recommended
    # on soft signals alone.
    has_real_activity = sales > 0 and (owners or 0) >= 5
    if not has_real_activity:
        red_flags.append(
            f"No independently verified trading yet ({sales} sale(s) in 24h, "
            f"{owners if owners is not None else 0} owner(s)) - nothing here has been market-tested"
        )

    if points >= _NFT_SCOPE_GREEN_THRESHOLD:
        tier, risk_tier = "green", "🟢 Strongest Signals (Still High Risk)"
    elif points >= _NFT_SCOPE_YELLOW_THRESHOLD:
        tier, risk_tier = "yellow", "🟡 Moderate Signals (High Risk)"
    elif points >= _NFT_SCOPE_RED_THRESHOLD:
        tier, risk_tier = "red", "🔴 Speculative (Very High Risk)"
    else:
        tier, risk_tier = "none", "⚪ Not Enough Signal"

    return {
        "score": points, "reasons": reasons, "red_flags": red_flags, "risk_tier": risk_tier,
        "tier": tier, "blocked": blocked, "has_real_activity": has_real_activity,
    }


def _nft_scope_worth_posting(score: dict) -> bool:
    return score["tier"] in ("green", "yellow", "red") and not score["blocked"] and score["has_real_activity"]


def _nft_scope_age_text(created_date: str | None) -> str | None:
    # A collection's age is context nothing else here conveys - a 3-day-old
    # mint and a 2-year-old collection can post near-identical numbers and
    # mean completely different things.
    if not created_date:
        return None
    try:
        created = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    if days < 0:
        return None
    if days < 1:
        return "minted today"
    if days < 2:
        return "1 day old"
    if days < 30:
        return f"{int(days)} days old"
    if days < 365:
        months = max(1, round(days / 30))
        return f"{months} month{'s' if months != 1 else ''} old"
    return f"{days / 365:.1f} years old"


def _nft_scope_analyst_take(c: dict, score: dict, rapid_activity: dict | None, kind: str) -> str:
    # Every other line in this embed is a template filled with this
    # project's own numbers, but nothing ties them together into a single
    # read on WHY this specific project, right now. This picks the single
    # strongest thing actually true about THIS collection and leads with
    # it in plain language, instead of a generic line that would read the
    # same on every post.
    name = c.get("name") or "This collection"
    chain = c.get("chain")
    chain_clause = f" on {chain.capitalize()}" if chain else ""
    owners = c.get("owners")
    supply = c.get("totalSupply")
    sales = c.get("sales24h") or 0

    if rapid_activity and rapid_activity.get("is_sharp"):
        return (
            f"{name}{chain_clause} is moving right now - {rapid_activity['sharp_count']} sale(s) landed in just the "
            f"last {rapid_activity['sharp_window_minutes']} minutes, part of a {rapid_activity['count']}-sale burst "
            f"across {rapid_activity['unique_buyers']} distinct buyer(s) in the last {rapid_activity['window_minutes']} min."
        )
    if rapid_activity:
        return (
            f"{name}{chain_clause} just posted a burst of {rapid_activity['count']} verified sale(s) in the last "
            f"{rapid_activity['window_minutes']} minutes from {rapid_activity['unique_buyers']} distinct buyer(s) - "
            "early, accelerating demand, not a stale 24h number catching up."
        )
    if kind == "momentum" and any("trending up" in r for r in score["reasons"]):
        return f"{name}{chain_clause} has been quietly building over multiple recent snapshots - several aligned trends moving together, not a one-off spike."
    if kind == "fresh":
        extra = f" - {sales} sale(s) and {owners} owner(s) already on board" if sales and owners else ""
        return f"{name}{chain_clause} is a fresh mint that's already cleared every wash-trade and activity screen here{extra} - early, not just new."
    if owners and supply and owners / supply >= 0.2 and sales > 0:
        return (
            f"{name}{chain_clause} is trading with real breadth - {owners} distinct owners across {supply} supply "
            f"and {sales} independently recorded sale(s) in the last 24h."
        )
    return f"{name}{chain_clause} cleared every screening gate here on real, independently verified on-chain activity."


def _nft_scope_embed(c: dict, score: dict, top_offer_amount: float | None, kind: str, rapid_activity: dict | None = None) -> dict:
    symbol = c.get("symbol") or "ETH"
    title_prefix = {
        "fresh": "🧭 NFT Scope · New Mint",
        "trending": "🧭 NFT Scope · Trending Pick",
        "momentum": "🧭 NFT Scope · Momentum Building",
    }.get(kind, "🧭 NFT Scope")
    lines = [f"*{_nft_scope_analyst_take(c, score, rapid_activity, kind)}*"]
    lines.append("")
    lines.append(f"**{score['score']}/100** · {score['risk_tier']}")
    if score["reasons"]:
        lines.append("")
        lines.append("**Why it's on the radar**")
        lines += [f"• {r}" for r in score["reasons"]]
    if score["red_flags"]:
        lines.append("")
        lines.append("**Worth noting**")
        lines += [f"⚠️ {f}" for f in score["red_flags"]]
    description_text = (c.get("description") or "").strip()
    if description_text:
        lines.append("")
        lines.append("**In the project's own words**")
        lines.append(description_text[:280] + ("…" if len(description_text) > 280 else ""))
    lines.append("")
    lines.append("*Heuristic read from public OpenSea/on-chain data - not financial advice. DYOR before any trade. NFA.*")

    top_offer_text = "-"
    if top_offer_amount is not None:
        top_offer_text = f"{top_offer_amount:.4f} {c.get('offerSymbol') or symbol}"
    floor_text = "-"
    if (floor := c.get("floor")) is not None:
        floor_text = f"{floor:.4f} {symbol}"
        if c.get("floorUsd") is not None:
            floor_text += f" (~${c['floorUsd']:,.2f})"
    fields = [
        {"name": "Floor", "value": floor_text, "inline": True},
        {"name": "Top Offer", "value": top_offer_text, "inline": True},
        {"name": "Owners / Supply", "value": f"{c.get('owners') if c.get('owners') is not None else '-'} / {c.get('totalSupply') if c.get('totalSupply') is not None else '-'}", "inline": True},
        {"name": "Chain", "value": (c.get("chain") or "-").capitalize(), "inline": True},
        {"name": "Age", "value": _nft_scope_age_text(c.get("createdDate")) or "-", "inline": True},
        {"name": "Category", "value": c.get("category") or "-", "inline": True},
    ]
    return {
        "title": f"{title_prefix} — {c['name']}",
        "url": c.get("openseaUrl"),
        "description": "\n".join(lines),
        "color": {"green": EMBED_COLOR_GOOD, "yellow": EMBED_COLOR_WARN, "red": EMBED_COLOR_BAD}.get(score["tier"], EMBED_COLOR_WARN),
        "fields": fields,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": {"text": f"{TOOLKIT_FOOTER['text']} · NFT Scope"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


_NFT_SCOPE_RAPID_WINDOW_SECONDS = 1800  # 30 min - reacts to a sweep/burst as it happens, not after a 24h aggregate catches up
_NFT_SCOPE_RAPID_MIN_SALES = 3
_NFT_SCOPE_RAPID_SURGE_THRESHOLD_PCT = 15  # min/max sale price spread within the burst that counts as a real surge, not noise
_NFT_SCOPE_RAPID_SHARP_WINDOW_SECONDS = 300  # 5 min - "this is happening right now", not just "recently"
_NFT_SCOPE_RAPID_SHARP_MIN_SALES = 2  # lower bar than the 30-min window since the timeframe itself is the signal


async def _detect_rapid_activity(client: httpx.AsyncClient, slug: str) -> dict | None:
    # A 24h volume figure or a multi-snapshot trend both take time to
    # build up - by the time either shows anything, an early sweep is
    # already over. This looks at raw sale events in a short window
    # instead, so a burst of genuine buying gets caught within the same
    # poll cycle it starts in, verified through the same wash-trade
    # module as everything else (a burst that's just self-trading isn't
    # an early signal, it's noise).
    try:
        recent = await _fetch_recent_sale_events(client, slug, window_seconds=_NFT_SCOPE_RAPID_WINDOW_SECONDS)
    except httpx.HTTPError:
        return None
    if len(recent) < _NFT_SCOPE_RAPID_MIN_SALES:
        return None
    analysis = _analyze_wash_trading(recent)
    if analysis["suspicious"]:
        return None

    # Sale count alone doesn't distinguish "a few genuine buys" from "a
    # few genuine buys at a rapidly climbing price" - the latter is a
    # much stronger degen/secondary signal. Reuses the same verified
    # event data already fetched above, no extra API call.
    prices = []
    for e in recent:
        payment = e.get("payment")
        if not payment:
            continue
        try:
            decimals = payment.get("decimals", 18)
            prices.append(int(payment["quantity"]) / (10 ** decimals))
        except (TypeError, ValueError, KeyError):
            continue
    price_surge_pct = None
    if len(prices) >= 2 and min(prices) > 0:
        price_surge_pct = (max(prices) - min(prices)) / min(prices) * 100

    # Sharper sub-signal: how much of this burst is happening in just the
    # last few minutes, not spread anywhere across the full 30-min window
    # - "still building right now" vs. "happened a while ago and cooled
    # off." Re-verified independently rather than assumed clean just
    # because the larger set was - a subset can surface a pattern (e.g.
    # two wallets trading back and forth) that enough surrounding
    # diversity masked in the full window. No extra API call, same data.
    now = time.time()
    sharp_window_events = [e for e in recent if now - (e.get("event_timestamp") or 0) <= _NFT_SCOPE_RAPID_SHARP_WINDOW_SECONDS]
    is_sharp = False
    if len(sharp_window_events) >= _NFT_SCOPE_RAPID_SHARP_MIN_SALES:
        is_sharp = not _analyze_wash_trading(sharp_window_events)["suspicious"]

    return {
        "count": len(recent),
        "unique_buyers": analysis["unique_buyers"],
        "unique_sellers": analysis["unique_sellers"],
        "window_minutes": _NFT_SCOPE_RAPID_WINDOW_SECONDS // 60,
        "price_surge_pct": price_surge_pct,
        "is_sharp": is_sharp,
        "sharp_count": len(sharp_window_events),
        "sharp_window_minutes": _NFT_SCOPE_RAPID_SHARP_WINDOW_SECONDS // 60,
    }


async def _nft_scope_clears_wash_check(client: httpx.AsyncClient, slug: str) -> bool:
    # Only called once a candidate already cleared the score bar (cheap
    # filters first, this extra API call last) - OpenSea's own sales24h
    # count doesn't distinguish organic sales from a wash-trading ring,
    # so a collection can clear every other check on inflated numbers.
    # Fails open (treats as clean) on a fetch error rather than silently
    # blocking every post whenever OpenSea hiccups.
    try:
        recent = await _fetch_recent_sale_events(client, slug, window_seconds=86400, limit=50)
    except httpx.HTTPError:
        return True
    if not recent:
        return True
    return not _analyze_wash_trading(recent)["suspicious"]


async def _nft_scope_top_offer_amount(client: httpx.AsyncClient, slug: str, c: dict) -> float | None:
    try:
        top_offer_raw = await _opensea_get_top_offer(client, slug)
    except httpx.HTTPError:
        return None
    if top_offer_raw is None or c.get("offerDecimals") is None:
        return None
    try:
        return top_offer_raw / (10 ** c["offerDecimals"])
    except (TypeError, ZeroDivisionError, OverflowError):
        return None


async def _nft_scope_scan(client: httpx.AsyncClient, per_chain_limit: int = 30) -> list[str]:
    posted: list[str] = []

    # Self-adjusting posting slots / scan budgets: expands toward the
    # surge ceiling when OpenSea is actually healthy (no recent real 429)
    # so a cycle with genuine multi-candidate demand isn't artificially
    # capped at 1 post per pass, and automatically contracts back to the
    # conservative base sizes the moment a real rate-limit is hit - no
    # manual retuning or redeploy either way.
    healthy = await _opensea_healthy(client)
    limits = _nft_scope_pass_limits(healthy)

    # ── Pass 1: fresh mints across the chains this community mints on ──
    # Reserved slot, not pooled with the passes below - see
    # _NFT_SCOPE_FRESH_MAX_POSTS. The posts-per-cycle cap must only gate
    # the actual Discord POST, not the scan/mark-seen bookkeeping -
    # stopping the whole scan the moment the cap is hit would leave every
    # later candidate unscanned AND unmarked, so the exact same backlog
    # gets re-evaluated (and likely re-capped) every single cycle without
    # ever making progress through the full candidate list.
    fresh_posted: list[str] = []
    for chain in _NFT_SCOPE_CHAINS:
        try:
            data = await _opensea_get(client, "/collections", {"order_by": "created_date", "limit": per_chain_limit, "chain": chain})
        except httpx.HTTPError:
            continue
        collections = (data or {}).get("collections") or []
        for raw in collections:
            slug = raw.get("collection")
            if not slug:
                continue
            try:
                if await _nft_mint_radar_seen_has(client, slug):
                    continue
                c = await _nft_collection_core(slug)
                top_offer_amount = await _nft_scope_top_offer_amount(client, slug, c)
                # Only spend the extra API call checking for a rapid burst
                # if the collection already shows SOME real sales - bounds
                # the added cost to candidates worth the look instead of
                # every single one of the ~60 scanned per cycle.
                rapid_activity = None
                if (c.get("sales24h") or 0) > 0:
                    rapid_activity = await _detect_rapid_activity(client, slug)
                score = _nft_scope_score(c, top_offer_amount, rapid_activity=rapid_activity)
                if (
                    len(fresh_posted) < limits["fresh_max"]
                    and _nft_scope_worth_posting(score)
                    and await _nft_scope_clears_wash_check(client, slug)
                ):
                    delivered = await _post_channel_message(client, settings.discord_nft_scope_channel_id, _nft_scope_embed(c, score, top_offer_amount, "fresh", rapid_activity=rapid_activity))
                    if delivered:
                        fresh_posted.append(slug)
                # Mark seen regardless of score or whether the post cap
                # was already hit - a weak collection doesn't become
                # worth re-checking every 5 minutes forever, and a strong
                # one that lost out to the cap this cycle shouldn't be
                # re-scored and re-queued every cycle after either.
                await _nft_mint_radar_mark_seen(client, slug)
            except HTTPException:
                continue
            except (httpx.HTTPError, KeyError, ZeroDivisionError, TypeError):
                logger.exception("nft-scope: fresh-mint check failed for %s", slug)
                continue
    posted.extend(fresh_posted)

    # ── Pass 2: trending discovery - established collections that are
    # genuinely active right now, regardless of when they were minted or
    # whether anyone has tracked them yet. Fresh mints (pass 1) and
    # already-tracked collections (pass 3) are both narrow nets on their
    # own - this is what actually "scours OpenSea" for secondary plays
    # nobody's watching yet. Runs through the exact same strict gates as
    # everything else; wider reach, not looser rules. Uses a cooldown
    # (re-check at most once/hour per candidate, whether it posted or
    # not) instead of the fresh-mint pass's permanent seen-table, since a
    # trending collection's situation can genuinely change.
    #
    # Three discovery sources, not one - ranking by dollar volume ALONE
    # structurally favors big blue-chips (a single high-floor sale
    # outweighs dozens of trades on a smaller collection), which is
    # exactly the "only ever big projects" bias this was built to fix.
    # twenty_four_hour_sales ranks by transaction COUNT instead, which
    # favors collections lots of people are actually trading right now,
    # cheap or expensive. Deduped by slug before the expensive per-
    # candidate checks, so this costs a couple extra cheap listing calls
    # per chain, not proportionally more real work.
    trending_posted: list[str] = []
    trending_scanned = 0  # counts actual evaluations (past the cooldown gate), not just discovery
    # Fetch every (chain, source) listing FIRST, before evaluating anything -
    # this used to be a per-chain loop where chain 1 (always "ethereum",
    # first in the list) could burn the entire cross-chain scan budget on
    # its own candidates before the loop ever moved on to chain 2, so
    # every other chain never got evaluated at all, cycle after cycle.
    # Merging every (chain, source) pair into ONE shuffled round-robin
    # list - the same fix already applied to sources within a chain,
    # widened to cover chains too - is what actually gets the scan budget
    # spread across the whole discovery surface instead of camping on
    # whichever chain happens to be listed first.
    sources = ("seven_day_volume", "twenty_four_hour_volume", "twenty_four_hour_sales")
    pairs = [(chain, source) for chain in _NFT_SCOPE_CHAINS for source in sources]
    random.shuffle(pairs)
    per_pair: dict[tuple[str, str], list[dict]] = {}
    for chain, order_by in pairs:
        try:
            data = await _opensea_get(client, "/collections", {"order_by": order_by, "limit": _NFT_SCOPE_TRENDING_LIMIT, "chain": chain})
        except httpx.HTTPError:
            data = None
        per_pair[(chain, order_by)] = (data or {}).get("collections") or []
    seen_slugs: set[str] = set()
    collections: list[dict] = []
    for i in range(_NFT_SCOPE_TRENDING_LIMIT):
        for pair in pairs:
            lst = per_pair.get(pair) or []
            if i >= len(lst):
                continue
            raw = lst[i]
            slug = raw.get("collection")
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                collections.append(raw)
    for raw in collections:
        if len(trending_posted) >= limits["trending_max"] or trending_scanned >= limits["trending_scan_budget"]:
            break
        slug = raw.get("collection")
        if not slug:
            continue
        try:
            scan_state = await _nft_alert_state_get(client, slug, "nft_scope_trending_scan")
            if not _nft_alert_cooled_down(scan_state, cooldown_seconds=_NFT_SCOPE_TRENDING_SCAN_COOLDOWN_SECONDS):
                continue
            # Checked BEFORE spending any real OpenSea calls on this
            # candidate - if it already claimed the trending slot
            # recently, evaluating it again just burns scan budget
            # that could go toward finding something new instead. This
            # is what actually forces rotation across the wider pool
            # instead of the same one or two winners repeating every
            # cycle they still clear the bar.
            post_state = await _nft_alert_state_get(client, slug, "nft_scope_trending_post")
            if not _nft_alert_cooled_down(post_state, cooldown_seconds=_NFT_SCOPE_TRENDING_POST_COOLDOWN_SECONDS):
                continue
            trending_scanned += 1
            c = await _nft_collection_core(slug)
            top_offer_amount = await _nft_scope_top_offer_amount(client, slug, c)
            rapid_activity = None
            if (c.get("sales24h") or 0) > 0:
                rapid_activity = await _detect_rapid_activity(client, slug)
            # Trending collections often already have snapshot
            # history from watchlist polling even if nobody
            # explicitly /monitor'd them - use it for momentum
            # scoring if it happens to exist, harmless if not.
            history = None
            try:
                history = await _nft_recent_snapshots(client, slug, limit=30)
            except httpx.HTTPError:
                history = None
            score = _nft_scope_score(c, top_offer_amount, history=history, rapid_activity=rapid_activity)
            if _nft_scope_worth_posting(score) and await _nft_scope_clears_wash_check(client, slug):
                delivered = await _post_channel_message(client, settings.discord_nft_scope_channel_id, _nft_scope_embed(c, score, top_offer_amount, "trending", rapid_activity=rapid_activity))
                if delivered:
                    trending_posted.append(slug)
                    await _nft_alert_state_set(client, slug, "nft_scope_trending_post", c.get("floor") or 0)
            # Mark as scanned regardless of outcome - rate-limits
            # re-checking this specific candidate, it doesn't mean
            # "never again" the way the fresh-mint seen-table does.
            await _nft_alert_state_set(client, slug, "nft_scope_trending_scan", c.get("floor") or 0)
        except HTTPException:
            continue
        except (httpx.HTTPError, KeyError, ZeroDivisionError, TypeError):
            logger.exception("nft-scope: trending check failed for %s", slug)
            continue
    posted.extend(trending_posted)

    # ── Pass 3: momentum on collections already being tracked (/watchlist
    # or /monitor), which is the only source of the snapshot history this
    # needs - an "old project about to cook" call is only as good as the
    # trend data behind it. Reserved slot, same reasoning as the other
    # two passes. ──
    momentum_posted: list[str] = []
    try:
        tracked_slugs = (await _nft_poll_tracked_slugs(client))[:limits["momentum_scan_limit"]]
    except httpx.HTTPError:
        tracked_slugs = []
    for slug in tracked_slugs:
        if len(momentum_posted) >= limits["momentum_max"]:
            break
        try:
            state = await _nft_alert_state_get(client, slug, "nft_scope_momentum")
            if not _nft_alert_cooled_down(state):
                continue
            c = await _nft_collection_core(slug)
            history = await _nft_recent_snapshots(client, slug, limit=30)
            if len(history) < _NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS:
                continue
            top_offer_amount = await _nft_scope_top_offer_amount(client, slug, c)
            rapid_activity = await _detect_rapid_activity(client, slug) if (c.get("sales24h") or 0) > 0 else None
            score = _nft_scope_score(c, top_offer_amount, history=history, rapid_activity=rapid_activity)
            if _nft_scope_worth_posting(score) and await _nft_scope_clears_wash_check(client, slug):
                delivered = await _post_channel_message(client, settings.discord_nft_scope_channel_id, _nft_scope_embed(c, score, top_offer_amount, "momentum", rapid_activity=rapid_activity))
                if delivered:
                    await _nft_alert_state_set(client, slug, "nft_scope_momentum", c.get("floor") or 0)
                    momentum_posted.append(slug)
        except HTTPException:
            continue
        except (httpx.HTTPError, KeyError, ZeroDivisionError, TypeError):
            logger.exception("nft-scope: momentum check failed for %s", slug)
            continue
    posted.extend(momentum_posted)

    return posted


def _nft_channel_explainer_embed() -> dict:
    return {
        "title": "📌 How this channel works",
        "description": (
            "Everything here is automated, posted every ~5 minutes - watchlist alerts for "
            "anything on anyone's `/watchlist`:\n\n"
            "✂️ **Supply Cut** — total supply just went down (burn, reveal, etc).\n"
            "📈 **Floor Change** — floor price moved meaningfully, up or down.\n"
            "📊 **Volume Spike** — 24h volume is running well above its recent average.\n"
            "🧹 **Possible Sweep** — several sales in a short window, concentrated in very few wallets.\n"
            "Don't see a collection here? Add it with `/watchlist add`.\n\n"
            "Nothing here is financial advice — these are automated, on-chain signals only."
        ),
        "color": EMBED_COLOR,
        "footer": TOOLKIT_FOOTER,
    }


async def _channel_already_pinned(client: httpx.AsyncClient, channel_id: str) -> bool:
    res = await client.get(f"{DISCORD_API}/channels/{channel_id}/pins", headers={"Authorization": f"Bot {settings.discord_bot_token}"})
    return res.status_code < 300 and len(res.json()) > 0


async def _post_and_pin(client: httpx.AsyncClient, channel_id: str, embed: dict) -> str:
    if not channel_id or not settings.discord_bot_token:
        return "skipped: no channel/token configured"
    if await _channel_already_pinned(client, channel_id):
        return "skipped: already has a pinned message"
    res = await _discord_post_with_retry(
        client, f"{DISCORD_API}/channels/{channel_id}/messages",
        {"Authorization": f"Bot {settings.discord_bot_token}"}, {"embeds": [embed]},
    )
    if res.status_code >= 300:
        return f"failed to post: {res.status_code} {res.text[:200]}"
    message_id = res.json()["id"]
    pin_res = await client.put(
        f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/pin",
        headers={"Authorization": f"Bot {settings.discord_bot_token}"},
    )
    return "posted and pinned" if pin_res.status_code < 300 else f"posted but pin failed: {pin_res.status_code}"


@app.get("/cron/nft-init-channels")
async def nft_init_channels(request: Request):
    # One-time (idempotent - skips a channel that already has a pin) setup
    # action, not a real schedule - same cron-secret pattern as
    # /cron/register-discord-commands. Run once by hand after the NFT
    # channel is created, so it's self-documenting for new members instead
    # of them wondering what an unexplained embed means.
    expected = f"Bearer {settings.nft_cron_secret}"
    if not settings.nft_cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with httpx.AsyncClient(timeout=15) as client:
        result = await _post_and_pin(client, settings.discord_nft_channel_id, _nft_channel_explainer_embed())
    return {"nft_channel": result}


_SNAPSHOT_RETENTION_DAYS = 30


async def _prune_old_snapshots(client: httpx.AsyncClient) -> bool:
    # nft_snapshot_history gets a new row every ~5 minutes per tracked
    # collection, forever, with no other cleanup - on a long enough
    # timeline that's the one thing in this whole system that grows
    # unbounded regardless of how many people use the bot, and could
    # eventually threaten Supabase's free-tier storage cap. 30 days is
    # far more history than any current feature (chart note, ATH, spike
    # baseline) actually looks back through.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_SNAPSHOT_RETENTION_DAYS)).isoformat()
    try:
        res = await client.delete(
            f"{settings.supabase_url}/rest/v1/nft_snapshot_history",
            headers=_supabase_headers(prefer="return=minimal"),
            params={"captured_at": f"lt.{cutoff}"},
        )
        return res.status_code < 300
    except httpx.HTTPError:
        logger.exception("nft-poll: snapshot pruning failed")
        return False


@app.get("/cron/nft-poll")
async def nft_poll(request: Request):
    expected = f"Bearer {settings.nft_cron_secret}"
    if not settings.nft_cron_secret or request.headers.get("authorization") != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Each phase isolated - if the schema.sql migration hasn't been run yet
    # (a separate manual step from deploying this code) or OpenSea hiccups
    # mid-cycle, one phase failing shouldn't take the other down with it.
    errors = []
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            alerted = await _nft_poll_watchlist_alerts(client)
        except (httpx.HTTPError, KeyError) as e:
            logger.exception("nft-poll: watchlist alert phase failed")
            alerted, errors = [], errors + [f"watchlist_alerts: {e}"]
        scoped = []
        if settings.nft_scope_enabled:
            try:
                scoped = await _nft_scope_scan(client)
            except (httpx.HTTPError, KeyError) as e:
                logger.exception("nft-poll: NFT Scope phase failed")
                scoped, errors = [], errors + [f"nft_scope: {e}"]
        pruned = await _prune_old_snapshots(client)

    return {"watchlist_alerts": alerted, "nft_scope_posts": scoped, "pruned_old_snapshots": pruned, "errors": errors}


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
    {"min": 0, "emoji": "🦐", "name": "Shrimp", "color": "#8A9BBF", "flavor": "Just getting started on-chain, every whale began here."},
    {"min": 14, "emoji": "🦀", "name": "Crab", "color": "#5A6A8A", "flavor": "Building a position, one transaction at a time."},
    {"min": 28, "emoji": "🐙", "name": "Octopus", "color": "#22D3EE", "flavor": "Dabbling across a few chains and protocols."},
    {"min": 42, "emoji": "🐟", "name": "Fish", "color": "#5B9BF8", "flavor": "An established, well-diversified retail wallet."},
    {"min": 58, "emoji": "🐬", "name": "Dolphin", "color": "#4D72FF", "flavor": "A serious, well-rounded on-chain presence."},
    {"min": 72, "emoji": "🦈", "name": "Shark", "color": "#1B42FF", "flavor": "A high-roller with real depth across the board."},
    {"min": 85, "emoji": "🐋", "name": "Whale", "color": "#F59E0B", "flavor": "Moves markets. Deep holdings, deep history."},
    {"min": 94, "emoji": "🐳", "name": "Humpback", "color": "#F59E0B", "flavor": "Apex on-chain presence: the top of the curve."},
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


async def _wallet_xray_core(address: str) -> dict:
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

        # A handful of real wallets (exchange hot wallets, very old/active
        # EOAs) hold thousands of tokens - mostly spam airdrops, but the
        # response itself can be large enough to need more than the shared
        # client timeout to fully download, and to occasionally miss the
        # first attempt entirely. Losing this silently is exactly what made
        # Net Worth read as "wrong" for a heavily-active wallet - retry once
        # like the other Blockscout calls before giving up.
        tok_json = await _get_with_retry(f"https://eth.blockscout.com/api/v2/addresses/{addr}/token-balances", timeout=25)
        # None = both attempts failed (genuinely unknown), [] = a real,
        # successful response saying "no tokens" - these must stay
        # distinguishable, same reasoning as counters_ok below.
        token_balances_ok = tok_json is not None
        token_balances = tok_json if isinstance(tok_json, list) else []

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
        _CHAIN_LABELS = {"bsc": "BNB Chain", "polygon": "Polygon", "arbitrum": "Arbitrum", "optimism": "Optimism", "base": "Base", "avalanche": "Avalanche", "robinhood": "Robinhood Chain"}
        other_chains = [_CHAIN_LABELS.get(k, k) for k in other_chain_results if k]

    eth_balance = int(info.get("coin_balance") or 0) / 1e18
    eth_price = float(info.get("exchange_rate") or 0)
    eth_usd = eth_balance * eth_price

    token_usd_total = 0.0
    fungible_tokens = 0
    unpriced_tokens = 0
    for tb in token_balances:
        tok = tb.get("token") or {}
        if tok.get("type") != "ERC-20":
            continue
        try:
            decimals = int(tok.get("decimals") or 0)
            raw_value = int(tb.get("value") or 0)
            rate = float(tok.get("exchange_rate") or 0)
            if rate <= 0:
                # Blockscout has no market price for this token - it's
                # real balance that just can't be priced, not zero value.
                # Net Worth silently excluding these (with no indication
                # anything was left out) is exactly what reads as "wrong"
                # for a wallet holding several unpriced tokens.
                if raw_value > 0:
                    unpriced_tokens += 1
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
            "unpricedTokens": unpriced_tokens,
            "tokenDataOk": token_balances_ok,
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


@app.get("/toolkit/wallet-xray")
@limiter.limit("20/minute")
async def wallet_xray(request: Request, address: str = Query(..., min_length=3, max_length=100)):
    return await _wallet_xray_core(address)


# ── Discord toolkit bot — slash commands ──────────────────────────────────
# Every handler below calls the exact same *_core() functions the website's
# toolkit endpoints use — no separate logic, no separate data source, so a
# fix or a new chain added to one surface is automatically correct on the
# other. Results post publicly in-channel per citizen preference, and every
# command is gated behind the Citizen role: this is a members-only perk, not
# a general-purpose public bot.
EMBED_COLOR = 0x1B42FF
EMBED_COLOR_GOOD = 0x10B981
EMBED_COLOR_BAD = 0xEF4444
EMBED_COLOR_WARN = 0xF59E0B
TOOLKIT_FOOTER = {"text": "Dash HQ Toolkit · dashhq.site"}

_GAS_CHAIN_DISPLAY = {
    "ethereum": "Ethereum", "bsc": "BNB Chain", "polygon": "Polygon", "arbitrum": "Arbitrum",
    "optimism": "Optimism", "base": "Base", "avalanche": "Avalanche", "robinhood": "Robinhood Chain",
    "solana": "Solana",
}
_RUG_CHAIN_DISPLAY = {
    "1": "Ethereum", "56": "BNB Chain", "137": "Polygon", "42161": "Arbitrum",
    "10": "Optimism", "8453": "Base", "43114": "Avalanche", "solana": "Solana",
}


def _citizen_role_ids(payload: dict) -> list[str]:
    return (payload.get("member") or {}).get("roles") or []


def _is_citizen(payload: dict) -> bool:
    # No role configured at all means the gate can't be enforced — fail
    # open rather than silently lock every citizen out of a working bot.
    if not settings.citizen_role_id:
        return True
    return settings.citizen_role_id in _citizen_role_ids(payload)


def _cmd_options(payload: dict) -> dict:
    opts = (payload.get("data") or {}).get("options") or []
    return {o["name"]: o.get("value") for o in opts if o.get("type") not in (1, 2)}


def _fmt_usd(n) -> str:
    if n is None:
        return "-"
    if n >= 1e9:
        return f"${n / 1e9:.2f}B"
    if n >= 1e6:
        return f"${n / 1e6:.2f}M"
    if n >= 1e3:
        return f"${n / 1e3:.1f}K"
    return f"${n:,.2f}"


def _fmt_price(n) -> str:
    if n is None:
        return "-"
    if n >= 1:
        return f"${n:,.2f}"
    if n >= 0.01:
        return f"${n:.4f}"
    return f"${n:.8f}".rstrip("0")


async def _cmd_xray(address: str) -> dict:
    data = await _wallet_xray_core(address)
    tier = data["tier"]
    crypto = data["crypto"]
    fields = [
        {"name": "Score", "value": f"{data['composite']} / 100", "inline": True},
        {"name": "Archetype", "value": data["archetype"], "inline": True},
        {"name": "Net Worth (est., USD)", "value": f"${crypto['netWorthUsd']:,}", "inline": True},
        {"name": "ETH Balance", "value": f"{crypto['ethBalance']} ETH", "inline": True},
        {"name": "Distinct Tokens", "value": str(crypto["distinctTokens"]), "inline": True},
        {"name": "NFT Collections", "value": str(data["nft"]["collections"]), "inline": True},
    ]
    if crypto.get("otherChains"):
        fields.append({"name": "Also active on", "value": ", ".join(crypto["otherChains"]), "inline": False})
    # Same reasoning as the website: Net Worth silently drops tokens with
    # no known market price rather than counting them as zero - say so,
    # so a low number doesn't just read as "wrong."
    if crypto.get("unpricedTokens"):
        n = crypto["unpricedTokens"]
        noun = "token holds" if n == 1 else "tokens hold"
        pron = "it isn't" if n == 1 else "they aren't"
        fields.append({"name": "Note", "value": f"{n} {noun} a real balance but have no market price available, so {pron} included in Net Worth.", "inline": False})
    if crypto.get("tokenDataOk") is False:
        fields.append({"name": "Note", "value": "Token holdings could not be fully loaded this scan. Net Worth and Distinct Tokens may be incomplete. Try again.", "inline": False})
    return {
        "title": f"{tier['emoji']} {tier['name']} · {data.get('ensName') or address}",
        "description": tier["flavor"],
        "color": int(tier["color"].lstrip("#"), 16),
        "fields": fields,
        "footer": TOOLKIT_FOOTER,
    }


async def _cmd_gas(chain: str) -> dict:
    chain = chain or "ethereum"
    data = await _gas_core(chain)
    label = _GAS_CHAIN_DISPLAY.get(chain, chain.title())
    if chain == "solana":
        fees = data.get("solana_fees")
        big = f"{fees['avg']:,} µ◎/CU" if fees else "-"
    else:
        gwei = data.get("gwei")
        big = f"{gwei:.4f} gwei" if gwei is not None else "-"
    fields = [{"name": "Current", "value": big, "inline": True}]
    if data.get("native_usd") is not None:
        fields.append({"name": "Native token price", "value": _fmt_usd(data["native_usd"]), "inline": True})
    return {"title": f"⛽ Gas: {label}", "color": EMBED_COLOR, "fields": fields, "footer": TOOLKIT_FOOTER}


async def _cmd_scan(address: str) -> dict:
    data = await _ca_scan_core(address)
    fields = [
        {"name": "Price", "value": _fmt_price(data["priceUsd"]), "inline": True},
        {"name": "24h Change", "value": f"{data['change24h']:+.2f}%" if data.get("change24h") is not None else "-", "inline": True},
        {"name": "Chain / DEX", "value": f"{data['chain']} · {data['dex']}", "inline": True},
        {"name": "Market Cap", "value": _fmt_usd(data["marketCap"]), "inline": True},
        {"name": "Liquidity", "value": _fmt_usd(data["liquidityUsd"]), "inline": True},
        {"name": "24h Volume", "value": _fmt_usd(data["volume24h"]), "inline": True},
    ]
    return {
        "title": f"🔍 {data['name']} ({data['symbol']})",
        "url": data.get("url"),
        "color": EMBED_COLOR,
        "fields": fields,
        "thumbnail": {"url": data["imageUrl"]} if data.get("imageUrl") else None,
        "footer": TOOLKIT_FOOTER,
    }


async def _cmd_rug(address: str, chain_id: str) -> dict:
    chain_id = chain_id or "1"
    data = await _rug_check_core(address, chain_id)
    color = {"low": EMBED_COLOR_GOOD, "medium": EMBED_COLOR_WARN}.get(data["level"], EMBED_COLOR_BAD)
    checklist = "\n".join(f"{'✅' if c['pass'] else '❌'} {c['label']}" for c in data["checks"])
    label = _RUG_CHAIN_DISPLAY.get(chain_id, chain_id)
    return {
        "title": f"🛡️ {data['label']}: {label}",
        "description": checklist,
        "color": color,
        "footer": TOOLKIT_FOOTER,
    }


async def _cmd_nft(query: str) -> dict:
    results = await _nft_search_core(query)
    if not results:
        return {"title": "No collections found", "description": f'No OpenSea results for "{query}".', "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
    slug = results[0]["slug"]
    # The search result is lean (no CA/pricing/total_supply) - fetch the full
    # single-collection shape for the one result we're actually displaying,
    # rather than the wasteful old behaviour of statting all 12 matches.
    try:
        c = await _nft_collection_core(slug)
    except HTTPException:
        c = results[0]

    async with httpx.AsyncClient(timeout=10) as client:
        top_offer_raw = await _opensea_get_top_offer(client, slug)
        try:
            history = await _nft_recent_snapshots(client, slug, limit=200)
        except httpx.HTTPError:
            # nft_snapshot_history may not exist yet if the schema.sql
            # migration hasn't been run - degrade to "no chart data" rather
            # than breaking the whole command.
            history = []

    check = "✅ " if c.get("verified") else ""
    floor = f"{c['floor']:.3f} {c['symbol']}" if c.get("floor") is not None else "-"
    if c.get("floorUsd") is not None:
        floor += f" ({_fmt_price(c['floorUsd'])})"

    if top_offer_raw is not None and c.get("offerDecimals") is not None:
        offer_amount = top_offer_raw / (10 ** c["offerDecimals"])
        top_offer = f"{offer_amount:.4f} {c.get('offerSymbol') or ''}".strip()
        if c.get("offerUsdRate"):
            top_offer += f" ({_fmt_price(offer_amount * c['offerUsdRate'])})"
    else:
        top_offer = "N/A"

    ath_floors = [h["floor"] for h in history if h.get("floor") is not None]
    if c.get("floor") is not None:
        ath_floors.append(c["floor"])
    ath = f"{max(ath_floors):.4f} {c['symbol']}" if ath_floors else "N/A"
    chart = "📉 Not enough history yet — check back after a few more polling cycles." if len(history) < 3 else \
        f"{len(history)} snapshots recorded so far."

    fields = [
        {"name": "Floor Price", "value": floor, "inline": True},
        {"name": "Top Offer", "value": top_offer, "inline": True},
        {"name": "ATH Floor", "value": ath, "inline": True},
        {"name": "Total Volume", "value": f"{c['volTotal']:,.2f} {c['symbol']}" if c.get("volTotal") is not None else "-", "inline": True},
        {"name": "Owners", "value": f"{c['owners']:,}" if c.get("owners") is not None else "-", "inline": True},
        {"name": "24h Volume", "value": f"{c['vol1d']:.2f} {c['symbol']}" if c.get("vol1d") is not None else "-", "inline": True},
    ]
    if c.get("category"):
        fields.append({"name": "Category", "value": c["category"], "inline": True})
    if c.get("contractAddress"):
        fields.append({"name": "Contract Address", "value": f"`{c['contractAddress']}`", "inline": False})
    fields.append({"name": "Chart", "value": chart, "inline": False})

    footer_text = TOOLKIT_FOOTER["text"]
    if c.get("listingUsdRate") and c.get("symbol") in ("ETH", "WETH"):
        footer_text += f" · ETH/USD: {_fmt_price(c['listingUsdRate'])}"

    return {
        "title": f"{check}{c['name']}",
        "url": c.get("openseaUrl"),
        "description": c.get("description"),
        "color": EMBED_COLOR,
        "fields": fields,
        "image": {"url": c["image"]} if c.get("image") else None,
        "footer": {"text": footer_text},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _pnl_render_core(collection_query: str, mint_price_raw, amount_minted: int, x_username: str) -> bytes:
    results = await _nft_search_core(collection_query)
    if not results:
        raise HTTPException(status_code=404, detail=f'No OpenSea results for "{collection_query}".')
    slug = results[0]["slug"]
    try:
        c = await _nft_collection_core(slug)
    except HTTPException:
        c = results[0]

    async with httpx.AsyncClient(timeout=15) as client:
        mint_price = await _parse_eth_amount(client, mint_price_raw, "mint price")
        try:
            history = await _nft_recent_snapshots(client, slug, limit=200)
        except httpx.HTTPError:
            # Same as /nft - degrade to "no history" if the snapshot table
            # migration hasn't been run yet, rather than failing the card.
            history = []
        thumb_bytes = None
        if c.get("image"):
            try:
                thumb_res = await client.get(c["image"])
                if thumb_res.status_code == 200:
                    thumb_bytes = thumb_res.content
            except httpx.HTTPError:
                pass

    ath_floors = [h["floor"] for h in history if h.get("floor") is not None]
    if c.get("floor") is not None:
        ath_floors.append(c["floor"])
    ath = max(ath_floors) if ath_floors else c.get("floor")

    data = {
        "project": c.get("name") or collection_query,
        "x_username": x_username,
        "mint_price": mint_price,
        "amount_minted": amount_minted,
        "fp": c.get("floor") or 0.0,
        "ath": ath,
        "symbol": c.get("symbol") or "ETH",
        "eth_usd": c.get("listingUsdRate") or 0,
    }
    return pnl_card.render_pnl_card(data, project_thumb_bytes=thumb_bytes)


async def _cmd_wallet(address: str) -> dict:
    data = await _wallet_card_core(address)
    title = data.get("ensName") or data["address"]
    return {
        "title": f"💳 {title}",
        "description": f"`{data['address']}`",
        "color": EMBED_COLOR,
        "image": {"url": data["qrUrl"]},
        "footer": TOOLKIT_FOOTER,
    }


async def _cmd_pairs(chain: str) -> dict:
    chain = chain or "eth"
    pools = await _pairs_core(chain, limit=5)
    label = _PAIRS_CHAIN_DISPLAY.get(chain, chain.title())
    if not pools:
        return {"title": f"🔥 Fresh Pairs: {label}", "description": "No pairs found right now.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
    lines = [f"[{p['name']}]({p['url']}): {_fmt_usd(p['liquidityUsd'])} liquidity" for p in pools]
    return {"title": f"🔥 Fresh Pairs: {label}", "description": "\n".join(lines), "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}


async def _announce_watchlist_change(discord_user_id: str, name: str, verb: str, check: str) -> None:
    # /watchlist works from anywhere, but same as /monitor, what changed
    # always lands in the dedicated nft-intel channel specifically -
    # DMs/private acks stay where they are, the fact of it is public in
    # one consistent place.
    if not settings.discord_nft_monitor_channel_id:
        return
    embed = {
        "title": f"📌 Watchlist {verb}: {check}{name}",
        "description": f"<@{discord_user_id}> just {verb} **{name}** {'to' if verb == 'added' else 'from'} the shared /watchlist.",
        "color": EMBED_COLOR_GOOD if verb == "added" else EMBED_COLOR,
        "footer": TOOLKIT_FOOTER,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await _post_channel_message(client, settings.discord_nft_monitor_channel_id, embed)


async def _cmd_watchlist(payload: dict, discord_user_id: str) -> dict:
    sub_options = (payload.get("data") or {}).get("options") or []
    if not sub_options:
        return {"title": "Missing subcommand", "description": "Use `/watchlist add`, `remove`, `list`, or `clear`.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
    sub = sub_options[0]
    sub_name = sub.get("name")
    sub_opts = {o["name"]: o.get("value") for o in (sub.get("options") or [])}

    if sub_name == "list":
        collections = await _discord_watchlist_list(discord_user_id)
        if not collections:
            return {"title": "📌 Your NFT Watchlist", "description": "Nothing watched yet. Try `/watchlist add`.", "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}
        lines = []
        for c in collections:
            check = "✅ " if c.get("verified") else ""
            floor = f"{c['floor']:.3f} {c['symbol']}" if c.get("floor") is not None else "-"
            lines.append(f"{check}**[{c['name']}]({c.get('openseaUrl')})**: Floor {floor}")
        return {"title": "📌 Your NFT Watchlist", "description": "\n".join(lines), "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}

    if sub_name == "clear":
        count = await _discord_watchlist_clear(discord_user_id)
        if count == 0:
            return {"title": "Nothing to clear", "description": "Your watchlist is already empty.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
        plural = "s" if count != 1 else ""
        await _announce_watchlist_change(discord_user_id, f"{count} collection{plural}", "cleared", "")
        return {"title": "📌 Watchlist cleared", "description": f"Removed all {count} collection{plural} from your watchlist.", "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}

    query = (sub_opts.get("collection") or "").strip()
    if not query:
        return {"title": "Missing collection", "description": "Provide a collection name.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}

    if sub_name == "add":
        matches = await _nft_search_core(query)
        if not matches:
            return {"title": "No collections found", "description": f'No OpenSea results for "{query}".', "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
        c = await _discord_watchlist_add(discord_user_id, matches[0]["slug"])
        check = "✅ " if c.get("verified") else ""
        await _announce_watchlist_change(discord_user_id, c["name"], "added", check)
        return {"title": f"Added to watchlist: {check}{c['name']}", "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}

    if sub_name == "remove":
        matches = await _nft_search_core(query)
        slug = matches[0]["slug"] if matches else query
        name_for_display = matches[0]["name"] if matches else query
        await _discord_watchlist_remove(discord_user_id, slug)
        await _announce_watchlist_change(discord_user_id, name_for_display, "removed", "")
        return {"title": f"Removed from watchlist: {query}", "color": EMBED_COLOR, "footer": TOOLKIT_FOOTER}

    return {"title": "Unknown subcommand", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}


async def _discord_deferred_ack(interaction_id: str, token: str, ephemeral: bool = False) -> None:
    # Ephemeral has to be set on this initial ack — it can't be added later
    # when the real content is patched in via _discord_edit_original.
    body = {"type": 5, "data": {"flags": 64}} if ephemeral else {"type": 5}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.post(f"{DISCORD_API}/interactions/{interaction_id}/{token}/callback", json=body)
        except httpx.HTTPError:
            logger.exception("Failed to send deferred ack for interaction %s", interaction_id)


async def _discord_edit_original(token: str, embed: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.patch(
                f"{DISCORD_API}/webhooks/{settings.discord_client_id}/{token}/messages/@original",
                json={"embeds": [embed]},
            )
        except httpx.HTTPError:
            logger.exception("Failed to edit original response for interaction token")


async def _discord_edit_original_with_file(token: str, filename: str, file_bytes: bytes, content: str | None = None) -> None:
    # File attachments on an interaction response go through the same
    # webhook-edit endpoint as a plain embed, but as multipart/form-data
    # instead of JSON - the JSON part becomes a "payload_json" form field
    # alongside the raw file bytes.
    payload_json = {"attachments": [{"id": 0, "filename": filename}]}
    if content:
        payload_json["content"] = content
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            await client.patch(
                f"{DISCORD_API}/webhooks/{settings.discord_client_id}/{token}/messages/@original",
                data={"payload_json": json.dumps(payload_json)},
                files={"files[0]": (filename, file_bytes, "image/png")},
            )
        except httpx.HTTPError:
            logger.exception("Failed to edit original response with file attachment")


def _error_embed(exc: Exception) -> dict:
    detail = exc.detail if isinstance(exc, HTTPException) else "Something went wrong running that command."
    return {"title": "⚠️ Command failed", "description": str(detail), "color": EMBED_COLOR_BAD, "footer": TOOLKIT_FOOTER}


def _clean_embed(embed: dict) -> dict:
    # Discord's embed schema treats an explicit null differently from an
    # absent key for some optional fields (description, thumbnail) - a few
    # formatters above build one or the other depending on the data (e.g.
    # a collection with no description, no image), so strip Nones here
    # once, centrally, rather than every formatter needing to remember to.
    return {k: v for k, v in embed.items() if v is not None}


# ── /dashboard — single entry point, browse-and-pick tool discovery ───────
TOOLKIT_TOOLS = {
    "xray": {
        "emoji": "🐋", "label": "Wallet X-Ray",
        "short": "Heuristic on-chain score for any wallet",
        "usage": "/xray address:<wallet address or ENS>",
        "example": "/xray address:vitalik.eth",
    },
    "gas": {
        "emoji": "⛽", "label": "Gas Tracker",
        "short": "Current gas price on any supported chain",
        "usage": "/gas chain:<optional, defaults to Ethereum>",
        "example": "/gas chain:Robinhood Chain",
    },
    "scan": {
        "emoji": "🔍", "label": "CA Scanner",
        "short": "Look up a token contract: price, liquidity, volume",
        "usage": "/scan address:<token contract address>",
        "example": "/scan address:0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
    "rug": {
        "emoji": "🛡️", "label": "Rug Checker",
        "short": "Quick red-flag check on a token contract",
        "usage": "/rug address:<token contract address> chain:<optional>",
        "example": "/rug address:0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2 chain:Ethereum",
    },
    "nft": {
        "emoji": "🖼️", "label": "NFT Lookup",
        "short": "Floor, top offer, ATH, total volume, owners, contract address & more for any collection",
        "usage": "/nft collection:<name or contract address>",
        "example": "/nft collection:Pudgy Penguins",
    },
    "wallet": {
        "emoji": "💳", "label": "Wallet Card",
        "short": "Shareable wallet card with a scannable QR code",
        "usage": "/wallet address:<wallet address or ENS>",
        "example": "/wallet address:vitalik.eth",
    },
    "pairs": {
        "emoji": "🔥", "label": "New Pair Scanner",
        "short": "Freshly created trading pairs on a chain",
        "usage": "/pairs chain:<optional, defaults to Ethereum>",
        "example": "/pairs chain:Base",
    },
    "watchlist": {
        "emoji": "📌", "label": "NFT Watchlist",
        "short": "Track your own personal list of NFT collections",
        "usage": "/watchlist add|remove|list|clear collection:<name or contract address>",
        "example": "/watchlist add collection:Pudgy Penguins",
    },
    "monitor": {
        "emoji": "🔔", "label": "NFT Monitor",
        "short": "Personal DM pings for specific events on a collection: floor up/down, supply cuts, mint progress, sweeps, volume spikes - plus a one-shot alert at a specific target price",
        "usage": "/monitor set|clear collection:<name or contract address> · /monitor price collection:<name> target:<ETH or $USD>|percent:<%> loop:<true/false> · /monitor list",
        "example": "/monitor price collection:Pudgy Penguins percent:-50 loop:true",
    },
    "pnl": {
        "emoji": "📊", "label": "PnL Card",
        "short": "Generate a shareable branded card showing your realized profit/loss on a mint",
        "usage": "/pnl collection:<name or contract address> mint_price:<ETH or $USD> amount_minted:<count> x_username:<optional>",
        "example": "/pnl collection:Pudgy Penguins mint_price:0.03 amount_minted:2",
    },
}


def _dashboard_select_component() -> dict:
    return {
        "type": 1,
        "components": [{
            "type": 3,
            "custom_id": "toolkit_select",
            "placeholder": "Choose a tool for instructions…",
            "options": [
                {"label": t["label"], "value": key, "description": t["short"][:100], "emoji": {"name": t["emoji"]}}
                for key, t in TOOLKIT_TOOLS.items()
            ],
        }],
    }


def _dashboard_response() -> dict:
    lines = [f"{t['emoji']} **{t['label']}**: {t['short']}" for t in TOOLKIT_TOOLS.values()]
    embed = {
        "title": "🧰 Dash HQ Toolkit",
        "description": "Pick a tool below to see exactly how to use it.\n\n" + "\n".join(lines),
        "color": EMBED_COLOR,
        "footer": TOOLKIT_FOOTER,
    }
    return {"embeds": [embed], "components": [_dashboard_select_component()]}


def _tool_help_response(tool_key: str) -> dict:
    t = TOOLKIT_TOOLS.get(tool_key)
    if not t:
        embed = {"title": "Unknown tool", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
    else:
        embed = {
            "title": f"{t['emoji']} {t['label']}",
            "description": f"{t['short']}\n\n**Usage**\n`{t['usage']}`\n\n**Example**\n`{t['example']}`",
            "color": EMBED_COLOR,
            "footer": TOOLKIT_FOOTER,
        }
    # Keep the select menu attached so people can browse another tool's
    # instructions without needing to run /dashboard again.
    return {"embeds": [embed], "components": [_dashboard_select_component()]}


async def _handle_toolkit_select(payload: dict) -> dict:
    if not _is_citizen(payload):
        return {"type": 4, "data": {"content": "This is reserved for verified Dash HQ citizens.", "flags": 64}}
    values = (payload.get("data") or {}).get("values") or []
    tool_key = values[0] if values else ""
    return {"type": 7, "data": _tool_help_response(tool_key)}  # UPDATE_MESSAGE — edits in place


async def _handle_toolkit_command(payload: dict) -> dict:
    if not _is_citizen(payload):
        return {"type": 4, "data": {"content": "This command is reserved for verified Dash HQ citizens. Head to the site and verify with Discord to unlock it.", "flags": 64}}

    name = (payload.get("data") or {}).get("name")
    opts = _cmd_options(payload)
    member_user = (payload.get("member") or {}).get("user") or {}
    discord_user_id = member_user.get("id", "")

    # Anything that surfaces a specific person's financial standing or
    # personal tracking list is private — only the command's own invoker
    # sees it. /dashboard is a private instructions manual for members
    # (not something to broadcast into the channel every time someone
    # looks something up), so it's ephemeral too.
    EPHEMERAL_COMMANDS = {"xray", "wallet", "watchlist", "dashboard"}
    ephemeral = name in EPHEMERAL_COMMANDS

    if name == "dashboard":
        return {"type": 4, "data": {**_dashboard_response(), "flags": 64}}

    if name == "monitor":
        return {"type": 4, "data": await _cmd_monitor_response(payload, discord_user_id)}

    # /xray is the one command that can genuinely exceed Discord's 3-second
    # ack window (a heavily-active wallet's token-balance fetch alone can
    # take 10-25s) - deferred response, then edit the original message once
    # the real result is ready, instead of risking "This interaction failed."
    if name == "xray":
        interaction_id = payload.get("id")
        token = payload.get("token")
        await _discord_deferred_ack(interaction_id, token, ephemeral=True)
        try:
            embed = await _cmd_xray(opts.get("address", ""))
        except Exception as exc:
            embed = _error_embed(exc)
        await _discord_edit_original(token, _clean_embed(embed))
        return {"type": 5}

    # /pnl: collection search + live floor/ATH lookup + thumbnail download +
    # Pillow rendering routinely exceeds Discord's 3-second ack window -
    # same deferred pattern as /xray, but the followup carries a PNG
    # attachment instead of an embed.
    if name == "pnl":
        interaction_id = payload.get("id")
        token = payload.get("token")
        await _discord_deferred_ack(interaction_id, token, ephemeral=False)
        try:
            collection = opts.get("collection", "")
            mint_price_raw = opts.get("mint_price", "0")
            amount_minted = int(opts.get("amount_minted", 1))
            x_username = (opts.get("x_username") or member_user.get("global_name") or member_user.get("username") or "citizen").strip()
            png_bytes = await _pnl_render_core(collection, mint_price_raw, amount_minted, x_username)
            await _discord_edit_original_with_file(token, "pnl.png", png_bytes)
        except Exception as exc:
            if not isinstance(exc, HTTPException):
                logger.exception("/pnl command failed")
            await _discord_edit_original(token, _clean_embed(_error_embed(exc)))
        return {"type": 5}

    try:
        if name == "gas":
            embed = await _cmd_gas(opts.get("chain"))
        elif name == "scan":
            embed = await _cmd_scan(opts.get("address", ""))
        elif name == "rug":
            embed = await _cmd_rug(opts.get("address", ""), opts.get("chain"))
        elif name == "nft":
            embed = await _cmd_nft(opts.get("collection", ""))
        elif name == "wallet":
            embed = await _cmd_wallet(opts.get("address", ""))
        elif name == "pairs":
            embed = await _cmd_pairs(opts.get("chain"))
        elif name == "watchlist":
            embed = await _cmd_watchlist(payload, discord_user_id)
        else:
            return {"type": 4, "data": {"content": "Unknown command.", "flags": 64}}
    except Exception as exc:
        if not isinstance(exc, HTTPException):
            logger.exception("Toolkit command /%s failed", name)
        embed = _error_embed(exc)

    data = {"embeds": [_clean_embed(embed)]}
    if ephemeral:
        data["flags"] = 64
    return {"type": 4, "data": data}


@app.get("/health")
async def health():
    return {"status": "ok"}
