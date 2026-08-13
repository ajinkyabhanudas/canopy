"""Unit tests for canopy.ui.app handler functions. No browser or server needed."""

from __future__ import annotations

import canopy.ui.app as ui_mod
from canopy.i18n import get_lang, set_locale, t
from canopy.query.executor import SQLGuardError
from canopy.query.loop import Interpretation, LoopResult, UnsupportedLanguageError


def _make_result(**overrides) -> LoopResult:
    defaults: dict = dict(
        question="How many detections?",
        sql="SELECT COUNT(*) FROM detections",
        columns=("count",),
        rows=((5,),),
        row_count=5,
        model_text="There are 5 detections.",
        timing={"total_s": 1.2, "llm_s": 1.1, "llm_calls": 1, "db_s": 0.05, "db_calls": 1},
    )
    merged = {**defaults, **overrides}
    if isinstance(merged.get("columns"), list):
        merged["columns"] = tuple(merged["columns"])
    if isinstance(merged.get("rows"), list):
        merged["rows"] = tuple(tuple(r) if isinstance(r, list) else r for r in merged["rows"])
    return LoopResult(**merged)


def _run(
    question: str, session_history: list | None = None, superseded: str | None = None
) -> tuple:
    """Drain the streaming generator and return the last yielded tuple."""
    history = session_history if session_history is not None else []
    result = None
    for result in ui_mod._run_query_handler(question, history, superseded):
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
    assert len(result) == 30
    sql, df, response, count_md, radio, timing, status, state, tabs, *_ = result
    assert sql == ""
    assert count_md == ""
    assert response == "some message"
    assert timing == ""
    assert status == ""
    assert state == []
    assert tabs == {"selected": 0, "__type__": "update"}


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
    """User should see loading state immediately before any model call.
    The step-log text lives in response_box; status_md is the generic
    "Working · Ns" ticker (see test_status_bar_never_duplicates_the_
    current_step_text for why these are deliberately different)."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    first, *_ = _all_yields("How many detections?")
    assert len(first) == 30
    _, _, response, _, _, _, status_md, state, *_ = first
    assert t("status_reading") in response
    assert t("status_bar_working") in status_md


# ---------------------------------------------------------------------------
# _loading_status_html — elapsed-time ticker (Step 2 of the perceived-
# latency plan). Deliberately shows only a generic "Working · Ns" label,
# not the current step text — that lives in response_box's step-log
# instead (_step_log_markdown), to avoid showing the same status in two
# places at once (caught in review: the top bar and the answer panel were
# both echoing "Found N detections" simultaneously).
# ---------------------------------------------------------------------------


def test_loading_status_html_contains_working_label():
    html = ui_mod._loading_status_html(is_first=True)
    assert t("status_bar_working") in html


def test_loading_status_html_first_yield_has_reset_marker():
    html = ui_mod._loading_status_html(is_first=True)
    assert 'data-first="1"' in html


def test_loading_status_html_later_yield_has_no_reset_marker():
    html = ui_mod._loading_status_html(is_first=False)
    assert 'data-first="1"' not in html


def test_loading_status_html_contains_no_script_tag():
    """Gradio's gr.Markdown routes content through a markdown-to-HTML parser
    even with sanitize_html=False, which mangles inline <script> content
    into inert text instead of executing it (confirmed live) — the ticker
    must be a static marker only, never an inline script here."""
    html = ui_mod._loading_status_html(is_first=False)
    assert "<script>" not in html
    assert 'data-canopy-loading="1"' in html
    assert "canopy-status-elapsed" in html


def test_status_ticker_head_script_targets_status_elements():
    """The actual ticker lives in the page-level head script (loaded once
    via Blocks.launch(head=...)), not per-yield — confirm it references the
    marker/target elements _loading_status_html emits."""
    script = ui_mod.STATUS_TICKER_HEAD_SCRIPT
    assert "<script>" in script
    assert "canopy-status" in script
    assert "data-canopy-loading" in script
    assert "MutationObserver" in script


def test_status_yield_first_call_marks_is_first(monkeypatch):
    """The handler's very first yield must carry the reset marker —
    otherwise a second query in the same session would inherit the first
    query's elapsed-time start and show an inflated counter."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    first, *_ = _all_yields("How many detections?")
    status_md = first[6]
    assert 'data-first="1"' in status_md


def test_status_bar_never_duplicates_the_current_step_text(monkeypatch):
    """Regression test: status_md (top bar) and response_box (step log)
    must not both show the same status string. Caught in review — the top
    bar used to echo the exact current step (e.g. "Found 5 detections")
    that response_box's step log already showed, one line lower."""
    result = _make_result()

    def _emit_status(q, status_cb=None, **_kw):
        if status_cb:
            status_cb("Searching the monitoring database…")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_status)
    yields = _all_yields("How many species?")
    for y in yields[:-1]:  # exclude the final success yield
        status_md = y[6]
        assert t("status_bar_working") in status_md
        assert "Searching the monitoring database…" not in status_md


