"""Global test fixtures shared across all test modules."""

from __future__ import annotations

import pytest

from canopy.config import ModelConnection


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect CANOPY_DATA_DIR to a per-test temp directory.

    Prevents test runs from polluting the real history and cache files
    in ~/.canopy or /data, and prevents eval artifacts from appearing
    in the UI history sidebar on first open.
    """
    monkeypatch.setenv("CANOPY_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _stub_active_connection(monkeypatch):
    """Stub get_active_connection() so tests never read models.yaml or need API keys.

    Returns a minimal azure-shaped ModelConnection. Tests that need a
    different connection can override this fixture or patch the function
    themselves — this fixture only runs if nothing else has already patched it.
    """
    _stub = ModelConnection(
        id="test-conn",
        backend="azure",
        api_key="test-key",
        models=["test-model"],
        endpoint="https://test.openai.azure.com/openai/v1/",
        timeout=60.0,
    )
    monkeypatch.setattr("canopy.query.loop.get_active_connection", lambda **_kw: _stub)
    monkeypatch.setattr("canopy.config.get_active_connection", lambda **_kw: _stub)
