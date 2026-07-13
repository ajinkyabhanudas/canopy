"""
Multi-model benchmark runner for canopy.

For each connection defined in models.yaml:
  - If models: [] (empty) → discover deployments via the Foundry /models API
  - Otherwise → test the explicitly listed model names

At most MAX_TESTED_MODELS models are benchmarked per connection. If more are
available, the extras are recorded in the JSON output and README but not run
(they are too expensive and slow to test on every run).

Runs ground-truth (43 cases) + adversarial (16 cases) eval suites against
every (connection, model) pair. Prints a comparison table to stdout and writes
benchmark_results.json and benchmark_results.csv next to this script.

Usage:
  python scripts/run_benchmark.py                  # all connections
  python scripts/run_benchmark.py --gt-only        # ground-truth suite only
  python scripts/run_benchmark.py --adv-only       # adversarial suite only

Exit codes:
  0 — completed (individual model results are in the table — no hard threshold here)
  1 — configuration or import error
"""

from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from canopy.cache import clear_cache  # noqa: E402
from canopy.config import ModelConnection, load_model_connections  # noqa: E402
from canopy.query.executor import SQLGuardError  # noqa: E402
from canopy.query.loop import run_query  # noqa: E402
from tests.eval.adversarial import ADVERSARIAL_CASES  # noqa: E402
from tests.eval.queries import EVAL_CASES  # noqa: E402

# ---------------------------------------------------------------------------
# Pricing constants — $/1K tokens. Update if Azure pricing changes.
# ---------------------------------------------------------------------------
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6":  {"in": 0.003,   "out": 0.015},
    "claude-sonnet":      {"in": 0.003,   "out": 0.015},
    "gpt-5.1-codex-mini": {"in": 0.00075, "out": 0.003},
    "gpt-5.1-2":          {"in": 0.003,   "out": 0.012},
    "_azure_default":     {"in": 0.002,   "out": 0.008},  # fallback for unknown Azure models
}

_OUT_DIR = _REPO_ROOT / "benchmark_results"
MAX_TESTED_MODELS = 5


def _est_cost(model: str, backend: str, in_tok: int, out_tok: int) -> float:
    pricing = _PRICING.get(model) or (
        _PRICING["_azure_default"] if backend == "azure" else {"in": 0.003, "out": 0.015}
    )
    return (in_tok * pricing["in"] + out_tok * pricing["out"]) / 1000


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------


def _discover_models(conn: ModelConnection) -> tuple[list[str], list[str]]:
    """Return (to_test, available_but_skipped) for a connection.

    to_test          — at most MAX_TESTED_MODELS models, in declaration order
    available_but_skipped — any beyond that cap; recorded but not benchmarked

    If conn.models is non-empty, use that list directly.
    Otherwise query the Foundry /models API to discover deployed models.
    """
    if conn.models:
        all_models = conn.models
    elif conn.backend == "anthropic":
        all_models = ["claude-sonnet-4-6"]
    else:
        # Azure: discover via azure-ai-inference
        from azure.ai.inference import ChatCompletionsClient
        from azure.core.credentials import AzureKeyCredential

        client = ChatCompletionsClient(
            endpoint=conn.endpoint,
            credential=AzureKeyCredential(conn.api_key),
        )
        try:
            all_models = [m.id for m in client.list_models()]
            print(f"  [discover] {conn.id}: found {len(all_models)} deployment(s)")
        except Exception as exc:
            print(f"  [discover] {conn.id}: model list failed ({exc}) — skipping connection")
            return [], []

    to_test = all_models[:MAX_TESTED_MODELS]
    skipped = all_models[MAX_TESTED_MODELS:]
    if skipped:
        print(
            f"  [cap] {conn.id}: testing {len(to_test)}/{len(all_models)} models"
            f" (skipping: {', '.join(skipped)})"
        )
    return to_test, skipped


# ---------------------------------------------------------------------------
# Single-case runner
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    conn_id: str
    backend: str
    model: str
    suite: str
    case_id: str
    question: str
    passed: bool
    latency_s: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    error: str = ""


def _run_case(
    conn_id: str,
    backend: str,
    model: str,
    suite: str,
    case_id: str,
    case: Any,
    guard_pass: bool = False,
) -> CaseResult:
    t0 = time.monotonic()
    try:
        result = run_query(case.question, connection_override=conn_id)
        latency = time.monotonic() - t0
        passed = case.check_fn(result)
        in_tok = result.timing.get("input_tokens", 0) if result.timing else 0
        out_tok = result.timing.get("output_tokens", 0) if result.timing else 0
        return CaseResult(
            conn_id=conn_id, backend=backend, model=model,
            suite=suite, case_id=case_id, question=case.question,
            passed=passed, latency_s=round(latency, 2),
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=round(_est_cost(model, backend, in_tok, out_tok), 5),
        )
    except SQLGuardError as exc:
        latency = time.monotonic() - t0
        return CaseResult(
            conn_id=conn_id, backend=backend, model=model,
            suite=suite, case_id=case_id, question=case.question,
            passed=guard_pass, latency_s=round(latency, 2),
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            error=f"SQLGuardError: {exc}",
        )
    except Exception as exc:
        latency = time.monotonic() - t0
        err_str = str(exc)
        # Azure content management policy 400 = model correctly blocked a hostile
        # prompt. Applies to both RuntimeError (AzureResponsesClient) and
        # openai.BadRequestError (AzureOpenAICompatClient). PASS for adversarial cases.
        content_filtered = (
            "content management policy" in err_str.lower()
            or "content_filter" in err_str.lower()
            or "ResponsibleAIPolicyViolation" in err_str
        )
        passed = guard_pass if content_filtered else False
        return CaseResult(
            conn_id=conn_id, backend=backend, model=model,
            suite=suite, case_id=case_id, question=case.question,
            passed=passed, latency_s=round(latency, 2),
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            error=err_str[:200],
        )


