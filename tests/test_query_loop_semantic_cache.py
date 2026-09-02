"""Integration tests for the semantic-cache wiring inside run_query() (loop.py).

Mirrors test_query_loop.py's mocking style: _run_agent is mocked so no real
model/DB call happens. canopy.semantic_cache itself is unit-tested separately
in test_semantic_cache.py — these tests only verify loop.py's wiring: does a
semantic hit skip the agent, does a miss still write to the semantic cache,
is the feature a true no-op when disabled.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from canopy.query.executor import QueryResult
from canopy.query.loop import run_query
from canopy.semantic_cache import SemanticHit


@pytest.fixture(autouse=True)
def _bypass_exact_cache(monkeypatch):
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q, **_kw: None)
    monkeypatch.setattr("canopy.query.loop.write_cache", lambda r, **_kw: None)


def _agent_mock(model_text="Found 1 result.", sql="SELECT 1"):
    query_result = QueryResult(columns=("n",), rows=((1,),), row_count=1)

    async def _mock(question, status_cb, state, conn_id, active_model, *_a):
        state["last_sql"] = sql
        state["last_query_result"] = query_result
        state["llm_times"] = [0.5]
        state["db_times"] = [0.05]
        return model_text

    return _mock


def test_semantic_hit_skips_agent_call(monkeypatch):
    """When the semantic cache reports a hit, _run_agent must never be invoked —
    only the SQL is re-executed live."""
    monkeypatch.setattr("canopy.query.loop.is_semantic_cache_enabled", lambda: True)
    monkeypatch.setattr(
        "canopy.query.loop.semantic_cache.lookup",
        lambda q, connection_id: SemanticHit(sql="SELECT 1", question="similar question"),
    )
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.description = [("n",)]
    mock_conn.cursor.return_value.fetchall.return_value = [(1,)]
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    monkeypatch.setattr("canopy.query.executor.release_connection", lambda c: None)

    def _should_not_be_called(*a, **kw):
        raise AssertionError("_run_agent should not be called on a semantic cache hit")

    with patch("canopy.query.loop._run_agent", new=_should_not_be_called):
        result = run_query("how many detections")

    assert result.sql == "SELECT 1"
    assert result.row_count == 1
    assert result.timing.get("semantic_cache_hit") is True
    assert "current" in result.model_text.lower()


def test_semantic_hit_fires_status_and_trace_id_callbacks(monkeypatch):
    """A semantic hit must fire status_cb("CACHE_HIT") and trace_id_cb, same as
    an exact-match hit — these are the two branches only exercised when a caller
    actually passes callbacks, which the other semantic-hit test doesn't."""
    monkeypatch.setattr("canopy.query.loop.is_semantic_cache_enabled", lambda: True)
    monkeypatch.setattr(
        "canopy.query.loop.semantic_cache.lookup",
        lambda q, connection_id: SemanticHit(sql="SELECT 1", question="similar question"),
    )
    monkeypatch.setattr("canopy.query.loop.trace_query", lambda **_kw: "trace-semantic-hit")
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.description = [("n",)]
    mock_conn.cursor.return_value.fetchall.return_value = [(1,)]
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    monkeypatch.setattr("canopy.query.executor.release_connection", lambda c: None)

    statuses = []
    trace_ids = []

    def _should_not_be_called(*a, **kw):
        raise AssertionError("_run_agent should not be called on a semantic cache hit")

    with patch("canopy.query.loop._run_agent", new=_should_not_be_called):
        result = run_query(
            "how many detections",
            status_cb=statuses.append,
            trace_id_cb=trace_ids.append,
        )

    assert result.sql == "SELECT 1"
    assert statuses == ["CACHE_HIT"]
    assert trace_ids == ["trace-semantic-hit"]


def test_semantic_disabled_by_default_falls_through_to_agent(monkeypatch):
    """With the flag off (the default), run_query() must behave exactly as before —
    the semantic cache is never consulted."""
    monkeypatch.setattr("canopy.query.loop.is_semantic_cache_enabled", lambda: False)
    called = []
    monkeypatch.setattr(
        "canopy.query.loop.semantic_cache.lookup",
        lambda *a, **kw: called.append("lookup") or None,
    )

    with patch("canopy.query.loop._run_agent", new=_agent_mock()):
        result = run_query("how many detections")

    assert called == []
    assert result.sql == "SELECT 1"


def test_semantic_miss_falls_through_to_agent_and_writes(monkeypatch):
    monkeypatch.setattr("canopy.query.loop.is_semantic_cache_enabled", lambda: True)
    monkeypatch.setattr("canopy.query.loop.semantic_cache.lookup", lambda *a, **kw: None)
    written = []
    monkeypatch.setattr(
        "canopy.query.loop.semantic_cache.write",
        lambda question, sql, connection_id: written.append((question, sql)),
    )

    with patch("canopy.query.loop._run_agent", new=_agent_mock(sql="SELECT 1")):
        result = run_query("how many detections")

    assert result.sql == "SELECT 1"
    assert written == [("how many detections", "SELECT 1")]


def test_semantic_hit_reexecution_failure_falls_back_to_agent(monkeypatch):
    """If the cached SQL fails to re-execute (schema drift, transient DB error),
    fall back to a full agent run rather than surfacing the error."""
    monkeypatch.setattr("canopy.query.loop.is_semantic_cache_enabled", lambda: True)
    monkeypatch.setattr(
        "canopy.query.loop.semantic_cache.lookup",
        lambda q, connection_id: SemanticHit(sql="SELECT 1", question="similar question"),
    )
    monkeypatch.setattr("canopy.query.loop.semantic_cache.write", lambda *a, **kw: None)

    def _broken_execute(sql):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr("canopy.query.loop.execute_query", _broken_execute)

    with patch("canopy.query.loop._run_agent", new=_agent_mock(sql="SELECT 2")):
        result = run_query("how many detections")

    assert result.sql == "SELECT 2"  # came from the fallback agent run, not the broken hit