def test_empty_result_status_is_not_wrapped_in_loading_html():
    """Terminal/error states use the plain status string — they are not
    "in progress," so they must not carry the loading ticker/pulse."""
    _, _, _, _, _, _, status, *_ = ui_mod._empty_result("msg", [], status="⚠ Some error")
    assert status == "⚠ Some error"
    assert "<script>" not in status


# ---------------------------------------------------------------------------
# _run_query_handler — happy path (last yielded tuple)
# ---------------------------------------------------------------------------


def test_handler_empty_question(monkeypatch):
    sql, df, response, count_md, radio, timing, status, state, *_ = _run("   ")
    assert sql == ""
    assert count_md == ""
    assert t("error_empty_question") in response
    assert timing == ""
    assert status == ""


def test_handler_valid_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    sql, df, response, count_md, radio, timing, status, state, *_ = _run("How many detections?")
    assert sql.startswith("SELECT COUNT(*) FROM detections")
    assert response == "There are 5 detections."
    assert "5" in count_md
    assert status == ""  # cleared on success


def test_handler_timing_line(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    sql, _, _, _, _, timing, _, _, *_ = _run("q")
    assert t("timing_live", total=1.0)[:14] in timing
    # dev metrics moved to sql comment
    assert "LLM" in sql
    assert "DB" in sql


def test_handler_singular_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result(row_count=1, rows=[(1,)])
    )
    _, _, _, count_md, _, _, _, _, *_ = _run("q")
    assert t("count_row_singular", n=1) in count_md
    assert "rows" not in count_md


def test_handler_plural_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None, **_kw: _make_result(row_count=3, rows=[(1,), (2,), (3,)]),
    )
    _, _, _, count_md, _, _, _, _, *_ = _run("q")
    assert t("count_row_plural", n=3) in count_md


def test_handler_rows_converted_to_lists(monkeypatch):
    result = _make_result(rows=[(1, "a"), (2, "b")], columns=["id", "name"], row_count=2)
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)
    _, df, _, _, _, _, _, _, *_ = _run("q")
    import gradio as gr
    assert isinstance(df, gr.Dataframe)


def test_handler_null_sql(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None, **_kw: _make_result(sql=None, rows=[], row_count=0),
    )
    sql, _, _, _, _, _, _, _, *_ = _run("q")
    assert sql == ""


# ---------------------------------------------------------------------------
# Fuzzy suggestion buttons — "did you mean X?" recovery path
#
# Trailing output shape: 3 groups (species, site, management_unit) x
# (1 prompt + 3 buttons + 3 q-states) = 21 slots. Group 1 = species,
# group 2 = site, group 3 = management_unit (FUZZY_COLUMNS registration
# order in fuzzy_match.py).
# ---------------------------------------------------------------------------


def test_handler_shows_suggestions_on_fuzzy_match(monkeypatch):
    from canopy.query.fuzzy_match import FuzzyMatch

    match = FuzzyMatch(
        literal="Gralari gigantae",
        candidates=("Grallaria gigantea", "Grallaria ridgelyi"),
        label_key="species",
    )
    result = _make_result(sql="...", rows=[], row_count=0, fuzzy_matches=(match,))
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    (*_, g1_prompt, g1_b1, g1_b2, g1_b3, g1_q1, g1_q2, g1_q3,
     g2_prompt, g2_b1, g2_b2, g2_b3, g2_q1, g2_q2, g2_q3,
     g3_prompt, g3_b1, g3_b2, g3_b3, g3_q1, g3_q2, g3_q3) = _run(
        "How many detections of Gralari gigantae are there?"
    )

    assert g1_prompt["visible"] is True
    assert "Species" in g1_prompt["value"]
    assert g1_b1["visible"] is True
    assert g1_b1["value"] == "Grallaria gigantea"
    assert g1_b2["visible"] is True
    assert g1_b2["value"] == "Grallaria ridgelyi"
    assert g1_b3["visible"] is False
    assert g1_q1 == "How many detections of Grallaria gigantea are there?"
    assert g1_q2 == "How many detections of Grallaria ridgelyi are there?"
    assert g1_q3 is None

    # Remaining groups (site, management_unit) stay fully hidden — only one
    # column was mistyped.
    assert g2_prompt["visible"] is False
    assert g2_b1["visible"] is False
    assert g2_q1 is None
    assert g3_prompt["visible"] is False
    assert g3_b1["visible"] is False
    assert g3_q1 is None


def test_handler_fuzzy_match_falls_back_to_candidate_when_literal_not_in_question(monkeypatch):
    """If the SQL literal isn't found verbatim in the user's question (the LLM
    may have reformatted it), the rewritten question falls back to just the
    candidate name rather than leaving the question unchanged."""
    from canopy.query.fuzzy_match import FuzzyMatch

    match = FuzzyMatch(
        literal="Gralari gigantae", candidates=("Grallaria gigantea",), label_key="species"
    )
    result = _make_result(sql="...", rows=[], row_count=0, fuzzy_matches=(match,))
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    (*_, _g1_prompt, _g1_b1, _g1_b2, _g1_b3, g1_q1, _g1_q2, _g1_q3,
     _g2_prompt, _g2_b1, _g2_b2, _g2_b3, _g2_q1, _g2_q2, _g2_q3,
     _g3_prompt, _g3_b1, _g3_b2, _g3_b3, _g3_q1, _g3_q2, _g3_q3) = _run(
        "Tell me about the giant antpitta"
    )

    assert g1_q1 == "Grallaria gigantea"


