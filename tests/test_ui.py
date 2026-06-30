"""Unit tests for canopy.ui.app handler functions. No browser or server needed."""

from __future__ import annotations

import canopy.ui.app as ui_mod
from canopy.i18n import t
from canopy.query.executor import SQLGuardError
from canopy.query.loop import LoopResult


def _make_result(**overrides) -> LoopResult:
    defaults = dict(
        question="How many detections?",
        sql="SELECT COUNT(*) FROM detections",
        columns=["count"],
        rows=[(5,)],
        row_count=5,
        model_text="There are 5 detections.",
        timing={"total_s": 1.2, "llm_s": 1.1, "llm_calls": 1, "db_s": 0.05, "db_calls": 1},
    )
    return LoopResult(**{**defaults, **overrides})


def _run(question: str, session_history: list | None = None) -> tuple:
    """Drain the streaming generator and return the last yielded tuple."""
    history = session_history if session_history is not None else []
    result = None
    for result in ui_mod._run_query_handler(question, history):
        pass
    return result


def _all_yields(question: str, session_history: list | None = None) -> list[tuple]:
    """Return all yielded tuples from the streaming handler."""
    history = session_history if session_history is not None else []
    return list(ui_mod._run_query_handler(question, history))


# ---------------------------------------------------------------------------
# _empty_result
# ---------------------------------------------------------------------------


def test_empty_result_structure():
    result = ui_mod._empty_result("some message", [])
    assert len(result) == 8
    sql, df, response, count_md, radio, timing, status, state = result
    assert sql == ""
    assert count_md == ""
    assert response == "some message"
    assert timing == ""
    assert status == ""
    assert state == []


def test_empty_result_with_status():
    result = ui_mod._empty_result("msg", [], status="⏳ Working…")
    assert result[6] == "⏳ Working…"


def test_empty_result_passes_session_history_through():
    history = ["prev q"]
    result = ui_mod._empty_result("error", history)
    assert result[7] == history


# ---------------------------------------------------------------------------
# Streaming: first yield is immediate loading state
# ---------------------------------------------------------------------------


def test_handler_first_yield_is_loading(monkeypatch):
    """User should see loading state immediately before any model call."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    first, *_ = _all_yields("How many detections?")
    assert len(first) == 8
    _, _, response, _, _, _, status_md, state = first
    assert t("status_reading") in response
    assert t("status_reading") in status_md


# ---------------------------------------------------------------------------
# _run_query_handler — happy path (last yielded tuple)
# ---------------------------------------------------------------------------


def test_handler_empty_question(monkeypatch):
    sql, df, response, count_md, radio, timing, status, state = _run("   ")
    assert sql == ""
    assert count_md == ""
    assert t("error_empty_question") in response
    assert timing == ""
    assert status == ""


def test_handler_valid_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    sql, df, response, count_md, radio, timing, status, state = _run("How many detections?")
    assert sql.startswith("SELECT COUNT(*) FROM detections")
    assert response == "There are 5 detections."
    assert "5" in count_md
    assert status == ""  # cleared on success


def test_handler_timing_line(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    sql, _, _, _, _, timing, _, _ = _run("q")
    assert t("timing_live", total=1.0)[:14] in timing
    # dev metrics moved to sql comment
    assert "LLM" in sql
    assert "DB" in sql


def test_handler_singular_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod, "run_query", lambda q, status_cb=None: _make_result(row_count=1, rows=[(1,)])
    )
    _, _, _, count_md, _, _, _, _ = _run("q")
    assert t("count_row_singular", n=1) in count_md
    assert "rows" not in count_md


def test_handler_plural_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None: _make_result(row_count=3, rows=[(1,), (2,), (3,)]),
    )
    _, _, _, count_md, _, _, _, _ = _run("q")
    assert t("count_row_plural", n=3) in count_md


def test_handler_rows_converted_to_lists(monkeypatch):
    result = _make_result(rows=[(1, "a"), (2, "b")], columns=["id", "name"], row_count=2)
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)
    _, df, _, _, _, _, _, _ = _run("q")
    import gradio as gr
    assert isinstance(df, gr.Dataframe)


def test_handler_null_sql(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None: _make_result(sql=None, rows=[], row_count=0),
    )
    sql, _, _, _, _, _, _, _ = _run("q")
    assert sql == ""


# ---------------------------------------------------------------------------
# Session history — per-browser localStorage-backed isolation
# ---------------------------------------------------------------------------


def test_handler_appends_question_to_session_history(monkeypatch):
    """Successful query prepends the question to session history."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    _, _, _, _, radio, _, _, new_state = _run("new question")
    import gradio as gr
    assert isinstance(radio, gr.Radio)
    assert "new question" in new_state


def test_handler_prepends_to_existing_history(monkeypatch):
    """New question goes to the front; previous entries are preserved."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    _, _, _, _, _, _, _, new_state = _run("new question", session_history=["old question"])
    assert new_state == ["new question", "old question"]


def test_handler_caps_history_at_20(monkeypatch):
    """History is capped at 20 entries — oldest entries are dropped."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    initial = [f"q{i}" for i in range(20)]
    _, _, _, _, _, _, _, new_state = _run("new question", session_history=initial)
    assert len(new_state) == 20
    assert new_state[0] == "new question"
    assert "q19" not in new_state  # oldest dropped


