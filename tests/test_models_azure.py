"""Unit tests for AzureFoundryClient and the shared model interface contract."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from canopy.models.anthropic import AnthropicClient
from canopy.models.azure import AzureFoundryClient, _to_sdk_messages, _to_sdk_tool
from canopy.models.base import ModelResponse, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(**kwargs) -> AzureFoundryClient:
    defaults = dict(
        model="gpt-4o",
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/models",
        timeout=30.0,
    )
    with patch("canopy.models.azure.ChatCompletionsClient"):
        return AzureFoundryClient(**{**defaults, **kwargs})


def _make_azure_response(
    content=None, tool_calls=None, finish_reason="stop", in_tok=10, out_tok=20
):
    """Build a minimal mock that looks like an azure-ai-inference ChatCompletions response."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok)
    return SimpleNamespace(choices=[choice], usage=usage)


# ---------------------------------------------------------------------------
# _to_sdk_tool
# ---------------------------------------------------------------------------


def test_to_sdk_tool_converts_anthropic_schema():
    anthropic_tool = {
        "name": "execute_sql",
        "description": "Run a SQL query",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    }
    result = _to_sdk_tool(anthropic_tool)
    assert result.function.name == "execute_sql"
    assert result.function.description == "Run a SQL query"
    assert result.function.parameters["properties"]["sql"]["type"] == "string"


def test_to_sdk_tool_no_description_defaults_to_empty():
    tool = {"name": "noop", "input_schema": {}}
    result = _to_sdk_tool(tool)
    assert result.function.description == ""


# ---------------------------------------------------------------------------
# _to_sdk_messages
# ---------------------------------------------------------------------------


def test_to_sdk_messages_user_message():
    from azure.ai.inference.models import UserMessage

    msgs = _to_sdk_messages([{"role": "user", "content": "hello"}])
    assert len(msgs) == 1
    assert isinstance(msgs[0], UserMessage)


def test_to_sdk_messages_tool_message():
    from azure.ai.inference.models import ToolMessage

    msgs = _to_sdk_messages([{"role": "tool", "tool_call_id": "tc-1", "content": "result"}])
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    assert msgs[0].tool_call_id == "tc-1"


def test_to_sdk_messages_assistant_without_tool_calls():
    from azure.ai.inference.models import AssistantMessage

    msgs = _to_sdk_messages([{"role": "assistant", "content": "hi"}])
    assert isinstance(msgs[0], AssistantMessage)


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------


def test_generate_text_response():
    client = _make_client()
    mock_resp = _make_azure_response(content="There are 5 species.", finish_reason="stop")
    client._client.complete = MagicMock(return_value=mock_resp)
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
    mock_resp = _make_azure_response(content=None, tool_calls=[tc], finish_reason="tool_calls")
    client = _make_client()
    client._client.complete = MagicMock(return_value=mock_resp)
    result = client.generate("sys", [], tools=[{"name": "execute_sql", "input_schema": {}}])
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc_result = result.tool_calls[0]
    assert tc_result.id == "tc-1"
    assert tc_result.name == "execute_sql"
    assert tc_result.arguments == {"sql": "SELECT 1"}


def test_generate_passes_system_as_first_message():
    from azure.ai.inference.models import SystemMessage

    client = _make_client()
    mock_resp = _make_azure_response(content="ok")
    captured = {}

    def _capture(**kwargs):
        captured["messages"] = kwargs["messages"]
        return mock_resp

    client._client.complete = MagicMock(side_effect=_capture)
    client.generate("My system prompt", [{"role": "user", "content": "Q"}])
    assert isinstance(captured["messages"][0], SystemMessage)
    assert captured["messages"][0].content == "My system prompt"


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
# Anthropic regression: format_tool_results() must return list[dict]
# ---------------------------------------------------------------------------


def _make_anthropic_client() -> AnthropicClient:
    from canopy.config import ModelConfig

    with patch("canopy.models.anthropic.anthropic.Anthropic"), patch(
        "canopy.models.anthropic.get_model_config"
    ) as mock_cfg:
        mock_cfg.return_value = ModelConfig(
            backend="anthropic", api_key="fake", model="claude-sonnet-4-6", timeout=60.0
        )
        return AnthropicClient()


def test_anthropic_format_tool_results_returns_list():
    client = _make_anthropic_client()
    results = client.format_tool_results([("tid-1", "content-1"), ("tid-2", "content-2")])
    assert isinstance(results, list), "format_tool_results() must return list[dict], not dict"
    assert len(results) == 1
    assert results[0]["role"] == "user"
    content = results[0]["content"]
    assert len(content) == 2
    assert content[0]["tool_use_id"] == "tid-1"
    assert content[1]["tool_use_id"] == "tid-2"


def test_anthropic_format_tool_result_single():
    client = _make_anthropic_client()
    result = client.format_tool_result("tid-1", "some content")
    assert isinstance(result, dict)
    assert result["role"] == "user"


def test_anthropic_generate_text_response():
    client = _make_anthropic_client()
    text_block = SimpleNamespace(type="text", text="Here is the answer.")
    usage = SimpleNamespace(input_tokens=10, output_tokens=20)
    mock_response = SimpleNamespace(content=[text_block], usage=usage)
    client._client.messages.create.return_value = mock_response

    resp = client.generate(
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "Hello"}],
    )

    assert resp.text == "Here is the answer."
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 20


def test_anthropic_generate_tool_call_response():
    client = _make_anthropic_client()
    tool_block = SimpleNamespace(
        type="tool_use",
        id="toolu_01",
        name="execute_sql",
        input={"sql": "SELECT 1"},
    )
    usage = SimpleNamespace(input_tokens=50, output_tokens=30)
    mock_response = SimpleNamespace(content=[tool_block], usage=usage)
    client._client.messages.create.return_value = mock_response

    resp = client.generate(
        system_prompt="You are helpful.",
        messages=[{"role": "user", "content": "How many species?"}],
        tools=[{"name": "execute_sql", "description": "Run SQL", "input_schema": {}}],
    )

    assert resp.text is None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "toolu_01"
    assert resp.tool_calls[0].name == "execute_sql"
    assert resp.tool_calls[0].arguments == {"sql": "SELECT 1"}
    assert resp.stop_reason == "tool_use"


def test_anthropic_format_assistant_turn_text_only():
    client = _make_anthropic_client()
    response = ModelResponse(text="Plain answer.", tool_calls=[])
    turn = client.format_assistant_turn(response)
    assert turn["role"] == "assistant"
    assert turn["content"] == [{"type": "text", "text": "Plain answer."}]


def test_anthropic_format_assistant_turn_with_tool_call():
    client = _make_anthropic_client()
    tc = ToolCall(id="toolu_01", name="execute_sql", arguments={"sql": "SELECT 1"})
    response = ModelResponse(text=None, tool_calls=[tc])
    turn = client.format_assistant_turn(response)
    assert turn["role"] == "assistant"
    assert len(turn["content"]) == 1
    block = turn["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "toolu_01"
    assert block["name"] == "execute_sql"
    assert block["input"] == {"sql": "SELECT 1"}


# ---------------------------------------------------------------------------
# Registry: azure backend is registered
# ---------------------------------------------------------------------------


def test_registry_has_azure_backend():
    from canopy.models.registry import _BACKENDS

    assert "azure" in _BACKENDS