def test_handler_shows_suggestions_for_two_simultaneous_typos(monkeypatch):
    """A question with typos in BOTH a species name AND a site name shows
    two independent suggestion groups, each labeled and clickable on its
    own — not just the first typo, and not merged into one group."""
    from canopy.query.fuzzy_match import FuzzyMatch

    species_match = FuzzyMatch(
        literal="Gralari gigantae", candidates=("Grallaria gigantea",), label_key="species"
    )
    site_match = FuzzyMatch(
        literal="Buenaventuraa", candidates=("Reserva Buenaventura",), label_key="site"
    )
    result = _make_result(
        sql="...", rows=[], row_count=0, fuzzy_matches=(species_match, site_match)
    )
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    (*_, g1_prompt, g1_b1, _g1_b2, _g1_b3, g1_q1, _g1_q2, _g1_q3,
     g2_prompt, g2_b1, _g2_b2, _g2_b3, g2_q1, _g2_q2, _g2_q3,
     g3_prompt, _g3_b1, _g3_b2, _g3_b3, _g3_q1, _g3_q2, _g3_q3) = _run(
        "How many detections of Gralari gigantae at Buenaventuraa are there?"
    )

    assert g1_prompt["visible"] is True
    assert "Species" in g1_prompt["value"]
    assert g1_b1["value"] == "Grallaria gigantea"
    assert g1_q1 == "How many detections of Grallaria gigantea at Buenaventuraa are there?"

    assert g2_prompt["visible"] is True
    assert "Site" in g2_prompt["value"]
    assert g2_b1["value"] == "Reserva Buenaventura"
    assert g2_q1 == "How many detections of Gralari gigantae at Reserva Buenaventura are there?"

    # Third group (management_unit) stays hidden — that column wasn't mistyped.
    assert g3_prompt["visible"] is False


def test_handler_shows_suggestions_for_three_simultaneous_typos(monkeypatch):
    """A question with typos in species, site, AND management_unit at once
    surfaces three independent suggestion groups — extends the two-column
    case now that a third fuzzy-checkable column is registered."""
    from canopy.query.fuzzy_match import FuzzyMatch

    species_match = FuzzyMatch(
        literal="Gralari gigantae", candidates=("Grallaria gigantea",), label_key="species"
    )
    site_match = FuzzyMatch(
        literal="Buenaventuraa", candidates=("Reserva Buenaventura",), label_key="site"
    )
    mu_match = FuzzyMatch(
        literal="Waman", candidates=("Wamani", "Wamaní"), label_key="management_unit"
    )
    result = _make_result(
        sql="...", rows=[], row_count=0, fuzzy_matches=(species_match, site_match, mu_match)
    )
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    (*_, g1_prompt, g1_b1, _g1_b2, _g1_b3, g1_q1, _g1_q2, _g1_q3,
     g2_prompt, g2_b1, _g2_b2, _g2_b3, g2_q1, _g2_q2, _g2_q3,
     g3_prompt, g3_b1, g3_b2, _g3_b3, g3_q1, g3_q2, _g3_q3) = _run(
        "How many detections of Gralari gigantae at Buenaventuraa in Waman are there?"
    )

    assert g1_prompt["visible"] is True
    assert "Species" in g1_prompt["value"]
    assert g1_b1["value"] == "Grallaria gigantea"
    assert "Grallaria gigantea" in g1_q1

    assert g2_prompt["visible"] is True
    assert "Site" in g2_prompt["value"]
    assert g2_b1["value"] == "Reserva Buenaventura"
    assert "Reserva Buenaventura" in g2_q1

    assert g3_prompt["visible"] is True
    assert "Management unit" in g3_prompt["value"]
    assert g3_b1["value"] == "Wamani"
    assert g3_b2["value"] == "Wamaní"
    assert "Wamani" in g3_q1
    assert "Wamaní" in g3_q2


