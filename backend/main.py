import asyncio
import base64
import hashlib
import logging
import re
import secrets
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

    if itype == 2:  # APPLICATION_COMMAND — a /slash command
        if (payload.get("data") or {}).get("name") == "history":
            return await _handle_history_command(payload)
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


async def _nft_search_core(q: str) -> list[dict]:
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
    c = results[0]
    check = "✅ " if c.get("verified") else ""
    fields = [
        {"name": "Floor", "value": f"{c['floor']:.3f} {c['symbol']}" if c.get("floor") is not None else "-", "inline": True},
        {"name": "24h Volume", "value": f"{c['vol1d']:.2f} {c['symbol']}" if c.get("vol1d") is not None else "-", "inline": True},
        {"name": "Owners", "value": f"{c['owners']:,}" if c.get("owners") is not None else "-", "inline": True},
    ]
    if c.get("category"):
        fields.append({"name": "Category", "value": c["category"], "inline": True})
    return {
        "title": f"{check}{c['name']}",
        "url": c.get("openseaUrl"),
        "description": c.get("description"),
        "color": EMBED_COLOR,
        "fields": fields,
        "thumbnail": {"url": c["image"]} if c.get("image") else None,
        "footer": TOOLKIT_FOOTER,
    }


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


async def _cmd_watchlist(payload: dict, discord_user_id: str) -> dict:
    sub_options = (payload.get("data") or {}).get("options") or []
    if not sub_options:
        return {"title": "Missing subcommand", "description": "Use `/watchlist add`, `remove`, or `list`.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
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

    query = (sub_opts.get("collection") or "").strip()
    if not query:
        return {"title": "Missing collection", "description": "Provide a collection name.", "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}

    if sub_name == "add":
        matches = await _nft_search_core(query)
        if not matches:
            return {"title": "No collections found", "description": f'No OpenSea results for "{query}".', "color": EMBED_COLOR_WARN, "footer": TOOLKIT_FOOTER}
        c = await _discord_watchlist_add(discord_user_id, matches[0]["slug"])
        check = "✅ " if c.get("verified") else ""
        return {"title": f"Added to watchlist: {check}{c['name']}", "color": EMBED_COLOR_GOOD, "footer": TOOLKIT_FOOTER}

    if sub_name == "remove":
        matches = await _nft_search_core(query)
        slug = matches[0]["slug"] if matches else query
        await _discord_watchlist_remove(discord_user_id, slug)
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
        "short": "Floor price, volume & verified status for any collection",
        "usage": "/nft collection:<collection name>",
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
        "usage": "/watchlist add|remove|list collection:<name>",
        "example": "/watchlist add collection:Pudgy Penguins",
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
    # sees it. /dashboard itself stays public (it's just a menu, nothing
    # sensitive in it) along with the price/market lookups that don't
    # reveal anything about who's asking.
    EPHEMERAL_COMMANDS = {"xray", "wallet", "watchlist"}
    ephemeral = name in EPHEMERAL_COMMANDS

    if name == "dashboard":
        return {"type": 4, "data": _dashboard_response()}

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
