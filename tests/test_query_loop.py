"""
Tests for canopy.query.loop — no live DB, no real model calls.

Architecture note: The hand-rolled model loop was replaced by a LlamaIndex
FunctionAgent in Phase 2. Tests now mock at two levels:

  1. _run_agent() — for all tests that verify loop orchestration (cache,
     history, LoopResult shape, status callbacks). The async agent call is
     replaced by a synchronous mock that populates the state dict directly.

  2. _format_result() / execute_query() — for tests that verify the security
     layer and result formatting. No model mock needed.

Tests that previously verified internal message formatting (format_tool_results,
format_assistant_turn call counts) are not reproduced here — those were testing
the hand-rolled loop's internal wiring, which is now LlamaIndex's responsibility.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from canopy.i18n import t
from canopy.query.executor import QueryResult
from canopy.query.loop import (
    LoopResult,
    _format_result,
    _load_sensitive_columns,
    run_query,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_cache(monkeypatch):
    """Prevent real cache reads/writes from interfering with loop unit tests."""
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: None)
    monkeypatch.setattr("canopy.query.loop.write_cache", lambda r, **_kw: None)


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection that returns one column and one row."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.description = [("n",)]
    cursor.fetchall.return_value = [(1,)]
    return conn


def _make_agent_mock(
    model_text: str = "Found 1 result.",
    sql: str = "SELECT 1",
    query_result: QueryResult | None = None,
) -> AsyncMock:
    """Return an async mock for _run_agent that populates the state dict."""
    if query_result is None:
        query_result = QueryResult(columns=("n",), rows=((1,),), row_count=1)

    async def _mock_run_agent(question, status_cb, state, conn_id, active_model):
        state["last_sql"] = sql
        state["last_query_result"] = query_result
        state["llm_times"] = [0.5]
        state["db_times"] = [0.05]
        if status_cb:
            status_cb(t("status_understanding"))
            status_cb(t("status_searching_db"))
            n = query_result.row_count
            key = "found_detections_singular" if n == 1 else "found_detections_plural"
            status_cb(t(key, n=n))
        return model_text

    return _mock_run_agent


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_single_tool_call_round_trip(monkeypatch, mock_conn):
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Found 1 result.", "SELECT 1")):
        result = run_query("How many rows are in the detections table?")

    assert result.question == "How many rows are in the detections table?"
    assert result.sql == "SELECT 1"
    assert result.row_count == 1
    assert result.rows == ((1,),)
    assert result.columns == ("n",)
    assert result.model_text == "Found 1 result."


def test_direct_text_response_no_sql(monkeypatch):
    """Agent declines to call the tool — no SQL executed."""
    async def _no_sql_agent(question, status_cb, state, conn_id, active_model):
        # state left empty: no SQL, no query result
        if status_cb:
            status_cb(t("status_understanding"))
        return "I cannot answer that from this database."

    with patch("canopy.query.loop._run_agent", new=_no_sql_agent):
        result = run_query("What is the conservation status of the giant antpitta?")

    assert result.sql is None
    assert result.row_count == 0
    assert result.columns == ()
    assert result.rows == ()
    assert result.model_text == "I cannot answer that from this database."


def test_last_sql_wins_when_multiple_queries(monkeypatch, mock_conn):
    """When agent executes multiple queries, the last SQL is recorded."""
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    with patch(
        "canopy.query.loop._run_agent",
        new=_make_agent_mock("Done.", sql="SELECT 2"),
    ):
        result = run_query("Run two queries.")

    assert result.sql == "SELECT 2"
    assert result.model_text == "Done."


# ---------------------------------------------------------------------------
# _format_result — tested directly (no model or DB mock needed)
# ---------------------------------------------------------------------------


def test_format_result_truncates_at_200_rows():
    result = QueryResult(
        columns=("id",),
        rows=tuple((i,) for i in range(250)),
        row_count=250,
    )
    text = _format_result(result)
    assert "50 more rows truncated" in text


def test_format_result_strips_sensitive_columns():
    result = QueryResult(
        columns=("scientific_name", "site", "latitude", "longitude"),
        rows=(("Grallaria gigantea", "Buenaventura", -1.23, -78.45),),
        row_count=1,
    )
    text = _format_result(result)
    assert "latitude" not in text
    assert "longitude" not in text
    assert "-1.23" not in text
    assert "-78.45" not in text
    assert "scientific_name" in text
    assert "site" in text


def test_format_result_empty_rows():
    result = QueryResult(columns=("id",), rows=(), row_count=0)
    text = _format_result(result)
    assert "Row count: 0" in text


def test_format_result_includes_columns_and_row_count():
    result = QueryResult(
        columns=("species", "count"),
        rows=(("Grallaria gigantea", 42),),
        row_count=1,
    )
    text = _format_result(result)
    assert "Columns: species, count" in text
    assert "Row count: 1" in text


# ---------------------------------------------------------------------------
# CANOPY_SENSITIVE_COLUMNS env var override
# ---------------------------------------------------------------------------


def test_sensitive_columns_env_override(monkeypatch):
    """CANOPY_SENSITIVE_COLUMNS env var overrides the default set."""
    monkeypatch.setenv("CANOPY_SENSITIVE_COLUMNS", "secret_col")
    sensitive = _load_sensitive_columns()
    assert "secret_col" in sensitive
    assert "latitude" not in sensitive
    assert "longitude" not in sensitive


def test_sensitive_columns_env_override_end_to_end(monkeypatch):
    """When env var strips secret_col but not lat/lon, format_result reflects this."""
    import canopy.query.loop as loop_mod

    monkeypatch.setenv("CANOPY_SENSITIVE_COLUMNS", "secret_col")
    monkeypatch.setattr(loop_mod, "_SENSITIVE_COLUMNS", loop_mod._load_sensitive_columns())

    result = QueryResult(
        columns=("scientific_name", "latitude", "secret_col"),
        rows=(("Grallaria gigantea", -1.23, "TOP_SECRET"),),
        row_count=1,
    )
    text = _format_result(result)
    assert "secret_col" not in text
    assert "TOP_SECRET" not in text
    assert "latitude" in text
    assert "-1.23" in text


# ---------------------------------------------------------------------------
# Cache integration
# ---------------------------------------------------------------------------


def test_cache_hit_skips_agent_call(monkeypatch):
    """If lookup_cache returns a hit, _run_agent must never be called."""
    cached = LoopResult(
        question="How many detections?",
        sql="SELECT COUNT(*) FROM detections",
        columns=("count",),
        rows=((42,),),
        row_count=42,
        model_text="There are 42 detections.",
        timing={"cache_hit": True, "cached_at": "2026-06-26T00:00:00+00:00"},
    )
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: cached)
    agent_called = []

    async def _should_not_be_called(*args, **kwargs):
        agent_called.append(True)
        return "should not reach here"

    with patch("canopy.query.loop._run_agent", new=_should_not_be_called):
        result = run_query("How many detections?")

    assert not agent_called
    assert result.row_count == 42
    assert result.timing.get("cache_hit") is True


def test_cache_miss_writes_result(monkeypatch):
    """On a cache miss, write_cache is called with the LoopResult."""
    written: list = []
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: None)
    monkeypatch.setattr("canopy.query.loop.write_cache", lambda r, **_kw: written.append(r))

    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.", "SELECT 1")):
        run_query("A fresh question.")

    assert len(written) == 1
    assert written[0].model_text == "Done."


def test_cache_write_failure_logs_warning_and_still_returns(monkeypatch):
    """A cache write failure must not propagate — result still returned."""
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: None)
    monkeypatch.setattr(
        "canopy.query.loop.write_cache",
        lambda r, **_kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.", "SELECT 1")):
        result = run_query("A question.")
    assert result.model_text == "Done."


def test_history_write_failure_logs_warning_and_still_returns(monkeypatch):
    """A history write failure must not propagate — result still returned."""
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: None)
    monkeypatch.setattr("canopy.query.loop.write_cache", lambda r, **_kw: None)
    monkeypatch.setattr(
        "canopy.query.loop.append_history",
        lambda r: (_ for _ in ()).throw(OSError("disk full")),
    )
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.", "SELECT 1")):
        result = run_query("A question.")
    assert result.model_text == "Done."


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_loop_result_is_immutable(monkeypatch):
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("No data.")):
        result = run_query("Anything.")

    with pytest.raises(Exception):  # FrozenInstanceError
        result.model_text = "hacked"  # type: ignore[misc]


def test_loop_result_rows_are_tuples(monkeypatch):
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.", "SELECT 1")):
        result = run_query("Anything.")
    assert isinstance(result.rows, tuple)
    assert isinstance(result.columns, tuple)


# ---------------------------------------------------------------------------
# Status callbacks
# ---------------------------------------------------------------------------


def test_status_cb_emits_understanding(monkeypatch):
    statuses: list[str] = []
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.")):
        run_query("How many detections?", status_cb=statuses.append)
    assert t("status_understanding") in statuses


def test_status_cb_emits_searching_db(monkeypatch):
    statuses: list[str] = []
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.")):
        run_query("A question.", status_cb=statuses.append)
    assert t("status_searching_db") in statuses


def test_status_cb_emits_plural_count(monkeypatch):
    qr = QueryResult(columns=("n",), rows=tuple((i,) for i in range(5)), row_count=5)
    statuses: list[str] = []
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("Done.", query_result=qr)):
        run_query("Five rows.", status_cb=statuses.append)
    assert t("found_detections_plural", n=5) in statuses


def test_status_cb_emits_singular_count(monkeypatch):
    qr = QueryResult(columns=("n",), rows=((1,),), row_count=1)
    statuses: list[str] = []
    with patch("canopy.query.loop._run_agent", new=_make_agent_mock("One.", query_result=qr)):
        run_query("One row.", status_cb=statuses.append)
    assert t("found_detections_singular", n=1) in statuses


def test_status_cb_emits_cache_hit(monkeypatch):
    cached = LoopResult(
        question="cached question",
        sql="SELECT 1",
        columns=("n",),
        rows=((1,),),
        row_count=1,
        model_text="One.",
        timing={"cache_hit": True, "cached_at": "2026-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: cached)
    statuses: list[str] = []

    async def _noop(*args, **kwargs):
        return ""

    with patch("canopy.query.loop._run_agent", new=_noop):
        run_query("cached question", status_cb=statuses.append)

    assert "CACHE_HIT" in statuses

