from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    discord_client_id: str
    discord_client_secret: str
    discord_redirect_uri: str
    discord_guild_id: str
    jwt_secret: str
    frontend_url: str = "http://localhost:3000"

    # Optional: bot token lets us check roles without asking extra OAuth scopes
    discord_bot_token: Optional[str] = None

    # Optional: Discord role IDs for tier detection
    tier_gold_role_id: Optional[str] = None
    tier_silver_role_id: Optional[str] = None

    model_config = {"env_file": ".env"}


settings = Settings()
