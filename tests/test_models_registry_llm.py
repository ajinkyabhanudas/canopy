"""Unit tests for registry.get_llm() — the LlamaIndex FunctionCallingLLM path.

Complements test_models_registry.py which covers get_model_client() only.
No network calls — model constructors don't hit the network on instantiation.
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest

from canopy.models.azure_responses_llm import AzureResponsesLLM
from canopy.models.llamaindex_compat import CanopyAzureCompatLLM
from canopy.models.registry import get_llm


def _write_yaml(tmp_path, content: str):
    p = tmp_path / "models.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def _patch_yaml(monkeypatch, tmp_path, content: str):
    import canopy.config as cfg
    yaml = _write_yaml(tmp_path, content)
    monkeypatch.setattr(cfg, "_connections_cache", {})
    return patch("canopy.config._models_yaml_path", return_value=yaml)


# ---------------------------------------------------------------------------
# openai-compat → CanopyAzureCompatLLM
# ---------------------------------------------------------------------------


def test_get_llm_openai_compat_returns_canopy_compat(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "az-compat")
    monkeypatch.setenv("AZURE_COMPAT_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: az-compat
            backend: azure
            api_key_env: AZURE_COMPAT_KEY
            models: [gpt-5.1-2]
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: openai-compat
    """):
        llm = get_llm()
    assert isinstance(llm, CanopyAzureCompatLLM)


def test_get_llm_openai_compat_sets_model(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "az-compat")
    monkeypatch.setenv("AZURE_COMPAT_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: az-compat
            backend: azure
            api_key_env: AZURE_COMPAT_KEY
            models: [gpt-5.1-2]
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: openai-compat
    """):
        llm = get_llm()
    assert llm.model == "gpt-5.1-2"


# ---------------------------------------------------------------------------
# openai-responses → AzureResponsesLLM
# ---------------------------------------------------------------------------


def test_get_llm_openai_responses_returns_responses_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "az-responses")
    monkeypatch.setenv("AZURE_RESPONSES_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: az-responses
            backend: azure
            api_key_env: AZURE_RESPONSES_KEY
            models: [gpt-5.1-codex-mini]
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: openai-responses
    """):
        llm = get_llm()
    assert isinstance(llm, AzureResponsesLLM)


def test_get_llm_openai_responses_sets_model(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "az-responses")
    monkeypatch.setenv("AZURE_RESPONSES_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: az-responses
            backend: azure
            api_key_env: AZURE_RESPONSES_KEY
            models: [gpt-5.1-codex-mini]
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: openai-responses
    """):
        llm = get_llm()
    assert llm.model == "gpt-5.1-codex-mini"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_get_llm_anthropic_raises_not_implemented(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "claude")
    monkeypatch.setenv("ANTHROPIC_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: claude
            backend: anthropic
            api_key_env: ANTHROPIC_KEY
            models: [claude-sonnet-4-6]
    """):
        with pytest.raises(NotImplementedError, match="Anthropic LlamaIndex"):
            get_llm()


def test_get_llm_no_model_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "az-empty")
    monkeypatch.setenv("AZURE_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: az-empty
            backend: azure
            api_key_env: AZURE_KEY
            models: []
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: openai-compat
    """):
        with pytest.raises(ValueError, match="no model specified"):
            get_llm()


def test_get_llm_unknown_api_style_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "az-unknown")
    monkeypatch.setenv("AZURE_KEY", "test-key")
    with _patch_yaml(monkeypatch, tmp_path, """
        connections:
          - id: az-unknown
            backend: azure
            api_key_env: AZURE_KEY
            models: [some-model]
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: unknown-style
    """):
        with pytest.raises(ValueError, match="Unknown api_style"):
            get_llm()
