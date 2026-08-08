from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = ""
    discord_guild_id: str = ""
    jwt_secret: str = "not_set"
    frontend_url: str = "https://www.dashhq.site"

    discord_bot_token: Optional[str] = None
    citizen_role_id: Optional[str] = None

    supabase_url: str = ""
    supabase_service_role_key: str = ""
    cron_secret: str = ""

    discord_public_key: str = ""
    discord_applications_channel_id: str = ""
    discord_invite_channel_id: str = ""  # channel accepted citizens land in
    # One shared channel for everything NFT-automated: supply cut / sweep /
    # volume spike alerts AND new-mint radar posts. Kept as a single
    # channel on purpose - fewer channels to manage in the server, and the
    # embeds are already visually distinct (different emoji/colors) so
    # interleaving them reads fine.
    discord_nft_channel_id: str = ""

    # Separate from cron_secret on purpose: this one only guards the NFT
    # poller, which is reachable from a public GitHub Actions workflow log
    # surface far more often than the other cron endpoints - keeping it
    # distinct limits the blast radius if it ever leaks.
    nft_cron_secret: str = ""

    # Pidgin AutoMod: English-only enforcement in #general (10-min timeout),
    # exempting every other channel (Discord AutoMod has no "only apply in
    # these channels" allowlist - only an exempt-list) so #lifestyle-chat
    # and everywhere else stays unaffected.
    discord_general_channel_id: str = ""
    discord_automod_alert_channel_id: str = ""

    x_client_id: str = ""
    x_client_secret: str = ""
    x_redirect_uri: str = "https://www.dashhq.site/auth/x/callback"

    model_config = {"env_file": ".env"}


settings = Settings()
