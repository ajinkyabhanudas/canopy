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

_PLACEHOLDER = (
    "e.g. Which bird species have been detected at Buenaventura "
    "in the last five years?"
)

# Type alias for the 6-tuple every handler output must match
_Output = tuple


def _history_choices() -> list[str]:
    """Return the last 20 questions in reverse-chronological order."""
    return [e["question"] for e in reversed(load_history(n=20))]


def _empty_result(message: str) -> _Output:
    """Return a blank 6-tuple with only the response message set."""
    return (
        "",
        gr.Dataframe(value=None),
        message,
        "",
        gr.Radio(choices=_history_choices()),
        "",
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

    def _status_cb(msg: str) -> None:
        status_q.put(msg)

    def _worker() -> None:
        try:
            result_holder[0] = run_query(question, status_cb=_status_cb)
        except BaseException as exc:  # noqa: BLE001
            error_holder[0] = exc
        finally:
            status_q.put(None)  # sentinel — signals main thread to stop waiting

    # Immediate feedback before the thread even starts
    yield ("", gr.Dataframe(value=None), "_Thinking…_", "", gr.Radio(choices=_history_choices()), "")

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    while True:
        msg = status_q.get()
        if msg is None:
            break
        yield (
            "",
            gr.Dataframe(value=None),
            f"⏳ {msg}",
            "",
            gr.Radio(choices=_history_choices()),
            "",
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
                    "The model generated a query canopy cannot execute.\n\n"
                    "Check the **SQL tab** to see what was generated.\n\n"
                    f"_Reason: {exc}_"
                ),
                "",
                gr.Radio(choices=_history_choices()),
                "",
            )
        else:
            _log.error("query failed in UI: %s", exc, exc_info=True)
            yield _empty_result(f"Sorry, I couldn't process that question.\n\nDetails: {exc}")
        return

    result = result_holder[0]
    rows = [list(row) for row in result.rows]
    df = gr.Dataframe(value=rows or None, headers=result.columns)
    count = result.row_count
    count_md = f"**{count} row{'s' if count != 1 else ''} returned**"
    t = result.timing
    n_calls = t.get("llm_calls", 0)
    timing_md = (
        f"⏱ {t.get('total_s', 0):.1f}s total · "
        f"LLM {t.get('llm_s', 0):.1f}s ({n_calls} call{'s' if n_calls != 1 else ''}) · "
        f"DB {t.get('db_s', 0):.3f}s"
    )
    yield (
        result.sql or "",
        df,
        result.model_text,
        count_md,
        gr.Radio(choices=_history_choices()),
        timing_md,
    )


def _clear_handler() -> tuple:
    clear_history()
    return gr.Radio(choices=[]), ""


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
                with gr.Tabs():
                    with gr.Tab("Response"):
                        response_box = gr.Markdown("")
                    with gr.Tab("Results"):
                        row_count_md = gr.Markdown("")
                        results_table = gr.Dataframe(
                            label="",
                            wrap=True,
                            interactive=False,
                        )
                    with gr.Tab("SQL"):
                        sql_box = gr.Code(
                            label="",
                            language="sql",
                            interactive=False,
                        )
                timing_md = gr.Markdown("", elem_classes=["timing-info"])

        _OUTPUTS = [sql_box, results_table, response_box, row_count_md, history_radio, timing_md]

        submit_btn.click(
            fn=_run_query_handler, inputs=[question_box], outputs=_OUTPUTS, streaming=True
        )
        question_box.submit(
            fn=_run_query_handler, inputs=[question_box], outputs=_OUTPUTS, streaming=True
        )
        history_radio.change(
            fn=lambda q: q or "",
            inputs=[history_radio],
            outputs=[question_box],
        )
        clear_btn.click(fn=_clear_handler, outputs=[history_radio, question_box])

    return app
