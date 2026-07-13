"""
models/registry.py
-------------------
Two entry points:
  get_llm()          — returns a LlamaIndex FunctionCallingLLM (used by loop.py)
  get_model_client() — returns a legacy ModelClient (kept for benchmark runner)

Phase 2a: openai-compat backends (gpt-5.1-2) use LlamaIndex's native OpenAI LLM.
Phase 2b: openai-responses backends (gpt-5.1-codex-mini) will use AzureResponsesLLM.
"""

from __future__ import annotations

from llama_index.core.llms.function_calling import FunctionCallingLLM

from ..config import get_active_connection
from .anthropic import AnthropicClient
from .azure import AzureFoundryClient
from .azure_compat import AzureOpenAICompatClient
from .azure_responses import AzureResponsesClient
from .base import ModelClient
from .llamaindex_compat import build_openai_compat_llm

_BACKENDS = frozenset({"anthropic", "azure"})


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
        raise NotImplementedError(
            "AzureResponsesLLM (Phase 2b) not yet implemented. "
            "Set MODEL_BACKEND=gpt-5.1-2 to use the openai-compat path."
        )

    raise ValueError(
        f"Unknown api_style '{conn.api_style}' for connection '{conn.id}'. "
        "Expected: openai-compat, openai-responses"
    )


def get_model_client(model_override: str | None = None) -> ModelClient:
    """Return a legacy ModelClient for the active connection in models.yaml.

    Kept for the benchmark runner (run_benchmark.py) which iterates over
    connections directly. The query loop (loop.py) uses get_llm() instead.
    """
    conn = get_active_connection(model_override=model_override)
    if conn.backend not in _BACKENDS:
        raise ValueError(
            f"Unknown backend '{conn.backend}' for connection '{conn.id}'. "
            f"Available: {sorted(_BACKENDS)}"
        )
    if conn.backend == "anthropic":
        model = model_override or (conn.models[0] if conn.models else "")
        return AnthropicClient(model=model, api_key=conn.api_key, timeout=conn.timeout)
    model = model_override or (conn.models[0] if conn.models else "")
    if not model:
        raise ValueError(
            f"Connection '{conn.id}' has no model specified and model_override was not given. "
            "Run the benchmark (make benchmark) to auto-discover available deployments."
        )
    if conn.api_style == "openai-compat":
        return AzureOpenAICompatClient(
            model=model, api_key=conn.api_key,
            endpoint=conn.endpoint, timeout=conn.timeout,
        )
    if conn.api_style == "openai-responses":
        return AzureResponsesClient(
            model=model, api_key=conn.api_key,
            endpoint=conn.endpoint, timeout=conn.timeout,
        )
    return AzureFoundryClient(
        model=model, api_key=conn.api_key,
        endpoint=conn.endpoint, timeout=conn.timeout,
    )
