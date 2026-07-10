"""Tests for canopy.config module."""

from __future__ import annotations

import textwrap

from canopy.config import get_active_connection as _real_get_active_connection
from canopy.config import get_ui_lang


def test_get_ui_lang_default(monkeypatch):
    monkeypatch.delenv("CANOPY_UI_LANG", raising=False)
    assert get_ui_lang() == "en"


def test_get_ui_lang_reads_env(monkeypatch):
    monkeypatch.setenv("CANOPY_UI_LANG", "ES")
    assert get_ui_lang() == "es"  # lowercased


def test_get_ui_lang_strips_whitespace(monkeypatch):
    monkeypatch.setenv("CANOPY_UI_LANG", " es ")
    assert get_ui_lang() == "es"


def test_get_ui_lang_unknown_value_passes_through(monkeypatch):
    # Validation (fallback to 'en') happens in set_locale(), not here
    monkeypatch.setenv("CANOPY_UI_LANG", "fr")
    assert get_ui_lang() == "fr"


# ---------------------------------------------------------------------------
# load_model_connections — in-process cache
# ---------------------------------------------------------------------------


def test_load_model_connections_cached_on_second_call(tmp_path, monkeypatch):
    """Second call with same path returns the cached list without re-reading the file."""
    import canopy.config as cfg

    yaml_content = textwrap.dedent("""
        connections:
          - id: test-conn
            backend: anthropic
            api_key_env: ANTHROPIC_API_KEY
            models: [claude-sonnet-4-6]
    """)
    yaml_file = tmp_path / "models.yaml"
    yaml_file.write_text(yaml_content)

    # Clear cache so this test is independent of import-time state.
    monkeypatch.setattr(cfg, "_connections_cache", {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    first = cfg.load_model_connections(yaml_file)
    # Overwrite the file — second call must still return the cached result.
    yaml_file.write_text("connections: []")
    second = cfg.load_model_connections(yaml_file)

    assert first is second  # same object — cache hit
    assert len(second) == 1
    assert second[0].id == "test-conn"


# ---------------------------------------------------------------------------
# get_model_config — empty models list branch (lines 36-37)
# ---------------------------------------------------------------------------


def test_get_model_config_empty_models_list(tmp_path, monkeypatch):
    """get_model_config() returns empty string for model when models list is empty."""
    import canopy.config as cfg
    from canopy.config import ModelConnection

    conn_no_models = ModelConnection(
        id="empty-conn", backend="anthropic", api_key="k", models=[], endpoint="", timeout=30.0
    )
    monkeypatch.setattr(cfg, "get_active_connection", lambda: conn_no_models)

    result = cfg.get_model_config()
    assert result.model == ""
    assert result.backend == "anthropic"


# ---------------------------------------------------------------------------
# _models_yaml_path — FileNotFoundError (line 69)
# ---------------------------------------------------------------------------


def test_models_yaml_path_raises_when_not_found(tmp_path, monkeypatch):
    """_models_yaml_path() raises FileNotFoundError when yaml is absent everywhere."""
    from pathlib import Path

    import pytest

    import canopy.config as cfg

    # Point cwd() to a dir with no models.yaml; also patch __file__ so the fallback
    # candidate (repo-root) also resolves inside tmp_path.
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    # Patch Path.exists to always return False so neither candidate is found.
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(FileNotFoundError, match="models.yaml not found"):
        cfg._models_yaml_path()


# ---------------------------------------------------------------------------
# load_model_connections — empty connections (line 109)
# ---------------------------------------------------------------------------


def test_load_model_connections_raises_on_empty_connections(tmp_path, monkeypatch):
    """load_model_connections raises ValueError when yaml has no connections."""
    import pytest

    import canopy.config as cfg

    yaml_file = tmp_path / "models.yaml"
    yaml_file.write_text("connections: []\n")
    monkeypatch.setattr(cfg, "_connections_cache", {})

    with pytest.raises(ValueError, match="no connections defined"):
        cfg.load_model_connections(yaml_file)


# ---------------------------------------------------------------------------
# get_active_connection — model_override path (line 129)
# ---------------------------------------------------------------------------


def test_get_active_connection_model_override(monkeypatch):
    """model_override replaces the models list on the returned connection (line 129).

    Uses _real_get_active_connection captured at module import time (before the
    autouse fixture replaces canopy.config.get_active_connection with a stub).
    """
    import canopy.config as cfg
    from canopy.config import ModelConnection

    base_conn = ModelConnection(
        id="my-backend", backend="azure", api_key="key", models=["gpt-4o"],
        endpoint="https://example.com", timeout=60.0,
    )
    monkeypatch.setattr(cfg, "load_model_connections", lambda: [base_conn])
    monkeypatch.setenv("MODEL_BACKEND", "my-backend")

    conn = _real_get_active_connection(model_override="gpt-4-turbo")
    assert conn.models == ["gpt-4-turbo"]
    assert conn.id == "my-backend"


# ---------------------------------------------------------------------------
# get_data_dir — PermissionError fallback (lines 180-183)
# ---------------------------------------------------------------------------


def test_get_data_dir_falls_back_on_permission_error(tmp_path, monkeypatch):
    """get_data_dir() falls back to ~/.canopy when configured path can't be created."""
    from pathlib import Path

    import canopy.config as cfg

    fallback = tmp_path / "fallback_canopy"
    monkeypatch.setenv("CANOPY_DATA_DIR", "/root/no_permission_here")

    original_mkdir = Path.mkdir

    def _selective_mkdir(self, *args, **kwargs):
        if str(self) == "/root/no_permission_here":
            raise PermissionError("Permission denied")
        if str(self) == str(fallback):
            fallback.mkdir(parents=True, exist_ok=True)
            return
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _selective_mkdir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = cfg.get_data_dir()
    assert result == tmp_path / ".canopy"
