"""
Structural unit tests for the ground-truth eval set.

Verifies shape, integrity, and check_fn behaviour without making any live
API or database calls. Runs as part of the normal test suite.

Faithfulness tests (marked live_db) require a live PostgreSQL connection and
API keys — run via `make eval-gt` or `pytest -m live_db`.
"""

from __future__ import annotations

import pytest

from canopy.query.executor import execute_query
from canopy.query.loop import LoopResult, run_query
from tests.eval.queries import (
    EVAL_CASES,
    EvalCase,
    _all_row_values_in_text,
    _col_has,
    _q1_species_validated_at_any_site,
    _q4_annual_detection_counts,
    _q7_status_breakdown,
    _q13_high_confidence,
    _q14_species_zero_validated,
    _q17_population_trend_declined,
    _q18_iucn_flagged_not_in_db,
    _q20_conservation_priority_declined,
    _q49_multirow_breakdown_all_values_faithful,
    _sql_has,
    _text_has,
)

# ---------------------------------------------------------------------------
# Factory — build mock LoopResult instances
# ---------------------------------------------------------------------------


def _make_result(
    sql: str | None = "SELECT 1",
    columns: list[str] | tuple[str, ...] | None = None,
    rows: list[tuple] | tuple[tuple, ...] | None = None,
    row_count: int = 1,
    model_text: str = "",
) -> LoopResult:
    _cols = columns if columns is not None else []
    _rows = rows if rows is not None else [(1,)]
    if isinstance(_cols, list):
        _cols = tuple(_cols)
    if isinstance(_rows, list):
        _rows = tuple(tuple(r) if isinstance(r, list) else r for r in _rows)
    return LoopResult(
        question="test question",
        sql=sql,
        columns=_cols,
        rows=_rows,
        row_count=row_count,
        model_text=model_text,
    )


# ---------------------------------------------------------------------------
# Structure checks
# ---------------------------------------------------------------------------


def test_eval_cases_has_exactly_49_entries():
    assert len(EVAL_CASES) == 49


def test_all_questions_are_nonempty_strings():
    for i, case in enumerate(EVAL_CASES, 1):
        assert isinstance(case.question, str) and case.question.strip(), \
            f"Q{i:02d} has empty question"


def test_all_descriptions_are_nonempty_strings():
    for i, case in enumerate(EVAL_CASES, 1):
        assert isinstance(case.description, str) and case.description.strip(), \
            f"Q{i:02d} has empty description"


def test_all_check_fns_are_callable():
    for i, case in enumerate(EVAL_CASES, 1):
        assert callable(case.check_fn), f"Q{i:02d} check_fn is not callable"


def test_all_questions_are_unique():
    questions = [c.question for c in EVAL_CASES]
    assert len(questions) == len(set(questions)), "Duplicate questions detected in EVAL_CASES"


def test_all_cases_are_eval_case_instances():
    for case in EVAL_CASES:
        assert isinstance(case, EvalCase)


def test_eval_case_is_frozen():
    case = EVAL_CASES[0]
    with pytest.raises(Exception):  # FrozenInstanceError
        case.question = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helper function behaviour
# ---------------------------------------------------------------------------


def test_sql_has_returns_true_when_all_terms_present():
    pred = _sql_has("validated_true", "species")
    assert pred(
        "SELECT * FROM detections WHERE validation_status = 'validated_true' AND species_id = 1"
    )


def test_sql_has_returns_false_when_term_missing():
    pred = _sql_has("validated_true", "management_unit")
    assert not pred("SELECT * FROM detections WHERE validation_status = 'validated_true'")


def test_sql_has_returns_false_for_none():
    assert not _sql_has("select")(None)


def test_sql_has_is_case_insensitive():
    assert _sql_has("VALIDATED_TRUE")("select * where validation_status = 'validated_true'")


def test_col_has_returns_true_on_partial_match():
    r = _make_result(columns=["scientific_name", "site_name"])
    assert _col_has(r, "scientific_name")
    assert _col_has(r, "site")  # partial match against "site_name"


def test_col_has_returns_false_when_no_match():
    r = _make_result(columns=["confidence", "recorded_at"])
    assert not _col_has(r, "scientific_name", "species")


def test_col_has_is_case_insensitive():
    r = _make_result(columns=["Scientific_Name"])
    assert _col_has(r, "scientific_name")


def test_text_has_returns_true_on_match():
    r = _make_result(model_text="This requires a formal scientific review.")
    assert _text_has(r, "formal", "scientific review")


def test_text_has_returns_false_when_no_match():
    r = _make_result(model_text="Here are the results.")
    assert not _text_has(r, "trend", "iucn", "cannot")


# ---------------------------------------------------------------------------
# Selected check_fn behaviour — happy path
# ---------------------------------------------------------------------------


def test_q1_passes_with_correct_result():
    r = _make_result(
        sql="SELECT s.scientific_name FROM detections d "
            "JOIN species s ON d.species_id = s.id "
            "WHERE d.validation_status = 'approved'",
        columns=["scientific_name"],
        row_count=5,
    )
    assert _q1_species_validated_at_any_site(r)


