"""Query result cache — exact-match, TTL-based, LRU-evicted, JSON-backed."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from canopy._json import Encoder

if TYPE_CHECKING:
    from canopy.query.loop import LoopResult

_log = logging.getLogger("canopy.cache")

# Matches ISO-8601 date or datetime strings produced by _Encoder so that
# datetime-typed row values survive the cache round-trip as datetime objects.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?$"
)
_WS_RE = re.compile(r"\s+")

_DEFAULT_TTL_HOURS = 24
_MAX_ENTRIES = 200
_write_lock = threading.Lock()


def _cache_file() -> Path:
    from canopy.config import get_data_dir
    return get_data_dir() / "cache.json"


def _ttl_hours() -> int:
    return int(os.environ.get("CANOPY_CACHE_TTL_HOURS", _DEFAULT_TTL_HOURS))


def _make_key(question: str, connection_id: str = "", model: str = "") -> str:
    q = unicodedata.normalize("NFC", question)
    normalised = _WS_RE.sub(" ", q.casefold().strip())
    payload = f"{connection_id}\x00{model}\x00{normalised}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _read_cache() -> dict:
    path = _cache_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        _log.warning("cache file unreadable or corrupt — starting empty: %s", path)
        return {}


def _write_cache_dict(data: dict) -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, cls=Encoder))
    tmp.rename(path)


def _maybe_datetime(v: object) -> object:
    """Reconstruct datetime objects that were serialised as ISO-8601 strings."""
    if isinstance(v, str) and _ISO_RE.match(v):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
    return v


def _deserialize_interpretation(raw: dict | None) -> object | None:
    """Reconstruct an Interpretation from its cached dict form, or None."""
    from canopy.query.loop import Interpretation

    if raw is None:
        return None
    return Interpretation(
        data_source=raw["data_source"],
        gaps=tuple(raw["gaps"]),
        research_questions=tuple(raw["research_questions"]),
    )


def _deserialize_fuzzy_matches(raw: list | None) -> tuple:
    """Reconstruct FuzzyMatch tuples from their cached list-of-dicts form."""
    from canopy.query.fuzzy_match import FuzzyMatch

    if not raw:
        return ()
    return tuple(
        FuzzyMatch(
            literal=item["literal"],
            candidates=tuple(item["candidates"]),
            label_key=item["label_key"],
        )
        for item in raw
    )


def lookup_cache(question: str, connection_id: str = "", model: str = "") -> LoopResult | None:
    """Return a cached LoopResult for question, or None on miss/expiry."""
    from canopy.query.loop import LoopResult

    key = _make_key(question, connection_id, model)
    data = _read_cache()
    entry = data.get(key)
    if entry is None:
        return None

    expires_at = datetime.fromisoformat(entry["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        _log.debug("cache expired for key %s", key)
        return None

    _log.debug("cache hit for key %s", key)
    return LoopResult(
        question=entry["question"],
        sql=entry["sql"],
        columns=tuple(entry["columns"]),
        rows=tuple(tuple(_maybe_datetime(v) for v in row) for row in entry["rows"]),
        row_count=entry["row_count"],
        model_text=entry["model_text"],
        timing={"cache_hit": True, "cached_at": entry["created_at"]},
        interpretation=_deserialize_interpretation(entry.get("interpretation")),
        fuzzy_matches=_deserialize_fuzzy_matches(entry.get("fuzzy_matches")),
    )


def write_cache(result: LoopResult, connection_id: str = "", model: str = "") -> None:
    """Write a LoopResult to the cache, evicting oldest/expired entries beyond _MAX_ENTRIES."""
    key = _make_key(result.question, connection_id, model)
    with _write_lock:
        data = _read_cache()

        # Prune expired entries first so capacity check reflects live entries only.
        now = datetime.now(timezone.utc)
        expired = [k for k, v in data.items() if datetime.fromisoformat(v["expires_at"]) <= now]
        for k in expired:
            del data[k]

        # LRU eviction: remove oldest entries if still at capacity after expiry pruning.
        if len(data) >= _MAX_ENTRIES:
            sorted_keys = sorted(data, key=lambda k: data[k].get("created_at", ""))
            for old_key in sorted_keys[: len(data) - _MAX_ENTRIES + 1]:
                del data[old_key]

        data[key] = {
            "question": result.question,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=_ttl_hours())).isoformat(),
            "sql": result.sql,
            "columns": result.columns,
            "rows": [list(row) for row in result.rows],
            "row_count": result.row_count,
            "model_text": result.model_text,
            "interpretation": (
                {
                    "data_source": result.interpretation.data_source,
                    "gaps": list(result.interpretation.gaps),
                    "research_questions": list(result.interpretation.research_questions),
                }
                if result.interpretation is not None
                else None
            ),
            "fuzzy_matches": [
                {
                    "literal": m.literal,
                    "candidates": list(m.candidates),
                    "label_key": m.label_key,
                }
                for m in result.fuzzy_matches
            ],
        }
        _write_cache_dict(data)


def clear_cache() -> None:
    """Delete the cache file. No-op if it does not exist."""
    path = _cache_file()
    if path.exists():
        path.unlink()
