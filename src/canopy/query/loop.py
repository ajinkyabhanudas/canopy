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
from canopy.observability import trace_query
from canopy.query.executor import QueryResult, execute_query
from canopy.query.fuzzy_match import (
    FuzzyMatch,
    effective_count,
    find_candidates,
    is_empty_result,
)
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
    compliance, not a guarantee — DECISIONS.md's M1 section documents it as
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


# Matches a short leading "I'm sorry, I can't help..." sentence — the
# synthetic refusal azure_responses_llm.py's _post() substitutes when Azure's
# content filter blocks one turn (see that module's content_filter handling).
# The FunctionAgent can retry after that turn and produce a real, correct
# answer in a LATER turn, but str(response) concatenates every turn's text —
# leaving the stray refusal fragment glued onto an otherwise-good response
# (confirmed live: eval case Q53, judged partial_hedge because the leftover
# fragment reads as a hedge, when the actual defect is a text-hygiene
# artifact from a filter retry, not a guardrail compliance issue).
#
# Bounded and anchored to the start of the string (not a general search) —
# this is a *fixed* short phrase, not an unbounded pattern over LLM text, so
# it does not carry the backtracking risk _parse_interpretation's docstring
# warns about for open-ended regexes.
_LEADING_REFUSAL_FRAGMENT_RE = re.compile(
    r"^(?:I['’]?m sorry,? but I can['’]?t help with that\.?(?: request\.?)?)\s*",
    re.IGNORECASE,
)
# Only strip when a meaningfully longer response follows — a refusal that IS
# the entire response is a real decline, not a stray fragment, and must be
# left untouched.
_MIN_TRAILING_CONTENT_LEN = 40


def _strip_leading_content_filter_fragment(model_text: str) -> str:
    """Remove a leading content-filter-refusal fragment left over from a
    mid-loop retry, when substantial real content follows it.

    Only fires on a short, fixed refusal phrase at the very start of the
    text — never touches a response that IS a genuine, complete decline.
    """
    match = _LEADING_REFUSAL_FRAGMENT_RE.match(model_text)
    if match is None:
        return model_text
    remainder = model_text[match.end():]
    if len(remainder.strip()) < _MIN_TRAILING_CONTENT_LEN:
        return model_text
    _log.info("stripped leading content-filter refusal fragment from model_text")
    return remainder


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


def _strip_sensitive_columns(result: QueryResult) -> QueryResult:
    """Return a QueryResult with CANOPY_SENSITIVE_COLUMNS columns removed.

    Was previously applied only inside _format_result() (the text sent to
    the LLM as tool output) — the primary defense against sensitive columns
    reaching a user is the system prompt instructing the model to never
    SELECT them in the first place (schema.py's guardrail), but that's a
    prompt-level control, not a code-level guarantee. LoopResult.rows/
    .columns (what the UI's "Full data table" tab actually renders) were
    built directly from the raw, unstripped QueryResult and never went
    through this filter — a defense-in-depth gap: if a query ever did
    select latitude/longitude despite the prompt instruction, the raw
    table would have shown it even though the LLM's own text wouldn't
    mention it. Fixed so both consumers share one stripped result.
    """
    safe_idx = [i for i, c in enumerate(result.columns) if c not in _SENSITIVE_COLUMNS]
    if len(safe_idx) == len(result.columns):
        return result
    safe_cols = tuple(result.columns[i] for i in safe_idx)
    safe_rows = tuple(tuple(row[i] for i in safe_idx) for row in result.rows)
    return QueryResult(columns=safe_cols, rows=safe_rows, row_count=result.row_count)


