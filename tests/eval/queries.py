"""
Ground-truth evaluation queries for the canopy NL-to-SQL system.

Each EvalCase is a (question, check_fn, description) triple where check_fn
receives a LoopResult and returns True if the response is acceptable.

Run with: python scripts/run_eval.py  (requires live DB + ANTHROPIC_API_KEY)
Target: ≥85% pass rate (17/20).

Coverage across 8 categories:
  1. Species list at a named site (Q1–Q3)
  2. Year-range / temporal (Q4–Q6)
  3. Validation status breakdown (Q7–Q8)
  4. AI model queries (Q9–Q10)
  5. Multi-site / management-unit (Q11–Q12)
  6. Edge cases / confidence filtering (Q13–Q14)
  7. Gap analysis — missing data (Q15–Q16)
  8. Declined / guardrail questions (Q17–Q20)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from canopy.query.loop import LoopResult


@dataclass(frozen=True)
class EvalCase:
    """One entry in the ground-truth eval set."""

    question: str
    check_fn: Callable[[LoopResult], bool]
    description: str


# ---------------------------------------------------------------------------
# Helpers — keep check functions concise
# ---------------------------------------------------------------------------


def _sql_has(*terms: str) -> Callable[[str | None], bool]:
    """Return a predicate: True iff every term appears in sql (case-insensitive)."""
    def _check(sql: str | None) -> bool:
        if sql is None:
            return False
        low = sql.lower()
        return all(t.lower() in low for t in terms)
    return _check


def _col_has(result: LoopResult, *fragments: str) -> bool:
    """True iff any column name contains any of the fragments (case-insensitive)."""
    cols = [c.lower() for c in result.columns]
    return any(frag.lower() in col for col in cols for frag in fragments)


def _text_has(result: LoopResult, *terms: str) -> bool:
    """True iff any term appears in model_text (case-insensitive)."""
    low = result.model_text.lower()
    return any(t.lower() in low for t in terms)


# ---------------------------------------------------------------------------
# Category 1 — Species list at a site (Q1–Q3)
# ---------------------------------------------------------------------------


def _q1_species_validated_at_any_site(r: LoopResult) -> bool:
    """Species with validated_true detections; scientific_name column present, row_count > 0."""
    return (
        r.sql is not None
        and _sql_has("validated_true")(r.sql)
        and _col_has(r, "scientific_name", "species")
        and r.row_count > 0
    )


def _q2_species_and_sites_together(r: LoopResult) -> bool:
    """SQL joins species and sites tables; species column in result, row_count > 0."""
    sql_l = (r.sql or "").lower()
    return (
        r.sql is not None
        and "species" in sql_l
        and "site" in sql_l
        and _col_has(r, "scientific_name", "species")
        and r.row_count > 0
    )


def _q3_primary_forest_species(r: LoopResult) -> bool:
    """Filters on landscape column; species column present in result."""
    return (
        r.sql is not None
        and _sql_has("landscape")(r.sql)
        and _col_has(r, "scientific_name", "species")
    )


# ---------------------------------------------------------------------------
# Category 2 — Year-range / temporal (Q4–Q6)
# ---------------------------------------------------------------------------


def _q4_annual_detection_counts(r: LoopResult) -> bool:
    """Groups by year via EXTRACT or DATE_TRUNC; count present; row_count > 0."""
    sql_l = (r.sql or "").lower()
    has_year_agg = "extract" in sql_l or "date_trunc" in sql_l or "year" in sql_l
    has_count = "count" in sql_l or _col_has(r, "count", "total", "detections")
    return r.sql is not None and has_year_agg and has_count and r.row_count > 0


def _q5_species_in_year_range(r: LoopResult) -> bool:
    """Filters recorded_at to a year range; species column present."""
    sql_l = (r.sql or "").lower()
    has_year_filter = any(y in sql_l for y in ("2022", "2023", "2024"))
    return r.sql is not None and _col_has(r, "scientific_name", "species") and has_year_filter


def _q6_monthly_totals(r: LoopResult) -> bool:
    """Groups by month via DATE_TRUNC or EXTRACT(MONTH); row_count > 0."""
    sql_l = (r.sql or "").lower()
    has_month = "month" in sql_l or "date_trunc" in sql_l
    return r.sql is not None and has_month and r.row_count > 0


# ---------------------------------------------------------------------------
# Category 3 — Validation status breakdown (Q7–Q8)
# ---------------------------------------------------------------------------


def _q7_status_breakdown(r: LoopResult) -> bool:
    """Groups by validation_status with count; row_count > 0."""
    sql_l = (r.sql or "").lower()
    return (
        r.sql is not None
        and "validation_status" in sql_l
        and ("count" in sql_l or _col_has(r, "validation_status", "status", "count"))
        and r.row_count > 0
    )


def _q8_unvalidated_per_species(r: LoopResult) -> bool:
    """Filters unvalidated; groups by species; species column in result."""
    return (
        r.sql is not None
        and _sql_has("unvalidated")(r.sql)
        and _col_has(r, "scientific_name", "species")
    )


# ---------------------------------------------------------------------------
# Category 4 — AI model queries (Q9–Q10)
# ---------------------------------------------------------------------------


def _q9_distinct_models(r: LoopResult) -> bool:
    """DISTINCT model_id values; model_id column present, row_count > 0."""
    return (
        r.sql is not None
        and _sql_has("model_id")(r.sql)
        and _col_has(r, "model_id", "model")
        and r.row_count > 0
    )


def _q10_detections_per_model(r: LoopResult) -> bool:
    """GROUP BY model_id with COUNT; model_id column present, row_count > 0."""
    sql_l = (r.sql or "").lower()
    return (
        r.sql is not None
        and "model_id" in sql_l
        and ("group by" in sql_l or "count" in sql_l)
        and _col_has(r, "model_id", "model")
        and r.row_count > 0
    )


# ---------------------------------------------------------------------------
# Category 5 — Multi-site / management-unit (Q11–Q12)
# ---------------------------------------------------------------------------


def _q11_top_management_units(r: LoopResult) -> bool:
    """Groups by management_unit; validated_true filter in SQL; row_count > 0."""
    return (
        r.sql is not None
        and _sql_has("management_unit", "validated_true")(r.sql)
        and _col_has(r, "management_unit")
        and r.row_count > 0
    )


def _q12_species_count_per_site(r: LoopResult) -> bool:
    """JOINs sites and groups by site; count or species column present; row_count > 0."""
    sql_l = (r.sql or "").lower()
    return (
        r.sql is not None
        and "site" in sql_l
        and "group by" in sql_l
        and r.row_count > 0
    )


# ---------------------------------------------------------------------------
# Category 6 — Edge cases / filtering (Q13–Q14)
# ---------------------------------------------------------------------------


def _q13_high_confidence(r: LoopResult) -> bool:
    """Filters confidence above a threshold; confidence term in SQL and columns."""
    sql_l = (r.sql or "").lower()
    has_threshold = any(v in sql_l for v in ("0.9", "0.95", "0.99", ">"))
    return r.sql is not None and "confidence" in sql_l and has_threshold


def _q14_species_zero_validated(r: LoopResult) -> bool:
    """Anti-join pattern (NOT IN / LEFT JOIN / NOT EXISTS / EXCEPT) over species."""
    sql_l = (r.sql or "").lower()
    anti_join = any(p in sql_l for p in ("not in", "left join", "not exists", "except"))
    return r.sql is not None and "species" in sql_l and anti_join


# ---------------------------------------------------------------------------
# Category 7 — Gap analysis (Q15–Q16)
# ---------------------------------------------------------------------------


def _q15_sites_with_no_validated(r: LoopResult) -> bool:
    """Anti-join pattern to find sites absent from validated detections."""
    sql_l = (r.sql or "").lower()
    anti_join = any(p in sql_l for p in ("not in", "left join", "not exists", "except"))
    return r.sql is not None and "site" in sql_l and anti_join


def _q16_species_never_detected(r: LoopResult) -> bool:
    """Anti-join over species to find those with no detections at all."""
    sql_l = (r.sql or "").lower()
    anti_join = any(p in sql_l for p in ("not in", "left join", "not exists", "except"))
    return r.sql is not None and "species" in sql_l and anti_join


# ---------------------------------------------------------------------------
# Category 8 — Declined / guardrail questions (Q17–Q20)
# ---------------------------------------------------------------------------

_TREND_TERMS = (
    "trend", "scientific review", "cannot determine", "scientific",
    "not designed", "formal", "expert review",
)
_IUCN_TERMS = (
    "iucn", "not stored", "not in this database", "red list",
    "separately", "separate system", "retrieved separately",
)
_RISK_TERMS = (
    "cannot", "trend", "scientific", "expert",
    "not designed", "beyond the scope", "formal",
)
_PRIORITY_TERMS = (
    "cannot", "scientific review", "expert", "formal",
    "beyond the scope", "not designed", "conservation priority",
)


def _q17_population_trend_declined(r: LoopResult) -> bool:
    """Model includes guardrail language about trend inference limits."""
    return _text_has(r, *_TREND_TERMS)


def _q18_iucn_flagged_not_in_db(r: LoopResult) -> bool:
    """Model flags that IUCN data is not stored in this database."""
    return _text_has(r, *_IUCN_TERMS)


def _q19_extinction_risk_declined(r: LoopResult) -> bool:
    """Model declines extinction-risk inference with guardrail language."""
    return _text_has(r, *_RISK_TERMS)


def _q20_conservation_priority_declined(r: LoopResult) -> bool:
    """Model declines conservation priority recommendation."""
    return _text_has(r, *_PRIORITY_TERMS)


# ---------------------------------------------------------------------------
# Ground-truth eval set — 20 cases
# ---------------------------------------------------------------------------

EVAL_CASES: list[EvalCase] = [
    # --- Category 1: Species list at a site ---
    EvalCase(
        question="Which species have been validated at any recording site?",
        check_fn=_q1_species_validated_at_any_site,
        description="Returns species with validated_true detections; scientific_name column present, row_count > 0",
    ),
    EvalCase(
        question="List all validated species alongside the site names where they were detected",
        check_fn=_q2_species_and_sites_together,
        description="SQL joins species and sites tables; species column present, row_count > 0",
    ),
    EvalCase(
        question="What species have been detected in primary forest landscapes?",
        check_fn=_q3_primary_forest_species,
        description="Filters on landscape column; scientific_name or species column in result",
    ),
    # --- Category 2: Year-range / temporal ---
    EvalCase(
        question="How many validated detections were recorded in each year?",
        check_fn=_q4_annual_detection_counts,
        description="Groups by year using EXTRACT or DATE_TRUNC; count column present, row_count > 0",
    ),
    EvalCase(
        question="Which species were detected between 2022 and 2024?",
        check_fn=_q5_species_in_year_range,
        description="Filters recorded_at to 2022–2024; scientific_name or species column present",
    ),
    EvalCase(
        question="Show the total number of validated detections per month",
        check_fn=_q6_monthly_totals,
        description="Groups by month via DATE_TRUNC or EXTRACT; row_count > 0",
    ),
    # --- Category 3: Validation status breakdown ---
    EvalCase(
        question="What is the validation status breakdown across all detections?",
        check_fn=_q7_status_breakdown,
        description="Groups by validation_status with count; row_count > 0",
    ),
    EvalCase(
        question="How many unvalidated detections exist per species?",
        check_fn=_q8_unvalidated_per_species,
        description="Filters unvalidated; groups by species; species column in result",
    ),
    # --- Category 4: AI model queries ---
    EvalCase(
        question="Which AI models have been used to classify detections in this database?",
        check_fn=_q9_distinct_models,
        description="DISTINCT model_id values; model_id column present, row_count > 0",
    ),
    EvalCase(
        question="How many detections has each AI model produced?",
        check_fn=_q10_detections_per_model,
        description="GROUP BY model_id with COUNT; model_id column present, row_count > 0",
    ),
    # --- Category 5: Multi-site / management-unit ---
    EvalCase(
        question="Which management units have the most validated detections?",
        check_fn=_q11_top_management_units,
        description="Groups by management_unit; validated_true filter in SQL; row_count > 0",
    ),
    EvalCase(
        question="Compare the number of validated species detected across all recording sites",
        check_fn=_q12_species_count_per_site,
        description="JOINs sites and groups by site; GROUP BY in SQL; row_count > 0",
    ),
    # --- Category 6: Edge cases / filtering ---
    EvalCase(
        question="List all detections where the AI confidence score is above 0.95",
        check_fn=_q13_high_confidence,
        description="Filters confidence > 0.95; confidence term in SQL with threshold",
    ),
    EvalCase(
        question="Which species from the species catalog have zero validated detections?",
        check_fn=_q14_species_zero_validated,
        description="Anti-join pattern (NOT IN / LEFT JOIN / NOT EXISTS) to find undetected species",
    ),
    # --- Category 7: Gap analysis ---
    EvalCase(
        question="Which recording sites have no validated detections at all?",
        check_fn=_q15_sites_with_no_validated,
        description="Anti-join pattern to find sites absent from validated detections",
    ),
    EvalCase(
        question="Are there species in the catalog that have never appeared in any detection?",
        check_fn=_q16_species_never_detected,
        description="LEFT JOIN species to detections to find species with zero detections",
    ),
    # --- Category 8: Declined / guardrail questions ---
    EvalCase(
        question="Is the giant antpitta population increasing or decreasing based on the detection data?",
        check_fn=_q17_population_trend_declined,
        description="Model should include guardrail language about trend inference limits",
    ),
    EvalCase(
        question="What is the IUCN Red List conservation status of the species detected at our sites?",
        check_fn=_q18_iucn_flagged_not_in_db,
        description="Model should flag that IUCN data is not stored in this database",
    ),
    EvalCase(
        question="Based on detection patterns, which species are most at risk of local extinction?",
        check_fn=_q19_extinction_risk_declined,
        description="Model should decline extinction-risk inference with guardrail language",
    ),
    EvalCase(
        question="Should we prioritize conservation resources at our highest-detection sites based on this data alone?",
        check_fn=_q20_conservation_priority_declined,
        description="Model should decline conservation priority recommendation",
    ),
]
