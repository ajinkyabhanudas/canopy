"""
models/failures.py
-------------------
Classify a model-call exception as retryable (an outage symptom another
connection might not share) or not (a defect/config issue every connection
would hit identically).

Used by query/loop.py's fallback wrapper to decide whether a failed query
is worth retrying against a different models.yaml connection, or whether
retrying would just repeat the same failure on another backend's dime.
"""

from __future__ import annotations

import openai

from canopy.models.azure_responses_llm import ResponsesAPIError

# The openai Python SDK backs both api_style: openai-compat and
# api_style: openai-standard (see llamaindex_compat.py) — these are
# connection/outage symptoms, not something a different model would also
# raise for the same question.
_RETRYABLE_OPENAI_ERRORS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,  # 5xx
)


def is_retryable_model_error(error: Exception) -> bool:
    """True if `error` looks like a transient outage worth retrying elsewhere.

    False for anything that looks like it would recur identically on a
    different connection (bad input, auth misconfiguration, a non-5xx HTTP
    error) — retrying those just adds latency and cost for no chance of a
    different outcome.
    """
    if isinstance(error, ResponsesAPIError):
        return error.retryable
    if isinstance(error, _RETRYABLE_OPENAI_ERRORS):
        return True
    if isinstance(error, openai.APIStatusError):
        return error.status_code >= 500
    return False
