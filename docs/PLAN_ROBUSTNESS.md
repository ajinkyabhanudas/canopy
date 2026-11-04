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

- `FaithfulnessVerdict` added to `src/canopy/judge.py`: `Literal["faithful",
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

## Phase 2: Evidence gate for a structured answer contract — done (inconclusive, by design)

This phase did not assume the gap is real. It set out to produce the evidence needed to decide
whether `undercover`-style Pydantic-validated structured output (its `Decision` model) is
worth porting to canopy's answer body — and found the available evidence insufficient to
decide either way, which `DECISIONS.md`'s new T5 section records as the actual, complete
outcome rather than forcing a guess. Full reasoning in T5.

### What was checked

- `_parse_interpretation`'s existing unit coverage (missing block, missing GAPS line, unclosed
  block, bullet-style variants, a never-raises property test, a ReDoS-resistance test) — strong
  evidence the parser is safe against malformed input, but silent on how often the model
  actually produces malformed input in real use.
- The one available real-world data point: T3's recorded 2026-07-28 live run noted Q45 (a
  Category 17 interpretation-block case) among 4 failures — but that run didn't capture the
  raw `model_text` for root-cause analysis, so whether Q45 failed because of a malformed block
  or an unrelated cause (bad SQL, empty result — both trip the same check_fn early-return) is
  not knowable from the existing record.
- No production traffic to check — Langfuse tracing (O5) is dormant by default and there's no
  record of it being turned on for real usage.

### Why this closes as "insufficient evidence" rather than a guess

Manufacturing "adequate as-is" from unit-test robustness alone would conflate "can't crash" with
"rarely fails" — two different claims. Manufacturing "needs a fix" from one ambiguous historical
data point would be acting on a sample of one with an unconfirmed cause. Neither is actually
supported by what's checkable in this environment (no live DB/API credentials). Saying so
plainly, with what *would* resolve it, is the honest version of this phase's deliverable —
matching this project's own precedent (T2's "tracked as a gap" rather than a guessed fix).

### Success criteria

- [x] A written finding (`DECISIONS.md`'s new T5 section) states exactly what evidence exists
      (unit coverage + one ambiguous historical data point), what it does and doesn't prove,
      and why that falls short of a confident call — with the alternative framings considered
      and rejected recorded in T5's table, same as every other entry in this document.
- [x] The finding does NOT manufacture a forced "adequate" or "needs fix" verdict from
      insufficient evidence — explicitly treated as the correct outcome, not a shortfall.
- [x] The path to an actual answer is recorded (re-run Category 17/18 live, inspect raw
      `model_text` on any failure) so this is resumable, not re-litigated from scratch, the
      next time a session has live credentials.
- [x] No code changes — review-only, as scoped.

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

> **Correction:** "full suite" above meant `pytest tests/ --ignore=tests/e2e/` — the regular
> run checked at the time. It missed `tests/e2e/conftest.py`'s `e2e-overflow` mock, which
> still fabricated the exact pre-fix `RuntimeError` shape this phase made obsolete, because
> e2e needs Playwright and runs as a separate CI job. CI's `e2e` check went red after this
> phase's PR merged as a result. Fixed in a follow-up commit (stub now raises
> `IterationLimitError`); see `DECISIONS.md`'s A1 correction note for the full account. The
> standard this sets, now in `docs/CODE_STANDARDS.md` rule 12: a fix that changes an
> exception's type or message needs every fixture that fabricates that exception checked,
> including ones outside the default test run.

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

## Phase 5: Code-standards review against undercover's documented standards — done

`undercover` has a real standards document (`docs/CODE_STANDARDS.md`, 39 numbered rules) that
every change is checked against; canopy had none — its conventions existed only as practice,
scattered across individual `DECISIONS.md` entries. This phase wrote canopy's own
`docs/CODE_STANDARDS.md`, grounded in what canopy actually does (each rule traces to a
`DECISIONS.md` section) plus a few rules adapted from `undercover`'s where they transplant
usefully, and did a real review pass against it — not a theoretical one — fixing what was
safely fixable and documenting the rest.

### What was reviewed

- A static sweep for common red flags: `TODO`/`FIXME`, bare `except`, stray `print()`,
  `os.getenv` outside `config.py`, long functions, module-level mutable state.
- `O1`'s "all env access lives in config.py" rule, checked against actual grep results rather
  than assumed held.
- Whether `DECISIONS.md`'s summary "Decision map" table still matched the body text of the
  entries it summarizes.
- A live `pip-audit` run against the resolved dependency set (something `ruff`/`pytest` alone
  never surface), and a `pip install` test of the suggested fix to confirm whether it was
  actually applicable before deciding how to treat the finding.

### What was found and fixed

- **O1 violated in 4 files** (`query/fuzzy_match.py`, `query/loop.py`, `observability.py`,
  `cache.py`) reading `os.environ` directly. Fixed: 4 new `config.py` getters, all call sites
  updated.
- **A real security-relevant inconsistency**: `semantic_cache.py`'s write-time sensitive-column
  guardrail gate had its own hardcoded column set, deaf to `CANOPY_SENSITIVE_COLUMNS` —
  `query/loop.py`'s strip already honored the override, so the two consumers could disagree
  about what's sensitive. Fixed: both now read one function, `config.get_sensitive_columns()`,
  called live rather than cached at import (which also removed a test workaround that had to
  manually re-read and monkeypatch around the old import-time binding).
- **A stale "Decision map"**: `O2`'s row still said "no pooling" after the body text recorded
  pooling as implemented and closed; `A3`'s row still described the replaced v1 `ModelClient`
  ABC as current. Corrected both; added missing `T4`/`T5`/`M3`/`O10` rows.
- **No dependency scanning in CI.** Added `pip-audit`, informational only — a live run found a
  real, currently-unfixable transitive `pillow` CVE (confirmed via a `pip install` test showing
  `gradio`'s pin blocks the fix), so blocking CI on it immediately would just fail every PR for
  an issue this review couldn't safely resolve. Same posture O8 already established for
  codecov.
- **A false claim caught before publishing it**: an early draft of `DECISIONS.md`'s T4 entry
  (Phase 1) claimed `run_eval.py` treats judge-backed cases as informational. Checked against
  the runner's actual code and found false — corrected in place rather than left standing.

### What was found and explicitly NOT fixed (and why)

- **Long functions** (`ui/app.py`'s `build_app()`, ≈250 lines) — not refactored. No way to
  verify a UI-wiring split doesn't regress behavior without a live browser/UI check, which
  this environment doesn't have. Recorded as a candidate for a review that can actually run
  the app afterward.
- **Secret-scanning in CI** — not added. A `detect-secrets`/`gitleaks` baseline needs a
  one-time full-history scan and manual triage; recorded as an open gap, not quietly skipped.
- **The `pillow` CVE itself** — not fixed. `gradio` pins `pillow<13.0`; forcing an upgrade
  untested would be worse than the informational scan.
- **`tests/eval/queries.py`'s "Target: ≥85% pass rate (≥42/61)"** — arithmetically inconsistent
  on its own terms, predating this review. Flagged in the docstring and `LIMITATIONS.md` rather
  than silently replaced with a guessed number.

### Success criteria

- [x] `docs/CODE_STANDARDS.md` exists, every rule traces to a `DECISIONS.md` section, and it
      explicitly states what it does *not* adopt from `undercover` and why (money/idempotency/
      durability rules; a hard function-length limit).
- [x] Every `os.getenv`/`os.environ` call in `src/` lives in `config.py` — verified by a
      repo-wide grep after the fix, not just at the 4 sites originally found.
- [x] `semantic_cache.py` and `query/loop.py` read sensitive columns from the same function.
- [x] `DECISIONS.md`'s Decision map matches its own body text for every row touched by this
      review (and the two pre-existing drifted rows found along the way).
- [x] CI has a dependency-vulnerability-scanning step; it is documented why it's informational
      rather than blocking, with a named revisit trigger.
- [x] `README.md` points to `docs/CODE_STANDARDS.md`; `Makefile`/CI stay in sync (`make audit`
      mirrors the new CI step, matching the project's existing `make lint`/CI-step convention).
- [x] Full suite passes with no regressions (730 passed throughout this phase — no count
      change, since this phase's fixes were refactors and doc corrections, not new tests),
      `ruff check` clean.
- [x] Every finding this phase could not safely fix is named in `LIMITATIONS.md`'s new
      "Operational & Security Tooling Gaps" table, not silently dropped.

## Phase 6: Runtime guardrail-bypass judge gate — done (enforce-by-default, no shadow-mode soak)

Promotes T3's eval-only `judge_guardrail_response()` into a live gate inside `run_query()`
itself, closing the gap between "the eval suite measures Q27/Q47" and "production does
nothing about them when they happen for a real user." Added in response to a direct question
about whether canopy's own guardrail-bypass eval findings could be closed with a programmatic,
LLM-judge-backed enforcement layer rather than left as a documented, accepted risk — the answer
researched against production guardrail-framework practice (NeMo Guardrails, Guardrails AI,
Llama Guard comparisons) was yes, and that the standard shape is a cheap always-on
pre-classifier gating an LLM-as-judge reserved for flagged cases, not a judge call on every
request. See `DECISIONS.md`'s T6 section for the full design and its named trade-offs.

### Build

- Moved `judge.py` from `src/canopy/eval/judge.py` to `src/canopy/judge.py` first — wiring a
  production call site (`query/loop.py`) into a module under the `eval` package would have
  been a layering smell the next reader shouldn't have to puzzle over. Mechanical move, five
  import sites updated, no behavior change.
- Added `_mentions_guardrail_topic()` to `query/loop.py` — a deliberately generous EN+ES
  substring match against the same 4 guardrail topics Category 21/22 already enumerate,
  checked against both the question and the produced `model_text`. Deciding whether to pay for
  a judge call, not deciding guilt — erring toward extra judge calls is the safe direction,
  since a false negative here means a real bypass risk skips the judge entirely.
- Added `_apply_guardrail_gate()` — calls `judge_guardrail_response()` only when the topic gate
  matches; in `enforce` mode (default) replaces the response with a fixed, code-authored
  decline (`t("error_guardrail_blocked")`, localized EN/ES) on a `complied`/`partial_hedge`
  verdict; `shadow` mode logs the verdict without altering the response; `off` skips the judge
  entirely. A judge-infrastructure failure (not a bad verdict — the call itself erroring) fails
  open and passes the original response through, logged via `timing["guardrail_judge_error"]`.
- `run_query()` calls the gate after the model's answer is produced and before `write_cache()`/
  `semantic_cache.write()`, so a blocked response is also what a repeat question gets served
  from cache.
- `get_guardrail_judge_mode()` added to `config.py` (O1-compliant — the one place
  `CANOPY_GUARDRAIL_JUDGE_MODE` is read), defaulting to `"enforce"`; an unrecognized value also
  falls back to `"enforce"` rather than silently defaulting to the weakest mode.
- `LoopResult.timing` gains `guardrail_checked`, `guardrail_verdict`,
  `guardrail_judge_connection_id`, `guardrail_judge_is_self_judge`, and (when applicable)
  `guardrail_blocked` / `guardrail_judge_error` — observable in traces/logs the same way
  `fallback_used` already is.

### Success criteria

- [x] `judge_guardrail_response()` is callable from `query/loop.py` without a layering smell —
      moved out of the `eval` package first.
- [x] A guardrail-topic question's response is judged before `run_query()` returns it; a
      non-guardrail-topic question never triggers a judge call (verified by a mock-assert, not
      just by inspection).
- [x] `enforce` mode replaces a `complied`/`partial_hedge` verdict with the fixed decline text;
      `clean_decline` passes the original response through unchanged.
- [x] `shadow` mode logs the verdict and leaves the response unchanged on every verdict.
- [x] `off` mode never calls the judge, even on a guardrail-topic question.
- [x] A judge-call failure (not a bad verdict — an exception) fails open: the original response
      passes through, and the failure is recorded in `timing`, not silently swallowed.
- [x] The blocked/unblocked text is what gets cached — a repeat of a previously-bypassed
      question doesn't re-serve the risky answer from cache.
- [x] `LIMITATIONS.md`'s Accepted Model Risks section and the Q27/Q47 table rows are updated to
      state what changed and what didn't (the gate blocks it; it doesn't fix the underlying
      model tendency, and its own false-positive rate hasn't been measured live yet).
- [x] `DECISIONS.md`'s T6 section documents the enforce-vs-shadow-mode-first trade-off
      explicitly, including that this shipped without a soak period by deliberate beta-stage
      decision — not silently.
- [x] Full suite passes with no regressions (748 passed, up from 731 — 17 new tests in
      `tests/test_guardrail_gate.py`), `ruff check` clean, `pyright` clean on every changed file.

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
- Phase 6 was added out of sequence, like Phase 4, in direct response to a question about
  whether Q27/Q47 specifically could be closed with a programmatic judge-backed enforcement
  layer rather than left as an accepted risk.
- Phase 6's biggest named risk is the opposite direction from Phase 1's: here the judge is
  blocking real user answers from day one, with no shadow-mode measurement of its
  false-positive rate first — a deliberate beta-stage trade-off, not an oversight (see
  `DECISIONS.md`'s T6 audit verdict). Review point: the first real report of a legitimate
  answer getting blocked should trigger either a prompt refinement or a drop to
  `CANOPY_GUARDRAIL_JUDGE_MODE=shadow` while the rate is measured, not a shrug.
