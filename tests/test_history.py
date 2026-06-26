"""Tests for canopy.history — append_history, load_history, clear_history."""

import json
from datetime import datetime, timezone

import pytest

import canopy.history as history_mod
from canopy.history import append_history, clear_history, load_history
from canopy.query.loop import LoopResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_history_file(tmp_path, monkeypatch):
    """Redirect _history_file() to a temp path for every test."""
    fake = tmp_path / ".canopy" / "history.jsonl"
    monkeypatch.setattr(history_mod, "_history_file", lambda: fake)


def _make_result(**overrides) -> LoopResult:
    defaults = dict(
        question="How many detections at Buenaventura?",
        sql="SELECT COUNT(*) FROM detections WHERE site_id = 1",
        columns=["count"],
        rows=[(42,)],
        row_count=1,
        model_text="There are 42 detections at Buenaventura.",
    )
    return LoopResult(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# append_history
# ---------------------------------------------------------------------------


def test_append_creates_parent_and_file():
    result = _make_result()
    append_history(result)
    assert history_mod._history_file().exists()


def test_append_writes_valid_json():
    result = _make_result()
    append_history(result)
    raw = history_mod._history_file().read_text().strip()
    entry = json.loads(raw)
    assert entry["question"] == result.question
    assert entry["sql"] == result.sql
    assert entry["columns"] == result.columns
    assert entry["row_count"] == result.row_count
    assert entry["model_text"] == result.model_text


def test_append_rows_are_lists_not_tuples():
    result = _make_result(rows=[(1, "a"), (2, "b")])
    append_history(result)
    entry = json.loads(history_mod._history_file().read_text())
    assert entry["rows"] == [[1, "a"], [2, "b"]]


def test_append_timestamp_is_utc_iso():
    before = datetime.now(timezone.utc)
    append_history(_make_result())
    entry = json.loads(history_mod._history_file().read_text())
    ts = datetime.fromisoformat(entry["timestamp"])
    assert ts.tzinfo is not None
    assert ts >= before


def test_append_multiple_produces_multiple_lines():
    for i in range(3):
        append_history(_make_result(question=f"q{i}"))
    lines = history_mod._history_file().read_text().splitlines()
    assert len(lines) == 3


def test_append_with_sql_none():
    result = _make_result(sql=None, columns=[], rows=[], row_count=0)
    append_history(result)
    entry = json.loads(history_mod._history_file().read_text())
    assert entry["sql"] is None


def test_append_with_empty_rows():
    result = _make_result(rows=[], row_count=0)
    append_history(result)
    entry = json.loads(history_mod._history_file().read_text())
    assert entry["rows"] == []


# ---------------------------------------------------------------------------
# load_history
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_no_file():
    assert load_history() == []


def test_load_returns_all_entries_when_n_greater_than_total():
    for i in range(3):
        append_history(_make_result(question=f"q{i}"))
    entries = load_history(n=20)
    assert len(entries) == 3


def test_load_returns_last_n_entries_in_order():
    for i in range(5):
        append_history(_make_result(question=f"q{i}"))
    entries = load_history(n=3)
    assert len(entries) == 3
    assert entries[0]["question"] == "q2"
    assert entries[1]["question"] == "q3"
    assert entries[2]["question"] == "q4"


def test_load_n_zero_returns_empty():
    append_history(_make_result())
    assert load_history(n=0) == []


def test_load_skips_corrupt_lines(tmp_path):
    """A single malformed JSON line must not crash load_history."""
    history_mod._history_file().parent.mkdir(parents=True, exist_ok=True)
    append_history(_make_result(question="good before"))
    history_mod._history_file().open("a").write("NOT_VALID_JSON\n")
    append_history(_make_result(question="good after"))
    entries = load_history()
    questions = [e["question"] for e in entries]
    assert "good before" in questions
    assert "good after" in questions


# ---------------------------------------------------------------------------
# clear_history
# ---------------------------------------------------------------------------


def test_clear_removes_file():
    append_history(_make_result())
    clear_history()
    assert not history_mod._history_file().exists()


def test_clear_is_noop_when_no_file():
    clear_history()  # must not raise
    assert not history_mod._history_file().exists()