def test_handler_no_suggestions_on_normal_success(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    (*_, g1_prompt, g1_b1, g1_b2, g1_b3, g1_q1, g1_q2, g1_q3,
     g2_prompt, g2_b1, g2_b2, g2_b3, g2_q1, g2_q2, g2_q3,
     g3_prompt, g3_b1, g3_b2, g3_b3, g3_q1, g3_q2, g3_q3) = _run(
        "How many detections?"
    )
    for prompt, b1, b2, b3, q1, q2, q3 in (
        (g1_prompt, g1_b1, g1_b2, g1_b3, g1_q1, g1_q2, g1_q3),
        (g2_prompt, g2_b1, g2_b2, g2_b3, g2_q1, g2_q2, g2_q3),
        (g3_prompt, g3_b1, g3_b2, g3_b3, g3_q1, g3_q2, g3_q3),
    ):
        assert prompt["visible"] is False
        assert b1["visible"] is False
        assert b2["visible"] is False
        assert b3["visible"] is False
        assert q1 is None and q2 is None and q3 is None


def test_handler_no_suggestions_on_zero_rows_without_fuzzy_match(monkeypatch):
    """0 rows with no fuzzy_matches set (find_candidates found nothing) shows no suggestions."""
    monkeypatch.setattr(
        ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result(rows=[], row_count=0)
    )
    (*_, g1_prompt, g1_b1, _g1_b2, _g1_b3, _g1_q1, _g1_q2, _g1_q3,
     _g2_prompt, _g2_b1, _g2_b2, _g2_b3, _g2_q1, _g2_q2, _g2_q3,
     _g3_prompt, _g3_b1, _g3_b2, _g3_b3, _g3_q1, _g3_q2, _g3_q3) = _run("q")
    assert g1_prompt["visible"] is False
    assert g1_b1["visible"] is False


def test_clear_handler_hides_suggestions(monkeypatch):
    monkeypatch.setattr(ui_mod, "clear_history", lambda: None)
    (*_, g1_prompt, g1_b1, g1_b2, g1_b3, g1_q1, g1_q2, g1_q3,
     g2_prompt, g2_b1, g2_b2, g2_b3, g2_q1, g2_q2, g2_q3,
     g3_prompt, g3_b1, g3_b2, g3_b3, g3_q1, g3_q2, g3_q3) = (
        ui_mod._clear_handler("q")
    )
    assert g1_prompt["visible"] is False
    assert g1_b1["visible"] is False
    assert g1_q1 is None
    assert g2_prompt["visible"] is False
    assert g2_q1 is None
    assert g3_prompt["visible"] is False
    assert g3_q1 is None


# ---------------------------------------------------------------------------
# Session history — per-browser localStorage-backed isolation
# ---------------------------------------------------------------------------


def test_handler_appends_question_to_session_history(monkeypatch):
    """Successful query prepends the question to session history."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    _, _, _, _, radio, _, _, new_state, *_ = _run("new question")
    import gradio as gr
    assert isinstance(radio, gr.Radio)
    assert "new question" in new_state


def test_handler_prepends_to_existing_history(monkeypatch):
    """New question goes to the front; previous entries are preserved."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    _, _, _, _, _, _, _, new_state, *_ = _run("new question", session_history=["old question"])
    assert new_state == ["new question", "old question"]


def test_handler_caps_history_at_20(monkeypatch):
    """History is capped at 20 entries — oldest entries are dropped."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    initial = [f"q{i}" for i in range(20)]
    _, _, _, _, _, _, _, new_state, *_ = _run("new question", session_history=initial)
    assert len(new_state) == 20
    assert new_state[0] == "new question"
    assert "q19" not in new_state  # oldest dropped


def test_handler_deduplicates_repeated_question(monkeypatch):
    """Re-running a question moves it to the top instead of adding a duplicate."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    initial = ["repeated q", "other q"]
    _, _, _, _, _, _, _, new_state, *_ = _run("repeated q", session_history=initial)
    assert new_state.count("repeated q") == 1
    assert new_state[0] == "repeated q"
    assert "other q" in new_state


def test_handler_drops_superseded_question_from_history(monkeypatch):
    """Clicking a fuzzy-match suggestion re-runs the corrected question and
    must drop the original mistyped one from history — not leave it sitting
    alongside the correction as a dead-end entry that hits the same 0-row
    result if clicked again."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    initial = ["How many detections of Gralari gigantae are there?", "other q"]
    _, _, _, _, _, _, _, new_state, *_ = _run(
        "How many detections of Grallaria gigantea are there?",
        session_history=initial,
        superseded="How many detections of Gralari gigantae are there?",
    )
    assert "How many detections of Gralari gigantae are there?" not in new_state
    assert new_state[0] == "How many detections of Grallaria gigantea are there?"
    assert "other q" in new_state


def test_handler_superseded_question_none_is_a_no_op(monkeypatch):
    """A normal (non-suggestion-click) run passes no superseded_question and
    must not accidentally drop anything from history."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: _make_result())
    initial = ["existing q"]
    _, _, _, _, _, _, _, new_state, *_ = _run("new question", session_history=initial)
    assert "existing q" in new_state
    assert "new question" in new_state


# ---------------------------------------------------------------------------
# _run_query_handler — error paths
# ---------------------------------------------------------------------------


def test_handler_run_query_raises(monkeypatch):
    def _boom(q, status_cb=None, **_kw):
        raise RuntimeError("DB is down")

    monkeypatch.setattr(ui_mod, "run_query", _boom)
    sql, df, response, count_md, _, timing, status, state, *__ = _run("anything")
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

    def _guard_fail(q, status_cb=None, **_kw):
        raise SQLGuardError("Only SELECT queries are permitted", sql=bad_sql)

    monkeypatch.setattr(ui_mod, "run_query", _guard_fail)
    sql, df, response, count_md, _, timing, status, state, *__ = _run("drop something")
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

    def _guard_fail(q, status_cb=None, **_kw):
        raise SQLGuardError("Only SELECT queries are permitted", sql=bad_sql)

    monkeypatch.setattr(ui_mod, "run_query", _guard_fail)
    _, _, response, _, _, _, status, _, *__ = _run("delete that detection")
    assert "DELETE" in response
    assert "DELETE" in status


def test_handler_statement_timeout_gives_actionable_message(monkeypatch):
    """psycopg2 QueryCanceled (statement_timeout) → specific timeout message."""
    import psycopg2.errors

    def _timeout(q, status_cb=None, **_kw):
        raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(ui_mod, "run_query", _timeout)
    _, _, response, _, _, _, status, _, *__ = _run("huge query")
    assert "too long" in response.lower()
    assert "⚠" in status
    assert "timed out" in status.lower()


def test_handler_catches_unsupported_language_error_from_run_query(monkeypatch):
    """Defense-in-depth path: is_unsupported_language() in the handler only
    inspects the raw question text, so it can't see what run_query() itself
    might raise. This exercises that branch directly — the same way
    SQLGuardError/QueryCanceled are exercised above — rather than leaving it
    uncovered because it's normally unreachable through the UI's own
    pre-check on a real (non-mocked) run_query."""

    def _raise_unsupported_language(q, status_cb=None, **_kw):
        raise UnsupportedLanguageError("Canopy only supports questions in English or Spanish.")

    monkeypatch.setattr(ui_mod, "run_query", _raise_unsupported_language)
    _, _, response, _, _, _, status, _, *__ = _run("a normal English question here")
    assert t("error_unsupported_language") in response
    assert t("error_unsupported_language_status") in status


def test_handler_db_connection_error_message(monkeypatch):
    """psycopg2 OperationalError (connection lost) → specific connection message."""
    import psycopg2

    def _conn_fail(q, status_cb=None, **_kw):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(ui_mod, "run_query", _conn_fail)
    _, _, response, _, _, _, status, _, *__ = _run("any question")
    assert "database" in response.lower()
    assert "⚠" in status
    assert "unreachable" in status.lower()


def test_handler_loop_exhausted_message(monkeypatch):
    """RuntimeError from MAX_ITERATIONS → specific complexity message."""

    def _exhaust(q, status_cb=None, **_kw):
        raise RuntimeError("Query loop exceeded maximum iterations")

    monkeypatch.setattr(ui_mod, "run_query", _exhaust)
    _, _, response, _, _, _, status, _, *__ = _run("very complex question")
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

    def _return_cached(q, status_cb=None, **_kw):
        if status_cb:
            status_cb("CACHE_HIT")
        return cached_result

    monkeypatch.setattr(ui_mod, "run_query", _return_cached)

    yields = _all_yields("How many detections?")
    # One of the intermediate yields' step log (response_box) should
    # mention cache — status_md is the generic "Working" ticker, not a
    # copy of the current step (see the no-duplication regression test).
    responses = [y[2] for y in yields]
    assert any("previous" in r.lower() or "cache" in r.lower() for r in responses)

    # Final yield timing_md should show cached indicator
    final = yields[-1]
    assert "⚡" in final[5] or "Cached" in final[5]


# ---------------------------------------------------------------------------
# Language gate — is_unsupported_language() moved to query/loop.py so
# run_query() is self-protecting for direct callers too (Phase 7). Pure
# detection-logic tests live in test_query_loop.py now; kept here is only
# the UI-integration test confirming app.py still wires the shared check in.
# ---------------------------------------------------------------------------


def test_handler_rejects_unsupported_language_before_calling_run_query(monkeypatch):
    """UI gate must reject non-EN/ES input without ever calling run_query()."""
    called = False

    def _should_not_be_called(q, status_cb=None, **_kw):
        nonlocal called
        called = True
        return _make_result()

    monkeypatch.setattr(ui_mod, "run_query", _should_not_be_called)
    _, _, response, _, _, _, status, _, *_ = _run(
        "Combien d'espèces ont été détectées en 2023?"
    )
    assert not called, "run_query() must not be called for rejected-language input"
    assert t("error_unsupported_language") in response


def test_handler_unsupported_language_rejected(monkeypatch):
    """French question: language gate rejects before run_query; shows localized error."""
    spy_calls: list = []
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None, **_kw: spy_calls.append(q) or _make_result(),
    )
    sql, df, response, count_md, radio, timing, status, state, *_ = _run(
        "Combien d'espèces ont été détectées en 2023?"
    )
    assert spy_calls == [], "run_query must not be called — language gate is a cost gate"
    assert t("error_unsupported_language") in response
    assert t("error_unsupported_language_status") in status
    assert sql == ""
    assert count_md == ""
    assert timing == ""


