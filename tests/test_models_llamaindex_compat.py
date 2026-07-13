"""Unit tests for CanopyAzureCompatLLM and build_openai_compat_llm.

No network calls — LlamaIndex instantiation is synchronous and only hits the
network on actual inference calls (complete/astream_chat), which are not
exercised here.
"""

from __future__ import annotations

from canopy.models.llamaindex_compat import (
    CanopyAzureCompatLLM,
    _DEFAULT_CONTEXT_WINDOW,
    build_openai_compat_llm,
)


# ---------------------------------------------------------------------------
# build_openai_compat_llm factory
# ---------------------------------------------------------------------------


def _build(**overrides) -> CanopyAzureCompatLLM:
    defaults = dict(
        model="gpt-5.1-2",
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/api/projects/p1/openai/v1/",
        timeout=30.0,
    )
    return build_openai_compat_llm(**{**defaults, **overrides})


def test_build_returns_canopy_compat_llm():
    assert isinstance(_build(), CanopyAzureCompatLLM)


def test_build_sets_model():
    assert _build(model="gpt-5.1-2").model == "gpt-5.1-2"


def test_build_sets_max_tokens():
    llm = _build()
    assert llm.max_tokens == 4096


def test_build_sets_temperature_zero():
    llm = _build()
    assert llm.temperature == 0.0


def test_build_uses_endpoint_as_api_base():
    endpoint = "https://example.services.ai.azure.com/api/projects/p1/openai/v1/"
    llm = _build(endpoint=endpoint)
    assert llm.api_base == endpoint


# ---------------------------------------------------------------------------
# CanopyAzureCompatLLM.metadata
# ---------------------------------------------------------------------------


def test_metadata_context_window():
    llm = _build()
    assert llm.metadata.context_window == _DEFAULT_CONTEXT_WINDOW


def test_metadata_is_chat_model():
    assert _build().metadata.is_chat_model is True


def test_metadata_is_function_calling_model():
    assert _build().metadata.is_function_calling_model is True


def test_metadata_model_name_matches_deployment():
    llm = _build(model="gpt-5.1-2")
    assert llm.metadata.model_name == "gpt-5.1-2"


def test_metadata_num_output_matches_max_tokens():
    llm = _build()
    assert llm.metadata.num_output == llm.max_tokens


# ---------------------------------------------------------------------------
# CanopyAzureCompatLLM._get_model_kwargs — max_tokens → max_completion_tokens
# ---------------------------------------------------------------------------


def test_get_model_kwargs_renames_max_tokens():
    """Azure project-scoped path rejects max_tokens; must use max_completion_tokens."""
    llm = _build()
    kwargs = llm._get_model_kwargs()
    assert "max_completion_tokens" in kwargs
    assert "max_tokens" not in kwargs


def test_get_model_kwargs_max_completion_tokens_value():
    llm = _build()
    kwargs = llm._get_model_kwargs()
    assert kwargs["max_completion_tokens"] == 4096


def test_get_model_kwargs_preserves_other_fields():
    llm = _build()
    kwargs = llm._get_model_kwargs()
    assert "model" in kwargs
    assert kwargs["temperature"] == 0.0
