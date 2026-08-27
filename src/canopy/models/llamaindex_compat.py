"""
models/llamaindex_compat.py
---------------------------
LlamaIndex FunctionCallingLLM wrappers for OpenAI-SDK-compatible endpoints —
both Azure's (with its quirks patched) and plain third-party hosts (NVIDIA's
integrate.api.nvidia.com and similar) that need no patching at all.

Three quirks of the Azure project-scoped openai/v1 path that require fixes:

  1. OpenAI.metadata calls openai_modelname_to_contextsize(), which only
     recognises public OpenAI names. Azure deployment names (e.g. gpt-5.1-2)
     raise ValueError. Fixed by overriding metadata with a fixed context_window.

  2. AzureOpenAI appends /openai/ to azure_endpoint, double-pathing when the
     endpoint already includes the full chat completions base path. Fixed by
     using the base OpenAI class which takes api_base verbatim.

  3. The Azure project-scoped path rejects max_tokens — must use
     max_completion_tokens instead. Fixed by overriding _get_model_kwargs.

In Phase 2b, AzureResponsesLLM extends this pattern for the openai-responses
wire format (gpt-5.1-codex-mini).
"""

from __future__ import annotations

from typing import Any

from llama_index.core.base.llms.types import LLMMetadata, MessageRole
from llama_index.llms.openai import OpenAI as _LlamaOpenAI

_DEFAULT_CONTEXT_WINDOW = 128_000


class CanopyAzureCompatLLM(_LlamaOpenAI):
    """OpenAI-compatible LLM tuned for Azure project-scoped deployments.

    Fixes three Azure-specific incompatibilities with the stock LlamaIndex
    OpenAI class (see module docstring for details).
    """

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=_DEFAULT_CONTEXT_WINDOW,
            num_output=self.max_tokens or -1,
            is_chat_model=True,
            is_function_calling_model=True,
            model_name=self.model,
            system_role=MessageRole.SYSTEM,
        )

    def _get_model_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        model_kwargs = super()._get_model_kwargs(**kwargs)
        if "max_tokens" in model_kwargs:
            model_kwargs["max_completion_tokens"] = model_kwargs.pop("max_tokens")
        return model_kwargs


def build_openai_compat_llm(
    model: str,
    api_key: str,
    endpoint: str,
    timeout: float = 60.0,
) -> CanopyAzureCompatLLM:
    """Return a LlamaIndex FunctionCallingLLM for an Azure openai-compat endpoint.

    endpoint must be the full base URL, e.g.:
        https://<resource>.services.ai.azure.com/api/projects/<id>/openai/v1/

    LlamaIndex appends /chat/completions; no further path manipulation is done.
    """
    return CanopyAzureCompatLLM(
        model=model,
        api_key=api_key,
        api_base=endpoint,
        max_tokens=4096,
        temperature=0.0,
        timeout=timeout,
    )


class CanopyOpenAIStandardLLM(_LlamaOpenAI):
    """OpenAI-SDK-compatible LLM for third-party hosts (e.g. NVIDIA's
    integrate.api.nvidia.com) that speak the standard wire format but serve
    model names OpenAI's SDK doesn't recognise.

    Only the metadata fix from CanopyAzureCompatLLM applies here — see that
    class's docstring for why max_completion_tokens (Azure-only) is NOT
    inherited. LlamaIndex's OpenAI.metadata calls
    openai_modelname_to_contextsize(), which raises ValueError for any name
    outside OpenAI's own catalogue (confirmed live: "Unknown model
    'deepseek-ai/deepseek-v4-flash-0731'... Please provide a valid OpenAI
    model name" — the same failure mode gpt-5.1-2's Azure deployment name
    hits, just from a different provider). Fixed the same way: override
    metadata with a fixed context window instead of asking LlamaIndex to
    look the name up.
    """

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=_DEFAULT_CONTEXT_WINDOW,
            num_output=self.max_tokens or -1,
            is_chat_model=True,
            is_function_calling_model=True,
            model_name=self.model,
            system_role=MessageRole.SYSTEM,
        )


def build_openai_standard_llm(
    model: str,
    api_key: str,
    endpoint: str,
    timeout: float = 60.0,
) -> CanopyOpenAIStandardLLM:
    """Return a LlamaIndex FunctionCallingLLM for a plain OpenAI-SDK-compatible
    endpoint — e.g. NVIDIA's integrate.api.nvidia.com, or any third-party host
    speaking the standard /chat/completions wire format.

    Uses max_tokens (not max_completion_tokens) — that Azure-only quirk from
    CanopyAzureCompatLLM does not apply to third-party hosts, which is
    exactly why this is a separate class rather than a reused one.
    """
    return CanopyOpenAIStandardLLM(
        model=model,
        api_key=api_key,
        api_base=endpoint,
        max_tokens=4096,
        temperature=0.0,
        timeout=timeout,
        is_function_calling_model=True,
    )