# ---------------------------------------------------------------------------
# _clear_handler
# ---------------------------------------------------------------------------


def test_clear_handler_calls_clear_history(monkeypatch):
    called = []
    monkeypatch.setattr(ui_mod, "clear_history", lambda: called.append(True))
    ui_mod._clear_handler("some question")
    assert called == [True]


def test_clear_handler_empties_question(monkeypatch):
    monkeypatch.setattr(ui_mod, "clear_history", lambda: None)
    # _clear_handler preserves the question box text passed in
    radio, question, response, row_count, table, sql, timing, status, state, *_ = (
        ui_mod._clear_handler("my question")
    )
    assert question == "my question"
    assert state == []


# ---------------------------------------------------------------------------
# Step-log narration — response_box accumulates every real status_cb()
# message as a running log (replaces the old static "I understood: {intent}
# ... Searching the database..." text, which could contradict the status
# bar once it had moved on to a later phase). No special INTENT: parsing
# exists — loop.py never sends that prefix; any status string, whatever
# its content, becomes one line in the log.
# ---------------------------------------------------------------------------


def test_handler_step_log_includes_every_status_message(monkeypatch):
    result = _make_result()

    def _emit_status(q, status_cb=None, **_kw):
        if status_cb:
            status_cb("Looking for species counts")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_status)
    yields = _all_yields("How many species?")

    responses = [y[2] for y in yields]
    assert any("Looking for species counts" in r for r in responses)


