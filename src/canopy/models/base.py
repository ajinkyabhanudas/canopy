"""
models/base.py
--------------
Vendor-neutral contract every model backend implements. The orchestrator
and tool layer (once built) talk only to this interface, never to a
vendor SDK directly. Changing which model answers questions means adding
a new file in this package plus a registry entry, not a rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class ModelClient(ABC):
    """One round trip to a model: a system prompt, the running message
    history, and the tools it may call."""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelResponse:
        ...

    @abstractmethod
    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        """Wrap a single tool's output in whatever shape this vendor expects.
        Prefer format_tool_results for new call sites."""
        ...

    @abstractmethod
    def format_tool_results(self, results: list[tuple[str, str]]) -> dict:
        """Bundle all tool results from one model response into a single user message.

        The Anthropic API (and most vendors) require all tool results that answer
        a single assistant turn to arrive in one user message. Appending them as
        separate messages creates consecutive user turns, which the API rejects.

        Args:
            results: Ordered list of (tool_call_id, content) pairs — one per
                     tool call returned in the preceding assistant turn.

        Returns:
            A single message dict ready to append to the message history.
        """
        ...

    @abstractmethod
    def format_assistant_turn(self, response: ModelResponse) -> dict:
        """Wrap this vendor's own response (text and/or tool calls) in the
        shape it expects to see echoed back in message history. Keeps
        vendor wire format out of every file except this one."""
        ...
