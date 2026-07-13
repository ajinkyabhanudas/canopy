"""
models/registry.py
-------------------
Single entry point: get_llm() returns a LlamaIndex FunctionCallingLLM for the
active connection declared in models.yaml.

  openai-compat     → CanopyAzureCompatLLM  (wraps LlamaIndex OpenAI LLM)
  openai-responses  → AzureResponsesLLM     (custom Responses API adapter)
"""

from __future__ import annotations

from llama_index.core.llms.function_calling import FunctionCallingLLM

from ..config import get_active_connection
from .azure_responses_llm import AzureResponsesLLM
from .llamaindex_compat import build_openai_compat_llm


def get_llm(model_override: str | None = None) -> FunctionCallingLLM:
    """Return a LlamaIndex FunctionCallingLLM for the active connection in models.yaml.

    Phase 2a: openai-compat → LlamaIndex OpenAI LLM pointed at Azure endpoint.
    Phase 2b: openai-responses → AzureResponsesLLM (FunctionCallingLLM subclass).
    """
    conn = get_active_connection(model_override=model_override)
    model = model_override or (conn.models[0] if conn.models else "")
    if not model:
        raise ValueError(
            f"Connection '{conn.id}' has no model specified. "
            "Run the benchmark (make benchmark) to auto-discover available deployments."
        )

    if conn.backend == "anthropic":
        raise NotImplementedError(
            "Anthropic LlamaIndex LLM wrapper not yet implemented. "
            "Set MODEL_BACKEND to an Azure connection."
        )

    if conn.api_style == "openai-compat":
        return build_openai_compat_llm(
            model=model,
            api_key=conn.api_key,
            endpoint=conn.endpoint,
            timeout=conn.timeout,
        )

    if conn.api_style == "openai-responses":
        return AzureResponsesLLM(
            model=model,
            api_key=conn.api_key,
            endpoint=conn.endpoint,
            timeout=conn.timeout,
        )

    raise ValueError(
        f"Unknown api_style '{conn.api_style}' for connection '{conn.id}'. "
        "Expected: openai-compat, openai-responses"
    )


