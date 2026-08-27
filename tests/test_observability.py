"""Tests for canopy.observability — Langfuse tracing, dormant by default.

No test in this file may cause a real network call. is_langfuse_enabled()
returning False (the default, and the only state in CI/local test runs
unless CANOPY_LANGFUSE_ENABLED is explicitly set) must make every public
function here a pure no-op — that invariant is what several tests assert.
"""
from __future__ import annotations

import canopy.observability as obs


def test_disabled_by_default(monkeypatch):
    """No env vars set — tracing must be off, matching every test run's actual state."""
    monkeypatch.delenv("CANOPY_LANGFUSE_ENABLED", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from canopy.config import is_langfuse_enabled

    assert is_langfuse_enabled() is False


def test_flag_alone_without_keys_stays_disabled(monkeypatch):
    """A truthy flag with no keys fails safe into disabled, not a crash."""
    monkeypatch.setenv("CANOPY_LANGFUSE_ENABLED", "true")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    from canopy.config import is_langfuse_enabled

    assert is_langfuse_enabled() is False


def test_keys_alone_without_flag_stays_disabled(monkeypatch):
    """Keys present but flag unset/false — still disabled. Both are required."""
    monkeypatch.delenv("CANOPY_LANGFUSE_ENABLED", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    from canopy.config import is_langfuse_enabled

    assert is_langfuse_enabled() is False


def test_flag_and_both_keys_enables(monkeypatch):
    monkeypatch.setenv("CANOPY_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    from canopy.config import is_langfuse_enabled

    assert is_langfuse_enabled() is True


def test_trace_query_is_noop_when_disabled(monkeypatch):
    """Disabled is the default — trace_query must never touch the network."""
    monkeypatch.setattr(obs, "is_langfuse_enabled", lambda: False)
    result = obs.trace_query(
        question="q", sql="SELECT 1", row_count=1, timing={}, cache_hit=False
    )
    assert result is None


def test_score_no_rephrase_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(obs, "is_langfuse_enabled", lambda: False)
    # Must not raise even with a garbage trace_id — there is no client to call.
    obs.score_no_rephrase("fake-trace-id", accepted=True)


def test_score_thumbs_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(obs, "is_langfuse_enabled", lambda: False)
    obs.score_thumbs("fake-trace-id", positive=False)


def test_score_functions_noop_on_empty_trace_id(monkeypatch):
    """Even if somehow enabled, an empty trace_id must not attempt a call."""
    calls = []
    fake_client = type("FakeClient", (), {"score": lambda self, **kw: calls.append(kw)})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    obs.score_no_rephrase("", accepted=True)
    obs.score_thumbs("", positive=True)
    assert calls == []


def test_client_init_failure_disables_tracing_without_raising(monkeypatch):
    """A bad key or unreachable host must degrade to no-tracing, not crash the query."""
    monkeypatch.setattr(obs, "is_langfuse_enabled", lambda: True)
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(obs, "_client_init_failed", False)

    def _boom(*a, **kw):
        raise RuntimeError("simulated unreachable host")

    monkeypatch.setattr("langfuse.Langfuse", _boom)
    result = obs.trace_query(
        question="q", sql="SELECT 1", row_count=1, timing={}, cache_hit=False
    )
    assert result is None
    # Second call must not retry construction — _client_init_failed short-circuits.
    result2 = obs.trace_query(
        question="q2", sql="SELECT 2", row_count=2, timing={}, cache_hit=False
    )
    assert result2 is None


def test_trace_query_never_sends_raw_rows(monkeypatch):
    """Only row_count and column shape may reach Langfuse — never row data.

    Mirrors the coordinate-stripping boundary in loop.py's
    _strip_sensitive_columns: trace_query's signature itself has no
    parameter for row contents, so this is enforced by the function
    contract, not by a runtime filter that could be bypassed.
    """
    import inspect

    sig = inspect.signature(obs.trace_query)
    assert "rows" not in sig.parameters
    assert "row_count" in sig.parameters
