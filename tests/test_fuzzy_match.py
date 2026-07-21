"""
Tests for canopy.query.fuzzy_match — no live database required.

execute_query() is monkeypatched throughout so these run in any environment.
"""

from __future__ import annotations

import pytest

from canopy.query.executor import QueryResult
from canopy.query.fuzzy_match import _cache, effective_count, find_candidates, is_empty_result

SPECIES_VALUES = ("Grallaria gigantea", "Grallaria ridgelyi", "Tinamus major")
SITE_VALUES = ("Reserva Narupa", "Reserva Buenaventura", "Reserva Antisana")
# Real near-duplicate found live in the DB: an accent divergence between two
# genuinely distinct entries ("Wamani" vs "Wamaní") — exactly the kind of
# typo-shaped inconsistency this feature exists to help with.
MANAGEMENT_UNIT_VALUES = ("Wamani", "Wamaní", "Yunguilla", "Buenaventura Corridor")


@pytest.fixture(autouse=True)
def _clear_value_cache():
    """Reset the module-level TTL cache before/after each test — cache state
    would otherwise leak between tests since it's a module singleton."""
    _cache._values.clear()
    _cache._fetched_at.clear()
    yield
    _cache._values.clear()
    _cache._fetched_at.clear()


def _mock_execute_query_by_table(species=(), sites=(), management_units=()):
    """Route DISTINCT lookups to the right value list by table name in the SQL."""
    def _fn(sql: str) -> QueryResult:
        if "species" in sql:
            values = species
        elif "sites" in sql:
            values = sites
        else:
            values = management_units
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


def test_whitespace_only_literal_returns_empty(monkeypatch):
    """A literal that matches the ILIKE pattern but strips down to nothing
    (e.g. '%   %') must be skipped, not treated as a real mistyped value."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(SPECIES_VALUES)
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%   %'"
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


def test_partial_epithet_only_literal_resolves(monkeypatch):
    """LLM-generated SQL often narrows ILIKE to just the species epithet
    ("ridgeleyi") rather than the full binomial ("Grallaria ridgelyi") —
    observed against a live model during Docker verification. Comparing
    that fragment only against the full candidate string scores well below
    threshold (0.59) even for an exact match; per-word scoring must catch it."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query",
        _mock_execute_query(("Grallaria ridgelyi", "Grallaria gigantea", "Tinamus major")),
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%ridgeleyi%'"
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert "Grallaria ridgelyi" in matches[0].candidates


