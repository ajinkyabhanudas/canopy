"""
db/connection.py
-----------------
Pooled psycopg2 connections built from the individual PG_* env vars defined
in config.py. See DECISIONS.md O2: a per-query connection was sound at
single-scientist load; the pool below implements that entry's own documented
revisit trigger (> 20 concurrent queries) ahead of need.

Callers use get_connection()/release_connection() as a pair (mirroring the
psycopg2.pool API) rather than closing the connection directly.
"""

from __future__ import annotations

import threading

import psycopg2
import psycopg2.extensions
import psycopg2.pool

from ..config import get_db_config, get_db_pool_size

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


class PoolExhaustedError(RuntimeError):
    """Raised when the connection pool has no connections available."""


def _build_pool() -> psycopg2.pool.ThreadedConnectionPool:
    cfg = get_db_config()
    if not cfg.is_configured():
        missing = [
            name
            for name, val in [
                ("PG_HOST", cfg.host),
                ("PG_PORT", cfg.port),
                ("PG_DBNAME", cfg.dbname),
                ("PG_USER", cfg.user),
                ("PG_PASSWORD", cfg.password),
            ]
            if not val
        ]
        raise ValueError(f"Missing required environment variables: {missing}")

    pool_size = get_db_pool_size()
    return psycopg2.pool.ThreadedConnectionPool(
        1,
        pool_size,
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
        options="-c statement_timeout=30000",  # 30 s — bounds runaway SQL
    )


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


def get_connection() -> psycopg2.extensions.connection:
    """Return a pooled, readonly psycopg2 connection.

    Raises:
        ValueError: if any required PG_* variable is missing.
        PoolExhaustedError: if the pool is at capacity (CANOPY_DB_POOL_SIZE).
    """
    pool = _get_pool()
    try:
        conn = pool.getconn()
    except psycopg2.pool.PoolError as exc:
        raise PoolExhaustedError(
            "Connection pool exhausted — too many concurrent queries. Try again shortly."
        ) from exc
    conn.set_session(readonly=True)
    return conn


def release_connection(conn: psycopg2.extensions.connection) -> None:
    """Return a connection to the pool instead of closing it."""
    _get_pool().putconn(conn)


def reset_pool() -> None:
    """Close and discard the pool. Used by tests to force a fresh pool per test."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