def _format_result(result: QueryResult) -> str:
    """Format a QueryResult for the model. Caller must already have applied
    _strip_sensitive_columns — this function only handles row-count capping
    and text layout, not column filtering (see _build_sql_tool's execute_sql
    closure, the only caller, for where stripping happens)."""
    rows = result.rows[:_ROW_DISPLAY_LIMIT]
    lines = [
        f"Columns: {', '.join(result.columns)}",
        f"Row count: {result.row_count}",
        "Rows:",
    ]
    for row in rows:
        lines.append(f"  {row}")
    if result.row_count > _ROW_DISPLAY_LIMIT:
        lines.append(f"  ... ({result.row_count - _ROW_DISPLAY_LIMIT} more rows truncated)")
    return "\n".join(lines)


def _build_sql_tool(
    status_cb: Callable[[str], None] | None,
    state: dict,
    result_cb: Callable[[QueryResult, str], None] | None = None,
) -> FunctionTool:
    """Build the execute_sql FunctionTool, capturing the callbacks and state via closure.

    result_cb is a separate channel from status_cb deliberately: status_cb carries
    display strings, result_cb carries structured data. Overloading one callback
    with both would force every existing consumer (tests, benchmark runner) to
    discriminate on payload type.
    """

    def execute_sql(sql: str) -> str:
        """Execute a read-only SQL SELECT query against the species monitoring database.

        Always call this tool to retrieve data — never guess or hallucinate results.

        Args:
            sql: A valid PostgreSQL SELECT statement.

        Returns:
            Formatted query result with column names, row count, and row data.
        """
        state["sql_attempts"] = state.get("sql_attempts", 0) + 1
        is_retry = state["sql_attempts"] > 1
        if status_cb:
            status_cb(t("status_searching_db"))
        t_db = time.perf_counter()
        result = execute_query(sql)
        state["db_times"].append(time.perf_counter() - t_db)
        state["last_sql"] = sql
        # is_empty_result/find_candidates/effective_count all need the RAW
        # column shape (e.g. distinguishing a COUNT(*) aggregate's single
        # column from a real 0-row result) — strip sensitive columns only
        # for what gets stored/displayed (last_query_result -> eventually
        # LoopResult.rows/.columns, what the UI's "Full data table" tab
        # renders), after those checks have already run on the raw result.
        state["last_query_result"] = _strip_sensitive_columns(result)
        _log.debug("db execute: %.3fs — %s", state["db_times"][-1], sql[:120])
        if result_cb:
            # Progressive disclosure: the rows are in hand here, but the model
            # still needs 16-25s to compose its narrative around them (measured
            # live — the narrative phase is 51-60% of total latency). Push the
            # stripped result to the UI now rather than holding it until the
            # final yield. Passes state["last_query_result"], never the raw
            # `result` — early disclosure must not widen what reaches a user.
            result_cb(state["last_query_result"], sql)
        empty = is_empty_result(sql, result)
        state["fuzzy_matches"] = find_candidates(sql) if empty else ()
        if status_cb:
            if empty:
                # A retry-worthy question (e.g. a mistyped name) can trigger
                # several execute_sql calls in one turn — each with its own
                # row count. Showing "Found 0 detections" then "Found 1" then
                # "Found 100" as the model retries with a corrected query
                # reads as nonsensical progress, not a search correction.
                # status_refining signals the model is still working the
                # question rather than reporting a (misleadingly low) count.
                status_cb(t("status_refining"))
            else:
                n = effective_count(sql, result)
                # Two non-empty results in the same turn (e.g. attempt 1
                # found 14, attempt 2 found 637 after the model corrected
                # its grouping) both said "Found N detections" with nothing
                # tying the second count to the retry that produced it —
                # read as two disconnected "found" events rather than one
                # search superseding the other. The _retry variant makes
                # the correction explicit.
                if is_retry:
                    key = (
                        "found_detections_singular_retry"
                        if n == 1
                        else "found_detections_plural_retry"
                    )
                else:
                    key = "found_detections_singular" if n == 1 else "found_detections_plural"
                status_cb(t(key, n=n))
        return _format_result(state["last_query_result"])

    return FunctionTool.from_defaults(fn=execute_sql)


