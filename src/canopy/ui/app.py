"""Gradio UI for canopy — two-panel layout: question/history | response/results/sql."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Generator

import gradio as gr
import psycopg2
import psycopg2.errors

from canopy.config import get_ui_lang
from canopy.history import clear_history
from canopy.i18n import set_locale, t
from canopy.query.executor import SQLGuardError
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
# comfortably and stays inside DECISIONS.md § O2's own "1-5 concurrent
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
"""

# A question can have typos in more than one fuzzy-checkable column at once
# (e.g. a mistyped species name AND a mistyped site name in the same SQL).
# Each affected column gets its own pre-mounted suggestion group — a prompt
# naming the column plus up to _GROUP_CANDIDATES buttons — stacked below
# status_md. _MAX_GROUPS caps how many groups are pre-mounted; FUZZY_COLUMNS
# in fuzzy_match.py currently registers 3 columns, so 3 covers every case
# today. Extra matches beyond _MAX_GROUPS are silently not shown rather than
# raising — better to surface the first N corrections than none.
# _MAX_GROUPS hardcoded to len(FUZZY_COLUMNS), not derived from it →
# upgrade (bump this value, or assert len(FUZZY_COLUMNS) <= _MAX_GROUPS at
# import time) when a 4th fuzzy-checkable column is registered.
_MAX_GROUPS = 3
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


def _render_response(result: LoopResult) -> str:
    """Build the Answer tab's full markdown: model_text (block stripped) + interpretation."""
    body = strip_interpretation_block(result.model_text)
    interpretation_md = _render_interpretation(result.interpretation)
    if not interpretation_md:
        return body
    return f"{body}\n\n{interpretation_md}"


def _empty_result(message: str, session_history: list, status: str = "") -> _Output:
    """Return a blank 13-tuple with only the response message and optional status set."""
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
        *_NO_SUGGESTIONS,
    )


def _status_yield(response_text: str, status_text: str, session_history: list) -> _Output:
    """Return a blank 13-tuple with only status/response text set (for streaming updates)."""
    return (
        "",
        gr.Dataframe(value=None),
        response_text,
        "",
        gr.Radio(choices=session_history),
        "",
        status_text,
        session_history,
        gr.update(selected=0),
        *_NO_SUGGESTIONS,
    )


def _run_query_handler(
    question: str, session_history: list, superseded_question: str | None = None
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
    """
    question = question.strip()
    if not question:
        yield _empty_result(t("error_empty_question"), session_history)
        return

    if is_unsupported_language(question):
        _log.info("language check rejected: %r", question[:60])
        yield _empty_result(
            t("error_unsupported_language"),
            session_history,
            status=t("error_unsupported_language_status"),
        )
        return

    status_q: queue.Queue[str | None] = queue.Queue()
    result_holder: list = [None]
    error_holder: list[BaseException | None] = [None]
    # Pre-populate with the question so the "I understood" message shows
    # even when the model goes straight to a tool call (no response.text).
    intent_text: list[str] = [question]

    def _status_cb(msg: str) -> None:
        status_q.put(msg)

    def _worker() -> None:
        try:
            result_holder[0] = run_query(question, status_cb=_status_cb)
        except BaseException as exc:  # noqa: BLE001
            error_holder[0] = exc
        finally:
            status_q.put(None)

    # Immediate feedback before the thread even starts
    yield _status_yield(t("status_reading"), t("status_reading"), session_history)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    while True:
        msg = status_q.get()
        if msg is None:
            break
        if msg == "CACHE_HIT":
            yield _status_yield(t("status_cache_hit"), t("status_cache_hit"), session_history)
            continue
        if msg.startswith("INTENT:"):
            intent_text[0] = msg[7:].strip()
            response_text = t("status_understood", intent=intent_text[0])
            status_text = t("status_searching_db")
        else:
            response_text = (
                t("status_understood", intent=intent_text[0])
                if intent_text[0]
                else ""
            )
            status_text = msg
        yield _status_yield(response_text, status_text, session_history)

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
                *_NO_SUGGESTIONS,
            )
        elif isinstance(exc, psycopg2.errors.QueryCanceled):
            _log.warning("statement_timeout exceeded for question: %r", question[:60])
            yield _empty_result(t("error_timeout"), session_history, status="⚠ Query timed out")
        elif isinstance(exc, psycopg2.OperationalError):
            _log.error("DB connection error: %s", exc)
            yield _empty_result(
                t("error_db_connection"), session_history, status="⚠ Database unreachable"
            )
        elif isinstance(exc, RuntimeError) and "maximum iterations" in str(exc):
            _log.warning("loop exhausted for question: %r", question[:60])
            yield _empty_result(
                t("error_iterations"), session_history, status="⚠ Question too complex"
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
            )
        else:
            _log.error("query failed in UI: %s", exc, exc_info=True)
            yield _empty_result(
                t("error_generic_response"),
                session_history,
                status=t("error_generic_status"),
            )
        return

    result = result_holder[0]
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

    yield (
        sql_display,
        df,
        _render_response(result),
        count_md,
        gr.Radio(choices=new_history, value=None),
        timing_md,
        "",
        new_history,
        gr.update(selected=0),
        *suggestion_updates,
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
                status_md = gr.Markdown("", elem_id="canopy-status")

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
                        {"prompt": prompt_md, "buttons": buttons, "q_states": q_states}
                    )

                with gr.Tabs() as result_tabs:
                    with gr.Tab(t("tab_answer"), id=0):
                        response_box = gr.Markdown(_IDLE_PROMPT)
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
            inputs=[question_box, history_state],
            outputs=_OUTPUTS,
            concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
        )
        question_box.submit(
            fn=_run_query_handler,
            inputs=[question_box, history_state],
            outputs=_OUTPUTS,
            concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
        )
        history_radio.input(
            fn=lambda q: q or "",
            inputs=[history_radio],
            outputs=[question_box],
        ).then(
            fn=_run_query_handler,
            inputs=[question_box, history_state],
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
                    inputs=[question_box, history_state, superseded_state],
                    outputs=_OUTPUTS,
                    concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
                )

    return app
