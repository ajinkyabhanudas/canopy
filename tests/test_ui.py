"""Unit tests for canopy.ui.app handler functions. No browser or server needed."""

from __future__ import annotations

import canopy.ui.app as ui_mod
from canopy.i18n import t
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
    """User should see loading state immediately before any model call."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    first, *_ = _all_yields("How many detections?")
    assert len(first) == 30
    _, _, response, _, _, _, status_md, state, *_ = first
    assert t("status_reading") in response
    assert t("status_reading") in status_md


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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    sql, df, response, count_md, radio, timing, status, state, *_ = _run("How many detections?")
    assert sql.startswith("SELECT COUNT(*) FROM detections")
    assert response == "There are 5 detections."
    assert "5" in count_md
    assert status == ""  # cleared on success


def test_handler_timing_line(monkeypatch):
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    sql, _, _, _, _, timing, _, _, *_ = _run("q")
    assert t("timing_live", total=1.0)[:14] in timing
    # dev metrics moved to sql comment
    assert "LLM" in sql
    assert "DB" in sql


def test_handler_singular_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod, "run_query", lambda q, status_cb=None: _make_result(row_count=1, rows=[(1,)])
    )
    _, _, _, count_md, _, _, _, _, *_ = _run("q")
    assert t("count_row_singular", n=1) in count_md
    assert "rows" not in count_md


def test_handler_plural_row_count(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None: _make_result(row_count=3, rows=[(1,), (2,), (3,)]),
    )
    _, _, _, count_md, _, _, _, _, *_ = _run("q")
    assert t("count_row_plural", n=3) in count_md


def test_handler_rows_converted_to_lists(monkeypatch):
    result = _make_result(rows=[(1, "a"), (2, "b")], columns=["id", "name"], row_count=2)
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)
    _, df, _, _, _, _, _, _, *_ = _run("q")
    import gradio as gr
    assert isinstance(df, gr.Dataframe)


def test_handler_null_sql(monkeypatch):
    monkeypatch.setattr(
        ui_mod,
        "run_query",
        lambda q, status_cb=None: _make_result(sql=None, rows=[], row_count=0),
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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    (*_, g1_prompt, g1_b1, g1_b2, g1_b3, g1_q1, g1_q2, g1_q3,
     g2_prompt, g2_b1, g2_b2, g2_b3, g2_q1, g2_q2, g2_q3,
     g3_prompt, g3_b1, g3_b2, g3_b3, g3_q1, g3_q2, g3_q3) = _run("How many detections?")
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
        ui_mod, "run_query", lambda q, status_cb=None: _make_result(rows=[], row_count=0)
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
     g3_prompt, g3_b1, g3_b2, g3_b3, g3_q1, g3_q2, g3_q3) = ui_mod._clear_handler("q")
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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    _, _, _, _, radio, _, _, new_state, *_ = _run("new question")
    import gradio as gr
    assert isinstance(radio, gr.Radio)
    assert "new question" in new_state


def test_handler_prepends_to_existing_history(monkeypatch):
    """New question goes to the front; previous entries are preserved."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    _, _, _, _, _, _, _, new_state, *_ = _run("new question", session_history=["old question"])
    assert new_state == ["new question", "old question"]


def test_handler_caps_history_at_20(monkeypatch):
    """History is capped at 20 entries — oldest entries are dropped."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    initial = [f"q{i}" for i in range(20)]
    _, _, _, _, _, _, _, new_state, *_ = _run("new question", session_history=initial)
    assert len(new_state) == 20
    assert new_state[0] == "new question"
    assert "q19" not in new_state  # oldest dropped


def test_handler_deduplicates_repeated_question(monkeypatch):
    """Re-running a question moves it to the top instead of adding a duplicate."""
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: _make_result())
    initial = ["repeated q", "other q"]
    _, _, _, _, _, _, _, new_state, *_ = _run("repeated q", session_history=initial)
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

    def _guard_fail(q, status_cb=None):
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

    def _guard_fail(q, status_cb=None):
        raise SQLGuardError("Only SELECT queries are permitted", sql=bad_sql)

    monkeypatch.setattr(ui_mod, "run_query", _guard_fail)
    _, _, response, _, _, _, status, _, *__ = _run("delete that detection")
    assert "DELETE" in response
    assert "DELETE" in status


def test_handler_statement_timeout_gives_actionable_message(monkeypatch):
    """psycopg2 QueryCanceled (statement_timeout) → specific timeout message."""
    import psycopg2.errors

    def _timeout(q, status_cb=None):
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

    def _raise_unsupported_language(q, status_cb=None):
        raise UnsupportedLanguageError("Canopy only supports questions in English or Spanish.")

    monkeypatch.setattr(ui_mod, "run_query", _raise_unsupported_language)
    _, _, response, _, _, _, status, _, *__ = _run("a normal English question here")
    assert t("error_unsupported_language") in response
    assert t("error_unsupported_language_status") in status


def test_handler_db_connection_error_message(monkeypatch):
    """psycopg2 OperationalError (connection lost) → specific connection message."""
    import psycopg2

    def _conn_fail(q, status_cb=None):
        raise psycopg2.OperationalError("could not connect to server")

    monkeypatch.setattr(ui_mod, "run_query", _conn_fail)
    _, _, response, _, _, _, status, _, *__ = _run("any question")
    assert "database" in response.lower()
    assert "⚠" in status
    assert "unreachable" in status.lower()


def test_handler_loop_exhausted_message(monkeypatch):
    """RuntimeError from MAX_ITERATIONS → specific complexity message."""

    def _exhaust(q, status_cb=None):
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
# Language gate — is_unsupported_language() moved to query/loop.py so
# run_query() is self-protecting for direct callers too (Phase 7). Pure
# detection-logic tests live in test_query_loop.py now; kept here is only
# the UI-integration test confirming app.py still wires the shared check in.
# ---------------------------------------------------------------------------


def test_handler_rejects_unsupported_language_before_calling_run_query(monkeypatch):
    """UI gate must reject non-EN/ES input without ever calling run_query()."""
    called = False

    def _should_not_be_called(q, status_cb=None):
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
        lambda q, status_cb=None: spy_calls.append(q) or _make_result(),
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
# Streaming INTENT: message branch (lines 168-179)
# ---------------------------------------------------------------------------


def test_handler_yields_intent_status_message(monkeypatch):
    """When run_query sends INTENT: message, a status with the intent text is yielded."""

    result = _make_result()

    def _emit_intent(q, status_cb=None):
        if status_cb:
            status_cb("INTENT:Looking for species counts")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_intent)
    yields = _all_yields("How many species?")

    # One of the intermediate yields should contain the intent text
    responses = [y[2] for y in yields]
    assert any("Looking for species counts" in r for r in responses)


def test_handler_yields_other_status_messages(monkeypatch):
    """Non-CACHE_HIT, non-INTENT status messages are passed through as status text."""
    result = _make_result()

    def _emit_status(q, status_cb=None):
        if status_cb:
            status_cb("Querying the database...")
        return result

    monkeypatch.setattr(ui_mod, "run_query", _emit_status)
    yields = _all_yields("How many species?")

    statuses = [y[6] for y in yields]
    assert any("Querying" in s for s in statuses)


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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

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
    monkeypatch.setattr(ui_mod, "run_query", lambda q, status_cb=None: result)

    _, _, response, *_ = _run("q")
    assert "DATA SOURCE:" not in response
    assert t("interpretation_heading") in response
