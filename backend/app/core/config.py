"""Cloud API settings (spec §68)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Development placeholder; refused in production by get_settings().
INSECURE_JWT_SECRET = "change-me-in-production"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/v1"
    internal_prefix: str = "/internal/edge/v1"

    database_url: str = "postgresql+asyncpg://nightshift:nightshift@localhost:5432/nightshift"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://nightshift:nightshift@localhost:5672//"

    # Refused at startup in production (see get_settings).
    jwt_secret: str = Field(default="change-me-in-production")
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    s3_endpoint: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "nightshift-media"
    s3_access_key: str = "nightshift"
    s3_secret_key: str = "nightshift-secret"  # noqa: S105 - dev default, overridden by env
    s3_use_path_style: bool = True
    s3_presign_ttl_seconds: int = 300

    ai_service_url: str = "http://localhost:8100"
    media_gateway_url: str = "http://localhost:9997"
    billing_provider: str = "noop"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production:
        if settings.jwt_secret == INSECURE_JWT_SECRET:
            raise RuntimeError("JWT_SECRET must be set in production")
        if len(settings.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must be at least 32 characters in production")
    return settings
