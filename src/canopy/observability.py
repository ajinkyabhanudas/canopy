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

Also hosts the show-SQL-by-default A/B experiment's variant assignment
(assign_variant, via PostHog feature flags) and exposure logging
(log_exposure, attached to the Langfuse trace) — see
config.is_show_sql_experiment_active and ~/Desktop/AB-Tests/ab-test-plan.md.
Gated by a separate switch from tracing itself: tracing being on does not
mean an experiment is running.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from canopy.config import get_posthog_host, is_langfuse_enabled, is_show_sql_experiment_active

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


# ---------------------------------------------------------------------------
# A/B experiment: show-SQL-by-default, via PostHog feature flags. See
# config.is_show_sql_experiment_active and ~/Desktop/AB-Tests/ab-test-plan.md
# for the full design. Kept in this module rather than loop.py: the variant
# is a UI/browser concept (which arm a given browser is in), not something
# the query loop itself needs to know — run_query()'s signature stays
# untouched by this experiment entirely.
#
# Uses PostHog rather than a hand-rolled assignment mechanism: an earlier
# version stored the assignment in a gr.BrowserState value, which hit a real
# persistence bug during Docker verification. PostHog's get_feature_flag()
# is deterministic — a hash of (flag key, distinct_id) — so the same
# distinct_id always gets the same variant with nothing to persist
# client-side at all. A stable distinct_id per browser (an opaque random ID,
# not the assignment itself) still needs to come from somewhere; app.py owns
# that via gr.BrowserState, same as session_history.
# ---------------------------------------------------------------------------

_AB_FLAG_KEY = "canopy-show-sql-by-default"

_posthog_client: Any = None
_posthog_client_init_failed = False


def _get_posthog_client() -> Any | None:
    """Return the shared PostHog client, or None if disabled/unavailable.

    Same lazy-construction, fail-safe-on-error pattern as _get_client() for
    Langfuse: importing this module must never touch the network, and a bad
    key or unreachable host must degrade to "no assignment" rather than
    crashing the query path.
    """
    global _posthog_client, _posthog_client_init_failed
    if not is_show_sql_experiment_active():
        return None
    if _posthog_client_init_failed:
        return None
    if _posthog_client is None:
        try:
            from posthog import Posthog

            api_key = os.environ.get("CANOPY_POSTHOG_API_KEY", "")
            _posthog_client = Posthog(project_api_key=api_key, host=get_posthog_host())
        except Exception as exc:  # noqa: BLE001 - the experiment must never break queries
            _log.warning("PostHog client init failed, assignment disabled: %s", exc)
            _posthog_client_init_failed = True
            return None
    return _posthog_client


def assign_variant(distinct_id: str) -> str | None:
    """Return this browser's variant for the show-SQL-by-default flag.

    distinct_id is a stable, opaque per-browser identifier (app.py generates
    and persists one via gr.BrowserState — an identity anchor, not the
    assignment itself). The SAME distinct_id always gets the SAME variant
    back from PostHog; nothing here needs to remember a prior result.

    Returns None if the experiment is inactive or PostHog is unreachable —
    callers must treat None as "no variant, render as control" rather than
    an error. Never raises: assignment failing must not fail a query.
    """
    client = _get_posthog_client()
    if client is None:
        return None
    try:
        return client.get_feature_flag(_AB_FLAG_KEY, distinct_id)
    except Exception as exc:  # noqa: BLE001 - the experiment must never break queries
        _log.warning("PostHog get_feature_flag failed: %s", exc)
        return None


def log_exposure(trace_id: str | None, *, variant: str) -> None:
    """Tag a Langfuse trace with which A/B variant the user that generated it was in.

    A no-op when tracing is disabled or trace_id is None (cache hits and any
    run where trace_query() itself returned nothing to attach to) — an
    exposure with no trace to attach it to is not measurable and would only
    create an orphaned record. Deliberately logged to Langfuse (which already
    holds this run's other metrics) rather than as a second PostHog event —
    one place to look for "did this trace convert," not two.
    """
    client = _get_client()
    if client is None or not trace_id:
        return
    try:
        client.score(
            trace_id=trace_id,
            name="ab_show_sql_variant",
            value=variant,
            data_type="CATEGORICAL",
        )
    except Exception as exc:  # noqa: BLE001 - tracing must never break queries
        _log.warning("Langfuse log_exposure failed: %s", exc)
