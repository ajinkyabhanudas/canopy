"""
config.py
---------
Single place that reads environment variables and the models.yaml registry.
Nothing else in this package should call os.getenv directly, so credential
handling stays auditable in one file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Legacy single-model config (Anthropic-only path, kept for internal compat)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelConfig:
    backend: str
    api_key: str
    model: str
    timeout: float


def get_model_config() -> ModelConfig:
    """Return active connection as a flat ModelConfig for backward-compatible callers."""
    conn = get_active_connection()
    return ModelConfig(
        backend=conn.backend,
        api_key=conn.api_key,
        model=conn.models[0] if conn.models else "",
        timeout=conn.timeout,
    )


# ---------------------------------------------------------------------------
# Multi-model registry (models.yaml)
# ---------------------------------------------------------------------------

@dataclass
class ModelConnection:
    id: str
    backend: str
    api_key: str
    models: list[str] = field(default_factory=list)
    endpoint: str = ""
    timeout: float = 60.0
    api_style: str = "azure-inference"  # "azure-inference" | "openai-compat" | "openai-responses"
    active: bool = True                 # False = skip in benchmark until admin activates


def _models_yaml_path() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (
        Path.cwd() / "models.yaml",
        here.parent.parent / "models.yaml",   # src/canopy/../../models.yaml = repo root
    ):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "models.yaml not found. Expected at repo root or current working directory."
    )


def load_model_connections(path: str | Path | None = None) -> list[ModelConnection]:
    """Parse models.yaml and resolve api_key_env → actual key value from env.

    Result is cached by resolved file path — re-reads when a non-default path is given
    or when the caller explicitly passes a path (benchmark runs).
    """
    import yaml  # lazy import — only needed when multi-model path is used

    yaml_path = Path(path) if path else _models_yaml_path()

    # Avoid re-parsing yaml on every LLM iteration (called once per model.generate call).
    cache_key = str(yaml_path)
    if cache_key in _connections_cache:
        return _connections_cache[cache_key]

    raw: dict[str, Any] = yaml.safe_load(yaml_path.read_text()) or {}
    connections: list[ModelConnection] = []

    for entry in raw.get("connections", []):
        api_key_env = entry.get("api_key_env", "")
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        connections.append(
            ModelConnection(
                id=entry["id"],
                backend=entry["backend"],
                api_key=api_key,
                models=entry.get("models") or [],
                endpoint=entry.get("endpoint", ""),
                timeout=float(entry.get("timeout", 60)),
                api_style=entry.get("api_style", "azure-inference"),
                active=entry.get("active", True),
            )
        )

    if not connections:
        raise ValueError(f"models.yaml at {yaml_path} has no connections defined.")

    _connections_cache[cache_key] = connections
    return connections


_connections_cache: dict[str, list[ModelConnection]] = {}


def get_active_connection(model_override: str | None = None) -> ModelConnection:
    """Return the connection matching MODEL_BACKEND env var.

    model_override replaces the model list with a single entry — used by the
    benchmark runner to pin a specific discovered deployment.
    """
    active_id = os.environ.get("MODEL_BACKEND", "gpt-5.1-codex-mini")
    connections = load_model_connections()
    for conn in connections:
        if conn.id == active_id:
            if model_override:
                conn = ModelConnection(
                    id=conn.id,
                    backend=conn.backend,
                    api_key=conn.api_key,
                    models=[model_override],
                    endpoint=conn.endpoint,
                    timeout=conn.timeout,
                    api_style=conn.api_style,
                    active=conn.active,
                )
            return conn
    available = [c.id for c in connections]
    raise ValueError(
        f"MODEL_BACKEND='{active_id}' not found in models.yaml. "
        f"Available: {available}"
    )


# ---------------------------------------------------------------------------
# Database config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DBConfig:
    host: str
    port: str
    dbname: str
    user: str
    password: str

    def is_configured(self) -> bool:
        return all([self.host, self.port, self.dbname, self.user, self.password])


def get_db_config() -> DBConfig:
    return DBConfig(
        host=os.environ.get("PG_HOST", ""),
        port=os.environ.get("PG_PORT", "5432"),
        dbname=os.environ.get("PG_DBNAME", ""),
        user=os.environ.get("PG_USER", ""),
        password=os.environ.get("PG_PASSWORD", ""),
    )


# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------

def get_data_dir() -> Path:
    """Return the directory used for persistent local data (history, cache).

    Override with CANOPY_DATA_DIR for Docker / cloud deployments.
    Mount a persistent volume at that path to survive container restarts.
    Falls back to ~/.canopy if the configured path cannot be created (e.g. when
    CANOPY_DATA_DIR=/data is set in a local .env but the Docker volume is absent).
    """
    configured = Path(os.environ.get("CANOPY_DATA_DIR", Path.home() / ".canopy"))
    try:
        configured.mkdir(parents=True, exist_ok=True)
        return configured
    except (PermissionError, OSError):
        fallback = Path.home() / ".canopy"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


# ---------------------------------------------------------------------------
# UI language
# ---------------------------------------------------------------------------

def get_ui_lang() -> str:
    """Return the UI locale from CANOPY_UI_LANG (default: 'en').

    Supported: 'en', 'es'. Unknown values fall back to 'en' at set_locale().
    """
    return os.environ.get("CANOPY_UI_LANG", "en").lower().strip()
