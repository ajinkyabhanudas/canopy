"""SQL execution layer — validates and runs read-only queries against the DB."""

from __future__ import annotations

import re
from dataclasses import dataclass

from canopy.db import get_connection

# Matches -- line comments and /* block comments */ so we can strip them
# before checking the first meaningful SQL token.
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class SQLGuardError(ValueError):
    """Raised when a generated query fails the SELECT-only guard.

    Carries the original SQL so callers can surface it in the UI.
    """

    def __init__(self, message: str, sql: str) -> None:
        super().__init__(message)
        self.sql = sql
        self.operation = _first_token(sql).upper() or "UNKNOWN"


@dataclass(frozen=True)
class QueryResult:
    """Immutable result of a single SQL query execution."""

    columns: list[str]
    rows: list[tuple]
    row_count: int


def _first_token(sql: str) -> str:
    """Return the first meaningful keyword of a SQL string, lower-cased.

    Strips -- and /* */ comments before tokenising so the guard is not
    fooled by comment-prefixed queries like '-- note\\nSELECT ...'.
    """
    cleaned = _COMMENT_RE.sub("", sql).strip()
    parts = cleaned.split()
    return parts[0].casefold() if parts else ""


def execute_query(sql: str) -> QueryResult:
    """Execute a read-only SQL SELECT query and return the result.

    Args:
        sql: A PostgreSQL SELECT or WITH…SELECT statement.

    Returns:
        QueryResult with column names, all rows, and total row count.

    Raises:
        SQLGuardError: If the query is not a SELECT/CTE statement.
        psycopg2.Error: If the database raises an error during execution.
    """
    stripped = sql.strip()
    if not stripped:
        raise SQLGuardError("Only SELECT queries are permitted", sql=stripped)
    # Allow plain SELECT and CTEs (WITH ... SELECT); strip comments first.
    if _first_token(stripped) not in ("select", "with"):
        raise SQLGuardError("Only SELECT queries are permitted", sql=stripped)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows))
    finally:
        conn.close()
