"""
Scaling benchmark runner for canopy — infra performance, not model quality.

Distinct from scripts/run_benchmark.py (which compares model/connection
quality and cost). This measures the gains from the scaling upgrade:
connection pooling under concurrency, the exact-match cache, and the
semantic SQL-plan cache. Requires a live DB + active MODEL_BACKEND API key,
same as run_benchmark.py.

Usage:
  python scripts/run_scaling_benchmark.py                  # all three suites
  python scripts/run_scaling_benchmark.py --concurrency-only
  python scripts/run_scaling_benchmark.py --cache-only
  python scripts/run_scaling_benchmark.py --semantic-only

Writes benchmark_results/scaling_<timestamp>.json and .csv, plus a markdown
summary — the numbers a product-value write-up should quote, not invent.
"""

from __future__ import annotations

import concurrent.futures
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from canopy.cache import clear_cache  # noqa: E402
from canopy.query.loop import run_query  # noqa: E402
from tests.eval.queries import EVAL_CASES  # noqa: E402

_OUT_DIR = _REPO_ROOT / "benchmark_results"

# Paraphrase pairs for the semantic-cache suite — deliberately small and
# hand-picked, not derived from EVAL_CASES (no existing paraphrase fixture
# set in this repo). Each pair is (original, paraphrase, same_entities).
_PARAPHRASE_PAIRS: list[tuple[str, str]] = [
    ("How many species have been detected at Reserva Narupa?",
     "What is the total number of species found at Reserva Narupa?"),
    ("Which species were detected in 2023?",
     "What species had detections during the year 2023?"),
    ("How many detections are validated as approved?",
     "What is the count of approved validation-status detections?"),
]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * pct), len(s) - 1)
    return s[idx]


# ---------------------------------------------------------------------------
# Suite 1 — concurrency
# ---------------------------------------------------------------------------


@dataclass
class ConcurrencyResult:
    concurrency: int
    latencies_s: list[float] = field(default_factory=list)
    errors: int = 0

    @property
    def p50(self) -> float:
        return round(_percentile(self.latencies_s, 0.50), 2)

    @property
    def p95(self) -> float:
        return round(_percentile(self.latencies_s, 0.95), 2)

    @property
    def wall_clock_s(self) -> float:
        return round(sum(self.latencies_s), 2)  # overwritten by caller with real wall clock


def _run_concurrency_sweep(levels: list[int]) -> list[ConcurrencyResult]:
    questions = [c.question for c in EVAL_CASES]
    results = []
    for n in levels:
        clear_cache()  # every call must be a live agent run — see measurement-integrity NFR
        batch = [questions[i % len(questions)] for i in range(n)]
        r = ConcurrencyResult(concurrency=n)
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_timed_run_query, q) for q in batch]
            for f in concurrent.futures.as_completed(futures):
                latency, ok = f.result()
                if ok:
                    r.latencies_s.append(latency)
                else:
                    r.errors += 1
        wall = time.monotonic() - t0
        r.wall_clock_s = round(wall, 2)  # type: ignore[misc]
        results.append(r)
        print(f"  concurrency={n:3d}  p50={r.p50:6.1f}s  p95={r.p95:6.1f}s  "
              f"wall={r.wall_clock_s:6.1f}s  errors={r.errors}")
    return results


def _timed_run_query(question: str) -> tuple[float, bool]:
    t0 = time.monotonic()
    try:
        run_query(question)
        return time.monotonic() - t0, True
    except Exception as exc:
        print(f"  [error] {question[:50]!r}: {exc}")
        return time.monotonic() - t0, False


# ---------------------------------------------------------------------------
# Suite 2 — exact-match cache
# ---------------------------------------------------------------------------


@dataclass
class CacheResult:
    question: str
    cold_latency_s: float
    warm_latency_s: float
    warm_was_hit: bool


def _run_exact_cache_suite(sample_size: int = 10) -> list[CacheResult]:
    clear_cache()
    questions = [c.question for c in EVAL_CASES[:sample_size]]
    results = []
    for q in questions:
        t0 = time.monotonic()
        run_query(q)
        cold_latency = time.monotonic() - t0

        t0 = time.monotonic()
        warm = run_query(q)
        warm_latency = time.monotonic() - t0

        results.append(CacheResult(
            question=q,
            cold_latency_s=round(cold_latency, 2),
            warm_latency_s=round(warm_latency, 2),
            warm_was_hit=bool(warm.timing.get("cache_hit")),
        ))
        print(f"  {q[:50]:<50}  cold={cold_latency:6.1f}s  warm={warm_latency:6.2f}s  "
              f"hit={results[-1].warm_was_hit}")
    return results


# ---------------------------------------------------------------------------
# Suite 3 — semantic cache
# ---------------------------------------------------------------------------


@dataclass
class SemanticResult:
    original: str
    paraphrase: str
    original_latency_s: float
    paraphrase_latency_s: float
    semantic_hit: bool


