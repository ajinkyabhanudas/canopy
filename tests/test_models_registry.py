"""
Confirms the model-swap story is real: changing MODEL_BACKEND to an
unregistered name fails clearly, and the registry only knows about
backends that are actually implemented.
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest

from canopy.models.registry import _BACKENDS, get_model_client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path, content: str):
    p = tmp_path / "models.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Existing tests
# ---------------------------------------------------------------------------


def test_registry_lists_anthropic():
    assert "anthropic" in _BACKENDS


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "not_a_real_backend")
    with pytest.raises(ValueError):
        get_model_client()


# ---------------------------------------------------------------------------
# Registry branch coverage
# ---------------------------------------------------------------------------


def test_registry_returns_anthropic_client(tmp_path, monkeypatch):
    yaml = _write_yaml(tmp_path, """
        connections:
          - id: claude
            backend: anthropic
            api_key_env: ANTHROPIC_API_KEY
            models: [claude-sonnet-4-6]
    """)
    import canopy.config as cfg
    monkeypatch.setattr(cfg, "_connections_cache", {})
    monkeypatch.setenv("MODEL_BACKEND", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch("canopy.config._models_yaml_path", return_value=yaml), \
         patch("canopy.models.anthropic.anthropic.Anthropic"):
        client = get_model_client()

    from canopy.models.anthropic import AnthropicClient
    assert isinstance(client, AnthropicClient)


def test_registry_returns_azure_foundry_client(tmp_path, monkeypatch):
    yaml = _write_yaml(tmp_path, """
        connections:
          - id: az-foundry
            backend: azure
            api_key_env: AZURE_API_KEY
            models: [gpt-4o]
            endpoint: https://example.services.ai.azure.com/models
            api_style: azure-inference
    """)
    import canopy.config as cfg
    monkeypatch.setattr(cfg, "_connections_cache", {})
    monkeypatch.setenv("MODEL_BACKEND", "az-foundry")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")

    with patch("canopy.config._models_yaml_path", return_value=yaml), \
         patch("canopy.models.azure.ChatCompletionsClient"):
        client = get_model_client()

    from canopy.models.azure import AzureFoundryClient
    assert isinstance(client, AzureFoundryClient)


def test_registry_returns_azure_compat_client(tmp_path, monkeypatch):
    yaml = _write_yaml(tmp_path, """
        connections:
          - id: az-compat
            backend: azure
            api_key_env: AZURE_API_KEY
            models: [phi-4]
            endpoint: https://example.services.ai.azure.com/openai/v1
            api_style: openai-compat
    """)
    import canopy.config as cfg
    monkeypatch.setattr(cfg, "_connections_cache", {})
    monkeypatch.setenv("MODEL_BACKEND", "az-compat")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")

    with patch("canopy.config._models_yaml_path", return_value=yaml), \
         patch("canopy.models.azure_compat.OpenAI"):
        client = get_model_client()

    from canopy.models.azure_compat import AzureOpenAICompatClient
    assert isinstance(client, AzureOpenAICompatClient)


def test_registry_returns_azure_responses_client(tmp_path, monkeypatch):
    yaml = _write_yaml(tmp_path, """
        connections:
          - id: az-responses
            backend: azure
            api_key_env: AZURE_API_KEY
            models: [gpt-5.1-codex-mini]
            endpoint: https://example.services.ai.azure.com/openai/v1/
            api_style: openai-responses
    """)
    import canopy.config as cfg
    monkeypatch.setattr(cfg, "_connections_cache", {})
    monkeypatch.setenv("MODEL_BACKEND", "az-responses")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")

    with patch("canopy.config._models_yaml_path", return_value=yaml):
        client = get_model_client()

    from canopy.models.azure_responses import AzureResponsesClient
    assert isinstance(client, AzureResponsesClient)


def test_registry_azure_no_model_raises(tmp_path, monkeypatch):
    yaml = _write_yaml(tmp_path, """
        connections:
          - id: az-empty
            backend: azure
            api_key_env: AZURE_API_KEY
            models: []
            endpoint: https://example.services.ai.azure.com/models
    """)
    import canopy.config as cfg
    monkeypatch.setattr(cfg, "_connections_cache", {})
    monkeypatch.setenv("MODEL_BACKEND", "az-empty")
    monkeypatch.setenv("AZURE_API_KEY", "test-key")

    with patch("canopy.config._models_yaml_path", return_value=yaml):
        with pytest.raises(ValueError, match="no model specified"):
            get_model_client()
