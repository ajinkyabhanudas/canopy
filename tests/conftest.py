"""Global test fixtures shared across all test modules."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Redirect CANOPY_DATA_DIR to a per-test temp directory.

    Prevents test runs from polluting the real history and cache files
    in ~/.canopy or /data, and prevents eval artifacts from appearing
    in the UI history sidebar on first open.
    """
    monkeypatch.setenv("CANOPY_DATA_DIR", str(tmp_path))
