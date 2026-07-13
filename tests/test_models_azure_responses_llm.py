"""Unit tests for AzureResponsesLLM — LlamaIndex FunctionCallingLLM adapter.

No network calls. _post() is patched or the test exercises methods that
don't call _post() at all.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from unittest.mock import patch

import pytest
from llama_index.core.base.llms.types import (
    ChatMessage,
    MessageRole,
    TextBlock,
    ToolCallBlock,
)
from llama_index.core.llms.llm import ToolSelection
from llama_index.core.tools import FunctionTool

from canopy.models.azure_responses_llm import _DEFAULT_CONTEXT_WINDOW, AzureResponsesLLM


def _run(coro):
    """Run a coroutine in a dedicated thread so Playwright's loop never interferes."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


def _llm(**kwargs) -> AzureResponsesLLM:
    defaults = dict(
        model="gpt-5.1-codex-mini",
        api_key="test-key",
        endpoint="https://example.services.ai.azure.com/openai/v1/",
        timeout=10.0,
    )
    return AzureResponsesLLM(**{**defaults, **kwargs})


def _text_response(text: str = "the answer") -> dict:
    return {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _tool_response(
    call_id: str = "fc-1",
    name: str = "execute_sql",
    args: str = '{"sql":"SELECT 1"}',
) -> dict:
    return {
        "output": [{"type": "function_call", "call_id": call_id, "name": name, "arguments": args}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_metadata_context_window():
    assert _llm().metadata.context_window == _DEFAULT_CONTEXT_WINDOW


def test_metadata_is_function_calling_model():
    assert _llm().metadata.is_function_calling_model is True


def test_metadata_model_name():
    assert _llm().metadata.model_name == "gpt-5.1-codex-mini"


# ---------------------------------------------------------------------------
# _url
# ---------------------------------------------------------------------------


def test_url_appends_responses_path():
    llm = _llm(endpoint="https://example.services.ai.azure.com/openai/v1/")
    assert llm._url() == "https://example.services.ai.azure.com/openai/v1/responses"


def test_url_no_trailing_slash():
    llm = _llm(endpoint="https://example.services.ai.azure.com/openai/v1")
    assert llm._url() == "https://example.services.ai.azure.com/openai/v1/responses"


# ---------------------------------------------------------------------------
# _messages_to_input
# ---------------------------------------------------------------------------


def test_messages_to_input_system():
    msgs = [ChatMessage(role=MessageRole.SYSTEM, content="be helpful")]
    items = AzureResponsesLLM._messages_to_input(msgs)
    assert items == [{"type": "message", "role": "system", "content": "be helpful"}]


def test_messages_to_input_user():
    msgs = [ChatMessage(role=MessageRole.USER, content="hello")]
    items = AzureResponsesLLM._messages_to_input(msgs)
    assert items == [{"type": "message", "role": "user", "content": "hello"}]


def test_messages_to_input_assistant_text():
    msgs = [ChatMessage(role=MessageRole.ASSISTANT, content="I see")]
    items = AzureResponsesLLM._messages_to_input(msgs)
    assert items == [{"type": "message", "role": "assistant", "content": "I see"}]


def test_messages_to_input_assistant_tool_call():
    tb = ToolCallBlock(
        tool_call_id="fc-1", tool_name="execute_sql", tool_kwargs={"sql": "SELECT 1"}
    )
    msg = ChatMessage(role=MessageRole.ASSISTANT, blocks=[tb])
    items = AzureResponsesLLM._messages_to_input([msg])
    assert items[0]["type"] == "function_call"
    assert items[0]["call_id"] == "fc-1"
    assert items[0]["name"] == "execute_sql"
    assert json.loads(items[0]["arguments"]) == {"sql": "SELECT 1"}


def test_messages_to_input_tool_result():
    msg = ChatMessage(
        role=MessageRole.TOOL,
        content="42 rows",
        additional_kwargs={"tool_call_id": "fc-1"},
    )
    items = AzureResponsesLLM._messages_to_input([msg])
    assert items == [{"type": "function_call_output", "call_id": "fc-1", "output": "42 rows"}]


def test_messages_to_input_mixed():
    msgs = [
        ChatMessage(role=MessageRole.SYSTEM, content="sys"),
        ChatMessage(role=MessageRole.USER, content="q"),
        ChatMessage(role=MessageRole.ASSISTANT, content="thinking"),
    ]
    items = AzureResponsesLLM._messages_to_input(msgs)
    assert len(items) == 3
    assert items[0]["role"] == "system"
    assert items[1]["role"] == "user"
    assert items[2]["role"] == "assistant"


# ---------------------------------------------------------------------------
# _tools_to_responses_format
# ---------------------------------------------------------------------------


def _make_tool(name: str = "execute_sql") -> FunctionTool:
    def fn(sql: str) -> str:
        """Run SQL."""
        return "result"
    return FunctionTool.from_defaults(fn=fn, name=name)


def test_tools_to_responses_format_basic():
    tool = _make_tool()
    specs = AzureResponsesLLM._tools_to_responses_format([tool])
    assert len(specs) == 1
    assert specs[0]["type"] == "function"
    assert specs[0]["name"] == "execute_sql"


def test_tools_to_responses_format_empty():
    assert AzureResponsesLLM._tools_to_responses_format([]) == []


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


def test_parse_response_text_block():
    data = _text_response("the answer")
    resp = AzureResponsesLLM._parse_response(data)
    text_blocks = [b for b in resp.message.blocks if isinstance(b, TextBlock)]
    assert len(text_blocks) == 1
    assert text_blocks[0].text == "the answer"


def test_parse_response_tool_call_block():
    data = _tool_response(call_id="fc-99", name="execute_sql", args='{"sql":"SELECT 1"}')
    resp = AzureResponsesLLM._parse_response(data)
    tool_blocks = [b for b in resp.message.blocks if isinstance(b, ToolCallBlock)]
    assert len(tool_blocks) == 1
    assert tool_blocks[0].tool_call_id == "fc-99"
    assert tool_blocks[0].tool_name == "execute_sql"
    assert tool_blocks[0].tool_kwargs == {"sql": "SELECT 1"}


def test_parse_response_invalid_json_args():
    data = {"output": [
        {"type": "function_call", "call_id": "fc-1", "name": "fn", "arguments": "not-json"}
    ]}
    resp = AzureResponsesLLM._parse_response(data)
    tool_blocks = [b for b in resp.message.blocks if isinstance(b, ToolCallBlock)]
    assert tool_blocks[0].tool_kwargs == {"_raw": "not-json"}


def test_parse_response_skips_reasoning():
    data = {
        "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": "done"}]},
        ]
    }
    resp = AzureResponsesLLM._parse_response(data)
    text_blocks = [b for b in resp.message.blocks if isinstance(b, TextBlock)]
    assert text_blocks[0].text == "done"


