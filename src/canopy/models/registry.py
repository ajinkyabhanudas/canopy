"""
models/registry.py
-------------------
One place to add a new backend. Backends are keyed by the "backend" field
in models.yaml. Adding a new provider = one import + one dict entry here,
nothing else in the codebase changes.
"""

from __future__ import annotations

from ..config import get_active_connection
from .anthropic import AnthropicClient
from .azure import AzureFoundryClient
from .azure_compat import AzureOpenAICompatClient
from .azure_responses import AzureResponsesClient
from .base import ModelClient

_BACKENDS = frozenset({"anthropic", "azure"})


def get_model_client(model_override: str | None = None) -> ModelClient:
    """Return a ModelClient for the active connection in models.yaml.

    model_override pins a specific model name — used by the benchmark runner
    to iterate over discovered Azure deployments.
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
    # conn.backend == "azure"
    model = model_override or (conn.models[0] if conn.models else "")
    if not model:
        raise ValueError(
            f"Connection '{conn.id}' has no model specified and model_override was not given. "
            "Run the benchmark (make benchmark) to auto-discover available deployments."
        )
    if conn.api_style == "openai-compat":
        return AzureOpenAICompatClient(
            model=model,
            api_key=conn.api_key,
            endpoint=conn.endpoint,
            timeout=conn.timeout,
        )
    if conn.api_style == "openai-responses":
        return AzureResponsesClient(
            model=model,
            api_key=conn.api_key,
            endpoint=conn.endpoint,
            timeout=conn.timeout,
        )
    # Default: "azure-inference" — azure-ai-inference SDK against /models endpoint
    return AzureFoundryClient(
        model=model,
        api_key=conn.api_key,
        endpoint=conn.endpoint,
        timeout=conn.timeout,
    )
