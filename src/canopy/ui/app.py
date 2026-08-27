"""Gradio UI for canopy — two-panel layout: question/history | response/results/sql."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Generator
from difflib import SequenceMatcher

import gradio as gr
import psycopg2
import psycopg2.errors

from canopy.config import get_ui_lang, is_langfuse_enabled, is_show_sql_experiment_active
from canopy.history import clear_history
from canopy.i18n import set_locale, t
from canopy.observability import (
    assign_variant,
    log_exposure,
    score_no_rephrase,
    score_thumbs,
)
from canopy.query.executor import QueryResult, SQLGuardError
from canopy.query.fuzzy_match import FUZZY_COLUMNS
from canopy.query.loop import (
    Interpretation,
    LoopResult,
    UnsupportedLanguageError,
    is_unsupported_language,
    run_query,
    strip_interpretation_block,
)

_log = logging.getLogger("canopy.ui")

set_locale(get_ui_lang())

_PLACEHOLDER = t("placeholder")
_IDLE_PROMPT = t("idle_prompt")

# Max simultaneous run_query() calls. Was 1 (serializing the whole app to one
# query at a time, globally — not per-user), which would visibly queue during
# the Week 8 multi-user handover session. 3 covers Jajean + reviewers
# comfortably and stays inside DECISIONS.md's O2 section's own "1-5 concurrent
# connections: no action needed" threshold — no connection pooling added,
# since O2 already covers when that becomes necessary (revisit above 20).
_QUERY_CONCURRENCY_LIMIT = 3

CSS = """
/* Status bar — typographic only, no box */
#canopy-status {
    font-size: 0.82em;
    color: var(--body-text-color-subdued);
    padding: 0 0 6px 0;
    min-height: 0;
    letter-spacing: 0.01em;
}
#canopy-status p { margin: 0; }

/* Loading pulse — a continuous, gentle opacity breathe on the status text
   while a query is in flight, so the bar visibly stays alive between the
   irregular (sometimes 15-40s apart) status_cb() updates instead of
   sitting frozen. #canopy-status-elapsed is the ticking "· Ns" suffix
   (see _loading_status_html in app.py). */
@keyframes canopy-status-pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.55; }
}
.canopy-status-loading {
    animation: canopy-status-pulse 1.6s ease-in-out infinite;
}
#canopy-status-elapsed {
    font-variant-numeric: tabular-nums;
    opacity: 0.85;
}
@media (prefers-reduced-motion: reduce) {
    .canopy-status-loading { animation: none; }
}

/* Gradio's default per-component "generating" indicator (Gradio's
   StreamingBar .generating class) fires on every yielded update to a
   component — it's NOT a class on the #canopy-status/#canopy-response
   block div itself, but on a nested child (data-testid="status-tracker")
   Gradio injects inside it. An earlier fix here targeted
   "#canopy-status.generating" directly and had zero effect (confirmed
   live via getComputedStyle — the selector never matched anything) because
   of that nesting. Because #canopy-status has near-zero height (see
   min-height: 0 above), the tracker's own 2px border collapses visually
   into a single thin horizontal "beating" line rather than a full box
   outline — one such line per component, flashing independently, which is
   the exact artifact reported in review. Our own pulse
   (.canopy-status-loading) and the step-log narration already communicate
   "in progress," so Gradio's indicator is pure noise here — suppress it on
   just these two components' nested tracker, not globally.
*/
#canopy-status [data-testid="status-tracker"].generating,
#canopy-response [data-testid="status-tracker"].generating {
    border: none !important;
    animation: none !important;
}

/* Timing footer */
.timing-info p {
    font-size: 0.78em !important;
    color: var(--body-text-color-subdued) !important;
    margin-top: 4px !important;
    letter-spacing: 0.01em;
}

/* Answer tab — filled bullets + breathing room */
.tabitem ul {
    list-style-type: disc;
    padding-left: 1.4em;
    margin-top: 4px;
}
.tabitem ul li {
    margin-bottom: 5px;
    line-height: 1.65;
}
.tabitem p {
    line-height: 1.65;
    margin-bottom: 0.7em;
}

/* Interpretation block — the hr before it visually separates it from the
   main answer; text is slightly subdued to read as supplementary context. */
.tabitem hr {
    margin: 1em 0 0.8em 0;
    border-color: var(--border-color-primary);
}

/* Answer-tab overflow safety net: the model is instructed not to render
   long markdown tables in the Response (schema.py), but any genuinely
   long answer — a big bullet list, a long interpretation block — should
   still communicate "there's more below" rather than silently requiring
   an unprompted scroll. Cap height, scroll internally.

   The fade itself is added/removed by JS (see the "canopy-has-overflow"
   class toggle in STATUS_TICKER_HEAD_SCRIPT), not by this pure-CSS block —
   a first version used a ::after pseudo-element unconditionally, which
   faded the last line of a fully-visible, non-scrollable answer too
   (caught in review: it made a *complete* answer look truncated, which is
   worse than no fade at all — a misleading "there's more" cue on content
   that has nothing more). Detecting "does this actually overflow" needs a
   real height comparison, which CSS alone cannot express.
*/
#canopy-response {
    max-height: 70vh;
    overflow-y: auto;
    position: relative;
}
/* padding-bottom reserves dedicated space for the fade so it never
   overlaps real text — a first version used margin-top: -32px to pull
   the fade back over the last 32px of content instead of giving it its
   own space, which washed out the actual last visible line (e.g. a
   bullet-list item) under the gradient (caught in review: readable text
   sat directly under the fade's start point, confirmed via
   elementsFromPoint at the fade boundary). The padding only applies
   when overflowing, so a complete answer's layout is unaffected. */
