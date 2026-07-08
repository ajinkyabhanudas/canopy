"""Tests for canopy.config module."""

from __future__ import annotations

import textwrap

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
