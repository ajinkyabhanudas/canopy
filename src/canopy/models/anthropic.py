"""
models/anthropic.py
--------------------
Adapter for Anthropic's Claude API, called directly with an API key. No
gateway, no Azure, the only model backend in this build. Implements the
ModelClient contract in base.py so nothing outside this file needs to
know Claude's specific wire format.
"""

from __future__ import annotations

import anthropic

from .base import ModelClient, ModelResponse, ToolCall

DEFAULT_MAX_TOKENS = 4096


class AnthropicClient(ModelClient):
    def __init__(self, model: str, api_key: str, timeout: float = 60.0) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self._model = model

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelResponse:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=DEFAULT_MAX_TOKENS,
            system=system_prompt,
            messages=messages,
            tools=tools or [],
        )

        text_parts = [block.text for block in response.content if block.type == "text"]
        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=block.input)
            for block in response.content
            if block.type == "tool_use"
        ]

        return ModelResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call_id, "content": content}],
        }

    def format_tool_results(self, results: list[tuple[str, str]]) -> list[dict]:
        # Anthropic bundles all tool results into a single user message.
        return [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": content}
                    for tid, content in results
                ],
            }
        ]

    def format_assistant_turn(self, response: ModelResponse) -> dict:
        blocks: list[dict] = []
        if response.text:
            blocks.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
        return {"role": "assistant", "content": blocks}
