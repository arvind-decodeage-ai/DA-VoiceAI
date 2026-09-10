"""M0 configuration module.

Loads settings from environment / .env via pydantic-settings. Deliberately has no
business logic and makes no external calls at import time. OPENROUTER_MODEL and
OPENROUTER_FALLBACK_MODEL are allowed to be empty strings during M0 — they only
become required starting at M1.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    sarvam_api_key: str = ""
    openrouter_api_key: str = ""

    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "secret"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/da_voice"
    redis_url: str = "redis://localhost:6379"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3001"

    openrouter_model: str = ""
    openrouter_fallback_model: str = ""


def get_settings() -> Settings:
    return Settings()
