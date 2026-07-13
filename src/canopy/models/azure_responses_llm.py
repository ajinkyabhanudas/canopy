"""
models/azure_responses_llm.py
------------------------------
LlamaIndex FunctionCallingLLM wrapper for the Azure OpenAI Responses API.

Used for gpt-5.1-codex-mini, which uses a non-standard wire format:
  POST /openai/v1/responses
  Request:  {"model": ..., "input": [...], "tools": [...], "max_output_tokens": N}
  Response: {"output": [{type: "reasoning"}, {type: "message", content: [...]},
                        {type: "function_call", call_id: ..., name: ..., arguments: ...}]}

Tool results re-enter via "input" as function_call_output items.

This class bridges that wire format into LlamaIndex's FunctionCallingLLM interface
so FunctionAgent can drive the tool-calling loop without knowing about the
Responses API internals.

Streaming is not implemented (not required by Canopy's sync run_query path).
Async methods delegate to their sync counterparts via asyncio.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Any, Sequence

from llama_index.core.base.llms.types import (
    ChatMessage,
    ChatResponse,
    ChatResponseAsyncGen,
    ChatResponseGen,
    CompletionResponse,
    CompletionResponseAsyncGen,
    CompletionResponseGen,
    LLMMetadata,
    MessageRole,
    TextBlock,
    ToolCallBlock,
)
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.llms.llm import ToolSelection
from llama_index.core.tools import BaseTool

_log = logging.getLogger("canopy.models.azure_responses_llm")

_DEFAULT_CONTEXT_WINDOW = 200_000  # gpt-5.1-codex-mini context window


class AzureResponsesLLM(FunctionCallingLLM):
    """LlamaIndex FunctionCallingLLM adapter for the Azure OpenAI Responses API.

    Canopy-specific; not a general-purpose implementation. Designed to work
    with FunctionAgent's synchronous call path via asyncio.run().
    """

    # Pydantic fields (LlamaIndex LLMs are Pydantic models)
    model: str
    api_key: str
    endpoint: str
    timeout: float = 60.0

    def __init__(self, model: str, api_key: str, endpoint: str, timeout: float = 60.0) -> None:
        super().__init__(model=model, api_key=api_key, endpoint=endpoint, timeout=timeout)

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _url(self) -> str:
        return self.endpoint.rstrip("/") + "/responses"

    def _post(self, body: dict) -> dict:
        ctx = ssl.create_default_context()
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            self._url(),
            data=payload,
            headers={"api-key": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode()
            try:
                err_json = json.loads(err_body)
                if err_json.get("error", {}).get("code") == "content_filter":
                    # Azure content filter blocked the request — return a synthetic
                    # "refusal" response so the agent can report it gracefully.
                    _log.info("responses_api: content filter blocked prompt (400)")
                    return {"output": [{"type": "message", "content": [
                        {"type": "output_text",
                         "text": "I'm sorry, but I can't help with that request."}
                    ]}]}
            except (json.JSONDecodeError, KeyError):
                pass
            raise RuntimeError(f"Responses API error {exc.code}: {err_body[:400]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Responses API network error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Responses API timed out after {self.timeout}s") from exc

    # ------------------------------------------------------------------
    # Message format helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _messages_to_input(messages: Sequence[ChatMessage]) -> list[dict]:
        """Convert LlamaIndex ChatMessage list to Responses API input items."""
        items: list[dict] = []
        for msg in messages:
            role = msg.role

            if role == MessageRole.SYSTEM:
                content = msg.content or ""
                items.append({"type": "message", "role": "system", "content": content})

            elif role == MessageRole.USER:
                content = msg.content or ""
                items.append({"type": "message", "role": "user", "content": content})

            elif role == MessageRole.ASSISTANT:
                # Assistant turn may contain ToolCallBlocks (LlamaIndex stores them in blocks)
                tool_blocks = [b for b in (msg.blocks or []) if isinstance(b, ToolCallBlock)]
                if tool_blocks:
                    for tb in tool_blocks:
                        items.append({
                            "type": "function_call",
                            "call_id": tb.tool_call_id or "",
                            "name": tb.tool_name,
                            "arguments": (
                                json.dumps(tb.tool_kwargs)
                                if isinstance(tb.tool_kwargs, dict)
                                else str(tb.tool_kwargs)
                            ),
                        })
                else:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": msg.content or "",
                    })

            elif role == MessageRole.TOOL:
                tool_call_id = (msg.additional_kwargs or {}).get("tool_call_id", "")
                content = msg.content or ""
                items.append({
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": content,
                })

        return items

    @staticmethod
    def _tools_to_responses_format(tools: Sequence[BaseTool]) -> list[dict]:
        """Convert LlamaIndex BaseTool list to Responses API tool specs."""
        specs = []
        for tool in tools:
            openai_spec = tool.metadata.to_openai_tool(skip_length_check=True)
            fn = openai_spec.get("function", {})
            specs.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        return specs

    @staticmethod
    def _parse_response(data: dict) -> ChatResponse:
        """Parse a Responses API response dict into a LlamaIndex ChatResponse."""
        output = data.get("output", [])
        blocks: list = []

        for item in output:
            item_type = item.get("type")
            if item_type == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        blocks.append(TextBlock(text=part.get("text", "")))

            elif item_type == "function_call":
                raw_args = item.get("arguments", "{}")
                try:
                    kwargs = json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    kwargs = {"_raw": raw_args}

                blocks.append(ToolCallBlock(
                    tool_call_id=item.get("call_id", ""),
                    tool_name=item.get("name", ""),
                    tool_kwargs=kwargs,
                ))

        message = ChatMessage(role=MessageRole.ASSISTANT, blocks=blocks)
        return ChatResponse(message=message, raw=data)

    # ------------------------------------------------------------------
    # FunctionCallingLLM required interface
    # ------------------------------------------------------------------

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=_DEFAULT_CONTEXT_WINDOW,
            num_output=4096,
            is_chat_model=True,
            is_function_calling_model=True,
            model_name=self.model,
            system_role=MessageRole.SYSTEM,
        )

    def _prepare_chat_with_tools(
        self,
        tools: Sequence[BaseTool],
        user_msg: str | ChatMessage | None = None,
        chat_history: list[ChatMessage] | None = None,
        verbose: bool = False,
        allow_parallel_tool_calls: bool = False,
        tool_required: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Assemble the kwargs dict that will be passed to chat()."""
        messages: list[ChatMessage] = list(chat_history or [])
        if isinstance(user_msg, str):
            messages.append(ChatMessage(role=MessageRole.USER, content=user_msg))
        elif user_msg is not None:
            messages.append(user_msg)
        return {"messages": messages, "tools": list(tools)}

    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        """Send messages to the Responses API and return a ChatResponse."""
        tools: list[BaseTool] = kwargs.get("tools", [])
        input_items = self._messages_to_input(messages)
        body: dict = {
            "model": self.model,
            "input": input_items,
            "max_output_tokens": 4096,
        }
        if tools:
            body["tools"] = self._tools_to_responses_format(tools)

        _log.debug("responses_api POST: %d input items, %d tools", len(input_items), len(tools))
        data = self._post(body)
        response = self._parse_response(data)
        _log.debug(
            "responses_api response: %d blocks",
            len(response.message.blocks or []),
        )
        return response

    def get_tool_calls_from_response(
        self,
        response: ChatResponse,
        error_on_no_tool_call: bool = True,
        **kwargs: Any,
    ) -> list[ToolSelection]:
        """Extract ToolCallBlocks from ChatResponse as ToolSelection objects."""
        tool_blocks = [
            b for b in (response.message.blocks or [])
            if isinstance(b, ToolCallBlock)
        ]
        if not tool_blocks:
            if error_on_no_tool_call:
                raise ValueError("Expected at least one tool call but got none.")
            return []
        return [
            ToolSelection(
                tool_id=tb.tool_call_id or "",
                tool_name=tb.tool_name,
                tool_kwargs=tb.tool_kwargs if isinstance(tb.tool_kwargs, dict) else {},
            )
            for tb in tool_blocks
        ]

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        """Not used by FunctionAgent — raises if called."""
        raise NotImplementedError("AzureResponsesLLM only supports chat-style calls.")

    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        raise NotImplementedError("AzureResponsesLLM does not support streaming.")

    def stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseGen:
        raise NotImplementedError("AzureResponsesLLM does not support streaming.")

    # Async versions — delegate to sync via asyncio (FunctionAgent uses async)
    async def achat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponse:
        return self.chat(messages, **kwargs)

    async def acomplete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        raise NotImplementedError("AzureResponsesLLM only supports chat-style calls.")

    async def astream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseAsyncGen:
        # FunctionAgent always calls astream_chat_with_tools, which routes here.
        # The Responses API has no streaming path — yield the full response as
        # a single "stream" chunk so FunctionAgent's streaming loop completes.
        response = self.chat(messages, **kwargs)

        async def _gen() -> ChatResponseAsyncGen:
            yield response

        return _gen()

    async def astream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseAsyncGen:
        raise NotImplementedError("AzureResponsesLLM only supports chat-style calls.")
