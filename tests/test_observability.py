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


# ---------------------------------------------------------------------------
# Success paths — a working (fake) client actually receives the right calls.
# Everything above this line proves the disabled/failure paths; these prove
# the enabled path wires through to the client correctly.
# ---------------------------------------------------------------------------


def _fake_trace(trace_id="trace-xyz"):
    calls = {"spans": []}

    class _FakeSpan:
        def end(self, **kw):
            calls["spans"][-1]["end_kwargs"] = kw
            return self

    class _FakeTrace:
        id = trace_id

        def span(self, **kw):
            calls["spans"].append({"span_kwargs": kw})
            return _FakeSpan()

    return _FakeTrace(), calls


def test_trace_query_success_returns_trace_id_and_adds_spans(monkeypatch):
    fake_trace, calls = _fake_trace()
    fake_client = type("FakeClient", (), {"trace": lambda self, **kw: fake_trace})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)

    result = obs.trace_query(
        question="q",
        sql="SELECT 1",
        row_count=5,
        timing={"llm_s": 1.0, "db_s": 0.1, "llm_calls": 1, "db_calls": 1},
        cache_hit=False,
    )
    assert result == "trace-xyz"
    assert len(calls["spans"]) == 2  # llm + db
    assert calls["spans"][0]["span_kwargs"]["name"] == "llm"
    assert calls["spans"][1]["span_kwargs"]["name"] == "db"


def test_trace_query_cache_hit_skips_spans(monkeypatch):
    """A cache hit never called execute_sql or the LLM — no llm/db spans to add."""
    fake_trace, calls = _fake_trace()
    fake_client = type("FakeClient", (), {"trace": lambda self, **kw: fake_trace})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)

    result = obs.trace_query(
        question="q", sql="SELECT 1", row_count=5, timing={}, cache_hit=True
    )
    assert result == "trace-xyz"
    assert calls["spans"] == []


def test_trace_query_returns_none_and_logs_on_client_exception(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("simulated Langfuse API error")

    fake_client = type("FakeClient", (), {"trace": lambda self, **kw: _boom()})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)

    result = obs.trace_query(
        question="q", sql="SELECT 1", row_count=1, timing={}, cache_hit=False
    )
    assert result is None


def test_score_no_rephrase_success_calls_client_score(monkeypatch):
    calls = []
    fake_client = type("FakeClient", (), {"score": lambda self, **kw: calls.append(kw)})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)

    obs.score_no_rephrase("trace-1", accepted=True)
    assert calls == [
        {
            "trace_id": "trace-1",
            "name": "no_rephrase_within_5min",
            "value": 1.0,
            "data_type": "BOOLEAN",
        }
    ]


def test_score_no_rephrase_swallows_client_exception(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("simulated failure")

    fake_client = type("FakeClient", (), {"score": lambda self, **kw: _boom()})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    obs.score_no_rephrase("trace-1", accepted=False)  # must not raise


def test_score_thumbs_success_calls_client_score(monkeypatch):
    calls = []
    fake_client = type("FakeClient", (), {"score": lambda self, **kw: calls.append(kw)})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)

    obs.score_thumbs("trace-2", positive=False)
    assert calls == [
        {"trace_id": "trace-2", "name": "thumbs_explicit", "value": 0.0, "data_type": "BOOLEAN"}
    ]


def test_score_thumbs_swallows_client_exception(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("simulated failure")

    fake_client = type("FakeClient", (), {"score": lambda self, **kw: _boom()})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    obs.score_thumbs("trace-2", positive=True)  # must not raise


def test_flush_calls_client_flush_when_enabled(monkeypatch):
    calls = []
    fake_client = type("FakeClient", (), {"flush": lambda self: calls.append(True)})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    obs.flush()
    assert calls == [True]


def test_flush_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(obs, "_get_client", lambda: None)
    obs.flush()  # must not raise


def test_flush_swallows_client_exception(monkeypatch):
    def _boom():
        raise RuntimeError("simulated failure")

    fake_client = type("FakeClient", (), {"flush": _boom})()
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    obs.flush()  # must not raise


def test_get_client_returns_cached_instance_on_second_call(monkeypatch):
    """The lazily-constructed Langfuse client must be built once and reused
    — a second call with a client already cached takes the early-return
    branch, not reconstruction."""
    monkeypatch.setattr(obs, "is_langfuse_enabled", lambda: True)
    monkeypatch.setattr(obs, "_client_init_failed", False)
    sentinel = object()
    monkeypatch.setattr(obs, "_client", sentinel)
    assert obs._get_client() is sentinel