def test_parse_response_empty_output():
    resp = AzureResponsesLLM._parse_response({"output": []})
    assert resp.message.blocks == []


# ---------------------------------------------------------------------------
# chat() — with _post patched
# ---------------------------------------------------------------------------


def test_chat_text_response():
    llm = _llm()
    with patch.object(llm, "_post", return_value=_text_response("hello")):
        resp = llm.chat([ChatMessage(role=MessageRole.USER, content="hi")])
    text_blocks = [b for b in resp.message.blocks if isinstance(b, TextBlock)]
    assert text_blocks[0].text == "hello"


def test_chat_sends_tools_when_provided():
    llm = _llm()
    captured = {}

    def fake_post(body):
        captured.update(body)
        return _text_response("ok")

    tool = _make_tool()
    with patch.object(llm, "_post", side_effect=fake_post):
        llm.chat([ChatMessage(role=MessageRole.USER, content="q")], tools=[tool])

    assert "tools" in captured
    assert captured["tools"][0]["name"] == "execute_sql"


def test_chat_omits_tools_when_empty():
    llm = _llm()
    captured = {}

    def fake_post(body):
        captured.update(body)
        return _text_response("ok")

    with patch.object(llm, "_post", side_effect=fake_post):
        llm.chat([ChatMessage(role=MessageRole.USER, content="q")])

    assert "tools" not in captured


def test_chat_sends_max_output_tokens():
    llm = _llm()
    captured = {}

    def fake_post(body):
        captured.update(body)
        return _text_response("ok")

    with patch.object(llm, "_post", side_effect=fake_post):
        llm.chat([ChatMessage(role=MessageRole.USER, content="q")])

    assert captured["max_output_tokens"] == 4096


# ---------------------------------------------------------------------------
# get_tool_calls_from_response
# ---------------------------------------------------------------------------


def test_get_tool_calls_returns_selections():
    llm = _llm()
    with patch.object(
        llm, "_post", return_value=_tool_response("fc-1", "execute_sql", '{"sql":"SELECT 1"}')
    ):
        resp = llm.chat([ChatMessage(role=MessageRole.USER, content="q")])

    selections = llm.get_tool_calls_from_response(resp, error_on_no_tool_call=False)
    assert len(selections) == 1
    assert isinstance(selections[0], ToolSelection)
    assert selections[0].tool_id == "fc-1"
    assert selections[0].tool_name == "execute_sql"
    assert selections[0].tool_kwargs == {"sql": "SELECT 1"}


def test_get_tool_calls_no_tool_calls_no_error():
    llm = _llm()
    with patch.object(llm, "_post", return_value=_text_response("just text")):
        resp = llm.chat([ChatMessage(role=MessageRole.USER, content="q")])

    result = llm.get_tool_calls_from_response(resp, error_on_no_tool_call=False)
    assert result == []


