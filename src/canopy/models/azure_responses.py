"""
models/azure_responses.py
-------------------------
Adapter for Azure AI Foundry endpoints that use the OpenAI Responses API
at /openai/v1/responses (e.g. gpt-5.1-codex-mini).

The Responses API differs from chat completions:
  - Request:  {"model": ..., "input": <str or list>, "tools": [...], "max_output_tokens": N}
  - Response: {"output": [{type: "reasoning", ...},
               {type: "message", content: [{type: "output_text", text: "..."}]}]}
  - Tool calls appear as output items with type="function_call"
  - Tool results are fed back via "input" as a list containing role/content items
    plus {"type": "function_call_output", "call_id": ..., "output": ...} items

Auth: api-key header. Uses stdlib urllib — no SDK dependency for this path.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request

from ._openai_format import (
    openai_format_assistant_turn,
    openai_format_tool_result,
    openai_format_tool_results,
)
from .base import ModelClient, ModelResponse, ToolCall

_log = logging.getLogger("canopy.models.azure_responses")


def _to_responses_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {}),
        }
        for t in tools
    ]


def _extract_text(output: list[dict]) -> str | None:
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    return part.get("text")
    return None


def _extract_tool_calls(output: list[dict]) -> list[ToolCall]:
    calls = []
    for item in output:
        if item.get("type") == "function_call":
            calls.append(
                ToolCall(
                    id=item["call_id"],
                    name=item["name"],
                    arguments=json.loads(item.get("arguments", "{}")),
                )
            )
    return calls


class AzureResponsesClient(ModelClient):
    def __init__(self, model: str, api_key: str, endpoint: str, timeout: float = 60.0) -> None:
        # endpoint should be the base /openai/v1/ URL
        self._url = endpoint.rstrip("/") + "/responses"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._ctx = ssl.create_default_context()

    def _post(self, body: dict) -> dict:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"api-key": self._api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:
            return json.loads(resp.read().decode())

    def generate(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelResponse:
        # Build input list: system instruction + prior messages
        input_items: list = [{"type": "message", "role": "system", "content": system_prompt}]
        for m in messages:
            role = m.get("role")
            if role == "user":
                input_items.append({"type": "message", "role": "user", "content": m["content"]})
            elif role == "assistant":
                # Replay assistant turn — may include function_call items
                raw_tcs = m.get("tool_calls")
                if raw_tcs:
                    for tc in raw_tcs:
                        input_items.append({
                            "type": "function_call",
                            "call_id": tc["id"],
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        })
                else:
                    input_items.append({
                        "type": "message", "role": "assistant",
                        "content": m.get("content", ""),
                    })
            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": m["tool_call_id"],
                    "output": m.get("content", ""),
                })

        body: dict = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": 4096,
        }
        if tools:
            body["tools"] = _to_responses_tools(tools)

        try:
            data = self._post(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode()[:300]
            raise RuntimeError(f"Responses API error {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Responses API network error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Responses API timed out after {self._timeout}s") from exc

        output = data.get("output", [])
        text = _extract_text(output)
        tool_calls = _extract_tool_calls(output)
        usage = data.get("usage", {})

        # Determine stop reason
        has_tool_calls = bool(tool_calls)
        stop_reason = "tool_use" if has_tool_calls else "end_turn"

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    def format_tool_result(self, tool_call_id: str, content: str) -> dict:
        # Stored as a raw dict; generate() converts it to function_call_output on replay
        return openai_format_tool_result(tool_call_id, content)

    def format_tool_results(self, results: list[tuple[str, str]]) -> list[dict]:
        return openai_format_tool_results(results)

    def format_assistant_turn(self, response: ModelResponse) -> dict:
        return openai_format_assistant_turn(response)
