"""
models/azure.py
---------------
Adapter for Azure AI Foundry via the azure-ai-inference SDK.
Endpoint: https://<resource>.services.ai.azure.com/models
Auth: api-key header via AzureKeyCredential.

The loop accumulates vendor-agnostic raw dicts (format_tool_result /
format_assistant_turn). generate() converts them to azure-ai-inference
message objects immediately before calling complete(). Every other method
keeps the same OpenAI-style dict shape — one role="tool" message per result.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import (
    AssistantMessage,
    ChatCompletionsToolDefinition,
    FunctionDefinition,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from azure.core.credentials import AzureKeyCredential

from .base import ModelClient, ModelResponse, ToolCall

_log = logging.getLogger("canopy.models.azure")


def _to_sdk_tool(tool: dict) -> ChatCompletionsToolDefinition:
    """Convert an Anthropic-style tool dict to an azure-ai-inference tool definition."""
    schema = tool.get("input_schema", {})
    return ChatCompletionsToolDefinition(
        function=FunctionDefinition(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=schema,
        )
    )


def _to_sdk_messages(messages: list[dict]) -> list[Any]:
    """Convert accumulated raw dicts to azure-ai-inference message objects."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append(UserMessage(content=m.get("content", "")))
        elif role == "assistant":
            raw_tcs = m.get("tool_calls")
            if raw_tcs:
                from azure.ai.inference.models import ChatCompletionsToolCall, FunctionCall

                sdk_tcs = [
                    ChatCompletionsToolCall(
                        id=tc["id"],
                        function=FunctionCall(
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        ),
                    )
                    for tc in raw_tcs
                ]
                out.append(AssistantMessage(content=m.get("content") or "", tool_calls=sdk_tcs))
            else:
                out.append(AssistantMessage(content=m.get("content", "")))
        elif role == "tool":
            out.append(ToolMessage(tool_call_id=m["tool_call_id"], content=m.get("content", "")))
        else:
            out.append(UserMessage(content=m.get("content", "")))
    return out


class AzureFoundryClient(ModelClient):
    def __init__(self, model: str, api_key: str, endpoint: str, timeout: float = 60.0) -> None:
        self._client = ChatCompletionsClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(api_key),
        )
        self._model = model
        self._timeout = timeout

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelResponse:
        sdk_messages = [SystemMessage(content=system_prompt)] + _to_sdk_messages(messages)
        sdk_tools = [_to_sdk_tool(t) for t in tools] if tools else None

        kwargs: dict = {"model": self._model, "messages": sdk_messages}
        if sdk_tools:
            kwargs["tools"] = sdk_tools

        resp = self._client.complete(**kwargs)
        choice = resp.choices[0]
        finish_reason = choice.finish_reason

        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments),
            )
            for tc in (choice.message.tool_calls or [])
        ]

        return ModelResponse(
            text=choice.message.content,
            tool_calls=tool_calls,
            stop_reason="tool_use" if str(finish_reason) == "tool_calls" else "end_turn",
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )

    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content}

    def format_tool_results(self, results: list[tuple[str, str]]) -> list[dict]:
        return [self.format_tool_result(tid, content) for tid, content in results]

    def format_assistant_turn(self, response: ModelResponse) -> dict:
        msg: dict = {"role": "assistant", "content": response.text or ""}
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg
