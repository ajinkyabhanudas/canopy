"""Agentic query loop — NL question → LlamaIndex FunctionAgent → SQL tool → LoopResult."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.tools import FunctionTool

from canopy.cache import lookup_cache, write_cache
from canopy.config import get_active_connection
from canopy.history import append_history
from canopy.i18n import t
from canopy.models import get_llm
from canopy.query.executor import QueryResult, execute_query
from canopy.schema import build_system_prompt

_log = logging.getLogger("canopy")

MAX_ITERATIONS = 5
_ROW_DISPLAY_LIMIT = 200


def _load_sensitive_columns() -> frozenset[str]:
    raw = os.environ.get("CANOPY_SENSITIVE_COLUMNS", "latitude,longitude,hashed_password")
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


_SENSITIVE_COLUMNS = _load_sensitive_columns()


@dataclass(frozen=True)
class LoopResult:
    """Enriched result returned by run_query()."""

    question: str
    sql: str | None
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    row_count: int
    model_text: str
    timing: dict = field(default_factory=dict)


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


def _build_sql_tool(
    status_cb: Callable[[str], None] | None,
    state: dict,
) -> FunctionTool:
    """Build the execute_sql FunctionTool, capturing status_cb and state via closure."""

    def execute_sql(sql: str) -> str:
        """Execute a read-only SQL SELECT query against the species monitoring database.

        Always call this tool to retrieve data — never guess or hallucinate results.

        Args:
            sql: A valid PostgreSQL SELECT statement.

        Returns:
            Formatted query result with column names, row count, and row data.
        """
        if status_cb:
            status_cb(t("status_searching_db"))
        t_db = time.perf_counter()
        result = execute_query(sql)
        state["db_times"].append(time.perf_counter() - t_db)
        state["last_sql"] = sql
        state["last_query_result"] = result
        _log.debug("db execute: %.3fs — %s", state["db_times"][-1], sql[:120])
        if status_cb:
            n = result.row_count
            key = "found_detections_singular" if n == 1 else "found_detections_plural"
            status_cb(t(key, n=n))
        return _format_result(result)

    return FunctionTool.from_defaults(fn=execute_sql)


async def _run_agent(
    question: str,
    status_cb: Callable[[str], None] | None,
    state: dict,
    conn_id: str,
    active_model: str,
) -> str:
    """Run the LlamaIndex FunctionAgent and return the final model text."""
    llm = get_llm()
    system_prompt = build_system_prompt()
    sql_tool = _build_sql_tool(status_cb, state)

    agent = FunctionAgent(
        tools=[sql_tool],
        llm=llm,
        system_prompt=system_prompt,
        max_iterations=MAX_ITERATIONS,
        verbose=False,
    )

    if status_cb:
        status_cb(t("status_understanding"))

    t_llm = time.perf_counter()
    handler = agent.run(question)
    response = await handler
    state["llm_times"].append(time.perf_counter() - t_llm)

    text = str(response)
    _log.info(
        "loop_iterations=%d question=%r",
        state.get("iterations", 1),
        question[:60],
    )
    return text


def run_query(
    question: str,
    status_cb: Callable[[str], None] | None = None,
    connection_override: str | None = None,
) -> LoopResult:
    """Translate a natural language question into SQL, execute it, and return the result.

    Uses a LlamaIndex FunctionAgent with a single execute_sql tool. The agent
    handles the loop — generating SQL, executing it, and synthesising a response.
    The security layer (regex guard + readonly session + coordinate stripping) sits
    between the agent and PostgreSQL, unchanged.

    Args:
        question: A natural language question about the species monitoring data.
        connection_override: Optional connection ID to use instead of MODEL_BACKEND.
            Used by the benchmark runner to switch connections without env var mutation.

    Returns:
        LoopResult containing the question, the SQL that was run, the raw query
        result, and the model's final plain-language response.

    Raises:
        RuntimeError: If the model exceeds MAX_ITERATIONS.
        SQLGuardError: If the model generates a non-SELECT SQL statement.
    """
    conn = get_active_connection(connection_id=connection_override)
    active_model = conn.models[0] if conn.models else conn.id
    _log.info(
        "run_query started — backend=%s model=%s question=%r",
        conn.id, active_model, question,
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

    state: dict = {
        "last_sql": None,
        "last_query_result": None,
        "llm_times": [],
        "db_times": [],
        "iterations": 0,
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
        model_text = _pool.submit(
            asyncio.run, _run_agent(question, status_cb, state, conn.id, active_model)
        ).result()

    last_query_result: QueryResult | None = state["last_query_result"]
    total_s = time.perf_counter() - t_total

    timing = {
        "total_s": round(total_s, 2),
        "llm_s": round(sum(state["llm_times"]), 2),
        "llm_calls": len(state["llm_times"]),
        "db_s": round(sum(state["db_times"]), 3),
        "db_calls": len(state["db_times"]),
        "iterations": state.get("iterations", len(state["llm_times"])),
        "connection_id": conn.id,
        "model": active_model,
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
        sql=state["last_sql"],
        columns=last_query_result.columns if last_query_result else (),
        rows=last_query_result.rows if last_query_result else (),
        row_count=last_query_result.row_count if last_query_result else 0,
        model_text=model_text,
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
    return result
