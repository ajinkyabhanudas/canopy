"""SQL execution layer — validates and runs read-only queries against the DB."""

from __future__ import annotations

from dataclasses import dataclass

from canopy.db import get_connection


@dataclass(frozen=True)
class QueryResult:
    """Immutable result of a single SQL query execution."""

    columns: list[str]
    rows: list[tuple]
    row_count: int


def execute_query(sql: str) -> QueryResult:
    """Execute a read-only SQL SELECT query and return the result.

    Args:
        sql: A PostgreSQL SELECT statement.

    Returns:
        QueryResult with column names, all rows, and total row count.

    Raises:
        ValueError: If the query is not a SELECT statement.
        psycopg2.Error: If the database raises an error during execution.
    """
    stripped = sql.strip()
    if not stripped:
        raise ValueError("Only SELECT queries are permitted")
    first_token = stripped.split()[0].casefold()
    # Allow CTEs: WITH ... AS (...) SELECT ...
    if first_token not in ("select", "with"):
        raise ValueError("Only SELECT queries are permitted")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return QueryResult(columns=columns, rows=rows, row_count=len(rows))
    finally:
        conn.close()
