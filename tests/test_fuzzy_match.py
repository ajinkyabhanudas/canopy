"""
Tests for canopy.query.fuzzy_match — no live database required.

execute_query() is monkeypatched throughout so these run in any environment.
"""

from __future__ import annotations

import pytest

from canopy.query.executor import QueryResult
from canopy.query.fuzzy_match import _cache, find_candidates

SPECIES_VALUES = ("Grallaria gigantea", "Grallaria ridgelyi", "Tinamus major")
SITE_VALUES = ("Reserva Narupa", "Reserva Buenaventura", "Reserva Antisana")


@pytest.fixture(autouse=True)
def _clear_value_cache():
    """Reset the module-level TTL cache before/after each test — cache state
    would otherwise leak between tests since it's a module singleton."""
    _cache._values.clear()
    _cache._fetched_at.clear()
    yield
    _cache._values.clear()
    _cache._fetched_at.clear()


def _mock_execute_query(values: tuple[str, ...]):
    def _fn(sql: str) -> QueryResult:
        return QueryResult(columns=("v",), rows=tuple((v,) for v in values), row_count=len(values))
    return _fn


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------


def test_no_registered_column_returns_none():
    sql = "SELECT * FROM detections WHERE validation_status = 'validated_true'"
    assert find_candidates(sql) is None


def test_unregistered_literal_shape_returns_none(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    # References the column name but not in a recognizable ILIKE/= literal shape.
    sql = "SELECT scientific_name FROM species ORDER BY scientific_name"
    assert find_candidates(sql) is None


# ---------------------------------------------------------------------------
# Fuzzy matching behavior
# ---------------------------------------------------------------------------


def test_close_typo_on_species_name_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    match = find_candidates(sql)
    assert match is not None
    assert match.literal == "Gralari gigantae"
    assert "Grallaria gigantea" in match.candidates


def test_exact_equality_literal_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name = 'Tinamus mayor'"
    match = find_candidates(sql)
    assert match is not None
    assert match.literal == "Tinamus mayor"
    assert "Tinamus major" in match.candidates


def test_site_name_column_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SITE_VALUES)
    )
    sql = "SELECT * FROM sites WHERE name ILIKE '%Buenaventuraa%'"
    match = find_candidates(sql)
    assert match is not None
    assert "Reserva Buenaventura" in match.candidates


def test_aliased_column_reference_resolves(monkeypatch):
    """Table alias prefix (sp.scientific_name) must not block extraction."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species sp WHERE sp.scientific_name ILIKE '%Gralari gigantae%'"
    match = find_candidates(sql)
    assert match is not None
    assert "Grallaria gigantea" in match.candidates


def test_genuinely_absent_value_returns_none(monkeypatch):
    """A name unrelated to anything real should not produce noisy suggestions."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Zzyzx qqqqq%'"
    assert find_candidates(sql) is None


def test_threshold_excludes_weak_matches(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    assert find_candidates(sql, threshold=0.999) is None


def test_limit_caps_candidate_count(monkeypatch):
    values = ("Grallaria gigantea", "Grallaria ridgelyi", "Grallaria excelsa", "Grallaria milleri")
    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _mock_execute_query(values))
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari%'"
    match = find_candidates(sql, threshold=0.3, limit=2)
    assert match is not None
    assert len(match.candidates) <= 2


def test_empty_value_list_returns_none(monkeypatch):
    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _mock_execute_query(()))
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    assert find_candidates(sql) is None


def test_lookup_failure_returns_none_not_raises(monkeypatch):
    """A DB error while fetching the value list must not break the agent loop."""
    def _raise(sql):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _raise)
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    assert find_candidates(sql) is None


# ---------------------------------------------------------------------------
# TTL cache behavior
# ---------------------------------------------------------------------------


def test_value_cache_reused_within_ttl(monkeypatch):
    mock_fn = _mock_execute_query(SPECIES_VALUES)
    call_count = {"n": 0}

    def _counting_fn(sql):
        call_count["n"] += 1
        return mock_fn(sql)

    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _counting_fn)
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    find_candidates(sql)
    find_candidates(sql)
    assert call_count["n"] == 1


def test_value_cache_refetches_after_ttl_expiry(monkeypatch):
    mock_fn = _mock_execute_query(SPECIES_VALUES)
    call_count = {"n": 0}

    def _counting_fn(sql):
        call_count["n"] += 1
        return mock_fn(sql)

    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _counting_fn)
    monkeypatch.setattr("canopy.query.fuzzy_match._ttl_seconds", lambda: 0)
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    find_candidates(sql)
    find_candidates(sql)
    assert call_count["n"] == 2
