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

    model_config = {"env_file": ".env"}


settings = Settings()