def _run_semantic_cache_suite() -> list[SemanticResult]:
    if os.environ.get("CANOPY_SEMANTIC_CACHE_ENABLED", "").lower() not in ("1", "true", "yes"):
        print("  [skip] CANOPY_SEMANTIC_CACHE_ENABLED not set — semantic cache is dormant")
        return []
    clear_cache()
    results = []
    for original, paraphrase in _PARAPHRASE_PAIRS:
        t0 = time.monotonic()
        run_query(original)
        original_latency = time.monotonic() - t0

        t0 = time.monotonic()
        para_result = run_query(paraphrase)
        paraphrase_latency = time.monotonic() - t0

        hit = bool(para_result.timing.get("semantic_cache_hit"))
        results.append(SemanticResult(
            original=original, paraphrase=paraphrase,
            original_latency_s=round(original_latency, 2),
            paraphrase_latency_s=round(paraphrase_latency, 2),
            semantic_hit=hit,
        ))
        print(f"  {paraphrase[:50]:<50}  orig={original_latency:6.1f}s  "
              f"paraphrase={paraphrase_latency:6.2f}s  semantic_hit={hit}")
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _write_outputs(
    concurrency: list[ConcurrencyResult],
    cache: list[CacheResult],
    semantic: list[SemanticResult],
) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    payload = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "concurrency": [vars(r) for r in concurrency],
        "exact_cache": [vars(r) for r in cache],
        "semantic_cache": [vars(r) for r in semantic],
    }
    json_path = _OUT_DIR / f"scaling_{ts}.json"
    json_path.write_text(json.dumps(payload, indent=2))

    md_lines = [f"# Scaling benchmark — {payload['run_at']}", ""]
    if concurrency:
        md_lines += [
            "## Concurrency", "",
            "| Concurrency | p50 (s) | p95 (s) | Wall (s) | Errors |",
            "|---|---|---|---|---|",
        ]
        for r in concurrency:
            row = f"| {r.concurrency} | {r.p50} | {r.p95} | {r.wall_clock_s} | {r.errors} |"
            md_lines.append(row)
        md_lines.append("")
    if cache:
        avg_cold = sum(r.cold_latency_s for r in cache) / len(cache)
        avg_warm = sum(r.warm_latency_s for r in cache) / len(cache)
        hit_rate = sum(r.warm_was_hit for r in cache) / len(cache) * 100
        speedup = (avg_cold / avg_warm) if avg_warm else float("inf")
        md_lines += [
            "## Exact-match cache", "",
            f"- Avg cold latency: {avg_cold:.1f}s",
            f"- Avg warm latency: {avg_warm:.2f}s",
            f"- Speedup: {speedup:.1f}x",
            f"- Hit rate: {hit_rate:.0f}%",
            "",
        ]
    if semantic:
        hits = sum(r.semantic_hit for r in semantic)
        hit_rate = hits / len(semantic) * 100
        md_lines += [
            "## Semantic SQL-plan cache", "",
            f"- Paraphrase hit rate: {hit_rate:.0f}% ({hits}/{len(semantic)})",
            "",
        ]
    md_path = _OUT_DIR / f"scaling_{ts}.md"
    md_path.write_text("\n".join(md_lines))

    csv_path = _OUT_DIR / f"scaling_{ts}.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["suite", "key", "value"])
        for r in concurrency:
            value = f"p50={r.p50} p95={r.p95} errors={r.errors}"
            writer.writerow(["concurrency", r.concurrency, value])
        for r in cache:
            value = f"cold={r.cold_latency_s} warm={r.warm_latency_s} hit={r.warm_was_hit}"
            writer.writerow(["exact_cache", r.question[:40], value])
        for r in semantic:
            writer.writerow(["semantic_cache", r.paraphrase[:40], f"hit={r.semantic_hit}"])

    print(f"\nResults written to:\n  {json_path}\n  {md_path}\n  {csv_path}")


def main() -> None:
    args = set(sys.argv[1:])
    run_concurrency = "--cache-only" not in args and "--semantic-only" not in args
    run_cache = "--concurrency-only" not in args and "--semantic-only" not in args
    run_semantic = "--concurrency-only" not in args and "--cache-only" not in args

    concurrency_results: list[ConcurrencyResult] = []
    cache_results: list[CacheResult] = []
    semantic_results: list[SemanticResult] = []

    if run_concurrency:
        print("\n=== Concurrency sweep ===")
        concurrency_results = _run_concurrency_sweep([1, 5, 10, 20])

    if run_cache:
        print("\n=== Exact-match cache ===")
        cache_results = _run_exact_cache_suite()

    if run_semantic:
        print("\n=== Semantic SQL-plan cache ===")
        semantic_results = _run_semantic_cache_suite()

    _write_outputs(concurrency_results, cache_results, semantic_results)


if __name__ == "__main__":
    main()
