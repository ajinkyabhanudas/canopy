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


@pytest.fixture(autouse=True)
def _reset_pool():
    """Ensure a fresh pool per test — env vars differ across tests, and the
    module-level pool singleton would otherwise cache the first test's config."""
    from canopy.db.connection import reset_pool

    reset_pool()
    yield
    reset_pool()


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


def test_connection_is_set_readonly(monkeypatch):
    """get_connection() must set readonly=True on the connection (belt-and-suspenders)."""
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv("PG_DBNAME", "testdb")
    monkeypatch.setenv("PG_USER", "user")
    monkeypatch.setenv("PG_PASSWORD", "pass")

    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    with patch("canopy.db.connection.psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool):
        from canopy.db.connection import get_connection

        conn = get_connection()

    mock_conn.set_session.assert_called_once_with(readonly=True)
    assert conn is mock_conn


def test_db_config_is_configured_true_when_all_set(monkeypatch):
    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv("PG_DBNAME", "testdb")
    monkeypatch.setenv("PG_USER", "user")
    monkeypatch.setenv("PG_PASSWORD", "pass")

    from canopy.config import get_db_config

    assert get_db_config().is_configured() is True


def test_pool_exhaustion_raises_pool_exhausted_error(monkeypatch):
    """A getconn() failure from the pool must surface as PoolExhaustedError,
    not an unhandled psycopg2.pool.PoolError — see executor.py's DatabaseBusyError
    translation and ui/app.py's friendly "system busy" message."""
    from unittest.mock import MagicMock, patch

    import psycopg2.pool

    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv("PG_DBNAME", "testdb")
    monkeypatch.setenv("PG_USER", "user")
    monkeypatch.setenv("PG_PASSWORD", "pass")

    mock_pool = MagicMock()
    mock_pool.getconn.side_effect = psycopg2.pool.PoolError("connection pool exhausted")
    with patch("canopy.db.connection.psycopg2.pool.ThreadedConnectionPool", return_value=mock_pool):
        from canopy.db.connection import PoolExhaustedError, get_connection

        with pytest.raises(PoolExhaustedError):
            get_connection()


def test_pool_size_env_var(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("PG_HOST", "localhost")
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv("PG_DBNAME", "testdb")
    monkeypatch.setenv("PG_USER", "user")
    monkeypatch.setenv("PG_PASSWORD", "pass")
    monkeypatch.setenv("CANOPY_DB_POOL_SIZE", "25")

    with patch("canopy.db.connection.psycopg2.pool.ThreadedConnectionPool") as mock_pool_cls:
        from canopy.db.connection import get_connection

        mock_pool_cls.return_value.getconn.return_value.set_session.return_value = None
        get_connection()

    assert mock_pool_cls.call_args[0] == (1, 25)


# ---------------------------------------------------------------------------
# Integration test — skipped when credentials are absent
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _db_configured, reason="PG_* variables not set")
def test_live_connection():
    from canopy.db.connection import get_connection, release_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            result = cur.fetchone()
        assert result == (1,)
    finally:
        release_connection(conn)
