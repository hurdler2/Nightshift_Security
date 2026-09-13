"""Edge agent configuration (spec §68).

XVR credentials are deliberately absent: they live only in the encrypted local secret
store, never in env vars or config files (spec §33).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EDGE_",
        env_file=".env",
        extra="ignore",
    )

    id: str = Field(default="", description="Edge gateway id issued at enrollment")
    data_dir: Path = Field(default=Path("./edge-data"))
    cloud_url: HttpUrl | None = None
    enrollment_token: str | None = None
    max_spool_gb: float = 10.0
    log_level: str = "INFO"

    #: Fernet key for the local secret store. Generated at first run when absent.
    secret_key: str | None = None

    heartbeat_seconds: int = 30
    event_heartbeat_seconds: int = 5
    event_codes: str = "[All]"
    enable_dahua_netsdk: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "queue.sqlite3"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / "secrets.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "spool").mkdir(parents=True, exist_ok=True)


def load_settings() -> EdgeSettings:
    settings = EdgeSettings()
    settings.ensure_dirs()
    return settings