def test_partial_genus_only_literal_resolves(monkeypatch):
    """Symmetric case to the epithet-only test: a mistyped genus alone
    (first word) must also resolve, not just a mistyped epithet (last word)."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query",
        _mock_execute_query(("Grallaria ridgelyi", "Grallaria gigantea", "Tinamus major")),
    )
    sql = "SELECT * FROM species WHERE scientific_name ILIKE '%Gralaria%'"
    matches = find_candidates(sql)
    assert len(matches) >= 1
    assert any("Grallaria" in c for m in matches for c in m.candidates)


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


def test_management_unit_column_resolves(monkeypatch):
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query", _mock_execute_query(MANAGEMENT_UNIT_VALUES)
    )
    sql = "SELECT * FROM detections WHERE management_unit ILIKE '%Waman%'"
    matches = find_candidates(sql)
    assert len(matches) == 1
    assert matches[0].label_key == "management_unit"
    # Real near-duplicate pair in the data — both should surface as candidates.
    assert "Wamani" in matches[0].candidates
    assert "Wamaní" in matches[0].candidates


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


def test_three_typos_across_three_columns_all_resolve(monkeypatch):
    """Extends the two-column case to all three registered columns at once —
    a query mistyping species, site, AND management_unit simultaneously must
    surface all three, not just the first two checked."""
    monkeypatch.setattr(
        "canopy.query.fuzzy_match.execute_query",
        _mock_execute_query_by_table(
            species=SPECIES_VALUES, sites=SITE_VALUES, management_units=MANAGEMENT_UNIT_VALUES
        ),
    )
    sql = (
        "SELECT * FROM species sp "
        "JOIN sites si ON sp.site_id = si.id "
        "JOIN detections d ON d.species_id = sp.id "
        "WHERE sp.scientific_name ILIKE '%Gralari gigantae%' "
        "AND si.name ILIKE '%Buenaventuraa%' "
        "AND d.management_unit ILIKE '%Waman%'"
    )
    matches = find_candidates(sql)
    assert len(matches) == 3

    by_label = {m.label_key: m for m in matches}
    assert set(by_label) == {"species", "site", "management_unit"}
    assert "Grallaria gigantea" in by_label["species"].candidates
    assert "Reserva Buenaventura" in by_label["site"].candidates
    assert "Wamani" in by_label["management_unit"].candidates


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


# ---------------------------------------------------------------------------
# is_empty_result — COUNT(*)/aggregate query detection
#
# Found via live Docker verification against a real LLM: "how many detections
# of X" is the most common phrasing for exactly the questions this module
# helps with, and the model reliably writes SELECT COUNT(*) for it. Such a
# query always returns row_count=1 (the single row holding the count value),
# even when the count itself is 0 — so row_count==0 alone never fires for the
# single most common real-world trigger shape.
# ---------------------------------------------------------------------------


def test_plain_zero_row_result_is_empty():
    result = QueryResult(columns=("scientific_name",), rows=(), row_count=0)
    sql = "SELECT scientific_name FROM species WHERE scientific_name = 'Nonexistent'"
    assert is_empty_result(sql, result) is True


def test_plain_nonzero_row_result_is_not_empty():
    result = QueryResult(columns=("scientific_name",), rows=(("Grallaria gigantea",),), row_count=1)
    sql = "SELECT scientific_name FROM species WHERE scientific_name = 'Grallaria gigantea'"
    assert is_empty_result(sql, result) is False


def test_count_star_with_zero_value_is_empty():
    """The exact shape observed live: COUNT(*) returning a single row with value 0."""
    result = QueryResult(columns=("approved_detections",), rows=((0,),), row_count=1)
    sql = (
        "SELECT COUNT(*) AS approved_detections FROM detections d "
        "JOIN species s ON d.species_id = s.id "
        "WHERE s.scientific_name = 'Cercomacroides tyranina' AND d.validation_status = 'approved'"
    )
    assert is_empty_result(sql, result) is True


def test_count_star_with_nonzero_value_is_not_empty():
    result = QueryResult(columns=("n",), rows=((100,),), row_count=1)
    sql = "SELECT COUNT(*) AS n FROM detections WHERE species_id = 12"
    assert is_empty_result(sql, result) is False


def test_lowercase_count_is_detected():
    result = QueryResult(columns=("n",), rows=((0,),), row_count=1)
    sql = "select count(*) as n from detections where species_id = 999"
    assert is_empty_result(sql, result) is True


def test_sum_avg_min_max_with_zero_or_none_detected():
    sql_sum = "SELECT SUM(count) AS total FROM detections WHERE species_id = 999"
    none_result = QueryResult(columns=("total",), rows=((None,),), row_count=1)
    zero_result = QueryResult(columns=("total",), rows=((0,),), row_count=1)
    nonzero_result = QueryResult(columns=("total",), rows=((42,),), row_count=1)
    assert is_empty_result(sql_sum, none_result) is True
    assert is_empty_result(sql_sum, zero_result) is True
    assert is_empty_result(sql_sum, nonzero_result) is False


def test_count_with_group_by_is_not_treated_as_aggregate_empty_check():
    """A GROUP BY query can legitimately return 1 row with count 0 only if
    that group's count is genuinely 0 — but such queries can also return
    multiple rows, so row_count==1 with a GROUP BY present is NOT the same
    structural guarantee as a plain COUNT(*) with no GROUP BY. Excluding
    GROUP BY queries from the aggregate check avoids false-triggering on a
    query shape that already reveals real per-group results via row_count."""
    result = QueryResult(columns=("site", "n"), rows=(("P00001", 0),), row_count=1)
    sql = "SELECT site, COUNT(*) AS n FROM detections GROUP BY site"
    assert is_empty_result(sql, result) is False


def test_two_column_count_result_is_not_treated_as_aggregate_empty_check():
    """len(columns) == 1 is part of the guard — a 2-column result (even with
    COUNT present) isn't the single-value aggregate shape this check targets."""
    result = QueryResult(columns=("species", "n"), rows=(("X", 0),), row_count=1)
    sql = "SELECT species, COUNT(*) AS n FROM detections WHERE species = 'X'"
    assert is_empty_result(sql, result) is False


# ---------------------------------------------------------------------------
# effective_count — the "how many did we actually find" number for status
# messages, distinct from row_count for aggregate queries
# ---------------------------------------------------------------------------


def test_effective_count_plain_query_uses_row_count():
    result = QueryResult(columns=("name",), rows=(("a",), ("b",), ("c",)), row_count=3)
    sql = "SELECT name FROM species"
    assert effective_count(sql, result) == 3


def test_effective_count_count_star_uses_aggregate_value_not_row_count():
    """The bug this exists to fix: COUNT(*) always returns row_count=1, so a
    naive status message would say "Found 1" even when the real count is 100."""
    result = QueryResult(columns=("n",), rows=((100,),), row_count=1)
    sql = "SELECT COUNT(*) AS n FROM detections WHERE species_id = 12"
    assert effective_count(sql, result) == 100


def test_effective_count_count_star_zero():
    result = QueryResult(columns=("n",), rows=((0,),), row_count=1)
    sql = "SELECT COUNT(*) AS n FROM detections WHERE species_id = 999"
    assert effective_count(sql, result) == 0


def test_effective_count_sum_uses_aggregate_value():
    result = QueryResult(columns=("total",), rows=((42,),), row_count=1)
    sql = "SELECT SUM(count) AS total FROM detections WHERE species_id = 999"
    assert effective_count(sql, result) == 42


def test_effective_count_group_by_uses_row_count_not_aggregate_value():
    """A GROUP BY query's row_count (number of groups) is the meaningful
    count here, not any individual group's aggregate value."""
    result = QueryResult(
        columns=("site", "n"), rows=(("P00001", 5), ("P00002", 3)), row_count=2
    )
    sql = "SELECT site, COUNT(*) AS n FROM detections GROUP BY site"
    assert effective_count(sql, result) == 2
