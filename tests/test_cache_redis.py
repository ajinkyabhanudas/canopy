"""Unit tests for canopy.cache's Redis backend — uses fakeredis, no real Redis needed.

These mirror test_cache.py's structure but exercise the Redis code path
(CANOPY_REDIS_URL set) rather than the file backend.
"""

from __future__ import annotations

import fakeredis
import pytest

from canopy.query.loop import Interpretation, LoopResult


def _result(**overrides) -> LoopResult:
    defaults = dict(
        question="Which species were detected?",
        sql="SELECT * FROM species",
        columns=("scientific_name",),
        rows=(("Grallaria gigantea",),),
        row_count=1,
        model_text="One species was detected.",
        timing={"total_s": 5.0},
    )
    merged = {**defaults, **overrides}
    return LoopResult(**merged)


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """Enable the Redis path and back it with an in-memory fakeredis instance."""
    import canopy.cache as cache_mod

    monkeypatch.setenv("CANOPY_REDIS_URL", "redis://fake:6379/0")
    cache_mod._reset_redis_client()
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(cache_mod, "_redis_client", lambda: fake)
    yield fake
    cache_mod._reset_redis_client()


def test_redis_lookup_miss_returns_none():
    from canopy.cache import lookup_cache

    assert lookup_cache("anything") is None


def test_redis_write_then_lookup_roundtrip():
    from canopy.cache import lookup_cache, write_cache

    r = _result()
    write_cache(r)
    result = lookup_cache(r.question)
    assert result is not None
    assert result.model_text == r.model_text
    assert result.rows == r.rows
    assert result.timing.get("cache_hit") is True


def test_redis_write_then_lookup_preserves_interpretation():
    from canopy.cache import lookup_cache, write_cache

    interp = Interpretation(
        data_source="detections · approved only",
        gaps=("Some species absent",),
        research_questions=("Do counts match last year?",),
    )
    r = _result(interpretation=interp)
    write_cache(r)
    result = lookup_cache(r.question)
    assert result is not None
    assert result.interpretation == interp


def test_redis_ttl_is_set_on_write(_fake_redis):
    from canopy.cache import _REDIS_KEY_PREFIX, _make_key, write_cache

    r = _result()
    write_cache(r)
    key = _REDIS_KEY_PREFIX + _make_key(r.question)
    ttl = _fake_redis.ttl(key)
    assert 0 < ttl <= 24 * 3600


def test_redis_write_uses_ttl_env_var(monkeypatch, _fake_redis):
    from canopy.cache import _REDIS_KEY_PREFIX, _make_key, write_cache

    monkeypatch.setenv("CANOPY_CACHE_TTL_HOURS", "1")
    r = _result()
    write_cache(r)
    key = _REDIS_KEY_PREFIX + _make_key(r.question)
    ttl = _fake_redis.ttl(key)
    assert 0 < ttl <= 3600


def test_redis_overwrite_updates_value():
    from canopy.cache import lookup_cache, write_cache

    write_cache(_result(model_text="First answer."))
    write_cache(_result(model_text="Second answer."))
    result = lookup_cache("Which species were detected?")
    assert result.model_text == "Second answer."


def test_redis_clear_cache_removes_entries(_fake_redis):
    from canopy.cache import clear_cache, lookup_cache, write_cache

    write_cache(_result())
    clear_cache()
    assert lookup_cache("Which species were detected?") is None


def test_redis_unreachable_on_lookup_falls_back_to_file(tmp_path, monkeypatch):
    """A Redis connection error at lookup time must degrade to the file backend,
    not raise — same fail-safe shape as is_langfuse_enabled()."""
    import canopy.cache as cache_mod

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(cache_mod, "_cache_file", lambda: cache_path)

    class _BrokenClient:
        def get(self, key):
            raise ConnectionError("redis down")

    monkeypatch.setattr(cache_mod, "_redis_client", lambda: _BrokenClient())

    # Must not raise, must behave like a miss against the (empty) file backend.
    assert cache_mod.lookup_cache("anything") is None


def test_redis_unreachable_on_write_falls_back_to_file(tmp_path, monkeypatch):
    """A Redis connection error at write time must fall back to writing the file
    backend instead — never silently drop the write, never crash run_query()."""
    import json

    import canopy.cache as cache_mod

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(cache_mod, "_cache_file", lambda: cache_path)

    class _BrokenClient:
        def set(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(cache_mod, "_redis_client", lambda: _BrokenClient())

    cache_mod.write_cache(_result())
    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert len(data) == 1
