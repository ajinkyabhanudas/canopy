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

## Phase 1: Faithfulness judge for numeric and multi-row claims

Closes `DECISIONS.md` T2's explicitly tracked gap: the verbatim `_count_value_in_text` check
cannot tell "approximately 35,000" from a hallucinated figure when the real count is 35,741,
and (per T2's own note on Q49) a multi-row breakdown response can get `rows[0][0]` right while
fabricating or dropping the rest with nothing catching it.

### Build

- Add `FaithfulnessVerdict` to `src/canopy/eval/judge.py`: a `Literal["faithful",
  "unsupported_claim", "contradicts_data"]` verdict plus a one-to-two sentence rationale,
  following the same `BaseModel` + `Field(description=...)` shape as `JudgeVerdict`.
- Reuse `get_judge_llm()` unchanged — do not write a second judge-selection path.
- Add a `_FAITHFULNESS_JUDGE_PROMPT` that receives `{question, columns, rows, model_text}`
  (rows capped the same way `_format_result` caps them for the model) and states explicitly
  that `columns`/`rows` are data to check against, never instructions to follow.
- Add `judge_faithfulness(question, columns, rows, model_text, under_test_connection_id=None)
  -> tuple[FaithfulnessVerdict, JudgeInfo]`, mirroring `judge_guardrail_response`'s signature.
- Wire it into `tests/eval/queries.py` as a new category (22), covering the existing Q21–Q23
  faithfulness cases and Q49's multi-row case: keep `_count_value_in_text` as the cheap first
  pass (it's a real, zero-cost check and should stay), and add the judge verdict as a second
  check for the semantic cases verbatim-matching can't resolve (rounding, multi-clause claims).
- Update `LIMITATIONS.md`'s "Open Eval Coverage Gaps" table: add a row for the numeric/
  aggregate faithfulness gap and mark it closed once this phase lands, matching the table's
  existing convention.
- Write (not run) `scripts/calibrate_judge.py`-equivalent calibration: a small hand-labeled
  set of `(question, columns, rows, model_text, expected_verdict)` tuples, deliberately
  including at least one ambiguous case (a correct rounding, a plausible-sounding but
  unsupported inference) the way T3's calibration included one genuinely ambiguous
  `partial_hedge` case.

### Also needed (don't skip)

- Run the calibration set live against the judge before trusting any CI result from it — T3's
  own audit is explicit that calibration proves "not broken," not "correct on every case."
  Record the live run date and pass rate in `DECISIONS.md`, same as T3's "Live results" line.
- Decide and record judge non-determinism handling: T3 uses `--repeat` in `run_eval.py`
  (`_report_judge_repeats`) to surface judge flakiness rather than hide it. Reuse that
  mechanism rather than inventing a second one.
- Explicitly scope out (in the same `DECISIONS.md` entry) the inline/live version of this
  judge as a separate, later decision — don't let "it would be nice to show this to the user
  live" expand this phase's blast radius.

### Success criteria

- [ ] `FaithfulnessVerdict` and `judge_faithfulness()` exist in `eval/judge.py`, reusing
      `get_judge_llm()` with no new judge-selection logic.
- [ ] The faithfulness judge prompt explicitly frames `columns`/`rows` as data, not
      instructions — a unit test asserts the prompt text contains that disclaimer.
- [ ] Category 22 eval cases exist for Q21–Q23 and Q49, each asserting against the judge
      verdict in addition to (not instead of) the existing verbatim check.
- [ ] A hand-labeled calibration set of at least 6 cases exists, with at least one
      deliberately ambiguous case, and a recorded live run shows the judge matching the
      labels (document the pass rate — it does not need to be 6/6, but a miss must be
      explained, same bar as T3).
- [ ] `run_eval.py --repeat` works against the new category, surfacing judge non-determinism
      the same way it already does for Category 21.
- [ ] `LIMITATIONS.md`'s Open Eval Coverage Gaps table has a new, now-closed row for this gap.
- [ ] `DECISIONS.md` has a new section (e.g. T4) recording the decision, the calibration
      result, and the explicit scoping-out of the inline/live variant, in the same style as
      T2/T3.
- [ ] No change to `src/canopy/query/loop.py` or any live-path file — this phase is additive
      to the eval harness only.

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

## Phase 3: Typed iteration-limit error

Replaces `app.py`'s `isinstance(exc, RuntimeError) and "maximum iterations" in str(exc)`
check, which is brittle against upstream LlamaIndex wording changes.

### Build

- Confirm exactly where the iteration-limit `RuntimeError` is raised (`FunctionAgent` itself,
  or somewhere in `handler.stream_events()` consumption in `_run_agent`) before changing
  anything — catching at the wrong boundary risks silently swallowing an unrelated
  `RuntimeError`.
- Add a canopy-owned `IterationLimitError` (same module as `SQLGuardError`/
  `DatabaseBusyError`, or `query/loop.py` itself if it's loop-specific), raised from the
  confirmed boundary in `_run_agent` or `run_query`.
- Update `app.py`'s exception handling (around line 1031) to match on `IterationLimitError`
  instead of the string check.

### Success criteria

- [ ] A test forces the iteration limit (stub tool that never produces a final answer) and
      asserts `run_query` raises `IterationLimitError`, not a bare `RuntimeError`.
- [ ] `app.py` no longer contains a string match against exception text for this case.
- [ ] Existing iteration-limit UI behavior (the human-readable message shown today) is
      unchanged — this is a refactor of the catch, not a UX change, and the existing
      E2E/component coverage for this path still passes unmodified.
- [ ] A comment or `DECISIONS.md` note records which LlamaIndex call site actually raises the
      limit error, so a future LlamaIndex upgrade that changes it is traceable.

## Sequencing, risks and review points

- Phases are independent and can land in any order — none depends on another's output.
  Suggested order is 3 (smallest, lowest-risk), then 1 (clear agreed gap, eval-only), then 2
  (needs the most evidence-gathering and may conclude "no work needed").
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
