"""Unit tests for the LlamaIndex agent internals in loop.py.

Covers _build_sql_tool and _run_agent — the two functions that
test_query_loop.py skips by mocking _run_agent at the boundary.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from unittest.mock import AsyncMock, MagicMock, patch

from canopy.i18n import t
from canopy.query.executor import QueryResult
from canopy.query.loop import _build_sql_tool, _run_agent


def _run(coro):
    """Run a coroutine in a dedicated thread so Playwright's loop never interferes."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


def _make_state() -> dict:
    return {
        "db_times": [],
        "last_sql": None,
        "last_query_result": None,
        "llm_times": [],
        "iterations": 0,
        "fuzzy_matches": (),
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
# _build_sql_tool — fuzzy-match wiring via is_empty_result()
#
# These exercise the REAL find_candidates()/is_empty_result() call inside
# execute_sql's closure (only execute_query is mocked) — the integration
# point a live-Docker test caught missing a COUNT(*) case that no earlier
# test here reached, since every test above uses a plain row-returning
# QueryResult and test_query_loop.py mocks _run_agent entirely, bypassing
# this closure altogether.
# ---------------------------------------------------------------------------


def test_build_sql_tool_populates_fuzzy_matches_on_zero_rows(monkeypatch):
    qr = QueryResult(columns=("scientific_name",), rows=(), row_count=0)
    state = _make_state()

    def _fake_find_candidates(sql):
        from canopy.query.fuzzy_match import FuzzyMatch
        match = FuzzyMatch(
            literal="Gralari", candidates=("Grallaria gigantea",), label_key="species"
        )
        return (match,)

    monkeypatch.setattr("canopy.query.loop.find_candidates", _fake_find_candidates)
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        tool.fn(sql="SELECT scientific_name FROM species WHERE scientific_name ILIKE '%Gralari%'")

    assert len(state["fuzzy_matches"]) == 1
    assert state["fuzzy_matches"][0].label_key == "species"


def test_build_sql_tool_populates_fuzzy_matches_on_count_star_zero(monkeypatch):
    """The exact live-Docker-discovered case: COUNT(*) returning row_count=1
    with the aggregate value 0 must still trigger fuzzy-match resolution."""
    qr = QueryResult(columns=("n",), rows=((0,),), row_count=1)
    state = _make_state()
    called_with: list[str] = []

    def _fake_find_candidates(sql):
        from canopy.query.fuzzy_match import FuzzyMatch
        called_with.append(sql)
        match = FuzzyMatch(
            literal="tyranina",
            candidates=("Cercomacroides tyrannina",),
            label_key="species",
        )
        return (match,)

    monkeypatch.setattr("canopy.query.loop.find_candidates", _fake_find_candidates)
    sql = (
        "SELECT COUNT(*) AS n FROM detections d JOIN species s ON d.species_id = s.id "
        "WHERE s.scientific_name = 'Cercomacroides tyranina'"
    )
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        tool.fn(sql=sql)

    assert called_with == [sql]
    assert len(state["fuzzy_matches"]) == 1


def test_build_sql_tool_no_fuzzy_matches_on_count_star_nonzero(monkeypatch):
    """COUNT(*) returning a real nonzero count must NOT trigger find_candidates —
    it's a genuine successful result, not an empty one."""
    qr = QueryResult(columns=("n",), rows=((100,),), row_count=1)
    state = _make_state()
    call_count = {"n": 0}

    def _fake_find_candidates(sql):
        call_count["n"] += 1
        return ()

    monkeypatch.setattr("canopy.query.loop.find_candidates", _fake_find_candidates)
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        tool.fn(sql="SELECT COUNT(*) AS n FROM detections WHERE species_id = 12")

    assert call_count["n"] == 0
    assert state["fuzzy_matches"] == ()


def test_build_sql_tool_no_fuzzy_matches_on_nonempty_row_result(monkeypatch):
    qr = _make_query_result(rows=((1,), (2,)))
    state = _make_state()
    call_count = {"n": 0}

    def _fake_find_candidates(sql):
        call_count["n"] += 1
        return ()

    monkeypatch.setattr("canopy.query.loop.find_candidates", _fake_find_candidates)
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(None, state)
        tool.fn(sql="SELECT n FROM t")

    assert call_count["n"] == 0
    assert state["fuzzy_matches"] == ()


# ---------------------------------------------------------------------------
# _build_sql_tool — status message on empty vs. nonempty results
#
# A retry-worthy question (e.g. a mistyped name) can trigger several
# execute_sql calls in one turn, each with its own row count. Showing
# "Found 0 detections" then "Found 1" then "Found 100" as the model retries
# with a corrected query reads as nonsensical progress, not a search
# correction — status_refining replaces the count message on any empty
# result (plain 0-row or COUNT(*)-with-zero-value) so only a call that
# actually found something reports a count.
# ---------------------------------------------------------------------------


def test_build_sql_tool_shows_refining_status_on_zero_rows(monkeypatch):
    qr = QueryResult(columns=("scientific_name",), rows=(), row_count=0)
    state = _make_state()
    calls: list[str] = []
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(lambda msg: calls.append(msg), state)
        tool.fn(sql="SELECT scientific_name FROM species WHERE scientific_name = 'Nonexistent'")
    assert t("status_refining") in calls
    assert not any("found" in c.lower() for c in calls)


def test_build_sql_tool_shows_refining_status_on_count_star_zero(monkeypatch):
    """The exact live-Docker-discovered case: COUNT(*) with aggregate value 0
    must also show status_refining, not "Found 0 detections"."""
    qr = QueryResult(columns=("n",), rows=((0,),), row_count=1)
    state = _make_state()
    calls: list[str] = []
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(lambda msg: calls.append(msg), state)
        tool.fn(sql="SELECT COUNT(*) AS n FROM detections WHERE species_id = 999")
    assert t("status_refining") in calls
    assert not any("found" in c.lower() for c in calls)


def test_build_sql_tool_shows_found_count_on_nonempty_result(monkeypatch):
    """A call that actually returns rows still reports "Found N" as before —
    only empty results are redirected to status_refining."""
    qr = _make_query_result(rows=((1,), (2,), (3,)))
    state = _make_state()
    calls: list[str] = []
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(lambda msg: calls.append(msg), state)
        tool.fn(sql="SELECT n FROM t")
    assert t("found_detections_plural", n=3) in calls
    assert t("status_refining") not in calls


def test_build_sql_tool_shows_found_count_on_count_star_nonzero(monkeypatch):
    qr = QueryResult(columns=("n",), rows=((100,),), row_count=1)
    state = _make_state()
    calls: list[str] = []
    with patch("canopy.query.loop.execute_query", return_value=qr):
        tool = _build_sql_tool(lambda msg: calls.append(msg), state)
        tool.fn(sql="SELECT COUNT(*) AS n FROM detections WHERE species_id = 12")
    assert t("found_detections_plural", n=100) in calls
    assert t("status_refining") not in calls


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
        result = _run(
            _run_agent("how many species?", None, state, "conn-1", "model-1")
        )
    assert result == "Species found: 3"


def test_run_agent_populates_llm_times():
    state = _make_state()
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        mock_fa_cls.return_value = _mock_agent_run("done")
        _run(_run_agent("q", None, state, "c", "m"))
    assert len(state["llm_times"]) == 1
    assert state["llm_times"][0] >= 0.0


def test_run_agent_calls_status_cb_understanding():
    state = _make_state()
    calls: list[str] = []
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        mock_fa_cls.return_value = _mock_agent_run("done")
        _run(_run_agent("q", lambda msg: calls.append(msg), state, "c", "m"))
    assert len(calls) == 1
    assert calls[0]  # some status message was emitted


def test_run_agent_no_status_cb():
    """Must not raise when status_cb is None."""
    state = _make_state()
    with patch("canopy.query.loop.FunctionAgent") as mock_fa_cls, \
         patch("canopy.query.loop.get_llm", return_value=MagicMock()):
        mock_fa_cls.return_value = _mock_agent_run("ok")
        result = _run(_run_agent("q", None, state, "c", "m"))
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
        _run(_run_agent("q", None, state, "c", "m"))

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
        _run(_run_agent("q", None, state, "c", "m"))

    from canopy.query.loop import MAX_ITERATIONS
    assert captured_kwargs["max_iterations"] == MAX_ITERATIONS
