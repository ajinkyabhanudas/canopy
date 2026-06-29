"""Tests for canopy.config module."""

from __future__ import annotations

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
