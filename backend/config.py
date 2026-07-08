from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = ""
    discord_guild_id: str = ""
    jwt_secret: str = "not_set"
    frontend_url: str = "https://dashhq.site"

    discord_bot_token: Optional[str] = None
    tier_gold_role_id: Optional[str] = None
    tier_silver_role_id: Optional[str] = None

    model_config = {"env_file": ".env"}


settings = Settings()