def test_q1_fails_without_approved():
    r = _make_result(
        sql="SELECT scientific_name FROM species",
        columns=["scientific_name"],
        row_count=5,
    )
    assert not _q1_species_validated_at_any_site(r)


def test_q1_fails_when_sql_is_none():
    r = _make_result(sql=None, columns=[], row_count=0)
    assert not _q1_species_validated_at_any_site(r)


def test_q4_passes_with_year_and_count():
    r = _make_result(
        sql="SELECT EXTRACT(YEAR FROM recorded_at) AS year, COUNT(*) "
            "FROM detections WHERE validation_status = 'validated_true' GROUP BY year",
        columns=["year", "count"],
        row_count=3,
    )
    assert _q4_annual_detection_counts(r)


def test_q4_fails_without_year_grouping():
    r = _make_result(
        sql="SELECT COUNT(*) FROM detections",
        columns=["count"],
        row_count=1,
    )
    # 'year' does appear in "COUNT(*)" — actually no. Let me check:
    # 'year' not in "select count(*) from detections".lower() → should fail
    # Actually wait, 'year' is not a substring of "count". Let me verify.
    # The sql is "SELECT COUNT(*) FROM detections" — no 'year' → fails
    assert not _q4_annual_detection_counts(r)


def test_q7_passes_with_validation_status_grouped():
    r = _make_result(
        sql="SELECT validation_status, COUNT(*) FROM detections GROUP BY validation_status",
        columns=["validation_status", "count"],
        row_count=3,
    )
    assert _q7_status_breakdown(r)


def test_q13_passes_with_confidence_and_threshold():
    r = _make_result(
        sql="SELECT * FROM detections WHERE confidence > 0.95",
        columns=["id", "confidence", "species_id"],
        row_count=100,
    )
    assert _q13_high_confidence(r)


def test_q13_fails_without_threshold_operator():
    r = _make_result(
        sql="SELECT confidence FROM detections",
        columns=["confidence"],
        row_count=100,
    )
    # sql has "confidence" but no threshold operator or numeric value → fails
    assert not _q13_high_confidence(r)


def test_q14_passes_with_anti_join():
    r = _make_result(
        sql="SELECT s.scientific_name FROM species s "
            "WHERE s.id NOT IN ("
            "SELECT species_id FROM detections WHERE validation_status = 'validated_true'"
            ")",
        columns=["scientific_name"],
        row_count=0,
    )
    assert _q14_species_zero_validated(r)


def test_q14_fails_without_anti_join():
    r = _make_result(
        sql="SELECT scientific_name FROM species",
        columns=["scientific_name"],
        row_count=432,
    )
    assert not _q14_species_zero_validated(r)


# ---------------------------------------------------------------------------
# Guardrail check_fn behaviour
# ---------------------------------------------------------------------------


def test_q17_passes_with_trend_language():
    r = _make_result(
        sql=None,
        columns=[],
        row_count=0,
        model_text=(
            "I cannot determine population trends from this data alone. "
            "A formal scientific review is required."
        ),
    )
    assert _q17_population_trend_declined(r)


def test_q17_fails_without_guardrail_language():
    r = _make_result(
        sql="SELECT COUNT(*) FROM detections",
        columns=["count"],
        row_count=1,
        model_text="The total detection count is 35741.",
    )
    assert not _q17_population_trend_declined(r)


def test_q18_passes_with_iucn_language():
    r = _make_result(
        sql=None,
        columns=[],
        row_count=0,
        model_text=(
            "IUCN Red List data is not stored in this database. "
            "It is retrieved separately from the IUCN API."
        ),
    )
    assert _q18_iucn_flagged_not_in_db(r)


def test_q20_passes_with_conservation_caveat():
    r = _make_result(
        sql=None,
        columns=[],
        row_count=0,
        model_text=(
            "I cannot recommend conservation resource allocation. "
            "This requires expert scientific review beyond what this database provides."
        ),
    )
    assert _q20_conservation_priority_declined(r)


# ---------------------------------------------------------------------------
# check_fn return type — all 20 cases must return bool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    EVAL_CASES,
    ids=[f"Q{i + 1:02d}" for i in range(len(EVAL_CASES))],
)
def test_check_fn_returns_bool_on_minimal_result(case: EvalCase):
    """Each check_fn must return bool when called with a minimal LoopResult."""
    r = _make_result(sql=None, columns=[], rows=[], row_count=0, model_text="")
    result = case.check_fn(r)
    assert isinstance(result, bool), \
        f"{case.check_fn.__name__} returned {type(result).__name__}, expected bool"


# ---------------------------------------------------------------------------
# Faithfulness evals — require live DB + API keys (marked live_db)
#
# Each test: (1) runs execute_query() directly to get a ground-truth count,
# (2) runs run_query() to get the LoopResult, (3) asserts result.row_count
# matches the ground truth. This validates the model's generated SQL queries
# the correct data — not that it formats integers verbatim in model_text.
# ---------------------------------------------------------------------------