def test_handler_deduplicates_repeated_question(monkeypatch):
    """Re-running a question moves it to the top instead of adding a duplicate."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    initial = ["repeated q", "other q"]
    _, _, _, _, _, _, _, new_state = _run("repeated q", session_history=initial)
    assert new_state.count("repeated q") == 1
    assert new_state[0] == "repeated q"
    assert "other q" in new_state


# ---------------------------------------------------------------------------
# _run_query_handler — error paths
# ---------------------------------------------------------------------------


def test_handler_run_query_raises(monkeypatch):
    def _boom(q, status_cb=None):
        raise RuntimeError("DB is down")

    monkeypatch.setattr(ui_mod, "run_query", _boom)
    sql, df, response, count_md, _, timing, status, state = _run("anything")
    assert sql == ""
    assert count_md == ""
    # Human-readable — no internal exception text exposed to user
    assert t("error_generic_response") in response
    assert "DB is down" not in response
    assert timing == ""
    assert "⚠" in status


def test_handler_sql_guard_error_shows_sql(monkeypatch):
    """SQLGuardError: rejected SQL in sql_box; operation named in response; no internals exposed."""
    bad_sql = "DROP TABLE species"

    def _guard_fail(q, status_cb=None):
        raise SQLGuardError("Only SELECT queries are permitted", sql=bad_sql)

    monkeypatch.setattr(ui_mod, "run_query", _guard_fail)
    sql, df, response, count_md, _, timing, status, state = _run("drop something")
    assert sql == bad_sql
    assert "DROP" in response
    assert "Database query" in response
    assert "SQLGuardError" not in response
    assert "ValueError" not in response
    assert timing == ""
    assert "DROP" in status
    assert "⚠" in status


def test_handler_guard_names_delete_operation(monkeypatch):
    """DELETE generates a message that names DELETE specifically."""
    bad_sql = "DELETE FROM detections WHERE id = 1"

    def _guard_fail(q, status_cb=None):
        raise SQLGuardError("Only SELECT queries are permitted", sql=bad_sql)

    monkeypatch.setattr(ui_mod, "run_query", _guard_fail)
    _, _, response, _, _, _, status, _ = _run("delete that detection")
    assert "DELETE" in response
    assert "DELETE" in status


def test_handler_statement_timeout_gives_actionable_message(monkeypatch):
    """psycopg2 QueryCanceled (statement_timeout) → specific timeout message."""
    import psycopg2.errors

    def _timeout(q, status_cb=None):
        raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(ui_mod, "run_query", _timeout)
    _, _, response, _, _, _, status, _ = _run("huge query")
    assert "too long" in response.lower()
    assert "⚠" in status
    assert "timed out" in status.lower()


def test_handler_db_connection_error_message(monkeypatch):
    """psycopg2 OperationalError (connection lost) → specific connection message."""
    import psycopg2

    def _conn_fail(q, status_cb=None):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(ui_mod, "run_query", _conn_fail)
    _, _, response, _, _, _, status, _ = _run("any question")
    assert "database" in response.lower()
    assert "⚠" in status
    assert "unreachable" in status.lower()


def test_handler_loop_exhausted_message(monkeypatch):
    """RuntimeError from MAX_ITERATIONS → specific complexity message."""

    def _exhaust(q, status_cb=None):
        raise RuntimeError("Query loop exceeded maximum iterations")

    monkeypatch.setattr(ui_mod, "run_query", _exhaust)
    _, _, response, _, _, _, status, _ = _run("very complex question")
    assert "steps" in response.lower()
    assert "⚠" in status
    assert "complex" in status.lower()


# ---------------------------------------------------------------------------
# Cache hit UX
# ---------------------------------------------------------------------------


def test_handler_cache_hit_shows_cached_status(monkeypatch):
    """When run_query sends CACHE_HIT, UI should show a cache-specific status."""
    cached_result = _make_result(
        timing={"cache_hit": True, "cached_at": "2026-06-26T10:00:00+00:00"}
    )

    def _return_cached(q, status_cb=None):
        if status_cb:
            status_cb("CACHE_HIT")
        return cached_result

    monkeypatch.setattr(ui_mod, "run_query", _return_cached)

    yields = _all_yields("How many detections?")
    # One of the intermediate yields should mention cache
    statuses = [y[6] for y in yields]
    assert any("previous" in s.lower() or "cache" in s.lower() for s in statuses)

    # Final yield timing_md should show cached indicator
    final = yields[-1]
    assert "⚡" in final[5] or "Cached" in final[5]


# ---------------------------------------------------------------------------
# _clear_handler
# ---------------------------------------------------------------------------


def test_clear_handler_calls_clear_history(monkeypatch):
    called = []
    monkeypatch.setattr(ui_mod, "clear_history", lambda: called.append(True))
    ui_mod._clear_handler()
    assert called == [True]


def test_clear_handler_empties_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "clear_history", lambda: None)
    radio, question, response, state = ui_mod._clear_handler()
    assert question == ""
    assert state == []
