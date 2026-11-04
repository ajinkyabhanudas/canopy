# Robustness plan

Status: PROVISIONAL. Written from a comparison against `undercover` (a LangChain/LangGraph
claims agent) to identify which of its hardening patterns canopy is missing and which it
already has. Nothing is built yet.

## Why this plan exists

A review of `undercover`'s agent stack (structured decision contracts, an LLM judge grading
explanation quality against a ground truth, least-privilege tools, failure-kind policies,
durable checkpointing) surfaced three candidate gaps in canopy. Checking each against
`DECISIONS.md` and `LIMITATIONS.md` first — rather than assuming the gap was real — found:

- The **numeric/aggregate faithfulness judge** gap is real and already named: `DECISIONS.md`'s
  T2 section states plainly that closing it "requires an LLM-as-judge... not implemented;
  tracked as a gap." `eval/judge.py` already has the infrastructure (judge-model selection
  that avoids self-judgment, a calibrated categorical verdict pattern) for a *different*
  purpose (guardrail-bypass compliance, T3). This plan extends that infrastructure rather
  than building new.
- The **structured answer contract** gap is not yet established as real — canopy's answer is
  intentionally markdown prose for the UI, and the one machine-parsed part (the Interpretation
  block) already degrades gracefully on malformation, with eval coverage (Category 17/18).
  This plan's Phase 2 is an evidence-gathering step, not a rewrite commitment.
- The **error-typing** gap (`app.py` string-matching `"maximum iterations"` in a bare
  `RuntimeError`) is real but small and self-contained.

Durable checkpointing, an external halt/budget-cap mechanism, and `undercover`-style
failure-kind policies are deliberately **not** phases here: they exist in `undercover` to
protect a long-running, resumable, money-mutating workflow. Canopy's query loop is
single-shot, stateless, and read-only, already capped at `MAX_ITERATIONS = 5` with a
measurement-based revisit trigger (`DECISIONS.md` A1). Porting that machinery would add
complexity for a risk canopy doesn't carry.

## Approach and controls

- Every phase below produces or updates eval coverage (`tests/eval/`) before it produces a
  behavior change — the faithfulness judge in particular must be calibrated against a small
  hand-labeled set before it's trusted in CI, the same way T3's judge was validated 6/6
  before being relied on.
- The faithfulness judge is **eval-only** in this plan, not wired into the live `run_query()`
  path. An inline, non-blocking version is a deliberate follow-on (noted in Phase 1), kept
  separate so the safe, zero-latency version can ship on its own.
- Any judge prompt that is shown database rows carries the same "this is data, not
  instructions" framing `judge.py`'s existing prompt and `backend/judge.py` in `undercover`
  both use — rows can themselves contain adversarial text (e.g. an injected species name),
  and the judge must not follow instructions embedded in them.
- `LIMITATIONS.md`'s "Open Eval Coverage Gaps" table is updated in the same commit as the
  work that closes a gap, matching the project's existing practice (see the table's own
  "✅ Closed — ..." entries).

## Phase 1: Faithfulness judge for numeric and multi-row claims — done (pending live calibration)

