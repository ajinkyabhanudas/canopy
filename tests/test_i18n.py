"""Tests for canopy.i18n — locale singleton, t(), and catalog completeness."""

from __future__ import annotations

import pytest

from canopy.i18n import get_lang, set_locale, t, _load_catalog
from canopy.locales import en, es


# ---------------------------------------------------------------------------
# Fixture: restore locale state after any test that mutates it
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_locale():
    original = get_lang()
    yield
    set_locale(original)


# ---------------------------------------------------------------------------
# Catalog completeness — the most important test in this file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", list(en.STRINGS.keys()))
def test_every_en_key_present_in_es(key):
    """Every key in the English catalog must exist in Spanish.

    This test fails CI when a developer adds a new string to en.py
    but forgets to add it to es.py — before it ever ships.
    """
    assert key in es.STRINGS, f"Key {key!r} is missing from es.STRINGS"


def test_en_and_es_catalogs_same_size():
    assert len(en.STRINGS) == len(es.STRINGS), (
        f"Catalog size mismatch: en={len(en.STRINGS)}, es={len(es.STRINGS)}"
    )


# ---------------------------------------------------------------------------
# set_locale / get_lang
# ---------------------------------------------------------------------------


def test_set_locale_en():
    set_locale("en")
    assert get_lang() == "en"


def test_set_locale_es():
    set_locale("es")
    assert get_lang() == "es"


def test_set_locale_unsupported_falls_back_to_en():
    set_locale("xx")
    assert get_lang() == "en"


def test_set_locale_unsupported_does_not_raise():
    set_locale("zz")  # no ImportError bubbles up


# ---------------------------------------------------------------------------
# t() — translate and format
# ---------------------------------------------------------------------------


def test_t_returns_english_string():
    set_locale("en")
    assert t("run_btn") == "Run Query"


def test_t_returns_spanish_string():
    set_locale("es")
    assert t("run_btn") == "Ejecutar consulta"


def test_t_formats_named_substitution():
    set_locale("en")
    result = t("timing_live", total=12.0)
    assert result == "Answer ready in 12s"


def test_t_formats_spanish_substitution():
    set_locale("es")
    result = t("timing_live", total=5.0)
    assert result == "Respuesta lista en 5s"


def test_t_missing_key_falls_back_to_english_value(caplog):
    set_locale("es")
    import canopy.locales.es as es_mod
    original = es_mod.STRINGS.pop("run_btn", None)
    try:
        result = t("run_btn")
        assert result == "Run Query"  # English fallback
        assert "missing translation key" in caplog.text
    finally:
        if original is not None:
            es_mod.STRINGS["run_btn"] = original


def test_t_completely_unknown_key_returns_bracketed_key():
    set_locale("en")
    result = t("__nonexistent_key__")
    assert result == "[__nonexistent_key__]"


# ---------------------------------------------------------------------------
# _load_catalog
# ---------------------------------------------------------------------------


def test_load_catalog_en_returns_dict():
    catalog = _load_catalog("en")
    assert isinstance(catalog, dict)
    assert len(catalog) >= 23


def test_load_catalog_unknown_raises_import_error():
    with pytest.raises(ImportError):
        _load_catalog("zz")