# ---------------------------------------------------------------------------
# Progressive result disclosure — rows reach the UI before the narrative
# ---------------------------------------------------------------------------


def _preview_qr():
    from canopy.query.executor import QueryResult

    return QueryResult(
        columns=("site", "n"), rows=(("Tapichalaca", 14), ("Yanacocha", 9)), row_count=2
    )


def _run_with_preview(monkeypatch, *, retry=False):
    """Drive the handler through a run where execute_sql reports its result early."""
    qr = _preview_qr()
    sql = "SELECT site, count(*) n FROM detections GROUP BY site"
    result = _make_result(row_count=2, rows=list(qr.rows), columns=list(qr.columns), sql=sql)

    def _emit(q, status_cb=None, result_cb=None, **_kw):
        status_cb(t("status_understanding"))
        if retry:
            # First attempt returns nothing, model corrects itself.
            from canopy.query.executor import QueryResult

            result_cb(QueryResult(columns=("n",), rows=(), row_count=0), "SELECT 1")
            status_cb(t("status_refining"))
        result_cb(qr, sql)
        status_cb(t("found_detections_plural", n=2))
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit)
    return _all_yields("Which sites had the most detections?")


def _table_rows(y):
    value = getattr(y[1], "value", None)
    if not value:
        return []
    return value.get("data") or []


def test_handler_shows_rows_before_final_yield(monkeypatch):
    """The whole point: real rows render while the model is still writing."""
    yields = _run_with_preview(monkeypatch)
    early = [i for i, y in enumerate(yields[:-1]) if _table_rows(y)]
    assert early, "table was still empty on every yield before the final one"
    assert _table_rows(yields[early[0]]) == [["Tapichalaca", 14], ["Yanacocha", 9]]


def test_handler_shows_sql_before_final_yield(monkeypatch):
    """The generated SQL is disclosed alongside the rows, not held back."""
    yields = _run_with_preview(monkeypatch)
    assert any(y[0] for y in yields[:-1]), "SQL box empty on every pre-final yield"


def test_handler_preview_persists_across_later_status_updates(monkeypatch):
    """Gradio tuples are fixed-shape — a later status tick must not blank the table."""
    yields = _run_with_preview(monkeypatch)
    first = next(i for i, y in enumerate(yields) if _table_rows(y))
    assert all(_table_rows(y) for y in yields[first:]), (
        "a status update after the preview cleared the table"
    )


def test_handler_retry_supersedes_earlier_preview(monkeypatch):
    """A corrected query replaces the previous table rather than appending to it."""
    yields = _run_with_preview(monkeypatch, retry=True)
    assert _table_rows(yields[-1]) == [["Tapichalaca", 14], ["Yanacocha", 9]]
    # The superseded 0-row attempt must never leave stale rows behind.
    assert all(
        rows in ([], [["Tapichalaca", 14], ["Yanacocha", 9]])
        for rows in (_table_rows(y) for y in yields)
    )


def test_handler_no_preview_when_result_cb_never_fires(monkeypatch):
    """Cache hits skip execute_sql — the table stays empty until the final yield."""
    result = _make_result()

    def _emit(q, status_cb=None, result_cb=None, **_kw):
        status_cb("CACHE_HIT")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit)
    yields = _all_yields("cached question")
    assert not any(_table_rows(y) for y in yields[:-1])


def test_step_log_markdown_mutes_completed_steps_and_bolds_current():
    md = ui_mod._step_log_markdown([("other", "Step one"), ("other", "Step two")])
    assert "~~Step one~~" in md
    assert "**Step two**" in md
    assert "~~Step two~~" not in md


def test_step_log_markdown_empty_list_returns_empty_string():
    assert ui_mod._step_log_markdown([]) == ""


def test_handler_step_log_does_not_duplicate_consecutive_identical_messages(monkeypatch):
    """A message repeated back-to-back (e.g. the same phase reported twice)
    must not create two identical lines in the log."""
    result = _make_result()

    def _emit_repeated(q, status_cb=None, **_kw):
        if status_cb:
            status_cb("Searching the monitoring database…")
            status_cb("Searching the monitoring database…")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_repeated)
    yields = _all_yields("How many species?")
    # Last intermediate yield (before the final success yield replaces
    # response_box with the real answer) holds the fullest step log.
    last_intermediate_response = yields[-2][2]
    assert last_intermediate_response.count("Searching the monitoring database…") <= 1