# ---------------------------------------------------------------------------
# Per-model benchmark
# ---------------------------------------------------------------------------

def _benchmark_model(
    conn: ModelConnection,
    model: str,
    run_gt: bool,
    run_adv: bool,
) -> list[CaseResult]:
    """Run eval suites for one (connection, model) pair.

    Uses connection_override so the active connection is switched at the run_query
    call level — no env var mutation or module reloading required.
    """
    results: list[CaseResult] = []

    # Clear query cache so every case runs live — cache hits produce zero token
    # counts and zero cost, making benchmark totals meaningless across runs.
    clear_cache()

    label = f"{conn.id}/{model}"
    print(f"\n{'─' * 70}")
    print(f"  {label}")
    print(f"{'─' * 70}")

    if run_gt:
        for i, case in enumerate(EVAL_CASES, 1):
            cid = f"Q{i:02d}"
            r = _run_case(conn.id, conn.backend, model, "ground-truth", cid, case)
            status = "PASS" if r.passed else "FAIL"
            print(f"  {cid}  [{status}]  {r.latency_s:.1f}s  {case.question[:60]}")
            results.append(r)

    if run_adv:
        for i, case in enumerate(ADVERSARIAL_CASES, 1):
            cid = f"A{i:02d}"
            r = _run_case(
                conn.id, conn.backend, model, "adversarial", cid, case, guard_pass=True
            )
            status = "PASS" if r.passed else "FAIL"
            print(f"  {cid}  [{status}]  {r.latency_s:.1f}s  {case.question[:60]}")
            results.append(r)

    return results


# ---------------------------------------------------------------------------
# Comparison table
# ---------------------------------------------------------------------------

@dataclass
class ModelSummary:
    conn_id: str
    backend: str
    model: str
    gt_pass: int = 0
    gt_total: int = 0
    adv_pass: int = 0
    adv_total: int = 0
    total_latency: float = 0.0
    case_count: int = 0
    total_in_tokens: int = 0
    total_out_tokens: int = 0
    total_cost: float = 0.0

    @property
    def gt_pct(self) -> str:
        return f"{self.gt_pass / self.gt_total * 100:.0f}%" if self.gt_total else "—"

    @property
    def adv_pct(self) -> str:
        return f"{self.adv_pass / self.adv_total * 100:.0f}%" if self.adv_total else "—"

    @property
    def avg_latency(self) -> float:
        return round(self.total_latency / self.case_count, 1) if self.case_count else 0.0

    @property
    def total_tokens(self) -> int:
        return self.total_in_tokens + self.total_out_tokens


def _summarise(all_results: list[CaseResult]) -> list[ModelSummary]:
    summaries: dict[tuple, ModelSummary] = {}
    for r in all_results:
        key = (r.conn_id, r.backend, r.model)
        if key not in summaries:
            summaries[key] = ModelSummary(conn_id=r.conn_id, backend=r.backend, model=r.model)
        s = summaries[key]
        if r.suite == "ground-truth":
            s.gt_total += 1
            s.gt_pass += int(r.passed)
        elif r.suite == "adversarial":
            s.adv_total += 1
            s.adv_pass += int(r.passed)
        s.total_latency += r.latency_s
        s.case_count += 1
        s.total_in_tokens += r.input_tokens
        s.total_out_tokens += r.output_tokens
        s.total_cost += r.cost_usd
    return list(summaries.values())


def _print_table(summaries: list[ModelSummary], total_cases: int) -> None:
    print(f"\n{'═' * 80}")
    print(f"  CANOPY MODEL BENCHMARK — {total_cases} cases")
    print(f"  Run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 80}")
    header = (
        f"  {'Connection':<18} {'Model':<28} {'GT%':>5} {'ADV%':>6}"
        f" {'Lat(s)':>7} {'Tokens':>7} {'$':>7}"
    )
    print(header)
    print(f"  {'─' * 76}")
    for s in summaries:
        cost_str = f"{s.total_cost:.3f}"
        print(
            f"  {s.conn_id:<18} {s.model:<28} {s.gt_pct:>5} {s.adv_pct:>6} "
            f"{s.avg_latency:>7.1f} {s.total_tokens:>7} {cost_str:>7}"
        )
    print(f"{'═' * 80}\n")


