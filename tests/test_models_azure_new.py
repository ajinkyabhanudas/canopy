"""Unit tests for AzureOpenAICompatClient and AzureResponsesClient."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from canopy.models.azure_compat import AzureOpenAICompatClient, _to_oai_tools
from canopy.models.azure_responses import (
    AzureResponsesClient,
    _extract_text,
    _extract_tool_calls,
    _to_responses_tools,
)
from canopy.models.base import ModelResponse, ToolCall

# ---------------------------------------------------------------------------
# AzureOpenAICompatClient helpers
# ---------------------------------------------------------------------------


def _make_compat_client(**kwargs) -> AzureOpenAICompatClient:
    defaults = dict(
        model="Phi-4",
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/openai/v1/",
        timeout=30.0,
    )
    with patch("canopy.models.azure_compat.OpenAI"):
        return AzureOpenAICompatClient(**{**defaults, **kwargs})


def _make_oai_response(content=None, tool_calls=None, finish_reason="stop", in_tok=5, out_tok=10):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok)
    resp = SimpleNamespace(choices=[choice], usage=usage)
    return resp


# ---------------------------------------------------------------------------
# _to_oai_tools
# ---------------------------------------------------------------------------


def test_to_oai_tools_converts_anthropic_schema():
    tools = [
        {
            "name": "execute_sql",
            "description": "Run SQL",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}},
        }
    ]
    result = _to_oai_tools(tools)
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "execute_sql"
    assert result[0]["function"]["parameters"]["properties"]["sql"]["type"] == "string"


def test_to_oai_tools_no_description():
    tools = [{"name": "ping", "input_schema": {}}]
    result = _to_oai_tools(tools)
    assert result[0]["function"]["description"] == ""


def test_to_oai_tools_empty_list():
    assert _to_oai_tools([]) == []


# ---------------------------------------------------------------------------
# AzureOpenAICompatClient.generate — text response
# ---------------------------------------------------------------------------


def test_compat_generate_text():
    client = _make_compat_client()
    mock_resp = _make_oai_response(content="Hello from Phi-4", in_tok=8, out_tok=12)
    client._client.chat.completions.create.return_value = mock_resp

    result = client.generate("sys", [{"role": "user", "content": "hi"}])

    assert result.text == "Hello from Phi-4"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 8
    assert result.output_tokens == 12
    assert result.tool_calls == []


def test_compat_generate_tool_call():
    client = _make_compat_client()
    tc = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="execute_sql", arguments='{"sql":"SELECT 1"}'),
    )
    mock_resp = _make_oai_response(tool_calls=[tc], finish_reason="tool_calls")
    client._client.chat.completions.create.return_value = mock_resp

    result = client.generate("sys", [{"role": "user", "content": "run sql"}])

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "execute_sql"
    assert result.tool_calls[0].arguments == {"sql": "SELECT 1"}


def test_compat_generate_passes_system_as_first_message():
    client = _make_compat_client()
    mock_resp = _make_oai_response(content="ok")
    client._client.chat.completions.create.return_value = mock_resp

    client.generate("be helpful", [{"role": "user", "content": "hi"}])

    call_kwargs = client._client.chat.completions.create.call_args.kwargs
    messages = call_kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "be helpful"}
    assert messages[1] == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------------------
# AzureOpenAICompatClient format helpers
# ---------------------------------------------------------------------------


def test_compat_format_tool_result():
    client = _make_compat_client()
    result = client.format_tool_result("call-99", "42 rows")
    assert result == {"role": "tool", "tool_call_id": "call-99", "content": "42 rows"}


def test_compat_format_tool_results_batch():
    client = _make_compat_client()
    results = client.format_tool_results([("c1", "a"), ("c2", "b")])
    assert len(results) == 2
    assert results[0]["tool_call_id"] == "c1"
    assert results[1]["content"] == "b"


def test_compat_format_assistant_turn_text_only():
    client = _make_compat_client()
    resp = ModelResponse(text="done", tool_calls=[], stop_reason="end_turn",
                         input_tokens=0, output_tokens=0)
    msg = client.format_assistant_turn(resp)
    assert msg == {"role": "assistant", "content": "done"}
    assert "tool_calls" not in msg


def test_compat_format_assistant_turn_with_tool_calls():
    client = _make_compat_client()
    tc = ToolCall(id="c1", name="execute_sql", arguments={"sql": "SELECT 1"})
    resp = ModelResponse(text=None, tool_calls=[tc], stop_reason="tool_use",
                         input_tokens=0, output_tokens=0)
    msg = client.format_assistant_turn(resp)
    assert msg["tool_calls"][0]["id"] == "c1"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"sql": "SELECT 1"}


# ---------------------------------------------------------------------------
# AzureResponsesClient helpers
# ---------------------------------------------------------------------------


def test_to_responses_tools_converts_schema():
    tools = [
        {
            "name": "execute_sql",
            "description": "Run SQL",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}},
        }
    ]
    result = _to_responses_tools(tools)
    assert result[0]["type"] == "function"
    assert result[0]["name"] == "execute_sql"
    assert result[0]["parameters"]["properties"]["sql"]["type"] == "string"


def test_extract_text_finds_message_output():
    output = [
        {"type": "reasoning", "content": []},
        {
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "the answer"}],
        },
    ]
    assert _extract_text(output) == "the answer"


def test_extract_text_returns_none_if_no_message():
    output = [{"type": "reasoning", "content": []}]
    assert _extract_text(output) is None


def test_extract_text_returns_none_on_empty():
    assert _extract_text([]) is None


def test_extract_tool_calls_finds_function_call():
    output = [
        {
            "type": "function_call",
            "call_id": "fc-1",
            "name": "execute_sql",
            "arguments": '{"sql":"SELECT 1"}',
        }
    ]
    calls = _extract_tool_calls(output)
    assert len(calls) == 1
    assert calls[0].id == "fc-1"
    assert calls[0].name == "execute_sql"
    assert calls[0].arguments == {"sql": "SELECT 1"}


def test_extract_tool_calls_empty_output():
    assert _extract_tool_calls([]) == []


def test_extract_tool_calls_skips_non_function_items():
    output = [
        {"type": "reasoning", "content": []},
        {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
    ]
    assert _extract_tool_calls(output) == []


# ---------------------------------------------------------------------------
# AzureResponsesClient.generate
# ---------------------------------------------------------------------------


def _make_responses_client(**kwargs) -> AzureResponsesClient:
    defaults = dict(
        model="gpt-5.1-codex-mini",
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/openai/v1/",
        timeout=30.0,
    )
    return AzureResponsesClient(**{**defaults, **kwargs})


def test_responses_generate_text():
    client = _make_responses_client()
    api_response = {
        "output": [
            {"type": "reasoning", "content": []},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello from Responses API"}],
            },
        ],
        "usage": {"input_tokens": 12, "output_tokens": 15},
    }
    with patch.object(client, "_post", return_value=api_response):
        result = client.generate("sys", [{"role": "user", "content": "hi"}])

    assert result.text == "Hello from Responses API"
    assert result.stop_reason == "end_turn"
    assert result.input_tokens == 12
    assert result.output_tokens == 15
    assert result.tool_calls == []


def test_responses_generate_tool_call():
    client = _make_responses_client()
    api_response = {
        "output": [
            {
                "type": "function_call",
                "call_id": "fc-99",
                "name": "execute_sql",
                "arguments": '{"sql":"SELECT COUNT(*) FROM detections"}',
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 5},
    }
    with patch.object(client, "_post", return_value=api_response):
        result = client.generate("sys", [{"role": "user", "content": "count rows"}])

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "execute_sql"


def test_responses_generate_builds_input_list_from_messages():
    client = _make_responses_client()
    api_response = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
        {"role": "tool", "tool_call_id": "c1", "content": "result-data"},
    ]
    captured: list[dict] = []

    def fake_post(body):
        captured.append(body)
        return api_response

    with patch.object(client, "_post", side_effect=fake_post):
        client.generate("be helpful", messages)

    body = captured[0]
    # System is first item
    assert body["input"][0] == {"type": "message", "role": "system", "content": "be helpful"}
    # User message
    assert body["input"][1] == {"type": "message", "role": "user", "content": "hello"}
    # Assistant text turn
    assert body["input"][2] == {"type": "message", "role": "assistant", "content": "world"}
    # Tool result
    assert body["input"][3] == {
        "type": "function_call_output", "call_id": "c1", "output": "result-data"
    }


def test_responses_generate_replays_assistant_tool_calls():
    client = _make_responses_client()
    api_response = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "done"}]}
        ],
        "usage": {},
    }
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "fc-1",
                    "type": "function",
                    "function": {"name": "execute_sql", "arguments": '{"sql":"SELECT 1"}'},
                }
            ],
        }
    ]
    captured: list[dict] = []

    def fake_post(body):
        captured.append(body)
        return api_response

    with patch.object(client, "_post", side_effect=fake_post):
        client.generate("sys", messages)

    replay = captured[0]["input"][1]
    assert replay["type"] == "function_call"
    assert replay["call_id"] == "fc-1"
    assert replay["name"] == "execute_sql"


# ---------------------------------------------------------------------------
# AzureResponsesClient format helpers
# ---------------------------------------------------------------------------


def test_responses_format_tool_result():
    client = _make_responses_client()
    result = client.format_tool_result("fc-7", "some data")
    assert result == {"role": "tool", "tool_call_id": "fc-7", "content": "some data"}


def test_responses_format_tool_results_batch():
    client = _make_responses_client()
    results = client.format_tool_results([("a", "x"), ("b", "y")])
    assert results[0]["tool_call_id"] == "a"
    assert results[1]["content"] == "y"


def test_responses_format_assistant_turn_text():
    client = _make_responses_client()
    resp = ModelResponse(text="hi", tool_calls=[], stop_reason="end_turn",
                         input_tokens=0, output_tokens=0)
    msg = client.format_assistant_turn(resp)
    assert msg == {"role": "assistant", "content": "hi"}


def test_responses_format_assistant_turn_with_tool_call():
    client = _make_responses_client()
    tc = ToolCall(id="fc-2", name="execute_sql", arguments={"sql": "SELECT 1"})
    resp = ModelResponse(text=None, tool_calls=[tc], stop_reason="tool_use",
                         input_tokens=0, output_tokens=0)
    msg = client.format_assistant_turn(resp)
    assert msg["tool_calls"][0]["id"] == "fc-2"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"sql": "SELECT 1"}


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_responses_client_builds_correct_url():
    client = _make_responses_client(
        endpoint="https://example.services.ai.azure.com/openai/v1/"
    )
    assert client._url == "https://example.services.ai.azure.com/openai/v1/responses"


def test_responses_client_url_no_trailing_slash():
    client = _make_responses_client(
        endpoint="https://example.services.ai.azure.com/openai/v1"
    )
    assert client._url == "https://example.services.ai.azure.com/openai/v1/responses"
