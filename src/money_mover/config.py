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

    @property
    def plaid_env_value(self) -> str:
        return self.plaid_env.strip().lower()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _plaid_secret(env: str) -> str:
    if env == "production":
        return _env("PLAID_SECRET_PRODUCTION")
    else:
        return _env("PLAID_SECRET_SANDBOX")


settings = Settings(
    plaid_client_id=_env("PLAID_CLIENT_ID"),
    plaid_secret=_plaid_secret(_env("PLAID_ENV", "sandbox")),
    plaid_env=_env("PLAID_ENV", "sandbox"),
    app_host=_env("APP_HOST", "127.0.0.1"),
    app_port=int(_env("APP_PORT", "8000")),
    db_path=Path(_env("DB_PATH", "data/money-mover.db")),
)
