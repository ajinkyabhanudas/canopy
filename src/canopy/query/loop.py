"""Agentic query loop — NL question → model → SQL tool call → LoopResult."""

from __future__ import annotations

import logging
from dataclasses import dataclass

_log = logging.getLogger("canopy")

from canopy.history import append_history
from canopy.models import get_model_client
from canopy.query.executor import QueryResult, execute_query
from canopy.schema import build_system_prompt

MAX_ITERATIONS = 5

EXECUTE_SQL_TOOL: dict = {
    "name": "execute_sql",
    "description": (
        "Execute a read-only SQL SELECT query against the species monitoring "
        "database and return the results. Always call this tool to retrieve "
        "data — never guess or hallucinate results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "A valid PostgreSQL SELECT statement.",
            }
        },
        "required": ["sql"],
    },
}

_ROW_DISPLAY_LIMIT = 200


@dataclass(frozen=True)
class LoopResult:
    """Enriched result returned by run_query()."""

    question: str
    sql: str | None
    columns: list[str]
    rows: list[tuple]
    row_count: int
    model_text: str


def run_query(question: str) -> LoopResult:
    """Translate a natural language question into SQL, execute it, and return the result.

    Runs an agentic loop: the model is asked the question with access to the
    execute_sql tool. Each time it calls the tool the SQL is executed and the
    result is fed back. The loop ends when the model stops calling tools or
    MAX_ITERATIONS is reached.

    Args:
        question: A natural language question about the species monitoring data.

    Returns:
        LoopResult containing the question, the SQL that was run, the raw query
        result, and the model's final plain-language response.

    Raises:
        RuntimeError: If the model keeps calling tools beyond MAX_ITERATIONS.
        ValueError: If the model generates a non-SELECT SQL statement.
    """
    _log.info("run_query started: %r", question)
    model = get_model_client()
    system_prompt = build_system_prompt()
    messages: list[dict] = [{"role": "user", "content": question}]

    last_sql: str | None = None
    last_query_result: QueryResult | None = None
    response = None

    for iteration in range(MAX_ITERATIONS):
        response = model.generate(
            system_prompt=system_prompt,
            messages=messages,
            tools=[EXECUTE_SQL_TOOL],
        )
        messages.append(model.format_assistant_turn(response))

        if response.stop_reason == "end_turn":
            _log.debug("loop ended at iteration %d", iteration + 1)
            break

        # stop_reason == "tool_use": execute every tool call, bundle into one message
        if not response.tool_calls:
            raise ValueError("Model returned stop_reason='tool_use' with no tool calls")
        tool_results: list[tuple[str, str]] = []
        for tool_call in response.tool_calls:
            last_sql = tool_call.arguments["sql"]
            _log.debug("executing sql: %s", last_sql)
            last_query_result = execute_query(last_sql)
            tool_results.append((tool_call.id, _format_result(last_query_result)))
        messages.append(model.format_tool_results(tool_results))
    else:
        raise RuntimeError("Query loop exceeded maximum iterations")

    result = LoopResult(
        question=question,
        sql=last_sql,
        columns=last_query_result.columns if last_query_result else [],
        rows=last_query_result.rows if last_query_result else [],
        row_count=last_query_result.row_count if last_query_result else 0,
        model_text=response.text or "",
    )
    try:
        append_history(result)
    except Exception as exc:
        _log.debug("history write failed: %s", exc)
    _log.info("run_query complete: %d rows returned", result.row_count)
    return result


def _format_result(result: QueryResult) -> str:
    """Format a QueryResult as a readable string for the tool result message."""
    lines = [
        f"Columns: {', '.join(result.columns)}",
        f"Row count: {result.row_count}",
        "Rows:",
    ]
    for row in result.rows[:_ROW_DISPLAY_LIMIT]:
        lines.append(f"  {row}")
    if result.row_count > _ROW_DISPLAY_LIMIT:
        lines.append(f"  ... ({result.row_count - _ROW_DISPLAY_LIMIT} more rows truncated)")
    return "\n".join(lines)