# ---------------------------------------------------------------------------
# Output: JSON + CSV
# ---------------------------------------------------------------------------

def _write_outputs(
    all_results: list[CaseResult],
    summaries: list[ModelSummary],
    skipped: dict[str, list[str]],
) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    json_path = _OUT_DIR / f"benchmark_{ts}.json"
    json_path.write_text(
        json.dumps(
            {
                "run_at": datetime.now(timezone.utc).isoformat(),
                "max_tested_models": MAX_TESTED_MODELS,
                "available_not_tested": skipped,
                "summary": [s.__dict__ for s in summaries],
                "cases": [r.__dict__ for r in all_results],
            },
            indent=2,
        )
    )

    csv_path = _OUT_DIR / f"benchmark_{ts}.csv"
    fieldnames = [
        "conn_id", "backend", "model", "suite", "case_id",
        "passed", "latency_s", "input_tokens", "output_tokens", "cost_usd", "error",
    ]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in all_results:
            writer.writerow(r.__dict__)

    _update_readme(summaries, skipped)

    if skipped:
        print(f"\nAvailable but not tested (beyond cap of {MAX_TESTED_MODELS}):")
        for conn_id, models in skipped.items():
            print(f"  {conn_id}: {', '.join(models)}")

    print("\nResults written to:")
    print(f"  {json_path}")
    print(f"  {csv_path}")


def _update_readme(summaries: list[ModelSummary], skipped: dict[str, list[str]]) -> None:
    """Update the 'Available models' table in README.md after each benchmark run."""
    readme_path = _REPO_ROOT / "README.md"
    if not readme_path.exists():
        return

    # Group by connection
    tested_by_conn: dict[str, list[str]] = {}
    for s in summaries:
        tested_by_conn.setdefault(s.conn_id, []).append(s.model)

    all_conn_ids = sorted(set(list(tested_by_conn) + list(skipped)))

    # Build one table per connection
    table_lines: list[str] = []
    for conn_id in all_conn_ids:
        table_lines.append(f"### Available models — {conn_id}")
        table_lines.append("")
        table_lines.append("<!-- Updated automatically by make benchmark. Do not edit by hand. -->")
        table_lines.append("| Status | Model |")
        table_lines.append("|--------|-------|")
        for m in tested_by_conn.get(conn_id, []):
            table_lines.append(f"| tested | `{m}` |")
        for m in skipped.get(conn_id, []):
            table_lines.append(f"| available (not tested) | `{m}` |")
        table_lines.append("")

    new_block = "\n".join(table_lines)

    text = readme_path.read_text()

    # Replace every existing "### Available models" block (one per connection)
    import re  # noqa: PLC0415

    pattern = re.compile(
        r"### Available models.*?(?=\n###|\Z)",
        re.DOTALL,
    )
    if pattern.search(text):
        updated = pattern.sub("", text).rstrip()
        # Re-insert after the model cap paragraph
        insert_marker = "tracked as a running\nrecord of what is deployed on each resource."
        updated = updated.replace(insert_marker, insert_marker + "\n\n" + new_block.rstrip())
    else:
        insert_marker = "tracked as a running\nrecord of what is deployed on each resource."
        updated = text.replace(insert_marker, insert_marker + "\n\n" + new_block.rstrip())

    readme_path.write_text(updated)
    print(f"  README.md model table updated ({sum(len(v) for v in tested_by_conn.values())} tested,"
          f" {sum(len(v) for v in skipped.values())} available-not-tested)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])
    run_gt = "--adv-only" not in args
    run_adv = "--gt-only" not in args

    try:
        connections = load_model_connections()
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    total_cases = (len(EVAL_CASES) if run_gt else 0) + (len(ADVERSARIAL_CASES) if run_adv else 0)
    all_results: list[CaseResult] = []
    all_skipped: dict[str, list[str]] = {}

    print(f"\nCanopy benchmark — {len(connections)} connection(s) from models.yaml")
    print(f"Suites: {'ground-truth' if run_gt else ''}  {'adversarial' if run_adv else ''}")
    print(f"Cases per model: {total_cases}  |  Model cap: {MAX_TESTED_MODELS} per connection")

    for conn in connections:
        if not conn.active:
            print(f"\n[SKIP] {conn.id}: marked inactive in models.yaml — activate before benchmarking")  # noqa: E501
            continue
        if not conn.api_key:
            print(f"\n[SKIP] {conn.id}: api_key_env not set in env — skipping")
            continue

        to_test, skipped = _discover_models(conn)
        if not to_test:
            print(f"\n[SKIP] {conn.id}: no models to benchmark")
            continue
        if skipped:
            all_skipped[conn.id] = skipped

        for model in to_test:
            results = _benchmark_model(conn, model, run_gt=run_gt, run_adv=run_adv)
            all_results.extend(results)

    if not all_results:
        print("\nNo results collected — check that API keys are set in .env.")
        sys.exit(1)

    summaries = _summarise(all_results)
    _print_table(summaries, total_cases)
    _write_outputs(all_results, summaries, all_skipped)


if __name__ == "__main__":
    main()
