from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    plaid_client_id: str
    plaid_secret: str
    plaid_env: str
    app_host: str
    app_port: int
    db_path: Path
    muse_api_token: str
    muse_host: str
    muse_port: int

    @property
    def plaid_env_value(self) -> str:
        return self.plaid_env.strip().lower()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _plaid_secret(env: str) -> str:
    """Env-specific secret if set, else the generic PLAID_SECRET.

    ``env`` must already be normalized (lowercased/stripped).
    """
    specific = (
        _env("PLAID_SECRET_PRODUCTION")
        if env == "production"
        else _env("PLAID_SECRET_SANDBOX")
    )
    return specific or _env("PLAID_SECRET")


_PLAID_ENV = _env("PLAID_ENV", "sandbox").strip().lower()

settings = Settings(
    plaid_client_id=_env("PLAID_CLIENT_ID"),
    plaid_secret=_plaid_secret(_PLAID_ENV),
    plaid_env=_PLAID_ENV,
    app_host=_env("APP_HOST", "127.0.0.1"),
    app_port=int(_env("APP_PORT", "8000")),
    db_path=Path(_env("DB_PATH", "data/money-mover.db")),
    muse_api_token=_env("MUSE_API_TOKEN"),
    muse_host=_env("MUSE_HOST"),
    muse_port=int(_env("MUSE_PORT", "8001")),
)
