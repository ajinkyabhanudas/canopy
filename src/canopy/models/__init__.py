"""Model-agnostic interface and backend registry."""

from .registry import get_llm, get_model_client

__all__ = ["get_llm", "get_model_client"]
