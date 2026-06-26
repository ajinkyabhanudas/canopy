"""
Tests for canopy.query.loop — no live DB, no real model calls.

get_model_client() and get_connection() are both monkeypatched.
execute_query() is NOT mocked — we test the real integration between the
loop and the executor, using only a mocked DB connection underneath.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from canopy.models.base import ModelResponse, ToolCall
from canopy.query.loop import MAX_ITERATIONS, run_query

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_cache(monkeypatch):
    """Prevent real cache reads/writes from interfering with loop unit tests."""
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q: None)
    monkeypatch.setattr("canopy.query.loop.write_cache", lambda r: None)


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection that returns one column and one row."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.description = [("n",)]
    cursor.fetchall.return_value = [(1,)]
    return conn


@pytest.fixture
def mock_model():
    """Mock ModelClient with configurable generate() return values."""
    model = MagicMock()
    model.format_assistant_turn.return_value = {"role": "assistant", "content": []}
    model.format_tool_results.return_value = {"role": "user", "content": []}
    return model


def _tool_response(sql: str = "SELECT 1", call_id: str = "tc1") -> ModelResponse:
    return ModelResponse(
        text=None,
        tool_calls=[ToolCall(id=call_id, name="execute_sql", arguments={"sql": sql})],
        stop_reason="tool_use",
    )


def _text_response(text: str) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=[], stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_single_tool_call_round_trip(monkeypatch, mock_model, mock_conn):
    mock_model.generate.side_effect = [
        _tool_response("SELECT 1", call_id="tc1"),
        _text_response("Found 1 result."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    result = run_query("How many rows are in the detections table?")

    assert result.question == "How many rows are in the detections table?"
    assert result.sql == "SELECT 1"
    assert result.row_count == 1
    assert result.rows == [(1,)]
    assert result.columns == ["n"]
    assert result.model_text == "Found 1 result."


def test_direct_text_response(monkeypatch, mock_model):
    """Model declines tool — e.g. question is out of scope."""
    mock_model.generate.return_value = _text_response(
        "I cannot answer that from this database."
    )
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)

    result = run_query("What is the conservation status of the giant antpitta?")

    assert result.sql is None
    assert result.row_count == 0
    assert result.columns == []
    assert result.rows == []
    assert result.model_text == "I cannot answer that from this database."


def test_multiple_tool_calls_accumulate(monkeypatch, mock_model, mock_conn):
    """Model calls the tool twice before producing a final response."""
    mock_model.generate.side_effect = [
        _tool_response("SELECT 1", call_id="tc1"),
        _tool_response("SELECT 2", call_id="tc2"),
        _text_response("Done."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    result = run_query("Run two queries.")

    # last SQL wins
    assert result.sql == "SELECT 2"
    assert result.model_text == "Done."
    assert mock_model.generate.call_count == 3


# ---------------------------------------------------------------------------
# Guard: max iterations
# ---------------------------------------------------------------------------


def test_max_iterations_guard(monkeypatch, mock_model, mock_conn):
    """Loop raises RuntimeError after MAX_ITERATIONS without end_turn."""
    mock_model.generate.return_value = _tool_response("SELECT 1")
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    with pytest.raises(RuntimeError, match="Query loop exceeded maximum iterations"):
        run_query("Infinite loop question.")

    assert mock_model.generate.call_count == MAX_ITERATIONS


# ---------------------------------------------------------------------------
# Message history integrity
# ---------------------------------------------------------------------------


def test_format_assistant_turn_called_each_iteration(monkeypatch, mock_model, mock_conn):
    mock_model.generate.side_effect = [
        _tool_response("SELECT 1"),
        _text_response("Done."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    run_query("A question.")

    assert mock_model.format_assistant_turn.call_count == 2


def test_format_tool_results_called_with_correct_id(monkeypatch, mock_model, mock_conn):
    mock_model.generate.side_effect = [
        _tool_response("SELECT 1", call_id="abc123"),
        _text_response("Done."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    run_query("A question.")

    # format_tool_results receives a list of (id, content) tuples
    results_list = mock_model.format_tool_results.call_args[0][0]
    assert len(results_list) == 1
    tool_id, result_str = results_list[0]
    assert tool_id == "abc123"
    assert "Columns:" in result_str
    assert "Row count:" in result_str


# ---------------------------------------------------------------------------
# _format_result behaviour (tested via the loop)
# ---------------------------------------------------------------------------


def test_format_result_truncates_at_200_rows(monkeypatch, mock_model, mock_conn):
    mock_conn.cursor.return_value.description = [("id",)]
    mock_conn.cursor.return_value.fetchall.return_value = [(i,) for i in range(250)]
    mock_model.generate.side_effect = [
        _tool_response("SELECT id FROM detections"),
        _text_response("Done."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    run_query("Get 250 rows.")

    _, result_str = mock_model.format_tool_results.call_args[0][0][0]
    assert "50 more rows truncated" in result_str


def test_format_result_strips_sensitive_columns(monkeypatch, mock_model, mock_conn):
    """lat/lon columns must not appear in the string sent back to the model."""
    mock_conn.cursor.return_value.description = [
        ("scientific_name",), ("site",), ("latitude",), ("longitude",)
    ]
    mock_conn.cursor.return_value.fetchall.return_value = [
        ("Grallaria gigantea", "Buenaventura", -1.23, -78.45)
    ]
    mock_model.generate.side_effect = [
        _tool_response(  # noqa: E501
            "SELECT s.scientific_name, si.name AS site, d.latitude, d.longitude "
            "FROM detections d JOIN species s ON d.species_id = s.id "
            "JOIN sites si ON d.site_id = si.id LIMIT 1"
        ),
        _text_response("Found species at site."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    run_query("Which species at which site?")

    _, result_str = mock_model.format_tool_results.call_args[0][0][0]
    assert "latitude" not in result_str
    assert "longitude" not in result_str
    assert "-1.23" not in result_str
    assert "-78.45" not in result_str
    # Non-sensitive columns still present
    assert "scientific_name" in result_str
    assert "site" in result_str


def test_format_result_empty_rows(monkeypatch, mock_model, mock_conn):
    mock_conn.cursor.return_value.description = [("id",)]
    mock_conn.cursor.return_value.fetchall.return_value = []
    mock_model.generate.side_effect = [
        _tool_response("SELECT id FROM detections WHERE id = -1"),
        _text_response("No results found."),
    ]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    result = run_query("Find nothing.")

    assert result.row_count == 0
    _, result_str = mock_model.format_tool_results.call_args[0][0][0]
    assert "Row count: 0" in result_str


# ---------------------------------------------------------------------------
# Parallel tool calls (single model response, multiple tool_use blocks)
# ---------------------------------------------------------------------------


def test_parallel_tool_calls_bundled_into_one_message(monkeypatch, mock_model, mock_conn):
    """Two tool calls in one response must produce exactly one user message."""
    parallel_response = ModelResponse(
        text=None,
        tool_calls=[
            ToolCall(id="tc1", name="execute_sql", arguments={"sql": "SELECT 1"}),
            ToolCall(id="tc2", name="execute_sql", arguments={"sql": "SELECT 2"}),
        ],
        stop_reason="tool_use",
    )
    mock_model.generate.side_effect = [parallel_response, _text_response("Done.")]
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    result = run_query("Run two queries in parallel.")

    # format_tool_results called exactly once (not twice)
    assert mock_model.format_tool_results.call_count == 1
    # The single call received both results
    results_list = mock_model.format_tool_results.call_args[0][0]
    assert len(results_list) == 2
    assert results_list[0][0] == "tc1"
    assert results_list[1][0] == "tc2"
    # Last SQL wins in LoopResult
    assert result.sql == "SELECT 2"
    assert result.model_text == "Done."


# ---------------------------------------------------------------------------
# format_tool_results shape (AnthropicClient unit tests)
# ---------------------------------------------------------------------------


def test_anthropic_format_tool_results_single(monkeypatch):
    """Single result produces one tool_result block."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr("canopy.models.anthropic.anthropic", MagicMock())

    from canopy.models.anthropic import AnthropicClient

    client = AnthropicClient()
    msg = client.format_tool_results([("id1", "result content")])

    assert msg["role"] == "user"
    assert len(msg["content"]) == 1
    assert msg["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "id1",
        "content": "result content",
    }