#canopy-response.canopy-has-overflow {
    padding-bottom: 32px;
}
#canopy-response.canopy-has-overflow::after {
    content: "";
    position: sticky;
    bottom: 0;
    left: 0;
    display: block;
    height: 32px;
    margin-top: -32px;
    background: linear-gradient(to bottom, transparent, var(--background-fill-primary));
    pointer-events: none;
}
"""

# A question can have typos in more than one fuzzy-checkable column at once
# (e.g. a mistyped species name AND a mistyped site name in the same SQL).
# Each affected column gets its own pre-mounted suggestion group — a prompt
# naming the column plus up to _GROUP_CANDIDATES buttons — stacked below
# status_md. Gradio requires every callback output component to be declared
# statically at layout time, so groups can't be created dynamically per
# result — _MAX_GROUPS pre-mounts one group per registered fuzzy column
# (derived from FUZZY_COLUMNS, not hardcoded) and unused groups are hidden.
_MAX_GROUPS = len(FUZZY_COLUMNS)
_GROUP_CANDIDATES = 3
# Per group: 1 prompt + _GROUP_CANDIDATES buttons + _GROUP_CANDIDATES hidden
# question states.
_SLOTS_PER_GROUP = 1 + 2 * _GROUP_CANDIDATES

# Type alias for the every handler output must match: the fixed 9-tuple
# ([sql_box, results_table, response_box, row_count_md, history_radio,
#   timing_md, status_md, history_state, result_tabs]) followed by
# _MAX_GROUPS suggestion groups, each _SLOTS_PER_GROUP slots
# (prompt_md, btn_1..btn_N, q_state_1..q_state_N).
_Output = tuple


def _no_suggestions_for_group() -> tuple:
    """Hidden/empty update tuple for one suggestion group (_SLOTS_PER_GROUP slots)."""
    return (
        gr.update(visible=False),
        *(gr.update(visible=False, value="") for _ in range(_GROUP_CANDIDATES)),
        *(None for _ in range(_GROUP_CANDIDATES)),
    )


# Suggestion groups are hidden by default and only shown on a 0-row result
# with fuzzy matches. This tuple is yielded for the trailing
# _MAX_GROUPS * _SLOTS_PER_GROUP output slots on every path that isn't the
# fuzzy-suggestion success case, so no group ever lingers visible from a
# previous query.
_NO_SUGGESTIONS: tuple = tuple(
    v for _ in range(_MAX_GROUPS) for v in _no_suggestions_for_group()
)


def _render_interpretation(interpretation: Interpretation | None) -> str:
    """Return a markdown fragment for the interpretation section, or '' if None.

    Pure markdown, no wrapping HTML element — Gradio's markdown-it renderer
    does not parse markdown syntax inside raw HTML blocks (confirmed via
    browser screenshot: an earlier version wrapped this in a <div> and every
    bold/bullet rendered as literal text). The horizontal rule (---) already
    renders correctly and is the visual separator; CSS targets it via
    `.tabitem hr` rather than a custom wrapper class.

    Mirrors the model's own optionality rules from schema.py: an empty gaps
    tuple renders as a literal 'none' line (matching what the model itself
    writes inline), and an empty research_questions tuple omits that
    sub-section entirely rather than showing an empty heading.
    """
    if interpretation is None:
        return ""

    lines = [
        "---",
        f"**{t('interpretation_heading')}**",
        "",
        f"**{t('interpretation_source')}:** {interpretation.data_source}",
        "",
        f"**{t('interpretation_gaps')}:**",
    ]
    if interpretation.gaps:
        lines.extend(f"- {gap}" for gap in interpretation.gaps)
    else:
        lines.append(t("interpretation_gaps_none"))

    if interpretation.research_questions:
        lines.append("")
        lines.append(f"**{t('interpretation_research')}:**")
        lines.extend(f"- {q}" for q in interpretation.research_questions)

    return "\n".join(lines)


def _render_response(result: LoopResult, *, show_sql_inline: bool = False) -> str:
    """Build the Answer tab's full markdown: model_text (block stripped) + interpretation.

    show_sql_inline is the entire code surface of the show-SQL-by-default A/B
    experiment (see observability.assign_variant): when True (treatment arm),
    the generated SQL — the exact same string already rendered in the
    "Database query" tab, nothing new computed or exposed — is appended
    inline so the user sees it without a click. When False (control, and the
    default for every caller that doesn't pass this explicitly), behavior is
    byte-for-byte unchanged from before this experiment existed.
    """
    body = strip_interpretation_block(result.model_text)
    interpretation_md = _render_interpretation(result.interpretation)
    if interpretation_md:
        body = f"{body}\n\n{interpretation_md}"
    if show_sql_inline and result.sql:
        body = f"{body}\n\n---\n\n**Query used:**\n```sql\n{result.sql}\n```"
    return body


class _StepKind:
    """Classification tags for step-log entries.

    Replaces an earlier design where the log was a plain list[str] and
    every operation (dedup, "is this superseded by a retry", collapsing
    a now-adjacent duplicate after a drop) had to re-derive meaning by
    string-matching the *rendered*, locale-dependent text. That approach
    went through several rounds in review, each fixing one interaction
    the last round's string-matching missed — e.g. dropping a superseded
    "Found N" line could leave two previously non-adjacent "Searching..."
    lines newly adjacent, which the string-level dedup check (only ever
    comparing to the immediately preceding entry) couldn't catch because
    adjacency changed *after* its check already ran.

    Tagging each entry with its kind at the point status_cb() receives it
    makes "is this a result", "does a retry supersede this", and
    "are two entries the same kind" simple field comparisons instead of
    string pattern-matching — correctness no longer depends on guessing
    locale template shapes.
    """

    OTHER = "other"
    SEARCHING = "searching"
    RESULT = "result"  # "Found N" / "Refining the search" — superseded by a retry
    RETRY = "retry"  # "Trying a different approach (attempt N)..."


def _classify_step(msg: str) -> str:
    """Tag a raw status_cb() message with its _StepKind.

    Matches on each locale template's own static prefix (the text before
    its first "{" placeholder) rather than hardcoded English, so this
    stays correct under CANOPY_UI_LANG=es. This is the ONLY place in the
    step-log pipeline that inspects rendered text — every other function
    downstream operates on the tag, not the string.
    """
    if msg == t("status_searching_db"):
        return _StepKind.SEARCHING
    if msg == t("status_refining"):
        return _StepKind.RESULT
    if msg.startswith(t("status_writing_sql_retry", n=0).split("0", 1)[0].split("(", 1)[0].strip()):
        return _StepKind.RETRY
    found_prefixes = (
        t(key, n=0).split("0", 1)[0]
        for key in (
            "found_detections_singular",
            "found_detections_plural",
            "found_detections_singular_retry",
            "found_detections_plural_retry",
        )
    )
    if any(msg.startswith(p) for p in found_prefixes):
        return _StepKind.RESULT
    return _StepKind.OTHER


def _append_step(steps: list[tuple[str, str]], msg: str) -> None:
    """Append msg to steps as a (kind, text) pair, in place.

    Handles two real-world orderings confirmed live against the actual
    agent event stream (see loop.py's _run_agent) — the "Searching the
    monitoring database..." for a retried attempt can arrive either
    before or after that attempt's own "Trying a different approach..."
    announcement, since they're emitted from two different code paths
    (execute_sql's own status_cb call vs. the agent's ToolCall stream
    handler) with no fixed ordering between them:

    1. A retry announcement supersedes the most recent RESULT entry
       (that search's "Found N"/"Refining" didn't produce the answer,
       this attempt is trying instead) — remove it rather than leave a
       stale, easily-misread count sitting in the log. Stop scanning
       backward at the first earlier RETRY entry, so an earlier attempt's
       already-cleaned boundary is never crossed.
    2. Removing an entry from the middle of the list can leave two
       entries of the same kind newly adjacent (e.g. two SEARCHING
       entries, one from each attempt, with the dropped RESULT between
       them) — collapse immediate same-kind duplicates after any removal,
       not just at append time.
    3. Ordinary consecutive-identical-text de-dup (unrelated to retries)
       still applies for the general case, e.g. a status repeated as-is.

    Correctness invariant this relies on (enforced by loop.py's status_cb
    call sites, not by this function): a SEARCHING entry always precedes
    a RESULT, and two RESULT entries never appear consecutively without
    an intervening SEARCHING or RETRY. If loop.py's call ordering ever
    changes, re-verify this function's collapse logic against the new
    ordering — the single-pair collapse below does not defend against a
    cascading multi-pair collapse, which the current message vocabulary
    and call ordering make unreachable but do not structurally prevent.
    """
    kind = _classify_step(msg)
    if kind == _StepKind.RETRY:
        for i in range(len(steps) - 1, -1, -1):
            if steps[i][0] == _StepKind.RESULT:
                del steps[i]
                if 0 < i < len(steps) and steps[i - 1][0] == steps[i][0]:
                    del steps[i]
                break
            if steps[i][0] == _StepKind.RETRY:
                break
    if not steps or steps[-1] != (kind, msg):
        steps.append((kind, msg))


def _step_log_markdown(steps: list[tuple[str, str]]) -> str:
    """Render the accumulated real status steps as a narrative log.

    Shown in the main Answer panel while a query is in flight — each step
    is a real event that already happened (status_cb() only fires when
    something real occurred: a phase started, SQL was retried, results
    came back), so by the time the final answer replaces this, the trail
    reads as "here's what led to your answer," not an abstract wait.
    Completed steps are muted; the current (last) step is emphasized so
    it's clear what's happening *right now* versus what's already done.
    """
    if not steps:
        return ""
    texts = [text for _kind, text in steps]
    lines = [f"~~{s}~~" for s in texts[:-1]] + [f"**{texts[-1]}**"]
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Acceptance-proxy: no_rephrase_within_5min
#
# Only knowable in retrospect, and only from the NEXT query this browser
# makes — there is no live 5-minute clock. A user who is satisfied and never
# returns gets no score at all, which is the honest gap: scoring a silent
# tab-close as "accepted" would be a false positive with no evidence behind
# it, and file 06 itself asks for the bias to be named rather than hidden.
# No background thread, no scheduled job — this evaluates lazily, the moment
# (if ever) a following query arrives, reusing difflib the same way
# fuzzy_match.py already does rather than adding a new similarity dependency.
# ---------------------------------------------------------------------------

_REPHRASE_WINDOW_S = 300  # 5 minutes
_REPHRASE_SIMILARITY_THRESHOLD = 0.6  # looser than fuzzy_match's 0.72: a
# rephrase changes wording, not just one typo'd literal, so demanding a high
# ratio would miss real rephrases and understate how often users retry.


def _check_rephrase(
    last_trace: dict | None, new_question: str
) -> tuple[str, bool] | None:
    """Score the PREVIOUS trace against this new question, if there is one to score.

    Returns (trace_id, accepted) for the previous trace, or None if there was
    no previous trace, it already scored, or it's outside the window (in
    which case it ages out unscored rather than being guessed at).
    """
    if not last_trace or last_trace.get("scored"):
        return None
    trace_id = last_trace.get("trace_id")
    if not trace_id:
        return None
    elapsed = time.monotonic() - last_trace.get("at", 0)
    if elapsed > _REPHRASE_WINDOW_S:
        return None  # too late to say anything meaningful — leave unscored
    similar = (
        SequenceMatcher(
            a=last_trace.get("question", "").casefold(),
            b=new_question.casefold(),
        ).ratio()
        >= _REPHRASE_SIMILARITY_THRESHOLD
    )
    # A rephrase (similar wording, asked again soon) means the previous
    # answer did NOT satisfy the user — accepted=False for the PREVIOUS trace.
    return trace_id, not similar


def _empty_result(
    message: str,
    session_history: list,
    status: str = "",
    *,
    last_trace: dict | None = None,
    ab_distinct_id: str | None = None,
) -> _Output:
    """Return a blank output tuple with only the response message and optional status set.

    last_trace and ab_distinct_id pass through unchanged — these are error/
    empty-input paths where no query ran, so there is nothing new to
    remember, nothing to score, and no exposure to log.
    """
    return (
        "",
        gr.Dataframe(value=None),
        message,
        "",
        gr.Radio(choices=session_history),
        "",
        status,
        session_history,
        gr.update(selected=0),
        last_trace,
        ab_distinct_id,
        *_NO_SUGGESTIONS,
    )


def _loading_status_html(*, is_first: bool) -> str:
    """Elapsed-time ticker markup for the thin top status bar.

    Perceived-wait fix (Step 2): the previous status bar only repainted on
    each status_cb() call, so between calls — sometimes 15-40s apart — the
    DOM was frozen with no signal the app was still working.

    Deliberately does NOT repeat the current status text — that already
    lives in response_box's step-log (_step_log_markdown), which has more
    room to show the full trail. Showing the same "Found N detections"
    string in both the top bar and the big answer panel was pure
    duplication (caught in review): the top bar's only job is the ticking
    "· Ns" — a fixed, generic label plus the counter, not a second copy of
    whatever the answer panel already says.

    This function only emits static HTML (a `data-canopy-loading` marker
    span, reset via `data-first` on the very first yield of a run) — it
    does NOT inject a <script> tag. Gradio's gr.Markdown routes its value
    through a markdown-to-HTML parser even with sanitize_html=False, which
    mangles inline <script> content into inert <p> text instead of
    executing it (confirmed live: window.__canopyStatusStart stayed
    undefined). The actual ticker is a single persistent script loaded
    once via Blocks.launch(head=...) in scripts/run_ui.py, using a
    MutationObserver on #canopy-status to react to these markers — see
    STATUS_TICKER_HEAD_SCRIPT below.
    """
    first_attr = ' data-first="1"' if is_first else ""
    return (
        f'<span class="canopy-status-loading" data-canopy-loading="1"{first_attr}>'
        f'{t("status_bar_working")}<span id="canopy-status-elapsed"></span></span>'
    )


# Persistent, page-level script — loaded once via gr.Blocks.launch(head=...)
# in scripts/run_ui.py, NOT re-injected per status update (Markdown content
# can't host executable <script> tags, see _loading_status_html above).
# Watches #canopy-status for the data-canopy-loading marker this function
# emits and drives a client-side elapsed-time ticker independent of Gradio's
# irregular (sometimes 15-40s apart) status_cb() update cadence.
STATUS_TICKER_HEAD_SCRIPT = """
<script>
(function() {
  var observer = null;

  // tick() writes into a node inside the observed subtree (#canopy-status),
  // which would otherwise re-trigger the MutationObserver on every write —
  // an infinite synchronous callback loop that hangs the main thread
  // (confirmed live: reproduced a "Page Unresponsive" browser dialog before
  // this guard existed). Disconnect before writing, reconnect after.
  function tick() {
    var node = document.getElementById('canopy-status-elapsed');
    if (!node || !window.__canopyStatusStart) return;
    var s = Math.floor((Date.now() - window.__canopyStatusStart) / 1000);
    var text = ' \\u00b7 ' + s + 's';
    if (node.textContent === text) return;
    if (observer) observer.disconnect();
    node.textContent = text;
    if (observer) reattachObserver();
  }
  function onStatusChanged() {
    var host = document.getElementById('canopy-status');
    if (!host) return;
    var loading = host.querySelector('[data-canopy-loading="1"]');
    if (!loading) {
      window.__canopyStatusStart = null;
      return;
    }
    if (loading.hasAttribute('data-first') || !window.__canopyStatusStart) {
      // data-first: a new run started, reset the clock. No start time yet:
      // page loaded or observer attached mid-run — start from now rather
      // than leaving the counter blank.
      window.__canopyStatusStart = Date.now();
    }
    tick();
  }
  function reattachObserver() {
    var host = document.getElementById('canopy-status');
    if (!host) return;
    observer.observe(host, {childList: true, subtree: true});
  }
  function attach() {
    var host = document.getElementById('canopy-status');
    if (!host) {
      setTimeout(attach, 200);
      return;
    }
    observer = new MutationObserver(onStatusChanged);
    reattachObserver();
    setInterval(tick, 1000);
    onStatusChanged();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach);
  } else {
    attach();
  }
})();

