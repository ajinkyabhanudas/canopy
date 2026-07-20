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


def _mock_execute_query_by_table(species=(), sites=()):
    """Route DISTINCT lookups to the right value list by table name in the SQL."""
    def _fn(sql: str) -> QueryResult:
        values = species if "species" in sql else sites
        return QueryResult(columns=("v",), rows=tuple((v,) for v in values), row_count=len(values))
    return _fn


def _mock_execute_query(values: tuple[str, ...]):
    def _fn(sql: str) -> QueryResult:
        return QueryResult(columns=("v",), rows=tuple((v,) for v in values), row_count=len(values))
    return _fn


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------


def test_no_registered_column_returns_empty():
    sql = "SELECT * FROM detections WHERE validation_status = 'validated_true'"
    assert find_candidates(sql) == ()


def test_unregistered_literal_shape_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    # References the column name but not in a recognizable ILIKE/= literal shape.
    sql = "SELECT scientific_name FROM species ORDER BY scientific_name"
    assert find_candidates(sql) == ()


# ---------------------------------------------------------------------------
# Fuzzy matching behavior
# ---------------------------------------------------------------------------


def test_close_typo_on_species_name_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert matches[0].literal == "Gralari gigantae"
    assert matches[0].label_key == "species"
    assert "Grallaria gigantea" in matches[0].candidates


def test_exact_equality_literal_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name = 'Tinamus mayor'"
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert matches[0].literal == "Tinamus mayor"
    assert "Tinamus major" in matches[0].candidates


def test_site_name_column_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SITE_VALUES)
    )
    sql = "SELECT * FROM sites WHERE name ILIKE '%Buenaventuraa%'"
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert matches[0].label_key == "site"
    assert "Reserva Buenaventura" in matches[0].candidates


def test_aliased_column_reference_resolves(monkeypatch):
    """Table alias prefix (sp.scientific_name) must not block extraction."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species sp WHERE sp.scientific_name ILIKE '%Gralari gigantae%'"
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert "Grallaria gigantea" in matches[0].candidates


def test_genuinely_absent_value_returns_empty(monkeypatch):
    """A name unrelated to anything real should not produce noisy suggestions."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Zzyzx qqqqq%'"
    assert find_candidates(sql) == ()


def test_threshold_excludes_weak_matches(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    assert find_candidates(sql, threshold=0.999) == ()


def test_limit_caps_candidate_count(monkeypatch):
    values = ("Grallaria gigantea", "Grallaria ridgelyi", "Grallaria excelsa", "Grallaria milleri")
    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _mock_execute_query(values))
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari%'"
    matches = find_candidates(sql, threshold=0.3, limit=2)
    assert len(matches) == 1
    assert len(matches[0].candidates) <= 2


def test_empty_value_list_returns_empty(monkeypatch):
    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _mock_execute_query(()))
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    assert find_candidates(sql) == ()


def test_lookup_failure_returns_empty_not_raises(monkeypatch):
    """A DB error while fetching the value list must not break the agent loop."""
    def _raise(sql):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _raise)
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'"
    assert find_candidates(sql) == ()


# ---------------------------------------------------------------------------
# Multiple simultaneous typos across different columns
# ---------------------------------------------------------------------------


def test_two_typos_in_different_columns_both_resolve(monkeypatch):
    """A query with typos in BOTH species AND site name must surface a match
    for each column, not just the first one checked — silently dropping the
    second typo would leave the user unable to fix half their question."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query",
        _mock_execute_query_by_table(species=SPECIES_VALUES, sites=SITE_VALUES),
    )
    sql = (
        "SELECT * FROM species sp JOIN sites si ON sp.site_id = si.id "
        "WHERE sp.scientific_name ILIKE '%Gralari gigantae%' "
        "AND si.name ILIKE '%Buenaventuraa%'"
    )
    matches = find_candidates(sql)
    assert len(matches) == 2

    by_label = {m.label_key: m for m in matches}
    assert "species" in by_label
    assert "site" in by_label
    assert by_label["species"].literal == "Gralari gigantae"
    assert "Grallaria gigantea" in by_label["species"].candidates
    assert by_label["site"].literal == "Buenaventuraa"
    assert "Reserva Buenaventura" in by_label["site"].candidates


def test_two_typos_one_resolvable_one_not_returns_only_the_resolvable_one(monkeypatch):
    """If only one of two typo'd columns has a close-enough match, only that
    one should be returned — not padded with a None or empty entry."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query",
        _mock_execute_query_by_table(species=SPECIES_VALUES, sites=SITE_VALUES),
    )
    sql = (
        "SELECT * FROM species sp JOIN sites si ON sp.site_id = si.id "
        "WHERE sp.scientific_name ILIKE '%Gralari gigantae%' "
        "AND si.name ILIKE '%Zzyzx Nonexistent%'"
    )
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert matches[0].label_key == "species"


def test_lookup_failure_on_one_column_does_not_block_the_other(monkeypatch):
    """A DB error fetching one column's value list should not prevent
    resolving a different column that succeeds in the same query."""
    def _fn(sql: str) -> QueryResult:
        if "species" in sql:
            raise RuntimeError("db unavailable for species")
        return QueryResult(
            columns=("v",), rows=tuple((v,) for v in SITE_VALUES), row_count=len(SITE_VALUES)
        )

    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _fn)
    sql = (
        "SELECT * FROM species sp JOIN sites si ON sp.site_id = si.id "
        "WHERE sp.scientific_name ILIKE '%Gralari gigantae%' "
        "AND si.name ILIKE '%Buenaventuraa%'"
    )
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert matches[0].label_key == "site"


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
