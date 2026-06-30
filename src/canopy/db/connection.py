"""
db/connection.py
-----------------
Factory for a psycopg2 connection built from the individual PG_* env vars
defined in config.py. Callers are responsible for closing the connection.
"""

from __future__ import annotations

import psycopg2

from ..config import get_db_config


def get_connection() -> psycopg2.extensions.connection:
    """Return a new psycopg2 connection.

    Raises ValueError if any required PG_* variable is missing.
    """
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

    conn = psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
        options="-c statement_timeout=30000",  # 30 s — bounds runaway SQL
    )
    conn.set_session(readonly=True)
    return conn
