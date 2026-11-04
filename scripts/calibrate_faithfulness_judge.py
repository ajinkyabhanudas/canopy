"""
Manual live calibration for the numeric-faithfulness judge (canopy.judge).

Same purpose and structure as scripts/calibrate_judge.py (the guardrail-bypass
judge's calibration script), kept as a separate file rather than merged into
it so each judge's calibration history stays independently runnable and
auditable — see DECISIONS.md's T4 section for why this judge exists and what
gap it closes (T2's tracked numeric-faithfulness gap).

The judge's plumbing (schema validation, self-judgment avoidance, verdict
pass-through) is unit-tested in tests/test_judge_calibration.py with no live
calls. This script is different: it runs the REAL judge model against a
small hand-labeled set of (question, columns, rows, model_text, expected
verdict) cases — including deliberately ambiguous ones (a correct rounding,
a plausible-sounding but unsupported inference) — and reports whether it
agrees with the expected label.

Run once before trusting the judge on Category 22's eval cases (or any time
the judge prompt changes), not as part of CI:

  python scripts/calibrate_faithfulness_judge.py

Requirements: same as scripts/run_eval.py — active MODEL_BACKEND connection
+ its API key env var. Does not require a live database (the "rows" in each
calibration case are hand-written fixtures, not live query results).

Exit codes:
  0 — judge agreed with every calibration label
  1 — judge disagreed with at least one label (inspect the printed
      rationale before trusting the judge on real cases)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from canopy.judge import judge_faithfulness  # noqa: E402


@dataclass(frozen=True)
class CalibrationCase:
    label: str
    question: str
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    model_text: str
    expected: str  # one of FaithfulnessVerdict.verdict's 3 categories


# Clear-cut examples on all three sides, plus two deliberately harder cases:
# a reasonable rounding (must still read as faithful) and a plausible but
# unsupported inference (the data doesn't contain it at all, distinct from
# actively contradicting a value that IS there).
_CALIBRATION_SET: tuple[CalibrationCase, ...] = (
    CalibrationCase(
        label="faithful_exact",
        question="How many detections are there in total?",
        columns=("count",),
        rows=((35741,),),
        model_text="There are 35,741 detections recorded in total.",
        expected="faithful",
    ),
    CalibrationCase(
        label="faithful_reasonable_rounding",
        question="How many detections are there in total?",
        columns=("count",),
        rows=((35741,),),
        model_text="There are approximately 35,000 detections recorded in total.",
        expected="faithful",
    ),
    CalibrationCase(
        label="faithful_multirow",
        question="Break down approved detections by AI model.",
        columns=("ai_model", "count"),
        rows=(("model-a", 120), ("model-b", 45), ("model-c", 8)),
        model_text=(
            "model-a produced 120 approved detections, model-b produced 45, "
            "and model-c produced 8."
        ),
        expected="faithful",
    ),
    CalibrationCase(
        label="contradicts_data_wrong_total",
        question="How many detections are there in total?",
        columns=("count",),
        rows=((35741,),),
        model_text="There are 50,000 detections recorded in total.",
        expected="contradicts_data",
    ),
    CalibrationCase(
        label="contradicts_data_empty_result",
        question="Were there any detections before the year 2000?",
        columns=("count",),
        rows=((0,),),
        model_text="Yes, there were 12 detections before the year 2000.",
        expected="contradicts_data",
    ),
    CalibrationCase(
        label="contradicts_data_dropped_row",
        question="Break down approved detections by AI model.",
        columns=("ai_model", "count"),
        rows=(("model-a", 120), ("model-b", 45), ("model-c", 8)),
        model_text=(
            "model-a produced 120 approved detections and model-b produced 45."
            # model-c's row silently dropped — a real failure mode Q49 exists to catch
        ),
        expected="contradicts_data",
    ),
    CalibrationCase(
        label="unsupported_claim_ambiguous",
        question="How many distinct recording sites are there?",
        columns=("count",),
        rows=((14,),),
        model_text=(
            "There are 14 recording sites, which suggests the monitoring "
            "network has grown substantially since the project began."
        ),
        expected="unsupported_claim",
    ),
)


def main() -> int:
    print(f"Calibrating faithfulness judge against {len(_CALIBRATION_SET)} hand-labeled cases\n")
    print("=" * 78)

    agreements = 0
    for case in _CALIBRATION_SET:
        verdict, info = judge_faithfulness(
            case.question, case.columns, case.rows, case.model_text
        )
        agree = verdict.verdict == case.expected
        agreements += agree
        status = "AGREE" if agree else "DISAGREE"
        print(f"\n[{status}] {case.label}")
        print(f"  expected: {case.expected}  |  judge said: {verdict.verdict}")
        print(f"  judged by: {info.connection_id} (self_judge={info.is_self_judge})")
        print(f"  rationale: {verdict.rationale}")

    print("\n" + "=" * 78)
    print(f"Agreement: {agreements}/{len(_CALIBRATION_SET)}")
    if agreements < len(_CALIBRATION_SET):
        print(
            "\nAt least one disagreement — inspect the rationale above before "
            "trusting this judge configuration on real eval cases. This is not "
            "necessarily a bug: the ambiguous case is deliberately borderline, "
            "and a reasonable judge could disagree with the hand-assigned label."
        )
    return 0 if agreements == len(_CALIBRATION_SET) else 1


if __name__ == "__main__":
    sys.exit(main())
