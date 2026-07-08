"""Unit tests for canopy.cache — no DB or API key required."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from canopy.cache import _make_key, clear_cache, lookup_cache, write_cache
from canopy.query.loop import LoopResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(**overrides) -> LoopResult:
    defaults = dict(
        question="Which species were detected?",
        sql="SELECT * FROM species",
        columns=["scientific_name"],
        rows=[("Grallaria gigantea",)],
        row_count=1,
        model_text="One species was detected.",
        timing={"total_s": 5.0, "llm_s": 4.8, "llm_calls": 1, "db_s": 0.1, "db_calls": 1},
    )
    return LoopResult(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# _make_key — normalisation
# ---------------------------------------------------------------------------


def test_make_key_case_insensitive():
    assert _make_key("Which birds?") == _make_key("which birds?")


def test_make_key_whitespace_collapsed():
    assert _make_key("which  birds?") == _make_key("which birds?")


def test_make_key_leading_trailing_stripped():
    assert _make_key("  which birds?  ") == _make_key("which birds?")


def test_make_key_different_questions_differ():
    assert _make_key("Which birds?") != _make_key("Which mammals?")


def test_make_key_nfc_and_nfd_same_key():
    """Same accented text in NFC vs NFD composition must produce the same cache key.

    Without NFC normalisation, a user whose keyboard emits NFD-composed accents
    (base letter + combining diacritic) and one who emits precomposed NFC characters
    would get a cache miss even though they typed the same question.
    """
    import unicodedata
    nfc = unicodedata.normalize("NFC", "¿Cuántas detecciones?")
    nfd = unicodedata.normalize("NFD", "¿Cuántas detecciones?")
    assert nfc != nfd, "NFC and NFD must differ in raw bytes to make this test meaningful"
    assert _make_key(nfc) == _make_key(nfd)


def test_make_key_spanish_and_english_different_keys():
    """Spanish and English semantic equivalents hash to different keys — by design.

    LoopResult.model_text is language-specific. Sharing a cache entry would serve
    an English-language answer to a Spanish-asking user. Separate entries is correct.
    """
    assert _make_key("How many detections?") != _make_key("¿Cuántas detecciones?")


def test_make_key_returns_32_hex_chars():
    key = _make_key("any question")
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


# ---------------------------------------------------------------------------
# lookup_cache — miss cases
# ---------------------------------------------------------------------------


def test_lookup_returns_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("canopy.cache._cache_file", lambda: tmp_path / "cache.json")
    assert lookup_cache("anything") is None


def test_lookup_returns_none_for_missing_key(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"other_key": {}}))
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    assert lookup_cache("my question") is None


def test_lookup_returns_none_for_expired_entry(tmp_path, monkeypatch):
    question = "which species?"
    key = _make_key(question)
    past = datetime.now(timezone.utc) - timedelta(hours=25)
    entry = {
        "question": question,
        "created_at": past.isoformat(),
        "expires_at": past.isoformat(),  # already expired
        "sql": "SELECT 1",
        "columns": ["n"],
        "rows": [[1]],
        "row_count": 1,
        "model_text": "One.",
    }
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({key: entry}))
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    assert lookup_cache(question) is None


def test_lookup_returns_none_for_corrupt_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("not valid json{{")
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    assert lookup_cache("anything") is None


# ---------------------------------------------------------------------------
# lookup_cache — hit
# ---------------------------------------------------------------------------


def test_lookup_returns_loop_result_on_hit(tmp_path, monkeypatch):
    question = "which species?"
    key = _make_key(question)
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    created = datetime.now(timezone.utc)
    entry = {
        "question": question,
        "created_at": created.isoformat(),
        "expires_at": future.isoformat(),
        "sql": "SELECT scientific_name FROM species",
        "columns": ["scientific_name"],
        "rows": [["Grallaria gigantea"]],
        "row_count": 1,
        "model_text": "One species found.",
    }
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({key: entry}))
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)

    result = lookup_cache(question)
    assert result is not None
    assert result.question == question
    assert result.sql == "SELECT scientific_name FROM species"
    assert result.columns == ["scientific_name"]
    assert result.rows == [("Grallaria gigantea",)]
    assert result.row_count == 1
    assert result.model_text == "One species found."


def test_lookup_sets_cache_hit_flag(tmp_path, monkeypatch):
    question = "which species?"
    key = _make_key(question)
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    entry = {
        "question": question,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": future.isoformat(),
        "sql": None,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "model_text": "Nothing.",
    }
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({key: entry}))
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)

    result = lookup_cache(question)
    assert result is not None
    assert result.timing.get("cache_hit") is True
    assert "cached_at" in result.timing


def test_lookup_normalises_question_key(tmp_path, monkeypatch):
    """" Which Species? "  and "which species?" should be the same cache key."""
    question_stored = "which species?"
    question_lookup = "  Which  Species?  "
    key = _make_key(question_stored)
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    entry = {
        "question": question_stored,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": future.isoformat(),
        "sql": "SELECT 1",
        "columns": ["n"],
        "rows": [[1]],
        "row_count": 1,
        "model_text": "One.",
    }
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({key: entry}))
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)

    result = lookup_cache(question_lookup)
    assert result is not None


# ---------------------------------------------------------------------------
# write_cache
# ---------------------------------------------------------------------------


def test_write_creates_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    write_cache(_result())
    assert cache_path.exists()


def test_write_stores_entry(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    r = _result()
    write_cache(r)

    data = json.loads(cache_path.read_text())
    assert len(data) == 1
    key = _make_key(r.question)
    assert key in data
    entry = data[key]
    assert entry["question"] == r.question
    assert entry["sql"] == r.sql
    assert entry["row_count"] == r.row_count


def test_write_then_lookup_roundtrip(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    r = _result()
    write_cache(r)
    result = lookup_cache(r.question)
    assert result is not None
    assert result.model_text == r.model_text
    assert result.rows == r.rows


def test_write_overwrites_existing_key(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)

    write_cache(_result(model_text="First answer."))
    write_cache(_result(model_text="Second answer."))

    data = json.loads(cache_path.read_text())
    assert len(data) == 1  # same key, not duplicated
    entry = list(data.values())[0]
    assert entry["model_text"] == "Second answer."


def test_write_evicts_oldest_at_max_entries(tmp_path, monkeypatch):
    from canopy.cache import _MAX_ENTRIES

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)

    # Pre-fill the cache with MAX_ENTRIES entries using different questions
    pre_data = {}
    for i in range(_MAX_ENTRIES):
        q = f"question number {i}"
        key = _make_key(q)
        ts = (datetime.now(timezone.utc) - timedelta(hours=_MAX_ENTRIES - i)).isoformat()
        pre_data[key] = {
            "question": q,
            "created_at": ts,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "sql": None, "columns": [], "rows": [], "row_count": 0, "model_text": "",
        }
    cache_path.write_text(json.dumps(pre_data))

    # Write one more (different question) — should evict the oldest
    write_cache(_result(question="the new question"))

    data = json.loads(cache_path.read_text())
    assert len(data) == _MAX_ENTRIES
    # The new entry must be present
    assert _make_key("the new question") in data


def test_write_uses_ttl_env_var(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    monkeypatch.setenv("CANOPY_CACHE_TTL_HOURS", "48")

    write_cache(_result())
    data = json.loads(cache_path.read_text())
    entry = list(data.values())[0]
    created = datetime.fromisoformat(entry["created_at"])
    expires = datetime.fromisoformat(entry["expires_at"])
    diff_hours = (expires - created).total_seconds() / 3600
    assert abs(diff_hours - 48) < 0.01


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------


def test_clear_deletes_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{}")
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    clear_cache()
    assert not cache_path.exists()


def test_clear_noop_when_no_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)
    clear_cache()  # must not raise


# ---------------------------------------------------------------------------
# datetime round-trip — cache hit preserves type
# ---------------------------------------------------------------------------


def test_datetime_roundtrip_via_cache(tmp_path, monkeypatch):
    """datetime row values must come back as datetime objects on cache hit, not str."""
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("canopy.cache._cache_file", lambda: cache_path)

    dt_value = datetime(2023, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
    r = _result(
        columns=["recorded_at", "scientific_name"],
        rows=[(dt_value, "Grallaria gigantea")],
        row_count=1,
    )
    write_cache(r)
    result = lookup_cache(r.question)
    assert result is not None
    row = result.rows[0]
    assert isinstance(row[0], datetime), (
        f"Expected datetime on cache hit, got {type(row[0])}: {row[0]!r}"
    )
    assert row[0] == dt_value
    assert row[1] == "Grallaria gigantea"  # non-datetime values unchanged
