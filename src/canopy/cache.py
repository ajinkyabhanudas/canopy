"""Query result cache — exact-match, TTL-based, LRU-evicted, JSON-backed."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canopy.query.loop import LoopResult

_log = logging.getLogger("canopy.cache")

_DEFAULT_TTL_HOURS = 24
_MAX_ENTRIES = 200


def _cache_file() -> Path:
    from canopy.config import get_data_dir
    return get_data_dir() / "cache.json"


def _ttl_hours() -> int:
    return int(os.environ.get("CANOPY_CACHE_TTL_HOURS", _DEFAULT_TTL_HOURS))


def _make_key(question: str) -> str:
    normalised = re.sub(r"\s+", " ", question.casefold().strip())
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


def _read_cache() -> dict:
    path = _cache_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_cache_dict(data: dict) -> None:
    path = _cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def lookup_cache(question: str) -> LoopResult | None:
    """Return a cached LoopResult for question, or None on miss/expiry."""
    from canopy.query.loop import LoopResult

    key = _make_key(question)
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
        columns=entry["columns"],
        rows=[tuple(row) for row in entry["rows"]],
        row_count=entry["row_count"],
        model_text=entry["model_text"],
        timing={"cache_hit": True, "cached_at": entry["created_at"]},
    )


def write_cache(result: LoopResult) -> None:
    """Write a LoopResult to the cache, evicting oldest entries beyond _MAX_ENTRIES."""
    key = _make_key(result.question)
    data = _read_cache()

    # LRU eviction: remove oldest entries if at capacity
    if len(data) >= _MAX_ENTRIES:
        sorted_keys = sorted(data, key=lambda k: data[k].get("created_at", ""))
        for old_key in sorted_keys[: len(data) - _MAX_ENTRIES + 1]:
            del data[old_key]

    now = datetime.now(timezone.utc)
    data[key] = {
        "question": result.question,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=_ttl_hours())).isoformat(),
        "sql": result.sql,
        "columns": result.columns,
        "rows": [list(row) for row in result.rows],
        "row_count": result.row_count,
        "model_text": result.model_text,
    }
    _write_cache_dict(data)


def clear_cache() -> None:
    """Delete the cache file. No-op if it does not exist."""
    path = _cache_file()
    if path.exists():
        path.unlink()
