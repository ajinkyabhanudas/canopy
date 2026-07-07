"""Agentic query loop — NL question → model → SQL tool call → LoopResult."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from canopy.cache import lookup_cache, write_cache
from canopy.config import get_active_connection
from canopy.history import append_history
from canopy.i18n import t
from canopy.models import get_model_client
from canopy.query.executor import QueryResult, execute_query
from canopy.schema import build_system_prompt

_log = logging.getLogger("canopy")

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
_SENSITIVE_COLUMNS = frozenset({"latitude", "longitude"})


@dataclass(frozen=True)
class LoopResult:
    """Enriched result returned by run_query()."""

    question: str
    sql: str | None
    columns: list[str]
    rows: list[tuple]
    row_count: int
    model_text: str
    timing: dict = field(default_factory=dict)


def run_query(
    question: str,
    status_cb: Callable[[str], None] | None = None,
) -> LoopResult:
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
    conn = get_active_connection()
    active_model = conn.models[0] if conn.models else conn.id
    _log.info(
        "run_query started — backend=%s model=%s question=%r", conn.id, active_model, question
    )

    cached = lookup_cache(question, connection_id=conn.id, model=active_model)
    if cached is not None:
        if status_cb:
            status_cb("CACHE_HIT")
        _log.info(
            "cache hit: backend=%s model=%s question=%r", conn.id, active_model, question[:60]
        )
        return cached

    t_total = time.perf_counter()
    model = get_model_client()
    system_prompt = build_system_prompt()
    messages: list[dict] = [{"role": "user", "content": question}]

    last_sql: str | None = None
    last_query_result: QueryResult | None = None
    response = None
    llm_times: list[float] = []
    db_times: list[float] = []
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    for iteration in range(MAX_ITERATIONS):
        if status_cb:
            status_cb(t("status_understanding") if iteration == 0 else t("status_refining"))
        t_llm = time.perf_counter()
        response = model.generate(
            system_prompt=system_prompt,
            messages=messages,
            tools=[EXECUTE_SQL_TOOL],
        )
        llm_times.append(time.perf_counter() - t_llm)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
        _log.debug("llm call %d: %.2fs", iteration + 1, llm_times[-1])
        if iteration == 0 and response.text and status_cb:
            status_cb(f"INTENT:{response.text.strip()}")
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
            if status_cb:
                status_cb(t("status_searching_db"))
            t_db = time.perf_counter()
            last_query_result = execute_query(last_sql)
            db_times.append(time.perf_counter() - t_db)
            _log.debug("db execute: %.3fs — %s", db_times[-1], last_sql[:120])
            if status_cb:
                n = last_query_result.row_count
                key = "found_detections_singular" if n == 1 else "found_detections_plural"
                status_cb(t(key, n=n))
            tool_results.append((tool_call.id, _format_result(last_query_result)))
        messages.extend(model.format_tool_results(tool_results))
    else:
        raise RuntimeError("Query loop exceeded maximum iterations")

    total_s = time.perf_counter() - t_total
    timing = {
        "total_s": round(total_s, 2),
        "llm_s": round(sum(llm_times), 2),
        "llm_calls": len(llm_times),
        "db_s": round(sum(db_times), 3),
        "db_calls": len(db_times),
        "connection_id": conn.id,
        "model": active_model,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
    }
    _log.info(
        "run_query complete — backend=%s model=%s rows=%d total=%.1fs"
        " (llm %.1fs × %d, db %.3fs × %d)",
        conn.id, active_model,
        last_query_result.row_count if last_query_result else 0,
        total_s, timing["llm_s"], timing["llm_calls"],
        timing["db_s"], timing["db_calls"],
    )

    result = LoopResult(
        question=question,
        sql=last_sql,
        columns=last_query_result.columns if last_query_result else [],
        rows=last_query_result.rows if last_query_result else [],
        row_count=last_query_result.row_count if last_query_result else 0,
        model_text=response.text or "",
        timing=timing,
    )
    try:
        write_cache(result, connection_id=conn.id, model=active_model)
    except Exception as exc:
        _log.warning("cache write failed: %s", exc)
    try:
        append_history(result)
    except Exception as exc:
        _log.warning("history write failed (check CANOPY_DATA_DIR): %s", exc)
    _log.info("run_query complete: %d rows returned", result.row_count)
    return result


def _format_result(result: QueryResult) -> str:
    """Format a QueryResult for the model, stripping sensitive columns."""
    safe_idx = [i for i, c in enumerate(result.columns) if c not in _SENSITIVE_COLUMNS]
    safe_cols = [result.columns[i] for i in safe_idx]
    safe_rows = [tuple(row[i] for i in safe_idx) for row in result.rows[:_ROW_DISPLAY_LIMIT]]
    lines = [
        f"Columns: {', '.join(safe_cols)}",
        f"Row count: {result.row_count}",
        "Rows:",
    ]
    for row in safe_rows:
        lines.append(f"  {row}")
    if result.row_count > _ROW_DISPLAY_LIMIT:
        lines.append(f"  ... ({result.row_count - _ROW_DISPLAY_LIMIT} more rows truncated)")
    return "\n".join(lines)
