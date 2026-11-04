"""Unit tests for canopy.models.failures — model-error retryability classification."""

from __future__ import annotations

import httpx
import openai
import pytest

from canopy.models.azure_responses_llm import ResponsesAPIError
from canopy.models.failures import is_retryable_model_error


def _api_status_error(status_code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request, json={"error": {}})
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize("retryable", [True, False])
def test_responses_api_error_uses_its_own_flag(retryable: bool):
    error = ResponsesAPIError("boom", retryable=retryable)
    assert is_retryable_model_error(error) is retryable


def test_openai_connection_error_is_retryable():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    error = openai.APIConnectionError(request=request)
    assert is_retryable_model_error(error) is True


def test_openai_rate_limit_is_retryable():
    # RateLimitError is its own subclass, not a plain APIStatusError(429) —
    # construct it directly to match what the SDK actually raises.
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {}})
    error = openai.RateLimitError("rate limited", response=response, body=None)
    assert is_retryable_model_error(error) is True


def test_openai_internal_server_error_is_retryable():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(500, request=request, json={"error": {}})
    error = openai.InternalServerError("server error", response=response, body=None)
    assert is_retryable_model_error(error) is True


def test_openai_bad_request_is_not_retryable():
    """A 400 would fail identically on another connection — not an outage."""
    assert is_retryable_model_error(_api_status_error(400)) is False


def test_generic_api_status_error_5xx_is_retryable():
    assert is_retryable_model_error(_api_status_error(503)) is True


def test_unrelated_error_is_not_retryable():
    assert is_retryable_model_error(ValueError("bad decision shape")) is False