def test_anthropic_format_tool_results_multiple(monkeypatch):
    """Multiple results all land in one user message, preserving order."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr("canopy.models.anthropic.anthropic", MagicMock())

    from canopy.models.anthropic import AnthropicClient

    client = AnthropicClient()
    msg = client.format_tool_results([("tc1", "r1"), ("tc2", "r2")])

    assert msg["role"] == "user"
    assert len(msg["content"]) == 2
    assert msg["content"][0]["tool_use_id"] == "tc1"
    assert msg["content"][1]["tool_use_id"] == "tc2"
    assert msg["content"][0]["content"] == "r1"
    assert msg["content"][1]["content"] == "r2"


def test_anthropic_format_tool_result_singular_delegates(monkeypatch):
    """format_tool_result (singular) is now a one-element wrapper — verify it still works."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr("canopy.models.anthropic.anthropic", MagicMock())

    from canopy.models.anthropic import AnthropicClient

    client = AnthropicClient()
    msg = client.format_tool_result("myid", "mycontent")

    assert msg["role"] == "user"
    assert len(msg["content"]) == 1
    assert msg["content"][0]["tool_use_id"] == "myid"
    assert msg["content"][0]["content"] == "mycontent"


# ---------------------------------------------------------------------------
# Defensive guards
# ---------------------------------------------------------------------------


