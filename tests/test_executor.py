"""
Tests for canopy.query.executor — no live database required.

get_connection() is monkeypatched throughout so these run in any environment.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from canopy.query.executor import QueryResult, SQLGuardError, execute_query

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_conn():
    """A mock psycopg2 connection with a pre-configured cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    # Default: two columns, two rows — overridden per-test as needed.
    cursor.description = [("species",), ("site",)]
    cursor.fetchall.return_value = [
        ("Grallaria gigantea", "Reserva Narupa"),
        ("Tinamus major", "Reserva Antisana"),
    ]
    return conn


# ---------------------------------------------------------------------------
# Guard: reject non-SELECT statements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO species (scientific_name) VALUES ('x')",
        "UPDATE detections SET validation_status = 'validated_true'",
        "DELETE FROM detections WHERE id = 1",
        "DROP TABLE species",
        "TRUNCATE detections",
        "ALTER TABLE species ADD COLUMN foo text",
        "CREATE TABLE foo (id integer)",
        "",
        "   ",
        "\t\n",
    ],
)
def test_non_select_raises(sql, monkeypatch, mock_conn):
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    with pytest.raises(SQLGuardError, match="Only SELECT queries are permitted"):
        execute_query(sql)


def test_non_select_does_not_open_connection(monkeypatch, mock_conn):
    """The guard fires before get_connection() is called."""
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    with pytest.raises(SQLGuardError):
        execute_query("DROP TABLE species")
    mock_conn.cursor.assert_not_called()


def test_guard_error_carries_sql(monkeypatch, mock_conn):
    """SQLGuardError.sql holds the rejected query for UI surfacing."""
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    bad_sql = "DROP TABLE species"
    with pytest.raises(SQLGuardError) as exc_info:
        execute_query(bad_sql)
    assert exc_info.value.sql == bad_sql


@pytest.mark.parametrize("sql,expected_op", [
    ("DROP TABLE species", "DROP"),
    ("DELETE FROM detections WHERE id = 1", "DELETE"),
    ("UPDATE species SET name = 'x'", "UPDATE"),
    ("INSERT INTO species VALUES (1)", "INSERT"),
    ("TRUNCATE detections", "TRUNCATE"),
])
def test_guard_error_operation_uppercased(sql, expected_op, monkeypatch, mock_conn):
    """SQLGuardError.operation is the uppercased first SQL keyword of the blocked query."""
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    with pytest.raises(SQLGuardError) as exc_info:
        execute_query(sql)
    assert exc_info.value.operation == expected_op


# ---------------------------------------------------------------------------
# Guard: SELECT passes in all expected forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select 1",
        "Select 1",
        "   SELECT id FROM species",
        "\t\nSELECT id FROM species",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
        "with cte AS (SELECT id FROM species) SELECT * FROM cte",
        "-- find new bird species\nSELECT * FROM species",
        "-- note\nWITH cte AS (SELECT 1) SELECT * FROM cte",
        "/* block comment */\nSELECT 1",
        "/* multi\n   line */\nSELECT id FROM species",
    ],
)
def test_select_passes_guard(sql, monkeypatch, mock_conn):
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    result = execute_query(sql)
    assert isinstance(result, QueryResult)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_result_shape(monkeypatch, mock_conn):
    mock_conn.cursor.return_value.description = [("col_a",), ("col_b",)]
    mock_conn.cursor.return_value.fetchall.return_value = [
        ("a1", "b1"),
        ("a2", "b2"),
        ("a3", "b3"),
    ]
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    result = execute_query("SELECT col_a, col_b FROM detections")

    assert result.columns == ("col_a", "col_b")
    assert result.rows == (("a1", "b1"), ("a2", "b2"), ("a3", "b3"))
    assert result.row_count == 3


def test_empty_result(monkeypatch, mock_conn):
    mock_conn.cursor.return_value.description = [("id",)]
    mock_conn.cursor.return_value.fetchall.return_value = []
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    result = execute_query("SELECT id FROM species WHERE id = -1")

    assert result.row_count == 0
    assert result.rows == ()
    assert result.columns == ("id",)


def test_result_is_immutable(monkeypatch, mock_conn):
    """QueryResult is a frozen dataclass — fields cannot be reassigned."""
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    result = execute_query("SELECT species, site FROM detections")
    with pytest.raises(Exception):  # FrozenInstanceError
        result.row_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def test_connection_closed_on_success(monkeypatch, mock_conn):
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)
    execute_query("SELECT 1")
    mock_conn.close.assert_called_once()


def test_connection_closed_on_db_error(monkeypatch, mock_conn):
    """try/finally must close the connection even when execute() raises."""
    mock_conn.cursor.return_value.execute.side_effect = Exception("db error")
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    with pytest.raises(Exception, match="db error"):
        execute_query("SELECT 1")

    mock_conn.close.assert_called_once()


def test_none_description_raises_guard_error(monkeypatch, mock_conn):
    """cursor.description is None after non-result-producing query → SQLGuardError."""
    mock_conn.cursor.return_value.description = None
    monkeypatch.setattr("canopy.query.executor.get_connection", lambda: mock_conn)

    with pytest.raises(SQLGuardError, match="no result set"):
        execute_query("SELECT 1")
