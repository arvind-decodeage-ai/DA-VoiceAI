"""Settings for the Shopify sync scripts.

Deliberately separate from agent.config: this project reads .env directly
via its own pydantic-settings model instead of importing agent/, so it stays
decoupled from the rest of the repo per the approved design boundary.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5433/da_voice"

    shopify_shop_domain: str = ""
    shopify_access_token: str = ""
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_api_version: str = "2024-01"


def get_settings() -> Settings:
    return Settings()