def test_get_tool_calls_no_tool_calls_raises():
    llm = _llm()
    with patch.object(llm, "_post", return_value=_text_response("just text")):
        resp = llm.chat([ChatMessage(role=MessageRole.USER, content="q")])

    with pytest.raises(ValueError, match="Expected at least one tool call"):
        llm.get_tool_calls_from_response(resp, error_on_no_tool_call=True)


# ---------------------------------------------------------------------------
# _prepare_chat_with_tools
# ---------------------------------------------------------------------------


def test_prepare_chat_with_tools_string_user_msg():
    llm = _llm()
    tool = _make_tool()
    result = llm._prepare_chat_with_tools(tools=[tool], user_msg="what species?")
    msgs = result["messages"]
    assert msgs[-1].role == MessageRole.USER
    assert msgs[-1].content == "what species?"
    assert len(result["tools"]) == 1


def test_prepare_chat_with_tools_chatmessage_user_msg():
    llm = _llm()
    user_msg = ChatMessage(role=MessageRole.USER, content="hello")
    result = llm._prepare_chat_with_tools(tools=[], user_msg=user_msg)
    assert result["messages"][-1].content == "hello"


def test_prepare_chat_with_tools_no_user_msg():
    llm = _llm()
    history = [ChatMessage(role=MessageRole.USER, content="prior")]
    result = llm._prepare_chat_with_tools(tools=[], chat_history=history)
    assert result["messages"][0].content == "prior"


# ---------------------------------------------------------------------------
# Unimplemented methods raise
# ---------------------------------------------------------------------------


def test_complete_raises():
    with pytest.raises(NotImplementedError):
        _llm().complete("prompt")


def test_stream_complete_raises():
    with pytest.raises(NotImplementedError):
        _llm().stream_complete("prompt")


def test_stream_chat_raises():
    with pytest.raises(NotImplementedError):
        _llm().stream_chat([])


def test_acomplete_raises():
    with pytest.raises(NotImplementedError):
        _run(_llm().acomplete("prompt"))


def test_astream_complete_raises():
    with pytest.raises(NotImplementedError):
        _run(_llm().astream_complete("prompt"))


# ---------------------------------------------------------------------------
# Async methods
# ---------------------------------------------------------------------------


def test_achat_delegates_to_chat():
    llm = _llm()
    msgs = [ChatMessage(role=MessageRole.USER, content="hi")]
    with patch.object(llm, "_post", return_value=_text_response("async answer")):
        resp = _run(llm.achat(msgs))
    text_blocks = [b for b in resp.message.blocks if isinstance(b, TextBlock)]
    assert text_blocks[0].text == "async answer"


def test_astream_chat_yields_full_response():
    llm = _llm()
    msgs = [ChatMessage(role=MessageRole.USER, content="hi")]

    async def run():
        with patch.object(llm, "_post", return_value=_text_response("streamed")):
            gen = await llm.astream_chat(msgs)
            chunks = [chunk async for chunk in gen]
        return chunks

    chunks = _run(run())
    assert len(chunks) == 1
    text_blocks = [b for b in chunks[0].message.blocks if isinstance(b, TextBlock)]
    assert text_blocks[0].text == "streamed"


# ---------------------------------------------------------------------------
# _post error paths
# ---------------------------------------------------------------------------


def test_post_http_error_raises():
    import io
    import urllib.error

    llm = _llm()
    exc = urllib.error.HTTPError(
        url=llm._url(), code=500, msg="Internal Server Error",
        hdrs=None, fp=io.BytesIO(b"server blew up"),
    )
    with patch("urllib.request.urlopen", side_effect=exc):
        with pytest.raises(RuntimeError, match="Responses API error 500"):
            llm.chat([ChatMessage(role=MessageRole.USER, content="q")])


def test_post_content_filter_returns_refusal():
    import io
    import urllib.error

    llm = _llm()
    body = json.dumps({"error": {"code": "content_filter", "message": "blocked"}}).encode()
    exc = urllib.error.HTTPError(
        url=llm._url(), code=400, msg="Bad Request",
        hdrs=None, fp=io.BytesIO(body),
    )
    with patch("urllib.request.urlopen", side_effect=exc):
        resp = llm.chat([ChatMessage(role=MessageRole.USER, content="q")])

    text_blocks = [b for b in resp.message.blocks if isinstance(b, TextBlock)]
    assert any("sorry" in b.text.lower() or "can't" in b.text.lower() for b in text_blocks)


def test_post_url_error_raises():
    import urllib.error

    llm = _llm()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("unreachable")):
        with pytest.raises(RuntimeError, match="Responses API network error"):
            llm.chat([ChatMessage(role=MessageRole.USER, content="q")])


def test_post_timeout_raises():
    llm = _llm()
    with patch("urllib.request.urlopen", side_effect=TimeoutError()):
        with pytest.raises(RuntimeError, match="Responses API timed out"):
            llm.chat([ChatMessage(role=MessageRole.USER, content="q")])
