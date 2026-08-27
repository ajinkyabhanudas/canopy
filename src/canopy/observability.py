"""Langfuse tracing — dormant until Canopy has real production traffic.

Every function here is a no-op when `config.is_langfuse_enabled()` is False
(the default). This module exists so the online-eval work in
AI-Skills-Build's file 06 has instrumentation to switch on the day real
biologists start using Canopy, rather than needing a reactive build once
that traffic shows up — see DECISIONS.md's Operations section.

Deliberately thin: one trace per run_query() call, built from the timing
dict and state that _run_agent/execute_sql already populate (see loop.py).
No per-span instrumentation was added inside the agent loop itself — U2's
lesson about building machinery ahead of a measured need applies here too,
and the existing status_cb/result_cb callbacks already mark every phase
transition that matters.

Two acceptance-proxy scores attach to a trace independently, at different
times, and are never blended into one number here — see the challenge-loop
decision in the online-eval PR:
  - no_rephrase_within_5min: computed by a delayed background check
  - thumbs_explicit: attached whenever/if a user clicks a thumbs control
"""
from __future__ import annotations

import logging
from typing import Any

from canopy.config import is_langfuse_enabled

_log = logging.getLogger("canopy.observability")

_client: Any = None
_client_init_failed = False


def _get_client() -> Any | None:
    """Return the shared Langfuse client, or None if disabled/unavailable.

    Lazily constructed so importing this module never touches the network,
    and so a bad key or unreachable host degrades to "no tracing" rather
    than crashing anything that imports observability.py.
    """
    global _client, _client_init_failed
    if not is_langfuse_enabled():
        return None
    if _client_init_failed:
        return None
    if _client is None:
        try:
            from langfuse import Langfuse

            _client = Langfuse()
        except Exception as exc:  # noqa: BLE001 - tracing must never break queries
            _log.warning("Langfuse client init failed, tracing disabled: %s", exc)
            _client_init_failed = True
            return None
    return _client


def trace_query(
    *,
    question: str,
    sql: str | None,
    row_count: int,
    timing: dict,
    cache_hit: bool,
) -> str | None:
    """Record one trace for a completed run_query() call.

    Returns the trace_id so the UI can attach a delayed or explicit
    acceptance score to it later, or None if tracing is disabled/unavailable
    — callers must treat a None trace_id as "nothing to score", not an error.

    Sends the same shape of information already visible in the UI (question,
    generated SQL, row_count, timing) — never raw row data. This mirrors the
    coordinate-stripping boundary in loop.py's _strip_sensitive_columns:
    Langfuse gets what the model and the user already see, nothing wider.
    """
    client = _get_client()
    if client is None:
        return None
    try:
        trace = client.trace(
            name="canopy.run_query",
            input={"question": question},
            output={"sql": sql, "row_count": row_count},
            metadata={
                "cache_hit": cache_hit,
                "connection_id": timing.get("connection_id"),
                "model": timing.get("model"),
                "sql_attempts": timing.get("iterations"),
            },
        )
        if not cache_hit:
            trace.span(
                name="llm",
                start_time=None,
                end_time=None,
                metadata={"llm_calls": timing.get("llm_calls")},
            ).end(metadata={"duration_s": timing.get("llm_s")})
            trace.span(
                name="db",
                metadata={"db_calls": timing.get("db_calls")},
            ).end(metadata={"duration_s": timing.get("db_s")})
        return trace.id
    except Exception as exc:  # noqa: BLE001 - tracing must never break queries
        _log.warning("Langfuse trace_query failed: %s", exc)
        return None


def score_no_rephrase(trace_id: str, *, accepted: bool) -> None:
    """Attach the delayed no-rephrase acceptance proxy to an existing trace.

    accepted=True means the user did not re-ask a reworded version of the
    same question within the observation window — see the caller for the
    window length and the correlation logic, which lives in app.py next to
    session_history (the signal it depends on already exists there).
    """
    client = _get_client()
    if client is None or not trace_id:
        return
    try:
        client.score(
            trace_id=trace_id,
            name="no_rephrase_within_5min",
            value=1.0 if accepted else 0.0,
            data_type="BOOLEAN",
        )
    except Exception as exc:  # noqa: BLE001 - tracing must never break queries
        _log.warning("Langfuse score_no_rephrase failed: %s", exc)


def score_thumbs(trace_id: str, *, positive: bool) -> None:
    """Attach the explicit thumbs acceptance proxy to an existing trace."""
    client = _get_client()
    if client is None or not trace_id:
        return
    try:
        client.score(
            trace_id=trace_id,
            name="thumbs_explicit",
            value=1.0 if positive else 0.0,
            data_type="BOOLEAN",
        )
    except Exception as exc:  # noqa: BLE001 - tracing must never break queries
        _log.warning("Langfuse score_thumbs failed: %s", exc)


def flush() -> None:
    """Force any buffered traces to send now. Call on app shutdown only —
    the SDK's own background batching handles the normal case."""
    client = _get_client()
    if client is not None:
        try:
            client.flush()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Langfuse flush failed: %s", exc)
