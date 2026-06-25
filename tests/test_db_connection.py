"""
Tests for the database connection layer.

Unit tests run without any credentials. The integration test is skipped
automatically when PG_* variables are absent — it is not a failure.
"""

from __future__ import annotations

import os

import pytest

_DB_VARS = ("PG_HOST", "PG_PORT", "PG_DBNAME", "PG_USER", "PG_PASSWORD")
_db_configured = all(os.environ.get(v) for v in _DB_VARS)


# ---------------------------------------------------------------------------
# Unit tests — no credentials required
# ---------------------------------------------------------------------------


def test_missing_env_vars_raise(monkeypatch):
    for var in _DB_VARS:
        monkeypatch.delenv(var, raising=False)

    from canopy.db.connection import get_connection

    with pytest.raises(ValueError, match="Missing required environment variables"):
        get_connection()


def test_partial_env_vars_raise(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    for var in ("PG_PORT", "PG_DBNAME", "PG_USER", "PG_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    from canopy.db.connection import get_connection

    with pytest.raises(ValueError):
        get_connection()


def test_db_config_is_configured_false_when_empty(monkeypatch):
    for var in _DB_VARS:
        monkeypatch.delenv(var, raising=False)

    from canopy.config import get_db_config

    assert get_db_config().is_configured() is False


def test_db_config_is_configured_true_when_all_set(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv("PG_DBNAME", "testdb")
    monkeypatch.setenv("PG_USER", "user")
    monkeypatch.setenv("PG_PASSWORD", "pass")

    from canopy.config import get_db_config

    assert get_db_config().is_configured() is True


# ---------------------------------------------------------------------------
# Integration test — skipped when credentials are absent
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _db_configured, reason="PG_* variables not set")
def test_live_connection():
    from canopy.db.connection import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            result = cur.fetchone()
        assert result == (1,)
    finally:
        conn.close()