Closes `DECISIONS.md` T2's explicitly tracked gap: the verbatim `_count_value_in_text` check
cannot tell "approximately 35,000" from a hallucinated figure when the real count is 35,741,
and (per T2's own note on Q49) a multi-row breakdown response can get `rows[0][0]` right while
fabricating or dropping the rest with nothing catching it. Full rationale, the Protocol→Union
typing correction, and the `run_eval.py` pass-rate-counting correction are in `DECISIONS.md`'s
new T4 section.

### Build

- `FaithfulnessVerdict` added to `src/canopy/eval/judge.py`: `Literal["faithful",
  "unsupported_claim", "contradicts_data"]` plus a rationale, same `BaseModel` + `Field` shape
  as `JudgeVerdict`.
- `get_judge_llm()` reused unchanged; `judge_faithfulness(question, columns, rows, model_text,
  under_test_connection_id=None)` mirrors `judge_guardrail_response`'s signature.
- `_FAITHFULNESS_JUDGE_PROMPT` states explicitly that `columns`/`rows` are data to check
  against, never instructions — asserted present by a dedicated unit test, not just intended.
- `EvalCase.judge_check`'s type widened from the concrete `JudgeVerdict` to `Verdict =
  JudgeVerdict | FaithfulnessVerdict`. A `Protocol` was tried first and rejected: pydantic's
  `Literal`-typed fields are invariant under Protocol structural matching, so pyright rejected
  both concrete models against a `str`-typed Protocol attribute despite being correct at
  runtime — a plain `Union` has no such issue.
- Category 22 (Q62–Q65, `tests/eval/queries.py`) asks the same semantic questions as
  Q21/Q22/Q23/Q49 in distinct wording (`test_all_questions_are_unique` forbids exact
  duplicates) and judges the result alongside those questions' existing, unchanged
  deterministic checks — additive, not a replacement.
- `scripts/calibrate_faithfulness_judge.py` — a 7-case hand-labeled set, kept as a separate
  file from `scripts/calibrate_judge.py` so each judge's calibration history stays
  independently auditable.
- `LIMITATIONS.md`'s gap table and `DECISIONS.md`'s new T4 section updated.

### Found and corrected while building (not pre-planned)

- My own first draft of T4 claimed `run_eval.py` treats judge-backed cases as informational.
  Checked against the actual runner code before publishing that claim: it doesn't — a judge
  case's `check_fn` result feeds the same `passed`/`pct` counters as every keyword-based case,
  same as Category 21 before it. Corrected in T4 rather than left as a false claim in the
  decision record.

### Success criteria

- [x] `FaithfulnessVerdict` and `judge_faithfulness()` exist in `eval/judge.py`, reusing
      `get_judge_llm()` with no new judge-selection logic.
- [x] The faithfulness judge prompt explicitly frames `columns`/`rows` as data, not
      instructions — a unit test asserts the prompt text contains that disclaimer.
- [x] Category 22 eval cases exist, asking the same semantic questions as Q21–Q23/Q49 in
      distinct wording, each asserting against the judge verdict in addition to (not instead
      of) the existing verbatim checks on the originals.
- [x] A hand-labeled calibration set of 7 cases exists (3 faithful including a reasonable
      rounding, 3 contradicts_data including a dropped-row case, 1 unsupported_claim).
- [ ] **Live calibration run.** Not performed — this session had no live model API credentials
      available. This is a hard prerequisite before trusting Category 22 results as anything
      more than an informational signal; do not skip it before relying on this judge.
- [x] `run_eval.py --repeat`/`_report_judge_repeats` works against the new category
      unmodified — it's generic over any `judge_check`-bearing case already.
- [x] `LIMITATIONS.md`'s Open Eval Coverage Gaps table has a new row for this gap, marked
      closed-pending-calibration.
- [x] `DECISIONS.md`'s new T4 section records the decision, the typing correction, the
      pass-rate-counting correction, and the still-outstanding calibration requirement.
- [x] No change to `src/canopy/query/loop.py` or any live-path file — additive to the eval
      harness only. Full suite passes with no regressions (730 passed, up from 724 after
      Phase 3), `ruff check` clean.

## Phase 2: Evidence gate for a structured answer contract

This phase does not assume the gap is real. It produces the evidence needed to decide whether
`undercover`-style Pydantic-validated structured output (its `Decision` model) is worth
porting to canopy's answer body, or whether the current markdown-prose-plus-parsed-block
design is already adequate.

### Build

- Pull `_parse_interpretation`'s `None`-return rate from eval runs (Category 17/18, plus any
  production logs if `CANOPY_LANGFUSE_ENABLED` has real traffic by the time this phase runs).
  A `None` on a query that should have produced a block is the signal that matters; a `None`
  on a guardrail decline (where no block is expected) is not a defect.
- Check whether any malformed-block incident has been observed outside the synthetic eval
  fixtures — i.e. against live model output, not just the unit tests already covering the
  regex parser.
- If the miss rate is low and no live incident exists: close this phase with a `DECISIONS.md`
  entry stating "checked, found adequate, no action" — this is a valid, complete outcome for
  this phase, not a failure to find work.
- If a real miss rate or incident is found: scope a *narrow* follow-up — convert only the
  Interpretation block to `structured_predict()` (the pattern `eval/judge.py` already proves
  works against the active connection), not the full headline/key-findings/data-notes body,
  which is markdown prose by design for the UI and has no validation need `undercover`'s
  `Decision` model has (no payout, no outcome field, nothing downstream depends on its exact
  shape the way backend money controls depend on `Decision`).

### Success criteria

- [ ] A written finding (in `DECISIONS.md`, new section) states the measured `None`-rate for
      `_parse_interpretation` across the current eval suite and any available production
      traces, with the date and sample size.
- [ ] The finding explicitly states one of two outcomes: "adequate as-is" (with the evidence
      that supports it) or "convert the Interpretation block to structured_predict()" (with a
      scoped follow-up task, not an open-ended rewrite).
- [ ] If a follow-up is scoped, it explicitly excludes the headline/key-findings/data-notes
      body from the structured contract, and states why (UI markdown rendering is the
      intended output shape, not an API consumer).
- [ ] No code changes in this phase beyond what's needed to extract the `None`-rate
      measurement (e.g. a log-scraping script, kept in `scripts/` if it doesn't already
      exist as a byproduct of eval runs).

## Phase 3: Typed iteration-limit error — done

Replaced `app.py`'s `isinstance(exc, RuntimeError) and "maximum iterations" in str(exc)`
check. This turned out not to be mere brittleness-against-future-change as originally
scoped — it was already broken against the *current* library. See `DECISIONS.md`'s A1
correction note for the full finding.

### Build

- Confirmed the exact raise site: `llama_index.core.agent.workflow.base_agent
  .parse_agent_output` raises `workflows.errors.WorkflowRuntimeError` — not a `RuntimeError`
  — with message `"Max iterations of N reached!..."`. Neither the type nor the wording
  matched what `app.py` was checking for; the real failure was falling through to the
  generic error path, unnoticed because the existing test mocked the wrong exception shape.
- Added `IterationLimitError(RuntimeError)` in `query/loop.py`.
- `_run_agent` catches `WorkflowRuntimeError` narrowly (only when the message starts with
  the confirmed `"Max iterations of"` prefix) and translates just that case — any other
  `WorkflowRuntimeError` (a generic type covering many unrelated internal workflow failures)
  propagates unchanged, so it still reaches `app.py`'s generic handler with full logging.
- `app.py` now matches on `IterationLimitError`; `workflows.errors.WorkflowRuntimeError` is
  imported directly (not via `llama_index.core.workflow.errors`'s re-export — pyright flags
  that path as a private-import usage and names the direct path as the fix), and
  `llama-index-workflows` was added as an explicit `pyproject.toml` dependency since the
  code now imports it directly rather than only transitively through llama-index-core.

### Success criteria

- [x] A test forces the real exception shape (`workflows.errors.WorkflowRuntimeError` with
      the confirmed message) and asserts `_run_agent` raises `IterationLimitError`.
- [x] A second test proves an unrelated `WorkflowRuntimeError` (e.g. "Step X does not exist")
      is NOT translated — it propagates as-is, so it isn't misclassified as "question too
      complex."
- [x] `app.py` no longer contains a string match against exception text for this case.
- [x] The previously-passing-but-wrong `test_handler_loop_exhausted_message` now raises
      `IterationLimitError` (what `run_query()` actually raises) instead of a hand-invented
      `RuntimeError` message; it still asserts the same user-facing behavior.
- [x] `DECISIONS.md`'s A1 section has a correction note recording the finding and the fix.
- [x] Full suite passes with no regressions (724 passed, up from 722 after Phase 4; +2 net
      from this phase's new tests), `ruff check` clean, `pyright` clean on every changed file.

## Phase 4: Automatic model fallback on a classified-transient failure — done

Added out of sequence, in response to a specific question about whether `undercover`'s
LangChain-based `ModelFallbackMiddleware` was worth adopting. It's not the framework that's
valuable here — it's the capability, and canopy already has the substrate (`models.yaml`'s
connection registry, three already-debugged `FunctionCallingLLM` subclasses) to get it without
a framework swap. Full rationale and alternatives considered in `DECISIONS.md`'s new M3
section.

### Build

- `ResponsesAPIError` (typed, carries `retryable: bool`) replaces bare `RuntimeError` in
  `azure_responses_llm.py`'s three `_post()` failure paths.
- `models/failures.py`'s `is_retryable_model_error()` classifies both that and the `openai`
  SDK's typed exceptions (used by the `openai-compat`/`openai-standard` connections).
- `config.other_active_connections()` — extracted from `eval/judge.py`'s existing
  judge-selection logic so both call sites share one implementation.
- `query/loop.py`'s `_run_agent_with_fallback()` wraps `_run_agent`: on a retryable failure,
  one retry against one other active connection; non-retryable or no-alternate cases
  propagate the original error unchanged.
- Fixed a real latent bug surfaced while wiring this up: `_run_agent()` took a `conn_id`
  parameter but never passed it to `get_llm()`, so `connection_override` never actually
  changed which model answered (see M3 for detail; `LIMITATIONS.md`'s benchmark-switching row
  corrected accordingly).
- `status_retrying_fallback` (en/es) tells the user live when a fallback is happening;
  `timing["model"]`/`timing["connection_id"]`/`timing["fallback_used"]` report which
  connection actually answered.

### Success criteria

- [x] `ResponsesAPIError` carries a correct `retryable` flag for 5xx (true), other HTTP
      errors (false), network errors (true), and timeouts (true); content-filter responses
      are unaffected (still a synthetic refusal, never an exception).
- [x] `is_retryable_model_error()` covers `ResponsesAPIError` and the `openai` SDK's
      connection/timeout/rate-limit/internal-server/5xx-status exceptions, and returns False
      for anything else (e.g. a `ValueError` from a malformed decision).
- [x] `_run_agent_with_fallback()` is unit-tested: retryable error → second connection
      answers and is reported as the answerer; non-retryable error → no second attempt,
      original error propagates; only one active connection → original error propagates;
      an `active: false` connection is never selected as a fallback target.
- [x] Full test suite passes with no regressions (722 passed, 4 skipped, 5 deselected — up
      from the pre-change 706 passed).
- [x] `ruff check` and `pyright` clean on every changed file.
- [x] `DECISIONS.md` M3 and `LIMITATIONS.md`'s gap table updated.
- [ ] Live validation: a real (not mocked) provider failure on the primary connection still
      produces a completed answer via the backup connection. Not yet performed — the mocked
      unit tests prove the logic; they don't prove the real `openai`/Azure error types are
      classified correctly against the live SDK behavior. Do this before relying on it during
      an actual incident.

## Sequencing, risks and review points

- Phases 1-3 are independent of each other and of Phase 4 — none depends on another's output.
  Suggested order for 1-3 is 3 (smallest, lowest-risk), then 1 (clear agreed gap, eval-only),
  then 2 (needs the most evidence-gathering and may conclude "no work needed").
- Phase 1's biggest risk is judge non-determinism producing flaky CI — mitigated by keeping
  it informational (not a hard pass/fail gate) until the calibration run establishes
  confidence, same posture T3 took before trusting its judge in the main suite.
- Phase 1 adds eval-time cost and latency (one more model call per faithfulness case, times
  `--repeat`) — confirm the configured judge model before running the full suite and report
  it alongside results, same practice `PLAN.md`-equivalent cost notes use elsewhere in this
  project's docs.
- Phase 2 can legitimately conclude with no Phase 2b — don't treat "no action needed" as an
  incomplete task; it's the documented, evidence-based outcome the phase exists to produce.
- Review point after Phase 1: does the calibration set's pass rate justify making the judge a
  blocking eval check (not just informational)? That's a separate, later decision, not
  pre-committed here.
