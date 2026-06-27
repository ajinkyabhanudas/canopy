"""Gradio UI for canopy — two-panel layout: question/history | response/results/sql."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Generator

import gradio as gr

from canopy.history import clear_history, load_history
from canopy.query.executor import SQLGuardError
from canopy.query.loop import run_query

_log = logging.getLogger("canopy.ui")

_PLACEHOLDER = "e.g. How many confirmed species were detected at each reserve in 2023?"

_IDLE_PROMPT = """\
Ask a question to get started.

**Try asking:**
- "How many confirmed species were detected at each reserve in 2023?"
- "Which sites had the most activity last year?"
- "Show me all Jocotoco Antpitta detections since 2022."
- "How many AI detections are awaiting human review at each site?"
"""

# Type alias for the 7-tuple every handler output must match
# [sql_box, results_table, response_box, row_count_md, history_radio, timing_md, status_md]
_Output = tuple


def _history_choices() -> list[str]:
    """Return the last 20 questions in reverse-chronological order."""
    return [e["question"] for e in reversed(load_history(n=20))]


def _empty_result(message: str, status: str = "") -> _Output:
    """Return a blank 7-tuple with only the response message and optional status set."""
    try:
        choices = _history_choices()
    except Exception:
        choices = []
    return (
        "",
        gr.Dataframe(value=None),
        message,
        "",
        gr.Radio(choices=choices),
        "",
        status,
    )


def _run_query_handler(question: str) -> Generator[_Output, None, None]:
    """Streaming generator: yields status updates then the final result.

    Gradio streams each yielded tuple to the UI in real time so the user
    sees progress instead of a blank screen during the 10-90 second loop.
    """
    question = question.strip()
    if not question:
        yield _empty_result("Please enter a question.")
        return

    status_q: queue.Queue[str | None] = queue.Queue()
    result_holder: list = [None]
    error_holder: list[BaseException | None] = [None]
    # Track the last intent text so it persists through subsequent status yields
    intent_text: list[str] = [""]

    def _status_cb(msg: str) -> None:
        status_q.put(msg)

    def _worker() -> None:
        try:
            result_holder[0] = run_query(question, status_cb=_status_cb)
        except BaseException as exc:  # noqa: BLE001
            error_holder[0] = exc
        finally:
            status_q.put(None)  # sentinel — signals main thread to stop waiting

    # Snapshot history once — it doesn't change while the query runs.
    # The final yield (after append_history) calls _history_choices() fresh.
    pre_query_choices = _history_choices()

    # Immediate feedback before the thread even starts
    yield (
        "",
        gr.Dataframe(value=None),
        "_Thinking…_",
        "",
        gr.Radio(choices=pre_query_choices),
        "",
        "⏳ Understanding your question…",
    )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    while True:
        msg = status_q.get()
        if msg is None:
            break
        if msg == "CACHE_HIT":
            yield (
                "",
                gr.Dataframe(value=None),
                "_Loading from cache…_",
                "",
                gr.Radio(choices=pre_query_choices),
                "",
                "✓ Loading from cache…",
            )
            continue
        if msg.startswith("INTENT:"):
            intent_text[0] = msg[7:].strip()
            response_text = f"**I understood:** {intent_text[0]}\n\n_Searching the database…_"
            status_text = "⏳ Searching the monitoring database…"
        else:
            # Keep intent visible in response_box if we have it, otherwise blank
            response_text = (
                f"**I understood:** {intent_text[0]}\n\n_Searching the database…_"
                if intent_text[0]
                else ""
            )
            status_text = f"⏳ {msg}"
        yield (
            "",
            gr.Dataframe(value=None),
            response_text,
            "",
            gr.Radio(choices=pre_query_choices),
            "",
            status_text,
        )

    thread.join()

    exc = error_holder[0]
    if exc is not None:
        if isinstance(exc, SQLGuardError):
            _log.warning("SQL guard rejected generated query")
            yield (
                exc.sql,
                gr.Dataframe(value=None),
                (
                    "I wasn't able to run that query safely.\n\n"
                    "This sometimes happens with unusual question phrasing — "
                    "try asking what's in the data rather than asking to change it.\n\n"
                    "The generated query is shown in the **SQL tab** for reference."
                ),
                "",
                gr.Radio(choices=_history_choices()),
                "",
                "",
            )
        else:
            _log.error("query failed in UI: %s", exc, exc_info=True)
            yield _empty_result(
                "Something went wrong while searching. "
                "Please try again, or rephrase your question."
            )
        return

    result = result_holder[0]
    rows = [list(row) for row in result.rows]
    df = gr.Dataframe(value=rows or None, headers=result.columns)
    count = result.row_count
    count_md = f"**{count} row{'s' if count != 1 else ''} returned**"
    t = result.timing
    if t.get("cache_hit"):
        timing_md = "⚡ Loaded from your recent queries"
        sql_display = result.sql or ""
    else:
        total = t.get("total_s", 0)
        timing_md = f"Answer ready in {total:.0f}s"
        n_calls = t.get("llm_calls", 0)
        if result.sql:
            dev_comment = (
                f"\n-- {total:.1f}s total · "
                f"LLM {t.get('llm_s', 0):.1f}s ({n_calls} call{'s' if n_calls != 1 else ''}) · "
                f"DB {t.get('db_s', 0):.3f}s"
            )
            sql_display = result.sql + dev_comment
        else:
            sql_display = ""
    yield (
        sql_display,
        df,
        result.model_text,
        count_md,
        gr.Radio(choices=_history_choices(), value=None),
        timing_md,
        "",  # clear status_md on success
    )


def _clear_handler() -> tuple:
    clear_history()
    return gr.Radio(choices=[]), "", _IDLE_PROMPT


def build_app() -> gr.Blocks:
    """Build and return the Gradio Blocks application."""
    with gr.Blocks(title="Canopy") as app:
        gr.Markdown(
            "# 🌿 Canopy\n"
            "Ask questions about Jocotoco's species monitoring data in plain English."
        )

        with gr.Row():
            # ── Left panel ─────────────────────────────────────────────────────
            with gr.Column(scale=1, min_width=280):
                question_box = gr.Textbox(
                    label="Ask a question",
                    placeholder=_PLACEHOLDER,
                    lines=3,
                )
                submit_btn = gr.Button("Run Query", variant="primary", size="lg")

                gr.Markdown("### Recent queries")
                history_radio = gr.Radio(
                    choices=_history_choices(),
                    label="",
                    container=False,
                )
                clear_btn = gr.Button("Clear history", size="sm", variant="secondary")

            # ── Right panel ────────────────────────────────────────────────────
            with gr.Column(scale=2):
                # Status bar — always visible regardless of which tab is active
                status_md = gr.Markdown("", elem_id="canopy-status")
                with gr.Tabs():
                    with gr.Tab("Answer"):
                        response_box = gr.Markdown(_IDLE_PROMPT)
                    with gr.Tab("Full data table"):
                        row_count_md = gr.Markdown("")
                        results_table = gr.Dataframe(
                            label="",
                            wrap=True,
                            interactive=False,
                        )
                    with gr.Tab("Database query"):
                        sql_box = gr.Code(
                            label="",
                            language="sql",
                            interactive=False,
                        )
                timing_md = gr.Markdown("", elem_classes=["timing-info"])

        _OUTPUTS = [
            sql_box, results_table, response_box, row_count_md,
            history_radio, timing_md, status_md,
        ]

        submit_btn.click(
            fn=_run_query_handler, inputs=[question_box], outputs=_OUTPUTS,
            concurrency_limit=1,
        )
        question_box.submit(
            fn=_run_query_handler, inputs=[question_box], outputs=_OUTPUTS,
            concurrency_limit=1,
        )
        history_radio.change(
            fn=lambda q: q or "",
            inputs=[history_radio],
            outputs=[question_box],
        ).then(
            fn=_run_query_handler,
            inputs=[question_box],
            outputs=_OUTPUTS,
        )
        clear_btn.click(fn=_clear_handler, outputs=[history_radio, question_box, response_box])

    return app
