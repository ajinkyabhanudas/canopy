"""Shared OpenAI-style message formatting helpers used by all Azure adapters.

All three Azure backends (Foundry, OpenAI-compat, Responses) share the same
wire format for history reconstruction: role="tool" per result, and
role="assistant" with a tool_calls list for assistant turns. Centralising
here means a single change point if the format ever diverges.
"""

from __future__ import annotations

import json

from .base import ModelResponse, ToolCall


def openai_format_tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def openai_format_tool_results(results: list[tuple[str, str]]) -> list[dict]:
    return [openai_format_tool_result(tid, content) for tid, content in results]


def openai_format_assistant_turn(response: ModelResponse) -> dict:
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
