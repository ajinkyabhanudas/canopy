"""Unit tests for canopy.ui.app handler functions. No browser or server needed."""

from __future__ import annotations

import canopy.ui.app as ui_mod
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


def _run(question: str) -> tuple:
    """Drain the streaming generator and return the last yielded tuple."""
    result = None
    for result in ui_mod._run_query_handler(question):
        pass
    return result


def _all_yields(question: str) -> list[tuple]:
    """Return all yielded tuples from the streaming handler."""
    return list(ui_mod._run_query_handler(question))


# ---------------------------------------------------------------------------
# _history_choices
# ---------------------------------------------------------------------------


def test_history_choices_empty(monkeypatch):
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    assert ui_mod._history_choices() == []


def test_history_choices_reversed(monkeypatch):
    entries = [{"question": "first"}, {"question": "second"}, {"question": "third"}]
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: entries)
    assert ui_mod._history_choices() == ["third", "second", "first"]


# ---------------------------------------------------------------------------
# _empty_result
# ---------------------------------------------------------------------------


def test_empty_result_structure():
    result = ui_mod._empty_result("some message")
    assert len(result) == 7
    sql, df, response, count_md, radio, timing, status = result
    assert sql == ""
    assert count_md == ""
    assert response == "some message"
    assert timing == ""
    assert status == ""


def test_empty_result_with_status():
    result = ui_mod._empty_result("msg", status="⏳ Working…")
    assert result[6] == "⏳ Working…"


# ---------------------------------------------------------------------------
# Streaming: first yield is immediate loading state
# ---------------------------------------------------------------------------


def test_handler_first_yield_is_loading(monkeypatch):
    """User should see 'Thinking' immediately before any model call."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    first, *_ = _all_yields("How many detections?")
    assert len(first) == 7
    _, _, response, _, _, _, status_md = first
    assert "Reading" in response or "reading" in response.lower()
    assert "Reading" in status_md or "reading" in status_md.lower()


# ---------------------------------------------------------------------------
# _run_query_handler — happy path (last yielded tuple)
# ---------------------------------------------------------------------------


def test_handler_empty_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, radio, timing, status = _run("   ")
    assert sql == ""
    assert count_md == ""
    assert "Please enter a question" in response
    assert timing == ""
    assert status == ""


def test_handler_valid_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, radio, timing, status = _run("How many detections?")
    assert sql.startswith("SELECT COUNT(*) FROM detections")
    assert response == "There are 5 detections."
    assert "5" in count_md
    assert status == ""  # cleared on success


def test_handler_timing_line(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, _, _, _, _, timing, _ = _run("q")
    assert "Answer ready in" in timing
    # dev metrics moved to sql comment
    assert "LLM" in sql
    assert "DB" in sql


def test_handler_singular_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod, "run_query", lambda q, status_cb=None: _make_result(row_count=1, rows=[(1,)])
    )
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    _, _, _, count_md, _, _, _ = _run("q")
    assert "1 row returned" in count_md
    assert "rows" not in count_md


def test_handler_plural_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None: _make_result(row_count=3, rows=[(1,), (2,), (3,)]),
    )
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    _, _, _, count_md, _, _, _ = _run("q")
    assert "3 rows returned" in count_md


def test_handler_rows_converted_to_lists(monkeypatch):
    result = _make_result(rows=[(1, "a"), (2, "b")], columns=["id", "name"], row_count=2)
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    _, df, _, _, _, _, _ = _run("q")
    import gradio as gr
    assert isinstance(df, gr.Dataframe)


def test_handler_null_sql(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None: _make_result(sql=None, rows=[], row_count=0),
    )
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, _, _, _, _, _, _ = _run("q")
    assert sql == ""


def test_handler_updates_history(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [{"question": "prev q"}])
    _, _, _, _, radio, _, _ = _run("q")
    import gradio as gr
    assert isinstance(radio, gr.Radio)


# ---------------------------------------------------------------------------
# _run_query_handler — error paths
# ---------------------------------------------------------------------------


def test_handler_run_query_raises(monkeypatch):
    def _boom(q, status_cb=None):
        raise RuntimeError("DB is down")

    monkeypatch.setattr(ui_mod, "run_query", _boom)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, _, timing, status = _run("anything")
    assert sql == ""
    assert count_md == ""
    # Human-readable — no internal exception text exposed to user
    assert "went wrong" in response or "try again" in response.lower()
    assert "DB is down" not in response
    assert timing == ""
    assert "⚠" in status


def test_handler_sql_guard_error_shows_sql(monkeypatch):
    """SQLGuardError: rejected SQL appears in sql_box; human-readable message in response."""
    bad_sql = "DROP TABLE species"

    def _guard_fail(q, status_cb=None):
        raise SQLGuardError("Only SELECT queries are permitted", sql=bad_sql)

    monkeypatch.setattr(ui_mod, "run_query", _guard_fail)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    sql, df, response, count_md, _, timing, status = _run("drop something")
    assert sql == bad_sql
    assert "Database query tab" in response
    # Internal exception text must not be exposed
    assert "SQLGuardError" not in response
    assert "ValueError" not in response
    assert timing == ""
    assert "⚠" in status


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
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])

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
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    ui_mod._clear_handler()
    assert called == [True]


def test_clear_handler_empties_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "clear_history", lambda: None)
    monkeypatch.setattr(ui_mod, "load_history", lambda n=20: [])
    radio, question, response = ui_mod._clear_handler()
    assert question == ""
