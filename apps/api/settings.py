"""Every runtime knob the API and the worker read, from the environment only.

02 §F and 01 §A rule 8 together: tunables live in ``config/*.yaml``, secrets live in
the environment, and nothing lives in the repo. This module is the seam between those
two rules — it reads the environment for credentials and endpoints, and it reads
``config/`` through :mod:`oxbow.config` for economics and pipeline values. A value
found in neither is an error at startup, not a default discovered in production.

The local-JWT fallback is behind an explicit flag and is the only setting here with a
generated default, because the offline demo has to boot without Keycloak (C7) and the
alternative — an empty signing key — is how a prototype ends up accepting unsigned
tokens.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from oxbow.config import ConfigError, PipelineConfig, find_repo_root, load_pipeline_config

DEFAULT_API_PORT: Final = 8000
DEFAULT_ECHO_URL: Final = "http://localhost:8099/webhook"

# The outbox ladder and replay window are pinned by 02 §E; only the *secret* and the
# *endpoint* are environment values.
OUTBOX_MAX_ATTEMPTS: Final = 5
SIGNATURE_REPLAY_WINDOW_SECONDS: Final = 300


class Settings(BaseSettings):
    """Runtime configuration. Instantiated once, cached, never mutated."""

    model_config = SettingsConfigDict(
        env_file=None,  # loaded by the caller: `make` and compose inject the environment
        env_prefix="",
        extra="ignore",
    )

    # --- database and redis ------------------------------------------------
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")

    # --- identity ----------------------------------------------------------
    oidc_issuer: str = Field(default="", validation_alias="OXBOW_OIDC_ISSUER")
    oidc_audience: str = Field(default="oxbow-web", validation_alias="OXBOW_OIDC_AUDIENCE")
    oidc_client_id: str = Field(default="oxbow-web", validation_alias="KEYCLOAK_CLIENT_ID")
    oidc_jwks_url: str = Field(default="", validation_alias="OXBOW_OIDC_JWKS_URL")
    local_jwt_secret: str = Field(default="", validation_alias="OXBOW_LOCAL_JWT_SECRET")
    local_jwt_enabled: bool = Field(default=True, validation_alias="OXBOW_LOCAL_JWT_ENABLED")

    # --- outbound ----------------------------------------------------------
    webhook_signing_secret: str = Field(default="", validation_alias="WEBHOOK_SIGNING_SECRET")
    webhook_endpoint: str = Field(default=DEFAULT_ECHO_URL, validation_alias="WEBHOOK_ENDPOINT")
    slack_webhook_url: str = Field(default="", validation_alias="OXBOW_SLACK_WEBHOOK_URL")

    # --- object storage (MinIO in compose) ---------------------------------
    s3_endpoint_url: str = Field(default="", validation_alias="OXBOW_S3_ENDPOINT_URL")
    s3_bucket: str = Field(default="oxbow", validation_alias="OXBOW_S3_BUCKET")
    s3_access_key: str = Field(default="", validation_alias="MINIO_USER")
    s3_secret_key: str = Field(default="", validation_alias="MINIO_PASSWORD")

    # --- misc --------------------------------------------------------------
    run_salt: str = Field(default="", validation_alias="RUN_SALT")
    seed: int = Field(default=1337, validation_alias="OXBOW_SEED")
    repo_root: Path = Field(default_factory=find_repo_root, validation_alias="OXBOW_REPO_ROOT")
    request_id_header: str = Field(default="x-trace-id", validation_alias="OXBOW_TRACE_HEADER")
    http_timeout_seconds: float = Field(default=5.0, validation_alias="OXBOW_HTTP_TIMEOUT")

    @field_validator("seed")
    @classmethod
    def _seed_matches_contract(cls, value: int) -> int:
        if value != 1337:
            raise ValueError(
                f"OXBOW_SEED is {value} but the reproducibility contract fixes 1337 (01 §A rule 4)"
            )
        return value

    # --- derived -----------------------------------------------------------

    @property
    def sqlalchemy_url(self) -> str:
        """The configured URL, normalised to the psycopg3 dialect.

        Accepts the bare ``postgresql://`` form people paste from a README and the
        ``postgresql+psycopg://`` form the compose file uses, so the failure mode of a
        wrong driver is a correction rather than a crash at first connect.
        """
        url = self.database_url.strip()
        if not url:
            raise ConfigError(
                "DATABASE_URL is not set. Point it at the Compose Postgres "
                "(postgresql+psycopg://user:pass@127.0.0.1:5433/oxbow) — see .env.example. "
                "The API never falls back to SQLite: the warehouse is Postgres or the "
                "handoff does not exist."
            )
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def jwks_url(self) -> str:
        """Where to fetch signing keys, derived from the issuer when not set."""
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        if self.oidc_issuer:
            return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"
        return ""

    def local_secret(self) -> str:
        """The HS256 key for the offline fallback, minted once per process if unset.

        Minting rather than defaulting to a constant: a committed fallback key is a
        backdoor with good manners. Tokens minted this way die with the process, which
        is exactly right for a demo and exactly wrong for a deployment — hence the
        loud ``local_jwt_enabled`` flag in the startup log line.
        """
        if not self.local_jwt_enabled:
            raise ConfigError(
                "the local JWT fallback is disabled and no OIDC issuer is configured, so this "
                "deployment cannot authenticate anyone. Set OXBOW_OIDC_ISSUER."
            )
        if not self.local_jwt_secret:
            self.local_jwt_secret = secrets.token_urlsafe(48)
        return self.local_jwt_secret

    def signing_secret(self) -> str:
        if not self.webhook_signing_secret:
            raise ConfigError(
                "WEBHOOK_SIGNING_SECRET is unset. The outbox refuses to send unsigned "
                "payloads: an unverifiable delivery is indistinguishable from a forged one "
                "(02 §E)."
            )
        return self.webhook_signing_secret

    def pipeline_config(self) -> PipelineConfig:
        return load_pipeline_config(self.repo_root)

    def economics(self) -> dict[str, Any]:
        """The economic assumptions every money figure in a response traces to."""
        from oxbow.config import load_yaml

        return load_yaml(self.repo_root / "config" / "economics.yaml")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object.

    Cached because re-reading the environment per request would let a stray export
    change the seed or the signing key mid-run, which is a reproducibility failure
    hiding inside a convenience.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. For tests that change the environment."""
    get_settings.cache_clear()


__all__ = [
    "DEFAULT_API_PORT",
    "OUTBOX_MAX_ATTEMPTS",
    "SIGNATURE_REPLAY_WINDOW_SECONDS",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]