def test_tool_use_with_empty_tool_calls_raises(monkeypatch, mock_model):
    """Model signals tool_use but provides no tool calls — should raise clearly."""
    mock_model.generate.return_value = ModelResponse(
        text=None, tool_calls=[], stop_reason="tool_use"
    )
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)

    with pytest.raises(ValueError, match="no tool calls"):
        run_query("Broken model response.")


# ---------------------------------------------------------------------------
# Cache integration
# ---------------------------------------------------------------------------


def test_cache_hit_skips_llm_call(monkeypatch, mock_model, tmp_path):
    """If lookup_cache returns a result, the model should never be called."""
    from canopy.query.loop import LoopResult

    cached = LoopResult(
        question="How many detections?",
        sql="SELECT COUNT(*) FROM detections",
        columns=["count"],
        rows=[(42,)],
        row_count=42,
        model_text="There are 42 detections.",
        timing={"cache_hit": True, "cached_at": "2026-06-26T00:00:00+00:00"},
    )
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q: cached)
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)

    result = run_query("How many detections?")

    mock_model.generate.assert_not_called()
    assert result.row_count == 42
    assert result.timing.get("cache_hit") is True


def test_cache_miss_writes_result(monkeypatch, mock_model, mock_conn):
    """On a cache miss, the result should be written to cache after the query."""
    written: list = []
    monkeypatch.setattr("canopy.query.loop.lookup_cache", lambda q: None)
    monkeypatch.setattr("canopy.query.loop.write_cache", lambda r: written.append(r))
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    mock_model.generate.side_effect = [
        _tool_response("SELECT 1"),
        _text_response("Done."),
    ]

    run_query("A fresh question.")

    assert len(written) == 1
    assert written[0].model_text == "Done."


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_loop_result_is_immutable(monkeypatch, mock_model):
    mock_model.generate.return_value = _text_response("No data.")
    monkeypatch.setattr("canopy.query.loop.get_model_client", lambda: mock_model)

    result = run_query("Anything.")

    with pytest.raises(Exception):  # FrozenInstanceError
        result.model_text = "hacked"  # type: ignore[misc]
