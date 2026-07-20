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

# Type alias for the 16-tuple every handler output must match:
# [sql_box, results_table, response_box, row_count_md, history_radio,
#  timing_md, status_md, history_state, result_tabs, suggestion_prompt_md,
#  suggestion_btn_1, suggestion_btn_2, suggestion_btn_3,
#  suggestion_q_1, suggestion_q_2, suggestion_q_3]
_Output = tuple

# Suggestion buttons are hidden by default and only shown on a 0-row result
# with fuzzy candidates. This tuple is yielded for the trailing 7 output
# slots (prompt + 3 buttons + 3 hidden question states) on every path that
# isn't the fuzzy-suggestion success case, so the row never lingers visible
# from a previous query.
_NO_SUGGESTIONS: tuple = (
    gr.update(visible=False),
    gr.update(visible=False, value=""),
    gr.update(visible=False, value=""),
    gr.update(visible=False, value=""),
    None,
    None,
    None,
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
    question: str, session_history: list
) -> Generator[_Output, None, None]:
    """Streaming generator: yields status updates then the final result.

    Gradio streams each yielded tuple to the UI in real time so the user
    sees progress instead of a blank screen during the 10-90 second loop.

    session_history is a per-browser list backed by gr.BrowserState
    (localStorage). It is threaded through every yield unchanged until the
    final success yield, which prepends the new question.
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
    # rather than adding a duplicate entry.
    deduped = [q for q in session_history if q != question]
    new_history = ([question] + deduped)[:20]

    if result.row_count == 0 and result.fuzzy_match is not None:
        # Deterministic recovery path: a mistyped species/site name matched a
        # real column value closely enough to suggest it. The LLM's own "0
        # rows" text in _render_response is left untouched — this is an
        # additive UI affordance, not a replacement for it.
        #
        # Each button's *value* is the full corrected question (the mistyped
        # literal swapped for that candidate within the user's original
        # question), so clicking it re-runs the whole question with its
        # original context (date ranges, site filters, etc.) intact — not
        # just the bare candidate name. Falls back to a minimal question
        # built from just the candidate if the literal isn't found verbatim
        # in the user's text (the LLM may have reformatted it before writing
        # the SQL literal).
        literal = result.fuzzy_match.literal
        candidates = list(result.fuzzy_match.candidates[:3])
        rewritten = [
            question.replace(literal, c) if literal in question else c
            for c in candidates
        ]
        padded_labels = candidates + [None] * (3 - len(candidates))
        padded_questions = rewritten + [None] * (3 - len(rewritten))
        suggestion_updates = (
            gr.update(visible=True, value=t("fuzzy_suggestion_prompt")),
            *(
                gr.update(visible=True, value=label) if label is not None
                else gr.update(visible=False, value="")
                for label in padded_labels
            ),
            *padded_questions,
        )
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
                # close fuzzy match for a mistyped species/site name. Clicking
                # a suggestion repopulates the question and re-runs it,
                # mirroring history_radio's own select-and-rerun pattern below.
                suggestion_prompt_md = gr.Markdown("", visible=False, elem_id="canopy-suggestions")
                with gr.Row():
                    suggestion_btn_1 = gr.Button("", visible=False, size="sm", variant="secondary")
                    suggestion_btn_2 = gr.Button("", visible=False, size="sm", variant="secondary")
                    suggestion_btn_3 = gr.Button("", visible=False, size="sm", variant="secondary")
                # Each button's displayed label is just the candidate name
                # (short, readable); the full corrected question it should
                # re-run is carried separately here so the two can differ.
                suggestion_q_1 = gr.State(None)
                suggestion_q_2 = gr.State(None)
                suggestion_q_3 = gr.State(None)

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

        _OUTPUTS = [
            sql_box, results_table, response_box, row_count_md,
            history_radio, timing_md, status_md, history_state, result_tabs,
            suggestion_prompt_md, suggestion_btn_1, suggestion_btn_2, suggestion_btn_3,
            suggestion_q_1, suggestion_q_2, suggestion_q_3,
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
                suggestion_prompt_md, suggestion_btn_1, suggestion_btn_2, suggestion_btn_3,
                suggestion_q_1, suggestion_q_2, suggestion_q_3,
            ],
        )

        # Clicking a "did you mean" suggestion re-runs the corrected question
        # (typo swapped for the clicked candidate, rest of the question's
        # context preserved) carried in that button's paired gr.State — same
        # select-and-rerun pattern as history_radio.input() above.
        for suggestion_btn, suggestion_q in (
            (suggestion_btn_1, suggestion_q_1),
            (suggestion_btn_2, suggestion_q_2),
            (suggestion_btn_3, suggestion_q_3),
        ):
            suggestion_btn.click(
                fn=lambda q: q or "",
                inputs=[suggestion_q],
                outputs=[question_box],
            ).then(
                fn=_run_query_handler,
                inputs=[question_box, history_state],
                outputs=_OUTPUTS,
                concurrency_limit=_QUERY_CONCURRENCY_LIMIT,
            )

    return app
