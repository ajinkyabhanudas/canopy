"""
models/registry.py
-------------------
One place to add a new backend. Today there is exactly one: Claude,
called directly through Anthropic's API. Swapping which model answers
queries inside that backend is the ANTHROPIC_MODEL environment variable,
not a code change. A second backend (Azure AI Foundry, once provisioned)
gets added here as a new entry, nothing else in the codebase changes.
"""

from __future__ import annotations

from ..config import get_model_config
from .anthropic import AnthropicClient
from .base import ModelClient

_BACKENDS: dict[str, type[ModelClient]] = {
    "anthropic": AnthropicClient,
}


def get_model_client() -> ModelClient:
    backend = get_model_config().backend
    try:
        return _BACKENDS[backend]()
    except KeyError as exc:
        raise ValueError(
            f"Unknown MODEL_BACKEND '{backend}'. Available: {list(_BACKENDS)}"
        ) from exc
