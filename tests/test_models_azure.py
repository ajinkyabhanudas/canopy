"""Unit tests for AzureFoundryClient and the shared model interface contract."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from canopy.models.anthropic import AnthropicClient
from canopy.models.azure import AzureFoundryClient, _to_oai_tools
from canopy.models.base import ModelResponse, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(**kwargs) -> AzureFoundryClient:
    defaults = dict(
        model="gpt-4o",
        api_key="test-key",
        endpoint="https://example.openai.azure.com/openai/v1/",
        timeout=30.0,
    )
    return AzureFoundryClient(**{**defaults, **kwargs})


def _make_oai_response(content=None, tool_calls=None, finish_reason="stop", in_tok=10, out_tok=20):
    """Build a minimal mock that looks like openai.ChatCompletion."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok)
    return SimpleNamespace(choices=[choice], usage=usage)


# ---------------------------------------------------------------------------
# _to_oai_tools
# ---------------------------------------------------------------------------

def test_to_oai_tools_converts_anthropic_schema():
    anthropic_tool = {
        "name": "execute_sql",
        "description": "Run a SQL query",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    }
    result = _to_oai_tools([anthropic_tool])
    assert len(result) == 1
    assert result[0]["type"] == "function"
    fn = result[0]["function"]
    assert fn["name"] == "execute_sql"
    assert fn["description"] == "Run a SQL query"
    assert fn["parameters"]["properties"]["sql"]["type"] == "string"


def test_to_oai_tools_empty_list():
    assert _to_oai_tools([]) == []


def test_to_oai_tools_no_description_defaults_to_empty():
    tool = {"name": "noop", "input_schema": {}}
    result = _to_oai_tools([tool])
    assert result[0]["function"]["description"] == ""


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------

def test_generate_text_response():
    client = _make_client()
    mock_resp = _make_oai_response(content="There are 5 species.", finish_reason="stop")
    with patch.object(client._client.chat.completions, "create", return_value=mock_resp):
        result = client.generate("System prompt", [{"role": "user", "content": "How many?"}])
    assert isinstance(result, ModelResponse)
    assert result.text == "There are 5 species."
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []
    assert result.input_tokens == 10
    assert result.output_tokens == 20


def test_generate_tool_call_response():
    tc = SimpleNamespace(
        id="tc-1",
        function=SimpleNamespace(name="execute_sql", arguments='{"sql":"SELECT 1"}'),
    )
    mock_resp = _make_oai_response(content=None, tool_calls=[tc], finish_reason="tool_calls")
    client = _make_client()
    with patch.object(client._client.chat.completions, "create", return_value=mock_resp):
        result = client.generate("sys", [], tools=[{"name": "execute_sql", "input_schema": {}}])
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc_result = result.tool_calls[0]
    assert tc_result.id == "tc-1"
    assert tc_result.name == "execute_sql"
    assert tc_result.arguments == {"sql": "SELECT 1"}


def test_generate_includes_system_as_first_message():
    client = _make_client()
    mock_resp = _make_oai_response(content="ok")
    captured = {}
    def _capture(**kwargs):
        captured["messages"] = kwargs["messages"]
        return mock_resp
    with patch.object(client._client.chat.completions, "create", side_effect=_capture):
        client.generate("My system prompt", [{"role": "user", "content": "Q"}])
    assert captured["messages"][0] == {"role": "system", "content": "My system prompt"}


# ---------------------------------------------------------------------------
# format_tool_result() and format_tool_results()
# ---------------------------------------------------------------------------

def test_format_tool_result_returns_dict():
    client = _make_client()
    result = client.format_tool_result("tc-1", "5 rows")
    assert result == {"role": "tool", "tool_call_id": "tc-1", "content": "5 rows"}


def test_format_tool_results_returns_list():
    client = _make_client()
    results = client.format_tool_results([("tc-1", "rows1"), ("tc-2", "rows2")])
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0]["role"] == "tool"
    assert results[0]["tool_call_id"] == "tc-1"
    assert results[1]["tool_call_id"] == "tc-2"


def test_format_tool_results_one_message_per_result():
    """OpenAI needs separate messages — not bundled into one."""
    client = _make_client()
    results = client.format_tool_results([("a", "x"), ("b", "y"), ("c", "z")])
    assert len(results) == 3
    for r in results:
        assert r["role"] == "tool"


# ---------------------------------------------------------------------------
# format_assistant_turn()
# ---------------------------------------------------------------------------

def test_format_assistant_turn_text_only():
    client = _make_client()
    resp = ModelResponse(text="Hello", tool_calls=[], stop_reason="end_turn")
    msg = client.format_assistant_turn(resp)
    assert msg["role"] == "assistant"
    assert msg["content"] == "Hello"
    assert "tool_calls" not in msg


def test_format_assistant_turn_with_tool_calls():
    client = _make_client()
    tc = ToolCall(id="tc-1", name="execute_sql", arguments={"sql": "SELECT 1"})
    resp = ModelResponse(text=None, tool_calls=[tc], stop_reason="tool_use")
    msg = client.format_assistant_turn(resp)
    assert "tool_calls" in msg
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["id"] == "tc-1"
    assert msg["tool_calls"][0]["function"]["name"] == "execute_sql"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"sql": "SELECT 1"}


# ---------------------------------------------------------------------------
# Anthropic regression: format_tool_results() must return list[dict] after refactor
# ---------------------------------------------------------------------------

def _make_anthropic_client() -> AnthropicClient:
    """Build an AnthropicClient with config fully mocked out."""
    from canopy.config import ModelConfig
    with patch("canopy.models.anthropic.anthropic.Anthropic"), \
         patch("canopy.models.anthropic.get_model_config") as mock_cfg:
        mock_cfg.return_value = ModelConfig(
            backend="anthropic", api_key="fake", model="claude-sonnet-4-6", timeout=60.0
        )
        return AnthropicClient()


def test_anthropic_format_tool_results_returns_list():
    """Regression: return type changed from dict to list[dict] — must stay a list."""
    client = _make_anthropic_client()
    results = client.format_tool_results([("tid-1", "content-1"), ("tid-2", "content-2")])
    assert isinstance(results, list), "format_tool_results() must return list[dict], not dict"
    assert len(results) == 1  # Anthropic bundles into one user message
    assert results[0]["role"] == "user"
    content = results[0]["content"]
    assert len(content) == 2
    assert content[0]["tool_use_id"] == "tid-1"
    assert content[1]["tool_use_id"] == "tid-2"


def test_anthropic_format_tool_result_single():
    """format_tool_result() must return a single dict (not a list)."""
    client = _make_anthropic_client()
    result = client.format_tool_result("tid-1", "some content")
    assert isinstance(result, dict)
    assert result["role"] == "user"


# ---------------------------------------------------------------------------
# Registry: azure backend is registered
# ---------------------------------------------------------------------------

def test_registry_has_azure_backend():
    from canopy.models.registry import _BACKENDS
    assert "azure" in _BACKENDS
