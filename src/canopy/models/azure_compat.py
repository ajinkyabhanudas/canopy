"""
models/azure_compat.py
----------------------
Adapter for Azure AI Foundry endpoints that expose the OpenAI-compatible
chat completions API at /openai/v1/chat/completions.

Used for: Phi-4, Qwen, and any future deployment on that path.
Auth: api-key header via the openai SDK.
"""

from __future__ import annotations

import json
import logging

from openai import OpenAI

from .base import ModelClient, ModelResponse, ToolCall

_log = logging.getLogger("canopy.models.azure_compat")

DEFAULT_MAX_COMPLETION_TOKENS = 4096


def _to_oai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


class AzureOpenAICompatClient(ModelClient):
    def __init__(self, model: str, api_key: str, endpoint: str, timeout: float = 60.0) -> None:
        self._client = OpenAI(api_key=api_key, base_url=endpoint)
        self._model = model
        self._timeout = timeout

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelResponse:
        all_messages = [{"role": "system", "content": system_prompt}] + messages
        kwargs: dict = {
            "model": self._model,
            "messages": all_messages,
            "max_completion_tokens": DEFAULT_MAX_COMPLETION_TOKENS,
            "timeout": self._timeout,
        }
        if tools:
            kwargs["tools"] = _to_oai_tools(tools)

        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        finish_reason = resp.choices[0].finish_reason

        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=json.loads(tc.function.arguments),
            )
            for tc in (msg.tool_calls or [])
        ]

        return ModelResponse(
            text=msg.content,
            tool_calls=tool_calls,
            stop_reason="tool_use" if finish_reason == "tool_calls" else "end_turn",
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
