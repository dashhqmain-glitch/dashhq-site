import time
import urllib.parse

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
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
    allow_methods=["GET"],
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


@app.get("/auth/discord")
async def discord_login():
    return RedirectResponse(_oauth_url())


@app.get("/auth/discord/callback")
@limiter.limit("10/minute")
async def discord_callback(request: Request, code: str = None, error: str = None):
    portal = f"{settings.frontend_url}/portal.html"

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

        # 3. Check guild membership + roles
        is_member = False
        tier = "CITIZEN"
        nick = None
        joined_year = None

        if settings.discord_bot_token:
            member_res = await client.get(
                f"{DISCORD_API}/guilds/{settings.discord_guild_id}/members/{user_id}",
                headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            )
            if member_res.status_code == 200:
                is_member = True
                m = member_res.json()
                roles = m.get("roles", [])
                nick = m.get("nick")
                raw_joined = m.get("joined_at", "")
                joined_year = raw_joined[:4] if raw_joined else None

                if settings.tier_gold_role_id and settings.tier_gold_role_id in roles:
                    tier = "GOLD TIER"
                elif settings.tier_silver_role_id and settings.tier_silver_role_id in roles:
                    tier = "SILVER TIER"
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
            "avatar": _avatar_url(user),
            "is_member": is_member,
            "tier": tier,
            "joined": joined_year or "2023",
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


@app.get("/health")
async def health():
    return {"status": "ok"}