def test_handler_step_log_drops_superseded_result_on_retry(monkeypatch):
    """Regression test: a "Found N" line immediately followed by a retry
    announcement must be removed from the log, not left visible — two
    "Found N" lines back-to-back (or the same count appearing twice with
    nothing explaining why) reads as a duplicate/confusing result rather
    than one search correcting itself."""
    result = _make_result()

    def _emit_retry_sequence(q, status_cb=None, **_kw):
        if status_cb:
            status_cb(t("status_searching_db"))
            status_cb(t("found_detections_plural", n=14))
            status_cb(t("status_searching_db"))
            status_cb(t("status_writing_sql_retry", n=2))
            status_cb(t("status_searching_db"))
            status_cb(t("found_detections_plural_retry", n=637))
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_retry_sequence)
    yields = _all_yields("How many species?")
    last_intermediate_response = yields[-2][2]
    # The superseded "Found 14 detections" line must be gone by the time
    # the retry lands — only the final, current-attempt count remains.
    assert t("found_detections_plural", n=14) not in last_intermediate_response
    assert t("found_detections_plural_retry", n=637) in last_intermediate_response


def test_handler_step_log_collapses_adjacent_duplicates_after_drop(monkeypatch):
    """Regression test (caught live in review): attempt 2's own "Searching
    the monitoring database..." can arrive BEFORE its own retry
    announcement (execute_sql's status_cb and the agent's ToolCall stream
    handler run on different code paths with no fixed ordering) — so the
    sequence is Searching -> Found 14 -> Searching -> Trying again. Once
    "Found 14" is dropped as superseded, the two "Searching..." lines
    become adjacent and must collapse into one, not render as two
    identical lines back-to-back."""
    result = _make_result()

    def _emit_out_of_order_retry(q, status_cb=None, **_kw):
        if status_cb:
            status_cb(t("status_searching_db"))
            status_cb(t("found_detections_plural", n=14))
            status_cb(t("status_searching_db"))  # attempt 2's own search
            status_cb(t("status_writing_sql_retry", n=2))  # arrives after
            status_cb(t("found_detections_plural_retry", n=637))
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_out_of_order_retry)
    yields = _all_yields("How many species?")
    last_intermediate_response = yields[-2][2]
    assert last_intermediate_response.count(t("status_searching_db")) <= 1


def test_classify_step_tags_retry_message():
    msg = t("status_writing_sql_retry", n=3)
    assert ui_mod._classify_step(msg) == ui_mod._StepKind.RETRY
    assert ui_mod._classify_step(t("status_searching_db")) == ui_mod._StepKind.SEARCHING


def test_classify_step_tags_found_and_refining_as_result():
    assert ui_mod._classify_step(t("found_detections_singular", n=1)) == ui_mod._StepKind.RESULT
    assert ui_mod._classify_step(t("found_detections_plural", n=99)) == ui_mod._StepKind.RESULT
    assert ui_mod._classify_step(t("status_refining")) == ui_mod._StepKind.RESULT
    assert ui_mod._classify_step(t("status_searching_db")) != ui_mod._StepKind.RESULT
    assert ui_mod._classify_step(t("status_writing_sql_retry", n=2)) != ui_mod._StepKind.RESULT


def test_classify_step_tags_unrelated_text_as_other():
    assert ui_mod._classify_step("some arbitrary message") == ui_mod._StepKind.OTHER


def test_classify_step_correct_under_spanish_locale():
    """_classify_step's docstring claims it "stays correct under
    CANOPY_UI_LANG=es" — this was verified by hand mid-session but never
    captured as a permanent test (code-review finding). A future edit to
    es.py's wording could silently break Spanish users' step-log dedup
    with nothing else catching it."""
    original_lang = get_lang()
    try:
        set_locale("es")
        assert ui_mod._classify_step(t("status_searching_db")) == ui_mod._StepKind.SEARCHING
        assert ui_mod._classify_step(t("status_refining")) == ui_mod._StepKind.RESULT
        assert (
            ui_mod._classify_step(t("found_detections_singular", n=1))
            == ui_mod._StepKind.RESULT
        )
        assert (
            ui_mod._classify_step(t("found_detections_plural", n=42))
            == ui_mod._StepKind.RESULT
        )
        assert (
            ui_mod._classify_step(t("found_detections_plural_retry", n=42))
            == ui_mod._StepKind.RESULT
        )
        assert (
            ui_mod._classify_step(t("status_writing_sql_retry", n=2))
            == ui_mod._StepKind.RETRY
        )
    finally:
        set_locale(original_lang)


def test_append_step_deduplicates_consecutive_identical_entries():
    steps: list[tuple[str, str]] = []
    ui_mod._append_step(steps, t("status_searching_db"))
    ui_mod._append_step(steps, t("status_searching_db"))
    assert steps.count((ui_mod._StepKind.SEARCHING, t("status_searching_db"))) == 1


