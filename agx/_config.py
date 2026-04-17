"""
AG-X Community Edition — configuration.

All settings can be overridden via environment variables (prefix AGX_).
Uses pydantic-settings so the config is validated at import time.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for AG-X Community Edition.

    Environment variable prefix: AGX_
    Example: AGX_DATA_DIR=/tmp/agx
    """

    model_config = SettingsConfigDict(
        env_prefix="AGX_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Storage ---
    data_dir: str = Field(
        default="~/.agx",
        description=(
            "Root directory for local storage (~/.agx by default). "
            "Set to empty string for in-memory mode (useful in CI)."
        ),
    )

    # --- Cloud upgrade bridge ---
    endpoint: Optional[str] = Field(
        default=None,
        description="AG-X Cloud endpoint URL. When set, routes guard calls to cloud.",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="AG-X Cloud API key (tgak_...).",
    )

    # --- OpenTelemetry ---
    otel_endpoint: str = Field(
        default="http://localhost:4317",
        description="OTLP gRPC endpoint for OTel span export.",
    )
    otel_service_name: str = Field(
        default="agx-community",
        description="OTel service.name attribute.",
    )
    otel_enabled: bool = Field(
        default=False,
        description="Enable OTel export. Auto-set to True when agx.setup_otel() is called.",
    )

    # --- Logging ---
    log_level: str = Field(
        default="WARNING",
        description="Python log level for AGX internals.",
    )

    # --- Dashboard ---
    dashboard_host: str = Field(default="127.0.0.1", description="Dashboard bind host.")
    dashboard_port: int = Field(default=7000, description="Dashboard bind port.")

    # --- Guard behaviour ---
    default_session_ttl_seconds: int = Field(
        default=3600,
        description="Session expiry for in-memory run tracking.",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @property
    def resolved_data_dir(self) -> Optional[Path]:
        """Return the resolved data directory Path, or None for in-memory mode."""
        if not self.data_dir:
            return None
        if self.data_dir == "~/.agx" or not os.path.isabs(self.data_dir):
            return Path.home() / ".agx"
        return Path(self.data_dir)

    @property
    def traces_db_path(self) -> Optional[Path]:
        d = self.resolved_data_dir
        return d / "traces.db" if d else None

    @property
    def vaccines_dir(self) -> Optional[Path]:
        d = self.resolved_data_dir
        return d / "vaccines" if d else None

    @property
    def cloud_mode(self) -> bool:
        return bool(self.endpoint)


# Module-level singleton — imported everywhere as `from agx._config import settings`
settings = Settings()

# Apply log level
logging.getLogger("agx").setLevel(getattr(logging, settings.log_level, logging.WARNING))
