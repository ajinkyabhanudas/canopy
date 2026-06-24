"""
Confirms the model-swap story is real: changing MODEL_BACKEND to an
unregistered name fails clearly, and the registry only knows about
backends that are actually implemented.
"""

import pytest

from canopy.models.registry import _BACKENDS, get_model_client


def test_registry_lists_anthropic():
    assert "anthropic" in _BACKENDS


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "not_a_real_backend")
    with pytest.raises(ValueError):
        get_model_client()