def test_append_step_stops_backward_scan_at_earlier_retry():
    """A retry announcement must only drop the RESULT belonging to the
    attempt it directly follows — not reach back across an earlier
    attempt's already-cleaned retry boundary."""
    steps: list[tuple[str, str]] = []
    ui_mod._append_step(steps, t("found_detections_plural", n=1))  # attempt 1 result
    ui_mod._append_step(steps, t("status_writing_sql_retry", n=2))  # clears attempt 1's result
    ui_mod._append_step(steps, t("found_detections_plural", n=2))  # attempt 2 result
    ui_mod._append_step(steps, t("status_writing_sql_retry", n=3))  # clears attempt 2's result only
    kinds = [k for k, _ in steps]
    assert kinds.count(ui_mod._StepKind.RESULT) == 0
    assert kinds.count(ui_mod._StepKind.RETRY) == 2


def test_handler_yields_other_status_messages(monkeypatch):
    """Arbitrary status_cb() messages appear in response_box's step log —
    status_md (top bar) intentionally stays the generic "Working" ticker."""
    result = _make_result()

    def _emit_status(q, status_cb=None, **_kw):
        if status_cb:
            status_cb("Querying the database...")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_status)
    yields = _all_yields("How many species?")

    responses = [y[2] for y in yields]
    assert any("Querying" in r for r in responses)


# ---------------------------------------------------------------------------
# model_label — conn_id != model_name branch (line 229)
# ---------------------------------------------------------------------------


def test_handler_timing_shows_conn_slash_model_when_different(monkeypatch):
    """When connection_id differs from model name, timing shows conn_id/model_name."""
    result = _make_result(
        timing={
            "total_s": 1.0, "llm_s": 0.9, "llm_calls": 1,
            "db_s": 0.05, "db_calls": 1,
            "connection_id": "my-azure-conn",
            "model": "gpt-4o-mini",
        }
    )
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    _, _, _, _, _, timing_md, _, _, *_ = _run("q")
    assert "my-azure-conn/gpt-4o-mini" in timing_md


def test_handler_timing_shows_conn_only_when_same(monkeypatch):
    """When connection_id equals model name, timing shows just conn_id."""
    result = _make_result(
        timing={
            "total_s": 1.0, "llm_s": 0.9, "llm_calls": 1,
            "db_s": 0.05, "db_calls": 1,
            "connection_id": "gpt-4o",
            "model": "gpt-4o",
        }
    )
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    _, _, _, _, _, timing_md, _, _, *_ = _run("q")
    assert "· gpt-4o" in timing_md
    assert "gpt-4o/gpt-4o" not in timing_md


# ---------------------------------------------------------------------------
# _render_interpretation / _render_response
# ---------------------------------------------------------------------------


def test_render_interpretation_returns_empty_string_for_none():
    assert ui_mod._render_interpretation(None) == ""


def test_render_interpretation_full_block():
    interp = Interpretation(
        data_source="detections · approved only",
        gaps=("Some species absent",),
        research_questions=("Do counts match last year?",),
    )
    rendered = ui_mod._render_interpretation(interp)
    assert t("interpretation_heading") in rendered
    assert "detections · approved only" in rendered
    assert "Some species absent" in rendered
    assert "Do counts match last year?" in rendered


def test_render_interpretation_empty_gaps_shows_none_literal():
    interp = Interpretation(data_source="sites · all rows", gaps=(), research_questions=())
    rendered = ui_mod._render_interpretation(interp)
    assert t("interpretation_gaps_none") in rendered


def test_render_interpretation_omits_research_questions_when_empty():
    interp = Interpretation(data_source="sites · all rows", gaps=(), research_questions=())
    rendered = ui_mod._render_interpretation(interp)
    assert t("interpretation_research") not in rendered


def test_render_response_strips_raw_block_and_appends_rendering():
    model_text = (
        "**Headline:** 4 models used.\n\n"
        "---\n"
        "DATA SOURCE: detections · approved only\n"
        "GAPS: none\n"
        "---\n"
    )
    interp = Interpretation(
        data_source="detections · approved only", gaps=(), research_questions=()
    )
    result = _make_result(model_text=model_text, interpretation=interp)
    rendered = ui_mod._render_response(result)
    assert "DATA SOURCE:" not in rendered  # raw block stripped
    assert t("interpretation_heading") in rendered  # styled version present
    assert "**Headline:** 4 models used." in rendered


def test_render_response_unchanged_when_interpretation_none():
    result = _make_result(model_text="Plain answer, no block.", interpretation=None)
    assert ui_mod._render_response(result) == "Plain answer, no block."


def test_handler_response_box_uses_rendered_interpretation(monkeypatch):
    """End-to-end: the handler's final yield must use _render_response, not raw model_text."""
    model_text = (
        "Answer text.\n\n---\nDATA SOURCE: detections · approved only\nGAPS: none\n---\n"
    )
    interp = Interpretation(
        data_source="detections · approved only", gaps=(), research_questions=()
    )
    result = _make_result(model_text=model_text, interpretation=interp)
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None, **_kw: result)

    _, _, response, *_ = _run("q")
    assert "DATA SOURCE:" not in response
    assert t("interpretation_heading") in response
