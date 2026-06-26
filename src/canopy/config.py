"""
config.py
---------
Single place that reads environment variables. Nothing else in this
package should call os.getenv directly, so credential handling stays
auditable in one file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ModelConfig:
    backend: str
    api_key: str
    model: str
    timeout: float


@dataclass(frozen=True)
class DBConfig:
    host: str
    port: str
    dbname: str
    user: str
    password: str

    def is_configured(self) -> bool:
        return all([self.host, self.port, self.dbname, self.user, self.password])


def get_model_config() -> ModelConfig:
    return ModelConfig(
        backend=os.environ.get("MODEL_BACKEND", "anthropic"),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        timeout=float(os.environ.get("ANTHROPIC_TIMEOUT", "60")),
    )


def get_data_dir() -> Path:
    """Return the directory used for persistent local data (history, cache).

    Override with CANOPY_DATA_DIR for Docker / cloud deployments.
    Mount a persistent volume at that path to survive container restarts.
    """
    return Path(os.environ.get("CANOPY_DATA_DIR", Path.home() / ".canopy"))


def get_db_config() -> DBConfig:
    return DBConfig(
        host=os.environ.get("PG_HOST", ""),
        port=os.environ.get("PG_PORT", "5432"),
        dbname=os.environ.get("PG_DBNAME", ""),
        user=os.environ.get("PG_USER", ""),
        password=os.environ.get("PG_PASSWORD", ""),
    )