async def _run_agent(
    question: str,
    status_cb: Callable[[str], None] | None,
    state: dict,
    conn_id: str,
    active_model: str,
    result_cb: Callable[[QueryResult, str], None] | None = None,
) -> str:
    """Run the LlamaIndex FunctionAgent and return the final model text.

    Consumes handler.stream_events() rather than a single `await handler` so
    status_cb can report real phase transitions (SQL generation decided,
    retry attempt, final-answer synthesis started) instead of one silent
    span covering the whole agent turn — confirmed live that a single turn
    can span 60-90s and multiple SQL retries with nothing in between
    otherwise. AzureResponsesLLM does not support token streaming (see its
    astream_chat docstring), so AgentStream.delta is always empty here —
    only event *boundaries* are available, not live token output.
    """
    llm = get_llm()
    system_prompt = build_system_prompt()
    sql_tool = _build_sql_tool(status_cb, state, result_cb)

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

    sql_attempts = 0
    async for ev in handler.stream_events():
        ev_name = type(ev).__name__
        if ev_name == "ToolCall":
            sql_attempts += 1
            # First attempt: status_understanding (already fired) covers this
            # lead-in, and status_searching_db fires a beat later from
            # execute_sql itself — a third message here would be redundant.
            # Retries are the real gap this closes: previously the UI sat on
            # a stale "Found 0 — writing your answer…" for the entire silent
            # LLM turn between one execute_sql call and the next.
            #
            # Confirmed live (2026-08-13): LlamaIndex writes ToolCall to the
            # stream before invoking the tool, so in true execution order
            # this message precedes status_searching_db — but this async
            # generator and execute_sql's synchronous status_cb call share
            # one event loop, so on a retry both can land in the same tick
            # with either arriving first. Not worth forcing a strict order
            # for a sub-second difference the user can't perceive either way.
            if status_cb and sql_attempts > 1:
                status_cb(t("status_writing_sql_retry", n=sql_attempts))
        elif (
            status_cb
            and ev_name == "AgentOutput"
            and not getattr(ev, "tool_calls", None)
        ):
            status_cb(t("status_composing_answer"))

    response = await handler
    state["llm_times"].append(time.perf_counter() - t_llm)
    state["iterations"] = sql_attempts

    text = _strip_leading_content_filter_fragment(str(response))
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
    result_cb: Callable[[QueryResult, str], None] | None = None,
    trace_id_cb: Callable[[str], None] | None = None,
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
        result_cb: Optional callback invoked as (stripped_result, sql) the moment
            execute_sql returns, well before the model finishes composing its
            answer. Lets a UI show real rows during the narrative wait. Fires once
            per SQL attempt — a retry calls it again with the corrected result,
            which supersedes the previous one. Never fires on a cache hit.
        trace_id_cb: Optional callback invoked once with the Langfuse trace_id
            for this call, letting a UI attach a later acceptance score (thumbs
            click, no-rephrase check) to the right trace. Deliberately not a
            field on LoopResult — LoopResult round-trips through the on-disk
            cache, and a trace_id replayed from a cache hit days later would
            point at the wrong run. Never fires when tracing is disabled
            (CANOPY_LANGFUSE_ENABLED unset — the default, and always the case
            in tests).

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
        trace_id = trace_query(
            question=question,
            sql=cached.sql,
            row_count=cached.row_count,
            timing=cached.timing,
            cache_hit=True,
        )
        if trace_id and trace_id_cb:
            trace_id_cb(trace_id)
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
            asyncio.run,
            _run_agent(question, status_cb, state, conn.id, active_model, result_cb),
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
    trace_id = trace_query(
        question=question,
        sql=result.sql,
        row_count=result.row_count,
        timing=timing,
        cache_hit=False,
    )
    if trace_id and trace_id_cb:
        trace_id_cb(trace_id)
    return result
