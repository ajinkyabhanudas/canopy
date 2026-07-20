"""Agentic query loop — NL question → LlamaIndex FunctionAgent → SQL tool → LoopResult."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from langdetect import DetectorFactory, LangDetectException
from langdetect import detect as _lang_detect
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.tools import FunctionTool

from canopy.cache import lookup_cache, write_cache
from canopy.config import get_active_connection
from canopy.history import append_history
from canopy.i18n import t
from canopy.models import get_llm
from canopy.query.executor import QueryResult, execute_query
from canopy.query.fuzzy_match import FuzzyMatch, find_candidates
from canopy.schema import build_system_prompt

DetectorFactory.seed = 0  # deterministic language detection across calls

_log = logging.getLogger("canopy")

MAX_ITERATIONS = 5
_ROW_DISPLAY_LIMIT = 200

# langdetect is unreliable on very short strings; skip detection below this length
_MIN_LANG_DETECT_LEN = 30


class UnsupportedLanguageError(ValueError):
    """Raised when a question is not in English or Spanish.

    schema.py's secondary language instruction asks the model to respond in
    English/Spanish regardless of input language, but that's model
    compliance, not a guarantee — DECISIONS.md § M1 documents it as
    unreliable for direct run_query() callers that bypass app.py's UI gate.
    This makes the check structural: it runs inside run_query() itself, so
    every caller is protected, not just the ones that remember to check
    first. app.py's own langdetect check still runs first for a friendlier
    UI message before the model is ever called.
    """


def is_unsupported_language(question: str) -> bool:
    """Return True if question is not in English or Spanish.

    Same detection logic app.py's UI gate uses — kept here as the single
    source of truth so both the UI-layer check and this module's own
    structural check can't drift out of sync with each other.
    """
    if len(question.strip()) < _MIN_LANG_DETECT_LEN:
        return False  # too short for reliable detection — pass through
    try:
        return _lang_detect(question) not in ("en", "es")
    except LangDetectException:
        return False  # undetermined — pass through

# Finds the outermost --- ... --- delimited block schema.py instructs the model
# to emit. Deliberately simple (no nested quantifiers) to avoid catastrophic
# backtracking on adversarial/malformed model output — the block's internal
# structure (DATA SOURCE / GAPS / RESEARCH QUESTIONS) is parsed procedurally
# line-by-line in _parse_interpretation(), not by this regex.
#
# The closing --- is optional: observed live-model output (gpt-5.1-codex-mini)
# sometimes omits it, ending the response right after the last bullet instead.
# Treating end-of-string as an implicit close means this still parses cleanly
# instead of leaving the raw block visible to the user (verified via Docker +
# Playwright — the unclosed case previously showed "DATA SOURCE:"/"GAPS:" as
# literal text with no formatting at all).
_BLOCK_RE = re.compile(r"^---$(.*?)(?:^---$|\Z)", re.MULTILINE | re.DOTALL)
_BULLET_RE = re.compile(r"^\s*[•\-]\s*(.+)$")


def _load_sensitive_columns() -> frozenset[str]:
    raw = os.environ.get("CANOPY_SENSITIVE_COLUMNS", "latitude,longitude,hashed_password")
    return frozenset(c.strip() for c in raw.split(",") if c.strip())


_SENSITIVE_COLUMNS = _load_sensitive_columns()


@dataclass(frozen=True)
class Interpretation:
    """Structured breakdown of the model's interpretation block.

    Parsed from model_text — see _parse_interpretation(). All fields are
    immutable to match the rest of the loop's data-shape guarantees (A5).
    """

    data_source: str
    gaps: tuple[str, ...]
    research_questions: tuple[str, ...]


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
    interpretation: Interpretation | None = None
    fuzzy_matches: tuple[FuzzyMatch, ...] = ()


def _parse_interpretation(model_text: str) -> Interpretation | None:
    """Extract the DATA SOURCE / GAPS / RESEARCH QUESTIONS block from model_text.

    The outer --- ... --- block is located with a single bounded regex, then
    its lines are walked procedurally (no regex over the bullet lists) — this
    avoids the nested-quantifier backtracking that a monolithic regex over
    unbounded, LLM-generated text would risk.

    Conservative by design: any malformed or partial match returns None rather
    than a partially-populated Interpretation. A missing block is expected and
    valid (schema.py instructs the model to omit it when execute_sql was never
    called), so a miss is logged at DEBUG; a present-but-malformed block is
    logged at WARNING since it signals prompt-format drift worth tracking.
    """
    block_match = _BLOCK_RE.search(model_text)
    if block_match is None:
        _log.debug("no interpretation block found in model_text")
        return None

    lines = block_match.group(1).strip("\n").split("\n")

    data_source: str | None = None
    gaps: list[str] = []
    research_questions: list[str] = []
    gaps_is_none = False
    section: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("DATA SOURCE:"):
            data_source = line[len("DATA SOURCE:") :].strip()
            section = None
            continue
        if line.startswith("GAPS:"):
            rest = line[len("GAPS:") :].strip()
            gaps_is_none = rest.casefold() == "none"
            section = "gaps"
            continue
        if line.startswith("RESEARCH QUESTIONS:"):
            section = "research_questions"
            continue

        bullet_match = _BULLET_RE.match(raw_line)
        if bullet_match and section == "gaps":
            gaps.append(bullet_match.group(1).strip())
        elif bullet_match and section == "research_questions":
            research_questions.append(bullet_match.group(1).strip())

    if not data_source:
        _log.warning("interpretation block malformed — empty DATA SOURCE: %r", model_text[:200])
        return None

    if not gaps_is_none and not gaps:
        _log.warning("interpretation block malformed — no GAPS content: %r", model_text[:200])
        return None

    return Interpretation(
        data_source=data_source,
        gaps=tuple(gaps),
        research_questions=tuple(research_questions),
    )


def strip_interpretation_block(model_text: str) -> str:
    """Return model_text with the raw --- ... --- interpretation block removed.

    Used by the UI to avoid displaying the block twice: once as raw text
    within model_text, once as the styled rendering built from the parsed
    Interpretation. If no block is present, returns model_text unchanged.
    """
    return _BLOCK_RE.sub("", model_text).strip()


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
        state["fuzzy_matches"] = find_candidates(sql) if result.row_count == 0 else ()
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
        UnsupportedLanguageError: If question is not in English or Spanish.
    """
    if is_unsupported_language(question):
        _log.info("run_query rejected unsupported-language question: %r", question[:60])
        raise UnsupportedLanguageError(
            "Canopy only supports questions in English or Spanish."
        )

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
        "fuzzy_matches": (),
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
        interpretation=_parse_interpretation(model_text),
        fuzzy_matches=tuple(state.get("fuzzy_matches") or ()),
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
