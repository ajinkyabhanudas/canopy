"""Minimal locale support for Canopy. No external dependencies.

Locale is a process-singleton initialised once at startup from CANOPY_UI_LANG
(via set_locale). Falls back to English on unknown locale or missing key.
"""

from __future__ import annotations

import importlib
import logging

_log = logging.getLogger("canopy.i18n")
_lang: str = "en"
_catalog: dict[str, str] = {}


def _load_catalog(lang: str) -> dict[str, str]:
    mod = importlib.import_module(f"canopy.locales.{lang}")
    return mod.STRINGS


def set_locale(lang: str) -> None:
    global _lang, _catalog
    try:
        _catalog = _load_catalog(lang)
        _lang = lang
    except (ImportError, AttributeError):
        _log.warning("unsupported locale %r — falling back to 'en'", lang)
        _catalog = _load_catalog("en")
        _lang = "en"


def get_lang() -> str:
    return _lang


def t(key: str, **subs: object) -> str:
    try:
        template = _catalog[key]
    except KeyError:
        _log.warning("missing translation key %r in locale %r", key, _lang)
        from canopy.locales import en
        template = en.STRINGS.get(key, f"[{key}]")
    return template.format(**subs) if subs else template