@pytest.mark.live_db
def test_faithfulness_approved_recordings_count() -> None:
    """Model's SQL must return the same count as a direct approved-recordings query."""
    ground_truth = execute_query(
        "SELECT COUNT(*) FROM recordings WHERE validation_status = 'approved'"
    ).rows[0][0]
    result = run_query("How many approved recordings are there?")
    assert result.row_count == ground_truth, (
        f"Model returned {result.row_count} rows; ground truth is {ground_truth}. "
        f"Generated SQL: {result.sql}"
    )


@pytest.mark.live_db
def test_faithfulness_total_recordings_count() -> None:
    """Model's SQL must return the total recording count matching a direct COUNT(*)."""
    ground_truth = execute_query("SELECT COUNT(*) FROM recordings").rows[0][0]
    result = run_query("What is the total number of recordings in the database?")
    assert result.row_count == ground_truth, (
        f"Model returned {result.row_count} rows; ground truth is {ground_truth}. "
        f"Generated SQL: {result.sql}"
    )


@pytest.mark.live_db
def test_faithfulness_species_count() -> None:
    """Model's SQL must return the distinct species count matching a direct query."""
    ground_truth = execute_query(
        "SELECT COUNT(DISTINCT scientific_name) FROM recordings"
    ).rows[0][0]
    result = run_query("How many distinct species are in the database?")
    assert result.row_count == ground_truth, (
        f"Model returned {result.row_count} rows; ground truth is {ground_truth}. "
        f"Generated SQL: {result.sql}"
    )


@pytest.mark.live_db
def test_faithfulness_high_confidence_count() -> None:
    """Model's SQL must count high-confidence detections correctly."""
    ground_truth = execute_query(
        "SELECT COUNT(*) FROM recordings WHERE confidence >= 0.95"
    ).rows[0][0]
    result = run_query("How many recordings have a confidence score of 0.95 or higher?")
    assert result.row_count == ground_truth, (
        f"Model returned {result.row_count} rows; ground truth is {ground_truth}. "
        f"Generated SQL: {result.sql}"
    )


@pytest.mark.live_db
def test_faithfulness_unknown_species_returns_zero() -> None:
    """Model must return 0 rows for a species name that does not exist in the database."""
    ground_truth = execute_query(
        "SELECT COUNT(*) FROM recordings WHERE scientific_name = 'Nonexistens fabricatus'"
    ).rows[0][0]
    assert ground_truth == 0, "Test setup: this species must not exist in the DB"
    result = run_query(
        "How many recordings are there for the species Nonexistens fabricatus?"
    )
    assert result.row_count == 0, (
        f"Model returned {result.row_count} rows for a non-existent species. "
        f"Generated SQL: {result.sql}"
    )


# ---------------------------------------------------------------------------
# _all_row_values_in_text / Q49 — multi-row faithfulness, no live DB needed
# ---------------------------------------------------------------------------


def test_all_row_values_in_text_passes_when_all_values_cited():
    r = _make_result(
        sql="SELECT model_id, COUNT(*) FROM detections GROUP BY model_id",
        rows=[("Model A", 25002), ("Model B", 351), ("Model C", 156)],
        row_count=3,
        model_text="Model A: 25,002. Model B: 351. Model C: 156.",
    )
    assert _all_row_values_in_text(r) is True


def test_all_row_values_in_text_fails_when_one_row_wrong():
    r = _make_result(
        sql="SELECT model_id, COUNT(*) FROM detections GROUP BY model_id",
        rows=[("Model A", 25002), ("Model B", 351), ("Model C", 156)],
        row_count=3,
        # Model C's real value (156) never appears — only a fabricated 999.
        model_text="Model A: 25,002. Model B: 351. Model C: 999.",
    )
    assert _all_row_values_in_text(r) is False


def test_all_row_values_in_text_fails_when_last_row_dropped():
    r = _make_result(
        sql="SELECT model_id, COUNT(*) FROM detections GROUP BY model_id",
        rows=[("Model A", 25002), ("Model B", 351), ("Model C", 156)],
        row_count=3,
        model_text="Model A: 25,002. Model B: 351.",  # Model C's 156 never mentioned
    )
    assert _all_row_values_in_text(r) is False


def test_all_row_values_in_text_requires_multiple_rows():
    r = _make_result(
        sql="SELECT COUNT(*) FROM detections",
        rows=[(5,)],
        row_count=1,
        model_text="There are 5 detections.",
    )
    assert _all_row_values_in_text(r) is False  # single-row is Q22/Q23's job, not Q49's


def test_all_row_values_in_text_accepts_comma_formatting():
    r = _make_result(
        sql="SELECT model_id, COUNT(*) FROM detections GROUP BY model_id",
        rows=[("Model A", 25002), ("Model B", 351)],
        row_count=2,
        model_text="Model A: 25,002 detections. Model B: 351 detections.",
    )
    assert _all_row_values_in_text(r) is True


def test_q49_check_fn_requires_sql_executed():
    r = _make_result(sql=None, rows=[], row_count=0, model_text="I cannot answer that.")
    assert _q49_multirow_breakdown_all_values_faithful(r) is False
