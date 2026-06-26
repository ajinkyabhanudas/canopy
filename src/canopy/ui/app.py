"""Gradio UI for canopy — two-panel layout: question/history | response/results/sql."""

from __future__ import annotations

import logging

import gradio as gr

from canopy.history import clear_history, load_history
from canopy.query.loop import run_query

_log = logging.getLogger("canopy.ui")

_PLACEHOLDER = (
    "e.g. Which bird species have been detected at Buenaventura "
    "in the last five years?"
)


def _history_choices() -> list[str]:
    """Return the last 20 questions in reverse-chronological order."""
    return [e["question"] for e in reversed(load_history(n=20))]


def _run_query_handler(question: str) -> tuple:
    """Validate, run, and format a query for Gradio outputs.

    Returns: (sql, dataframe, response_text, row_count_md, history_radio)
    """
    question = question.strip()
    if not question:
        return _empty_result("Please enter a question.")

    try:
        result = run_query(question)
        rows = [list(row) for row in result.rows]
        df = gr.Dataframe(value=rows or None, headers=result.columns)
        count = result.row_count
        count_md = f"**{count} row{'s' if count != 1 else ''} returned**"
        return (
            result.sql or "",
            df,
            result.model_text,
            count_md,
            gr.Radio(choices=_history_choices()),
        )
    except Exception as exc:
        _log.error("query failed in UI: %s", exc, exc_info=True)
        return _empty_result(
            f"Sorry, I couldn't process that question.\n\nDetails: {exc}"
        )


def _empty_result(message: str) -> tuple:
    return (
        "",
        gr.Dataframe(value=None),
        message,
        "",
        gr.Radio(choices=_history_choices()),
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
                        response_box = gr.Textbox(
                            label="",
                            lines=14,
                            interactive=False,
                        )
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

        _OUTPUTS = [sql_box, results_table, response_box, row_count_md, history_radio]

        submit_btn.click(fn=_run_query_handler, inputs=[question_box], outputs=_OUTPUTS)
        question_box.submit(fn=_run_query_handler, inputs=[question_box], outputs=_OUTPUTS)
        history_radio.change(
            fn=lambda q: q or "",
            inputs=[history_radio],
            outputs=[question_box],
        )
        clear_btn.click(fn=_clear_handler, outputs=[history_radio, question_box])

    return app
