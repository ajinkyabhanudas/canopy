"""Query result cache — exact-match, TTL-based, backed by Redis or a JSON file.

Redis is used when CANOPY_REDIS_URL is set (see config.is_redis_cache_enabled());
otherwise, and on any Redis connection failure, falls back to the original
file-based backend (LRU-evicted, JSON-backed) — same fail-safe shape as
config.is_langfuse_enabled(). The file backend's implementation is unchanged
so existing tests that monkeypatch _cache_file()/_read_cache() keep working
against it directly.
"""

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
from canopy.config import get_redis_url, is_redis_cache_enabled

if TYPE_CHECKING:
    import redis

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


def _entry_to_result(entry: dict) -> LoopResult:
    """Shared deserialization: cache entry dict -> LoopResult. Used by both backends."""
    from canopy.query.loop import LoopResult

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


def _build_entry(result: LoopResult, now: datetime) -> dict:
    """Shared serialization: LoopResult -> cache entry dict. Used by both backends."""
    return {
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


# ---------------------------------------------------------------------------
# File backend — original implementation, unchanged behavior
# ---------------------------------------------------------------------------


def _lookup_cache_file(key: str) -> dict | None:
    data = _read_cache()
    entry = data.get(key)
    if entry is None:
        return None
    expires_at = datetime.fromisoformat(entry["expires_at"])
    if datetime.now(timezone.utc) >= expires_at:
        _log.debug("cache expired for key %s", key)
        return None
    return entry


def _write_cache_file(key: str, entry: dict) -> None:
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

        data[key] = entry
        _write_cache_dict(data)


# ---------------------------------------------------------------------------
# Redis backend — used when config.is_redis_cache_enabled() is True. Falls
# back to the file backend on any connection error at call time, per
# config.is_langfuse_enabled()'s fail-safe shape: an unreachable Redis
# degrades to today's behavior rather than crashing run_query().
# ---------------------------------------------------------------------------

_REDIS_KEY_PREFIX = "canopy:cache:"
_redis_client_singleton = None


def _redis_client() -> "redis.Redis":
    """Return a lazily-constructed, module-level redis client."""
    global _redis_client_singleton
    if _redis_client_singleton is None:
        import redis  # lazy import — only needed when Redis caching is enabled

        _redis_client_singleton = redis.Redis.from_url(get_redis_url(), socket_timeout=2)
    return _redis_client_singleton


def _reset_redis_client() -> None:
    """Discard the cached client. Used by tests to force a fresh client per test."""
    global _redis_client_singleton
    _redis_client_singleton = None


def _lookup_cache_redis(key: str) -> dict | None:
    try:
        raw = _redis_client().get(_REDIS_KEY_PREFIX + key)
    except Exception:
        _log.warning(
            "Redis unreachable on cache lookup — falling back to file cache", exc_info=True
        )
        return _lookup_cache_file(key)
    if raw is None:
        return None
    return json.loads(raw)


def _write_cache_redis(key: str, entry: dict, ttl_seconds: int) -> None:
    try:
        _redis_client().set(
            _REDIS_KEY_PREFIX + key, json.dumps(entry, cls=Encoder), ex=ttl_seconds
        )
    except Exception:
        _log.warning("Redis unreachable on cache write — falling back to file cache", exc_info=True)
        _write_cache_file(key, entry)


# ---------------------------------------------------------------------------
# Public API — dispatches to the active backend
# ---------------------------------------------------------------------------


def lookup_cache(question: str, connection_id: str = "", model: str = "") -> LoopResult | None:
    """Return a cached LoopResult for question, or None on miss/expiry."""
    key = _make_key(question, connection_id, model)
    entry = (
        _lookup_cache_redis(key) if is_redis_cache_enabled() else _lookup_cache_file(key)
    )
    if entry is None:
        return None
    _log.debug("cache hit for key %s", key)
    return _entry_to_result(entry)


def write_cache(result: LoopResult, connection_id: str = "", model: str = "") -> None:
    """Write a LoopResult to the cache.

    File backend: evicts oldest/expired entries beyond _MAX_ENTRIES (LRU).
    Redis backend: relies on native key TTL + the container's own
    maxmemory-policy allkeys-lru — no hand-rolled eviction bookkeeping needed.
    """
    key = _make_key(result.question, connection_id, model)
    now = datetime.now(timezone.utc)
    entry = _build_entry(result, now)
    if is_redis_cache_enabled():
        _write_cache_redis(key, entry, ttl_seconds=_ttl_hours() * 3600)
    else:
        _write_cache_file(key, entry)


def clear_cache() -> None:
    """Clear the active cache backend. No-op if there is nothing to clear."""
    if is_redis_cache_enabled():
        try:
            client = _redis_client()
            keys = list(client.scan_iter(match=_REDIS_KEY_PREFIX + "*"))
            if keys:
                client.delete(*keys)
        except Exception:
            _log.warning("Redis unreachable on cache clear", exc_info=True)
        return
    path = _cache_file()
    if path.exists():
        path.unlink()
