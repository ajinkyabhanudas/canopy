"""
Eval runner for the canopy ground-truth query set.

Runs every EvalCase from tests/eval/queries.py against the live database,
prints PASS/FAIL per question, and reports the overall score.

Requirements:
  - ANTHROPIC_API_KEY and PG_* env vars set (via .env or shell environment)
  - pip install -e ".[dev]" from the repo root

Usage:
  python scripts/run_eval.py

Exit code: 0 if ≥85% of cases pass, 1 otherwise.
Target: 17/20 (85%).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Add the repo root so tests.eval.queries is importable without a separate
# package install. The canopy src/ package is covered by the editable install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass  # rely on shell environment if python-dotenv is absent

from canopy.query.loop import run_query  # noqa: E402 — must come after sys.path fix
from tests.eval.queries import EVAL_CASES  # noqa: E402

_PASS_THRESHOLD = 0.85


def _truncate(text: str, n: int = 100) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def main() -> None:
    total = len(EVAL_CASES)
    threshold = int(total * _PASS_THRESHOLD)
    passed = 0
    failed_labels: list[str] = []

    print(f"\nCanopy ground-truth eval — {total} questions")
    print(f"Target: {_PASS_THRESHOLD:.0%}  ({threshold}/{total} to pass)")
    print("=" * 70)

    for i, case in enumerate(EVAL_CASES, start=1):
        label = f"Q{i:02d}"
        print(f"\n{label}  {case.question}")
        print(f"      check: {case.description}")

        t0 = time.monotonic()
        try:
            result = run_query(case.question)
            elapsed = time.monotonic() - t0
            ok = case.check_fn(result)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f"      [FAIL]  {elapsed:.1f}s  exception: {exc}")
            failed_labels.append(label)
            continue

        status = "PASS" if ok else "FAIL"
        sql_preview = _truncate((result.sql or "(no SQL)").replace("\n", " "))
        print(f"      [{status}]  {elapsed:.1f}s  rows={result.row_count}  sql={sql_preview}")

        if ok:
            passed += 1
        else:
            failed_labels.append(label)
            if result.model_text:
                print(f"      model_text: {_truncate(result.model_text, 200)}")

    pct = passed / total * 100
    print(f"\n{'=' * 70}")
    print(f"Score: {passed}/{total}  ({pct:.0f}%)")
    if failed_labels:
        print(f"Failed: {', '.join(failed_labels)}")
    else:
        print("All cases passed.")
    print()
    sys.exit(0 if passed >= threshold else 1)


if __name__ == "__main__":
    main()
