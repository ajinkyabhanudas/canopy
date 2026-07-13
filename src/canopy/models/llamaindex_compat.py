"""
models/llamaindex_compat.py
---------------------------
LlamaIndex FunctionCallingLLM wrapper for Azure openai-compat endpoints.

Wraps the LlamaIndex OpenAI LLM class, pointed at the Azure project-scoped
chat completions path. Used for gpt-5.1-2 in Phase 2a.

In Phase 2b, AzureResponsesLLM extends this pattern for the openai-responses
wire format (gpt-5.1-codex-mini).
"""

from __future__ import annotations

import os

from llama_index.llms.openai import OpenAI as LlamaOpenAI


def build_openai_compat_llm(
    model: str,
    api_key: str,
    endpoint: str,
    timeout: float = 60.0,
) -> LlamaOpenAI:
    """Return a LlamaIndex OpenAI LLM pointed at an Azure openai-compat endpoint.

    The endpoint must be the base URL for the chat completions path, e.g.:
        https://<resource>.services.ai.azure.com/api/projects/<id>/openai/v1/

    LlamaIndex's OpenAI client appends /chat/completions automatically.
    """
    # LlamaIndex OpenAI reads OPENAI_API_KEY from env if api_key kwarg is not set.
    # We set it explicitly so no env pollution is required.
    os.environ.setdefault("OPENAI_API_KEY", api_key)

    return LlamaOpenAI(
        model=model,
        api_key=api_key,
        api_base=endpoint,
        max_tokens=4096,
        timeout=timeout,
        is_function_calling_model=True,
    )
