"""Application configuration loaded from environment variables.

All configuration flows through this module. Other ``src/`` modules import
:func:`get_settings` rather than reading ``os.environ`` directly (see
CONTRIBUTING.md). Secrets have **no defaults** so the
application fails fast at startup when they are missing; non-secret values
have sensible defaults defined below.

The settings object is cached via :func:`functools.lru_cache` so that it is
constructed at most once per process. Tests can reset the cache with
``get_settings.cache_clear()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for the Manthan data layer."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM (Vultr Serverless Inference) ----------------------------------
    # Manthan reaches the upstream LLM through Vultr Serverless Inference's
    # OpenAI-compatible chat completions endpoint, so the same OpenAI SDK
    # shape (messages + tools + tool_calls) works unchanged. Enable
    # Serverless Inference in the Vultr console at https://my.vultr.com/inference/
    # to mint an API key.
    vultr_api_key: SecretStr = Field(
        ...,
        description="Vultr Serverless Inference API key. No default — must be provided via env.",
    )
    vultr_model: str = Field(
        default="MiniMaxAI/MiniMax-M2.7-normalize",
        description=(
            "Primary upstream model for Layer 1 classification + Layer 2 reasoning. "
            "Default = MiniMax M2.7 with normalize suffix for canonical OpenAI shape."
        ),
    )
    vultr_fallback_models: list[str] = Field(
        default=[
            "Qwen/Qwen3.6-27B-FP8",
        ],
        description=(
            "Ordered fallback model ids on Vultr. Tried when the primary "
            "is unavailable or rate-limited. If all fail, the deterministic "
            "heuristic classifier kicks in."
        ),
    )

    @property
    def resolved_model(self) -> str:
        """Primary model slug."""
        return self.vultr_model

    @property
    def resolved_fallback_models(self) -> list[str]:
        """Fallback model slugs."""
        return list(self.vultr_fallback_models)

    vultr_base_url: str = Field(
        default="https://api.vultrinference.com/v1",
        description=(
            "Vultr Serverless Inference base URL. Override to a Lobster Trap "
            "proxy URL (e.g. http://localhost:8080/v1) to route chat through "
            "deep prompt inspection."
        ),
    )

    # --- DuckDB ------------------------------------------------------------
    duckdb_memory_limit: str = Field(
        default="4GB",
        description="DuckDB memory_limit config value.",
    )
    duckdb_threads: int = Field(
        default=4,
        ge=1,
        description="Number of DuckDB worker threads.",
    )
    duckdb_temp_directory: Path = Field(
        default=Path("/tmp/duckdb"),
        description="DuckDB spill-to-disk scratch directory.",
    )

    # --- Sandbox -----------------------------------------------------------
    sandbox_image: str = Field(default="manthan-sandbox:latest")
    sandbox_memory_limit: str = Field(default="2g")
    sandbox_cpu_limit: int = Field(default=2, ge=1)
    sandbox_timeout_seconds: int = Field(default=60, ge=1)
    sandbox_network_disabled: bool = Field(default=True)

    # --- Rate Limiting -----------------------------------------------------
    rate_limit_whitelist: list[str] = Field(
        default_factory=list,
        description=(
            "Additional IPs to whitelist from rate limits. "
            "127.0.0.1 and ::1 are always whitelisted. "
            "Add your Layer 2/3 server IPs here."
        ),
    )

    # --- Storage -----------------------------------------------------------
    data_directory: Path = Field(
        default=Path("./data"),
        description="Root directory for per-dataset artifacts.",
    )
    max_upload_size_mb: int = Field(default=500, ge=1)

    # --- Server ------------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = Field(default="info")
    log_format: str = Field(default="json", description="Either 'json' or 'console'.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton for this process."""
    return Settings()