// Independent IIFE: toggles .canopy-has-overflow on #canopy-response based
// on a real scrollHeight vs. clientHeight comparison — the bottom fade
// (see the CSS block above) only renders when this class is present. A
// first version applied the fade unconditionally via CSS alone, which
// faded the last line of answers that fit entirely on screen — making a
// complete answer look truncated (caught in review). ResizeObserver
// catches both content growing/shrinking (streaming step-log updates,
// final answer replacing it) and container resizes (window resize,
// sidebar collapse).
(function() {
  function checkOverflow() {
    var el = document.getElementById('canopy-response');
    if (!el) return;
    var hasOverflow = el.scrollHeight > el.clientHeight + 1;
    el.classList.toggle('canopy-has-overflow', hasOverflow);
  }
  function attach() {
    var el = document.getElementById('canopy-response');
    if (!el) {
      setTimeout(attach, 200);
      return;
    }
    new ResizeObserver(checkOverflow).observe(el);
    var mo = new MutationObserver(checkOverflow);
    mo.observe(el, {childList: true, subtree: true, characterData: true});
    checkOverflow();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach);
  } else {
    attach();
  }
})();
</script>
"""


def _status_yield(
    response_text: str,
    session_history: list,
    *,
    is_first: bool = False,
    preview: tuple[QueryResult, str] | None = None,
    last_trace: dict | None = None,
    ab_distinct_id: str | None = None,
) -> _Output:
    """Return a mostly-blank output tuple for streaming updates.

    response_text is the full step-log narration (response_box). The thin
    top status bar (status_md) intentionally shows only a generic "Working
    · Ns" ticker, not a second copy of the current step — see
    _loading_status_html for why duplicating it there was removed.

    preview, when set, is the (rows, sql) pair from a completed execute_sql
    call. The model still needs 16-25s to write its answer around those rows,
    so they are rendered into the table and SQL box immediately instead of
    being withheld until the final yield — the user sees the real data at
    roughly the halfway point of the wait rather than at the end.

    Deliberately renders the rows as *data only*: no count sentence, no
    interpretation. The narrative is the only thing that gets to say what the
    rows mean, so a superseding retry replaces a table (reads as the search
    refining) rather than retracting a stated answer (reads as a wrong claim).
    See DECISIONS.md U2 for the failure this avoids.

    last_trace passes through unchanged during streaming — it only changes
    once, on the final success yield, when this run's own trace is known.
    ab_distinct_id passes through unchanged for the whole run — it's fixed
    for the browser, not something a single query changes.
    """
    if preview is not None:
        result, sql = preview
        rows = [list(row) for row in result.rows]
        table = gr.Dataframe(value=rows or None, headers=result.columns or None)
        sql_text = sql
    else:
        table = gr.Dataframe(value=None)
        sql_text = ""
    return (
        sql_text,
        table,
        response_text,
        "",
        gr.Radio(choices=session_history),
        "",
        _loading_status_html(is_first=is_first),
        session_history,
        gr.update(selected=0),
        last_trace,
        ab_distinct_id,
        *_NO_SUGGESTIONS,
    )


def _success_result(
    result: LoopResult,
    question: str,
    session_history: list,
    superseded_question: str | None,
    trace_id: str | None = None,
    show_sql_inline: bool = False,
    ab_distinct_id: str | None = None,
) -> _Output:
    """Build the final success output tuple from a completed LoopResult.

    Extracted from _run_query_handler so the success-rendering logic (row
    rendering, history dedup, fuzzy-suggestion regeneration, timing display)
    lives in one place rather than inline in the streaming generator.

    trace_id, when tracing is enabled, becomes this run's last_trace entry —
    the record the NEXT query checks to score whether this one was a
    rephrase (see _check_rephrase). None when tracing is disabled (the
    default), which _check_rephrase already treats as nothing to score.

    ab_distinct_id is passed through to the output tuple unchanged — it was
    already resolved (generated on first use if the experiment is active)
    before this run started, by the caller in _run_query_handler.
    """
    rows = [list(row) for row in result.rows]
    df = gr.Dataframe(value=rows or None, headers=result.columns or None)
    count = result.row_count
    count_md = t("count_row_singular", n=count) if count == 1 else t("count_row_plural", n=count)
    timing = result.timing
    model_label = ""
    conn_id = timing.get("connection_id")
    model_name = timing.get("model")
    if conn_id and model_name:
        model_label = f" · {conn_id}" if conn_id == model_name else f" · {conn_id}/{model_name}"

    if timing.get("cache_hit"):
        timing_md = t("timing_cached") + model_label
        sql_display = result.sql or ""
    else:
        total = timing.get("total_s", 0)
        timing_md = t("timing_live", total=total) + model_label
        n_calls = timing.get("llm_calls", 0)
        if result.sql:
            call_s = "calls" if n_calls != 1 else "call"
            dev_label = conn_id if conn_id == model_name else f"{conn_id}/{model_name}"
            dev_comment = (
                f"\n-- {total:.1f}s total · "
                f"LLM {timing.get('llm_s', 0):.1f}s ({n_calls} {call_s}) · "
                f"DB {timing.get('db_s', 0):.3f}s"
                f" · {dev_label}"
            )
            sql_display = result.sql + dev_comment
        else:
            sql_display = ""

    # Deduplicate then prepend — re-running a question moves it to the top
    # rather than adding a duplicate entry. A superseded_question (the
    # mistyped original a suggestion-click just corrected) is also dropped,
    # not just the exact question — otherwise the dead-end typo lingers in
    # history as a separate, still-clickable entry that leads nowhere new.
    deduped = [
        q for q in session_history if q != question and q != superseded_question
    ]
    new_history = ([question] + deduped)[:20]

    if result.fuzzy_matches:
        # Deterministic recovery path: one or more mistyped literals (e.g. a
        # species name AND a site name in the same query) each matched a
        # real column value closely enough to suggest it. The LLM's own "0
        # rows"/"zero detections" text in _render_response is left untouched
        # — this is an additive UI affordance, not a replacement for it. Each
        # affected column gets its own labeled suggestion group (up to
        # _MAX_GROUPS, extras silently dropped rather than raising).
        #
        # Checking fuzzy_matches directly (not row_count == 0) is required:
        # the backend populates fuzzy_matches using is_empty_result(), which
        # also recognizes an aggregate query (COUNT(*), no GROUP BY) whose
        # single mandatory row holds the value 0 — a shape row_count alone
        # cannot distinguish from "exactly one real row returned."
        #
        # Each button's *value* is the full corrected question (that match's
        # mistyped literal swapped for the clicked candidate within the
        # user's original question), so clicking it re-runs the whole
        # question with its original context (date ranges, other filters)
        # intact — not just the bare candidate name. Falls back to a
        # minimal question built from just the candidate if the literal
        # isn't found verbatim in the user's text (the LLM may have
        # reformatted it before writing the SQL literal).
        group_updates: list = []
        for match in result.fuzzy_matches[:_MAX_GROUPS]:
            candidates = list(match.candidates[:_GROUP_CANDIDATES])
            rewritten = [
                question.replace(match.literal, c) if match.literal in question else c
                for c in candidates
            ]
            padded_labels = candidates + [None] * (_GROUP_CANDIDATES - len(candidates))
            padded_questions = rewritten + [None] * (_GROUP_CANDIDATES - len(rewritten))
            group_updates.extend(
                (
                    gr.update(
                        visible=True,
                        value=t(
                            "fuzzy_suggestion_prompt",
                            label=t(f"fuzzy_column_{match.label_key}"),
                        ),
                    ),
                    *(
                        gr.update(visible=True, value=label) if label is not None
                        else gr.update(visible=False, value="")
                        for label in padded_labels
                    ),
                    *padded_questions,
                )
            )
        # Pad unused groups (fewer matches than _MAX_GROUPS) with hidden slots.
        for _ in range(_MAX_GROUPS - len(result.fuzzy_matches[:_MAX_GROUPS])):
            group_updates.extend(_no_suggestions_for_group())
        suggestion_updates = tuple(group_updates)
    else:
        suggestion_updates = _NO_SUGGESTIONS

    new_trace = (
        {"trace_id": trace_id, "question": question, "at": time.monotonic(), "scored": False}
        if trace_id
        else None
    )
    return (
        sql_display,
        df,
        _render_response(result, show_sql_inline=show_sql_inline),
        count_md,
        gr.Radio(choices=new_history, value=None),
        timing_md,
        "",
        new_history,
        gr.update(selected=0),
        new_trace,
        ab_distinct_id,
        *suggestion_updates,
    )


def _run_query_handler(
    question: str,
    session_history: list,
    superseded_question: str | None = None,
    last_trace: dict | None = None,
    ab_distinct_id: str | None = None,
) -> Generator[_Output, None, None]:
    """Streaming generator: yields status updates then the final result.

    Gradio streams each yielded tuple to the UI in real time so the user
    sees progress instead of a blank screen during the 10-90 second loop.

    session_history is a per-browser list backed by gr.BrowserState
    (localStorage). It is threaded through every yield unchanged until the
    final success yield, which prepends the new question.

    superseded_question, when set, is a mistyped question this run is
    correcting (via a clicked fuzzy-match suggestion) — it's dropped from
    history rather than kept alongside the corrected question. Without this,
    clicking a suggestion left the original dead-end query sitting in
    history: re-running it from there hits the same 0-row result and forces
    the user through the same suggestion click again.

    last_trace, when Langfuse tracing is enabled, is the previous run's
    {trace_id, question, at} — checked here (before this run starts) to
    score whether the PREVIOUS trace was a rephrase, and replaced at the end
    with this run's own trace. Always None when tracing is disabled, which
    every consumer already treats as "nothing to score."

    ab_distinct_id is this browser's opaque, stable identity anchor for the
    show-SQL-by-default experiment — NOT the variant itself. PostHog derives
    the variant deterministically from (flag key, distinct_id) on every
    call, so nothing about the assignment is stored here; only the identity
    anchor persists (generated once, via gr.BrowserState, on first use).
    None until the experiment has assigned this browser an ID.
    """
    question = question.strip()
    if not question:
        yield _empty_result(
            t("error_empty_question"),
            session_history,
            last_trace=last_trace,
            ab_distinct_id=ab_distinct_id,
        )
        return

    if is_unsupported_language(question):
        _log.info("language check rejected: %r", question[:60])
        yield _empty_result(
            t("error_unsupported_language"),
            session_history,
            status=t("error_unsupported_language_status"),
            last_trace=last_trace,
            ab_distinct_id=ab_distinct_id,
        )
        return

    # A distinct_id is generated only once a question actually clears both
    # checks above — an empty submission or a rejected language never
    # allocates an identity, since nothing ran that a variant could affect.
    show_sql_inline = False
    if is_show_sql_experiment_active():
        if not ab_distinct_id:
            import uuid

            ab_distinct_id = uuid.uuid4().hex
        variant = assign_variant(ab_distinct_id)
        show_sql_inline = variant == "treatment"

    # Score the PREVIOUS trace against THIS question before this run starts —
    # a rephrase means the prior answer didn't satisfy the user. No-op when
    # tracing is disabled (score_no_rephrase itself checks that) or there is
    # no previous trace to compare against.
    rephrase_check = _check_rephrase(last_trace, question)
    if rephrase_check is not None:
        prev_trace_id, accepted = rephrase_check
        score_no_rephrase(prev_trace_id, accepted=accepted)
        if last_trace is not None:
            last_trace = {**last_trace, "scored": True}

    # One queue carrying two kinds of event, tagged rather than two queues:
    # ordering between a status string and the data payload that follows it
    # must be preserved, and separate queues would let them interleave wrongly.
    #   ("status", str)                 — narration for the step log
    #   ("preview", (QueryResult, str)) — real rows, available ~16-25s early
    status_q: queue.Queue[tuple[str, object] | None] = queue.Queue()
    result_holder: list = [None]
    error_holder: list[BaseException | None] = [None]

    def _status_cb(msg: str) -> None:
        status_q.put(("status", msg))

    def _result_cb(result: QueryResult, sql: str) -> None:
        status_q.put(("preview", (result, sql)))

    trace_id_holder: list[str | None] = [None]

    def _trace_id_cb(trace_id: str) -> None:
        trace_id_holder[0] = trace_id

    def _worker() -> None:
        try:
            result_holder[0] = run_query(
                question,
                status_cb=_status_cb,
                result_cb=_result_cb,
                trace_id_cb=_trace_id_cb,
            )
        except BaseException as exc:  # noqa: BLE001
            error_holder[0] = exc
        finally:
            status_q.put(None)

    # Immediate feedback before the thread even starts
    first_status = t("status_reading")
    steps: list[tuple[str, str]] = []
    _append_step(steps, first_status)
    yield _status_yield(
        _step_log_markdown(steps),
        session_history,
        is_first=True,
        last_trace=last_trace,
        ab_distinct_id=ab_distinct_id,
    )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    # Accumulating narration: response_box shows every real step taken so
    # far, not a single sentence that can drift out of sync with the top
    # status bar (previously "I understood... Searching the database..."
    # sat directly above a status bar that had already moved on to
    # "Understanding your question...", reading as a contradiction). See
    # _append_step's docstring for the supersede/collapse rules.
    # preview holds the most recent (rows, sql) pair seen so far. A SQL retry
    # overwrites it rather than appending: the corrected query supersedes the
    # one before it, so the table always reflects the latest attempt. It is
    # re-sent on every subsequent yield because Gradio output tuples are
    # fixed-shape — omitting it would blank the table on the next status tick.
    preview: tuple[QueryResult, str] | None = None
    while True:
        item = status_q.get()
        if item is None:
            break
        kind, payload = item
        if kind == "preview":
            preview = payload  # type: ignore[assignment]
            yield _status_yield(
                _step_log_markdown(steps),
                session_history,
                preview=preview,
                last_trace=last_trace,
                ab_distinct_id=ab_distinct_id,
            )
            continue
        status_text = t("status_cache_hit") if payload == "CACHE_HIT" else payload
        _append_step(steps, status_text)
        yield _status_yield(
            _step_log_markdown(steps),
            session_history,
            preview=preview,
            last_trace=last_trace,
            ab_distinct_id=ab_distinct_id,
        )

    thread.join()

    exc = error_holder[0]
    if exc is not None:
        if isinstance(exc, SQLGuardError):
            _log.warning("SQL guard blocked %s", exc.operation)
            yield (
                exc.sql,
                gr.Dataframe(value=None),
                t("error_guard_readonly", operation=exc.operation),
                "",
                gr.Radio(choices=session_history),
                "",
                t("error_guard_readonly_status", operation=exc.operation),
                session_history,
                gr.update(selected=0),
                last_trace,
                ab_distinct_id,
                *_NO_SUGGESTIONS,
            )
        elif isinstance(exc, psycopg2.errors.QueryCanceled):
            _log.warning("statement_timeout exceeded for question: %r", question[:60])
            yield _empty_result(
                t("error_timeout"),
                session_history,
                status="⚠ Query timed out",
                last_trace=last_trace,
                ab_distinct_id=ab_distinct_id,
            )
        elif isinstance(exc, psycopg2.OperationalError):
            _log.error("DB connection error: %s", exc)
            yield _empty_result(
                t("error_db_connection"),
                session_history,
                status="⚠ Database unreachable",
                last_trace=last_trace,
                ab_distinct_id=ab_distinct_id,
            )
        elif isinstance(exc, RuntimeError) and "maximum iterations" in str(exc):
            _log.warning("loop exhausted for question: %r", question[:60])
            yield _empty_result(
                t("error_iterations"),
                session_history,
                status="⚠ Question too complex",
                last_trace=last_trace,
                ab_distinct_id=ab_distinct_id,
            )
        elif isinstance(exc, UnsupportedLanguageError):
            # Defense-in-depth: the is_unsupported_language() check above
            # already catches this before run_query() is ever called, so
            # this path is normally unreachable from the UI. It exists so
            # run_query() itself is self-protecting for any caller that
            # bypasses this handler (scripts, direct API use).
            _log.info("run_query rejected unsupported language: %r", question[:60])
            yield _empty_result(
                t("error_unsupported_language"),
                session_history,
                status=t("error_unsupported_language_status"),
                last_trace=last_trace,
                ab_distinct_id=ab_distinct_id,
            )
        else:
            _log.error("query failed in UI: %s", exc, exc_info=True)
            yield _empty_result(
                t("error_generic_response"),
                session_history,
                status=t("error_generic_status"),
                last_trace=last_trace,
                ab_distinct_id=ab_distinct_id,
            )
        return

    result = result_holder[0]
    if trace_id_holder[0] and is_show_sql_experiment_active():
        log_exposure(trace_id_holder[0], variant="treatment" if show_sql_inline else "control")
    yield _success_result(
        result,
        question,
        session_history,
        superseded_question,
        trace_id_holder[0],
        show_sql_inline,
        ab_distinct_id,
    )


def _clear_handler(current_question: str) -> tuple:
    """Clear history list and all result panels. Preserve the question box text."""
    clear_history()
    return (
        gr.Radio(choices=[]),   # history_radio — empty
        current_question,       # question_box — preserve what user typed
        _IDLE_PROMPT,           # response_box — reset to idle
        "",                     # row_count_md
        gr.Dataframe(value=None, headers=None),  # results_table
        "",                     # sql_box
        "",                     # timing_md
        "",                     # status_md
        [],                     # history_state
        None,                   # last_trace_state — nothing left to score against
        *_NO_SUGGESTIONS,       # prompt + 3 buttons + 3 hidden question states
    )


def build_app() -> gr.Blocks:
    """Build and return the Gradio Blocks application."""
    with gr.Blocks(title="Canopy") as app:
        gr.Markdown(f"# 🌿 Canopy\n{t('app_subtitle')}")

        # Per-browser history backed by localStorage — survives page refresh,
        # isolated per device. Default is empty; app.load() populates the
        # sidebar Radio from localStorage on every page load.
        history_state = gr.BrowserState(default_value=[], storage_key="canopy_history")

        # Per-browser record of the most recent trace, {trace_id, question, at,
        # scored} — read at the start of the NEXT query to score whether this
        # one was a rephrase (see _check_rephrase), written at the end of this
        # one. Always None when Langfuse tracing is disabled (the default).
        last_trace_state = gr.BrowserState(default_value=None, storage_key="canopy_last_trace")

        # A/B experiment: show-SQL-by-default (see observability.assign_variant,
        # config.is_show_sql_experiment_active, ~/Desktop/AB-Tests/ab-test-plan.md).
        # Holds an opaque identity anchor ONLY — never the variant itself.
        # PostHog's get_feature_flag() derives the variant deterministically
        # from (flag key, this ID) on every call, so the same ID always maps
        # to the same arm with nothing about the assignment stored here.
        # Generated once per browser on first use, then stable for that
        # browser's lifetime — a fresh random ID on every query would let
        # PostHog re-derive a DIFFERENT variant each time, which would make
        # "did the treatment work" unanswerable.
        ab_distinct_id_state = gr.BrowserState(
            default_value=None, storage_key="canopy_ab_distinct_id"
        )

        with gr.Row():
            # ── Left panel ─────────────────────────────────────────────────────
            with gr.Column(scale=1, min_width=280):
                question_box = gr.Textbox(
                    label=t("question_label"),
                    placeholder=_PLACEHOLDER,
                    lines=3,
                )
                submit_btn = gr.Button(t("run_btn"), variant="primary", size="lg")

                gr.Markdown(t("recent_queries"))
                history_radio = gr.Radio(
                    choices=[],  # populated from localStorage on app.load
                    label="",
                    container=False,
                )
                clear_btn = gr.Button(t("clear_btn"), size="sm", variant="secondary")

            # ── Right panel ────────────────────────────────────────────────────
            with gr.Column(scale=2):
                # sanitize_html=False: this component only ever receives
                # server-templated status strings (from t(), never raw user
                # input) plus the data-canopy-loading marker span from
                # _loading_status_html() — the default sanitizer strips
                # data-* attributes, which the head-script ticker needs to
                # find its target. No <script> tag lives in this value; see
                # STATUS_TICKER_HEAD_SCRIPT for why.
                status_md = gr.Markdown("", elem_id="canopy-status", sanitize_html=False)

                # Hidden by default — shown only when a 0-row result finds a
                # close fuzzy match for a mistyped species/site name. One
                # group per affected column (a question can have typos in
                # more than one fuzzy-checkable column at once — see
                # fuzzy_match.find_candidates). Clicking a suggestion
                # repopulates the question and re-runs it, mirroring
                # history_radio's own select-and-rerun pattern below.
                #
                # Each button's displayed label is just the candidate name
                # (short, readable); the full corrected question it should
                # re-run is carried separately in a paired gr.State, since
                # the two can differ (typo swapped in context vs bare name).
                suggestion_groups: list[dict] = []
                for _g in range(_MAX_GROUPS):
                    prompt_md = gr.Markdown(
                        "", visible=False, elem_id=f"canopy-suggestions-{_g}"
                    )
                    with gr.Row():
                        buttons = [
                            gr.Button("", visible=False, size="sm", variant="secondary")
                            for _ in range(_GROUP_CANDIDATES)
                        ]
                    q_states = [gr.State(None) for _ in range(_GROUP_CANDIDATES)]
                    suggestion_groups.append(
                        {
                            "prompt": prompt_md,
                            "buttons": buttons,
                            "q_states": q_states,
                        }
                    )

                with gr.Tabs() as result_tabs:
                    with gr.Tab(t("tab_answer"), id=0):
                        response_box = gr.Markdown(_IDLE_PROMPT, elem_id="canopy-response")
                        # Explicit acceptance-proxy signal (thumbs_explicit —
                        # see observability.py). Hidden unless tracing is
                        # enabled: with tracing off there is nowhere for a
                        # click to go, so showing the control would be a
                        # dead affordance. This is the ONLY new UI surface
                        # in the tracing work — everything else reuses
                        # existing components (see DECISIONS.md's Operations
                        # entry for the online-eval instrumentation).
                        if is_langfuse_enabled():
                            with gr.Row():
                                thumbs_up_btn = gr.Button(
                                    "👍", size="sm", variant="secondary", scale=0
                                )
                                thumbs_down_btn = gr.Button(
                                    "👎", size="sm", variant="secondary", scale=0
                                )
                    with gr.Tab(t("tab_data"), id=1):
                        row_count_md = gr.Markdown("")
                        results_table = gr.Dataframe(
                            label="",
                            wrap=True,
                            interactive=False,
                        )
                    with gr.Tab(t("tab_sql"), id=2):
                        sql_box = gr.Code(
                            label="",
                            language="sql",
                            interactive=False,
                        )
                timing_md = gr.Markdown("", elem_classes=["timing-info"])

        _suggestion_outputs: list = []
        for group in suggestion_groups:
            _suggestion_outputs.append(group["prompt"])
            _suggestion_outputs.extend(group["buttons"])
            _suggestion_outputs.extend(group["q_states"])

        _OUTPUTS = [
            sql_box, results_table, response_box, row_count_md,
            history_radio, timing_md, status_md, history_state, result_tabs,
            last_trace_state, ab_distinct_id_state,
            *_suggestion_outputs,
        ]

        # Restore history sidebar from localStorage on every page load
        app.load(
            fn=lambda h: gr.Radio(choices=h),
            inputs=[history_state],
            outputs=[history_radio],
        )

        submit_btn.click(
            fn=_run_query_handler,
            inputs=[
                question_box, history_state, gr.State(None), last_trace_state, ab_distinct_id_state,
            ],
            outputs=_OUTPUTS,
            concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
        )
        question_box.submit(
            fn=_run_query_handler,
            inputs=[
                question_box, history_state, gr.State(None), last_trace_state, ab_distinct_id_state,
            ],
            outputs=_OUTPUTS,
            concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
        )
        history_radio.input(
            fn=lambda q: q or "",
            inputs=[history_radio],
            outputs=[question_box],
        ).then(
            fn=_run_query_handler,
            inputs=[
                question_box, history_state, gr.State(None), last_trace_state, ab_distinct_id_state,
            ],
            outputs=_OUTPUTS,
            concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
        )
        clear_btn.click(
            fn=_clear_handler,
            inputs=[question_box],
            outputs=[
                history_radio, question_box, response_box,
                row_count_md, results_table, sql_box,
                timing_md, status_md, history_state,
                last_trace_state,
                *_suggestion_outputs,
            ],
        )

        # Clicking a "did you mean" suggestion re-runs the corrected question
        # (typo swapped for the clicked candidate, rest of the question's
        # context preserved) carried in that button's paired gr.State — same
        # select-and-rerun pattern as history_radio.input() above.
        #
        # The mistyped question still sitting in question_box at click-time
        # is captured into superseded_state before it's overwritten, so
        # _run_query_handler can drop that dead-end entry from history
        # instead of leaving it alongside the corrected question — clicking
        # it later would just hit the same 0-row result again.
        #
        # A "skip the LLM, just swap the name in the last SQL and re-run it"
        # fast path was built, tested and then removed — see DECISIONS.md's
        # U2 section. Short version: fuzzy_matches is only ever populated
        # when a query returned NOTHING (loop.py's execute_sql closure), and
        # on an empty result the agent often stops without ever writing the
        # real answer query — so the stored SQL is frequently a
        # lookup/verification step ("does this species name exist?"), not
        # the query that answers the user's question. Substituting into it
        # returns a confidently wrong answer. No SQL-text heuristic
        # separates the two cases reliably (an exploratory
        # `SELECT COUNT(*) ... ILIKE '%typo%'` is shape-identical to a real
        # count answer), so the direction was rejected rather than patched.
        superseded_state = gr.State(None)
        for group in suggestion_groups:
            for suggestion_btn, suggestion_q in zip(group["buttons"], group["q_states"]):
                suggestion_btn.click(
                    fn=lambda original: original,
                    inputs=[question_box],
                    outputs=[superseded_state],
                ).then(
                    fn=lambda q: q or "",
                    inputs=[suggestion_q],
                    outputs=[question_box],
                ).then(
                    fn=_run_query_handler,
                    inputs=[
                        question_box, history_state, superseded_state,
                        last_trace_state, ab_distinct_id_state,
                    ],
                    outputs=_OUTPUTS,
                    concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
                )

        # Explicit acceptance-proxy control — only present when tracing is
        # enabled (see the Answer tab above). Scores the CURRENT trace, not
        # the previous one: unlike _check_rephrase, a thumbs click is about
        # the answer the user is looking at right now.
        if is_langfuse_enabled():

            def _thumbs(last_trace: dict | None, *, positive: bool) -> None:
                if last_trace and last_trace.get("trace_id"):
                    score_thumbs(last_trace["trace_id"], positive=positive)

            thumbs_up_btn.click(
                fn=lambda lt: _thumbs(lt, positive=True),
                inputs=[last_trace_state],
                outputs=[],
            )
            thumbs_down_btn.click(
                fn=lambda lt: _thumbs(lt, positive=False),
                inputs=[last_trace_state],
                outputs=[],
            )

    return app
