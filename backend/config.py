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
    # Watchlist alerts channel: supply cut / sweep / volume spike / floor
    # move posts for anything on anyone's /watchlist.
    discord_nft_channel_id: str = ""

    # NFT Scope (formerly "mint radar") - a separate, deliberately
    # selective feed: a weighted scoring model (not a pass/fail checklist)
    # that studies genuine trading signals, actively screens out faked
    # top-offer manipulation, and covers both fresh mints AND already-
    # tracked collections showing real breakout momentum. Posts to its
    # own channel/server, fully decoupled from discord_nft_channel_id
    # above - empty means no posts anywhere (safe default) until a
    # channel is actually configured. nft_scope_enabled stays available
    # as an emergency off-switch without a redeploy.
    discord_nft_scope_channel_id: str = ""
    nft_scope_enabled: bool = True

    # /monitor is opt-in and normally DMs subscribers privately. When set,
    # any event a member has /monitor'd also gets posted here publicly
    # with those members @mentioned - visible even if their DMs are closed,
    # and lets others see who's tracking what. Leave blank to keep
    # /monitor fully private (DM-only), matching the original behavior.
    discord_nft_monitor_channel_id: str = ""

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
