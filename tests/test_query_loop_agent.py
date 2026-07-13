"""Unit tests for the LlamaIndex agent internals in loop.py.

Covers _build_sql_tool and _run_agent — the two functions that
test_query_loop.py skips by mocking _run_agent at the boundary.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from canopy.query.executor import QueryResult
from canopy.query.loop import _build_sql_tool, _run_agent


def _make_state() -> dict:
    return {
        "db_times": [],
        "last_sql": None,
        "last_query_result": None,
        "llm_times": [],
        "iterations": 0,
    }


def _make_query_result(rows: tuple = ((1,),), columns: tuple = ("n",)) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, row_count=len(rows))


# ---------------------------------------------------------------------------
# _build_sql_tool — the FunctionTool closure
# ---------------------------------------------------------------------------


def test_build_sql_tool_name():
    tool = _build_sql_tool(None, _make_state())
    assert tool.metadata.name == "execute_sql"


def test_build_sql_tool_executes_query():
    state = _make_state()
    qr = _make_query_result()
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        result = tool.fn(sql="SELECT 1")
    assert "SELECT 1" not in result  # result is formatted output, not raw SQL
    assert "Columns: n" in result
    assert state["last_sql"] == "SELECT 1"
    assert state["last_query_result"] is qr
    assert len(state["db_times"]) == 1


def test_build_sql_tool_populates_state():
    state = _make_state()
    qr = _make_query_result(rows=((42,),))
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        tool.fn(sql="SELECT 42")
    assert state["last_sql"] == "SELECT 42"
    assert state["last_query_result"].rows == ((42,),)


def test_build_sql_tool_calls_status_cb_before_and_after():
    state = _make_state()
    calls: list[str] = []
    qr = _make_query_result(rows=((5,), (6,)))
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(lambda msg: calls.append(msg), state)
        tool.fn(sql="SELECT n FROM t")
    assert any("search" in c.lower() or "status" in c.lower() for c in calls)
    assert len(calls) == 2  # before DB call + after (found N records)


def test_build_sql_tool_singular_row_message():
    state = _make_state()
    calls: list[str] = []
    qr = _make_query_result(rows=((1,),))
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(lambda msg: calls.append(msg), state)
        tool.fn(sql="SELECT 1")
    # Second status message should mention singular count
    assert any("1" in c for c in calls)


def test_build_sql_tool_no_status_cb():
    """No status_cb — tool must still work correctly."""
    state = _make_state()
    qr = _make_query_result()
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        result = tool.fn(sql="SELECT 1")
    assert "Row count: 1" in result


def test_build_sql_tool_formats_result_output():
    state = _make_state()
    qr = QueryResult(
        columns=("species", "count"),
        rows=(("Grallaria gigantea", 5), ("Puma concolor", 3)),
        row_count=2,
    )
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        result = tool.fn(sql="SELECT species, count FROM t")
    assert "Columns: species, count" in result
    assert "Row count: 2" in result


def test_build_sql_tool_strips_sensitive_columns():
    """latitude and longitude must not appear in the tool's output."""
    state = _make_state()
    qr = QueryResult(
        columns=("site", "latitude", "longitude"),
        rows=(("Buenaventura", -0.45, -77.99),),
        row_count=1,
    )
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        result = tool.fn(sql="SELECT site, latitude, longitude FROM detections")
    assert "latitude" not in result
    assert "longitude" not in result
    assert "Buenaventura" in result


# ---------------------------------------------------------------------------
# _run_agent
# ---------------------------------------------------------------------------


def _mock_agent_run(text: str = "Agent answer"):
    """Return a FunctionAgent mock whose .run() returns an awaitable response."""
    mock_response = MagicMock()
    mock_response.__str__ = lambda self: text
    mock_agent = MagicMock()
    mock_agent.run.return_value = AsyncMock(return_value=mock_response)()
    return mock_agent


def test_run_agent_returns_string():
    state = _make_state()
    mock_llm = MagicMock()
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=mock_llm):
        mock_fa_cls.return_value = _mock_agent_run("Species found: 3")
        result = asyncio.run(
            _run_agent("how many species?", None, state, "conn-1", "model-1")
        )
    assert result == "Species found: 3"


def test_run_agent_populates_llm_times():
    state = _make_state()
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        mock_fa_cls.return_value = _mock_agent_run("done")
        asyncio.run(_run_agent("q", None, state, "c", "m"))
    assert len(state["llm_times"]) == 1
    assert state["llm_times"][0] >= 0.0


def test_run_agent_calls_status_cb_understanding():
    state = _make_state()
    calls: list[str] = []
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        mock_fa_cls.return_value = _mock_agent_run("done")
        asyncio.run(_run_agent("q", lambda msg: calls.append(msg), state, "c", "m"))
    assert len(calls) == 1
    assert calls[0]  # some status message was emitted


def test_run_agent_no_status_cb():
    """Must not raise when status_cb is None."""
    state = _make_state()
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        mock_fa_cls.return_value = _mock_agent_run("ok")
        result = asyncio.run(_run_agent("q", None, state, "c", "m"))
    assert result == "ok"


def test_run_agent_builds_agent_with_system_prompt():
    """FunctionAgent must be constructed with the system_prompt argument."""
    state = _make_state()
    captured_kwargs: dict = {}

    def capture_agent(**kwargs):
        captured_kwargs.update(kwargs)
        return _mock_agent_run("ok")

    with patch("canopy.query.loop.FunctionAgent", side_effect=capture_agent), \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        asyncio.run(_run_agent("q", None, state, "c", "m"))

    assert "system_prompt" in captured_kwargs
    assert len(captured_kwargs["system_prompt"]) > 100  # non-trivial prompt


def test_run_agent_builds_agent_with_max_iterations():
    state = _make_state()
    captured_kwargs: dict = {}

    def capture_agent(**kwargs):
        captured_kwargs.update(kwargs)
        return _mock_agent_run("ok")

    with patch("canopy.query.loop.FunctionAgent", side_effect=capture_agent), \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        asyncio.run(_run_agent("q", None, state, "c", "m"))

    from canopy.query.loop import MAX_ITERATIONS
    assert captured_kwargs["max_iterations"] == MAX_ITERATIONS
