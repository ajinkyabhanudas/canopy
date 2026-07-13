"""Model-agnostic interface and backend registry."""

from .registry import get_llm

__all__ = ["get_llm"]
