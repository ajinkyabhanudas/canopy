"""Unit tests for canopy.semantic_cache — all 7 gates, using fakeredis and a
stub embedding model so no real Redis or ML model download is required."""

from __future__ import annotations

import fakeredis
import pytest

import canopy.semantic_cache as sc


class _StubEmbedder:
    """Deterministic stub: returns a fixed vector per input, keyed by a
    caller-supplied mapping, so cosine similarity is fully controllable."""

    def __init__(self, vectors: dict[str, list[float]], default=(0.0, 0.0, 1.0)):
        self._vectors = vectors
        self._default = list(default)

    def encode(self, text, normalize_embeddings=True):
        return self._vectors.get(text, self._default)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(sc, "_redis_client", lambda: fake)
    monkeypatch.setenv("CANOPY_SEMANTIC_CACHE_THRESHOLD", "0.9")
    yield fake


def _stub_embedder(monkeypatch, vectors):
    monkeypatch.setattr(sc, "_embedding_model", lambda: _StubEmbedder(vectors))


# ---------------------------------------------------------------------------
# Gate 1 — temporal safety
# ---------------------------------------------------------------------------


def test_gate1_relative_sql_function_always_safe():
    assert sc.sql_is_temporally_safe(
        "what new birds were found today", "SELECT * FROM detections WHERE d = CURRENT_DATE"
    )


def test_gate1_literal_date_with_relative_question_unsafe():
    assert not sc.sql_is_temporally_safe(
        "what new birds were found today", "SELECT * FROM detections WHERE d = '2026-09-02'"
    )


def test_gate1_literal_date_with_non_relative_question_safe():
    """An explicit year/date in the question (not a relative phrase) is fine to cache."""
    assert sc.sql_is_temporally_safe(
        "how many detections in 2023", "SELECT * FROM detections WHERE year = '2023-01-01'"
    )


def test_gate1_write_skips_temporally_unsafe_sql(fake_redis, monkeypatch):
    _stub_embedder(monkeypatch, {})
    sc.write("what birds today", "SELECT * FROM detections WHERE d = '2026-09-02'", "conn-a")
    assert fake_redis.smembers(sc._ids_key("conn-a")) == set()


def test_gate1_write_stores_temporally_safe_sql(fake_redis, monkeypatch):
    _stub_embedder(monkeypatch, {})
    sc.write("what birds today", "SELECT * FROM detections WHERE d = CURRENT_DATE", "conn-a")
    assert len(fake_redis.smembers(sc._ids_key("conn-a"))) == 1


# ---------------------------------------------------------------------------
# Gate 2 — entity verification
# ---------------------------------------------------------------------------


def test_gate2_entity_mismatch_rejects_candidate(fake_redis, monkeypatch):
    """Two structurally-identical questions differing only in species must not
    cross-hit even if their embeddings are (stubbed to be) identical."""

    monkeypatch.setattr(
        "canopy.query.fuzzy_match.FUZZY_COLUMNS",
        {"species.scientific_name": type("S", (), {"table": "species"})()},
        raising=False,
    )

    class _FakeValueCache:
        def get(self, key, spec):
            return ("andean condor", "spectacled bear")

    monkeypatch.setattr(sc, "extract_entities", lambda q: (
        frozenset({"andean condor"}) if "condor" in q.lower() else frozenset({"spectacled bear"})
    ))

    same_vec = [1.0, 0.0, 0.0]
    _stub_embedder(monkeypatch, {
        "how many andean condors at site a": same_vec,
        "how many spectacled bears at site a": same_vec,
    })

    sql = "SELECT count(*) FROM detections WHERE species='condor'"
    sc.write("how many andean condors at site a", sql, "conn-a")
    hit = sc.lookup("how many spectacled bears at site a", "conn-a")
    assert hit is None


def test_gate2_entity_match_accepts_paraphrase(fake_redis, monkeypatch):
    monkeypatch.setattr(sc, "extract_entities", lambda q: frozenset({"andean condor"}))
    same_vec = [1.0, 0.0, 0.0]
    _stub_embedder(monkeypatch, {
        "how many andean condors were seen": same_vec,
        "count of andean condors observed": same_vec,
    })
    sql = "SELECT count(*) FROM detections WHERE species='condor'"
    sc.write("how many andean condors were seen", sql, "conn-a")
    hit = sc.lookup("count of andean condors observed", "conn-a")
    assert hit is not None
    assert hit.sql == sql


# ---------------------------------------------------------------------------
# Gate 3 — guardrail / sensitive-column filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "SELECT latitude FROM detections",
    "SELECT d.longitude FROM detections d",
    "SELECT hashed_password FROM users",
])
def test_gate3_sensitive_column_blocks_write(sql, fake_redis, monkeypatch):
    _stub_embedder(monkeypatch, {})
    sc.write("where are the condors", sql, "conn-a")
    assert fake_redis.smembers(sc._ids_key("conn-a")) == set()


def test_gate3_non_sensitive_sql_not_blocked():
    assert not sc.sql_touches_sensitive_column("SELECT scientific_name FROM species")


# ---------------------------------------------------------------------------
# Gate 4 — connection isolation
# ---------------------------------------------------------------------------


def test_gate4_no_cross_connection_hit(fake_redis, monkeypatch):
    monkeypatch.setattr(sc, "extract_entities", lambda q: frozenset({"condor"}))
    same_vec = [1.0, 0.0, 0.0]
    _stub_embedder(monkeypatch, {"how many condors": same_vec})
    sc.write("how many condors", "SELECT count(*) FROM detections", "conn-a")
    hit = sc.lookup("how many condors", "conn-b")
    assert hit is None


# ---------------------------------------------------------------------------
# Gate 5 — schema-version invalidation
# ---------------------------------------------------------------------------


def test_gate5_schema_version_bump_invalidates_entries(fake_redis, monkeypatch):
    monkeypatch.setattr(sc, "extract_entities", lambda q: frozenset({"condor"}))
    same_vec = [1.0, 0.0, 0.0]
    _stub_embedder(monkeypatch, {"how many condors": same_vec})
    sc.write("how many condors", "SELECT count(*) FROM detections", "conn-a")

    monkeypatch.setattr(sc, "_SCHEMA_VERSION", "v2")
    monkeypatch.setattr(sc, "_KEY_PREFIX", "canopy:semantic:v2:")

    hit = sc.lookup("how many condors", "conn-a")
    assert hit is None


# ---------------------------------------------------------------------------
# Gate 6 — distinct tier-2 messaging
# ---------------------------------------------------------------------------


def test_gate6_notice_differs_from_tier1_wording():
    notice = sc.build_semantic_hit_notice()
    assert "24 hours" not in notice
    assert "current" in notice.lower()


# ---------------------------------------------------------------------------
# Gate 7 — fail-open on storage errors
# ---------------------------------------------------------------------------


def test_gate7_lookup_failure_returns_none(monkeypatch):
    class _Broken:
        def smembers(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(sc, "_redis_client", lambda: _Broken())
    assert sc.lookup("anything", "conn-a") is None


def test_gate7_write_failure_does_not_raise(monkeypatch):
    class _Broken:
        def set(self, *a, **k):
            raise ConnectionError("redis down")

    monkeypatch.setattr(sc, "_redis_client", lambda: _Broken())
    _stub_embedder(monkeypatch, {})
    sc.write("anything", "SELECT 1", "conn-a")  # must not raise


# ---------------------------------------------------------------------------
# Threshold behavior
# ---------------------------------------------------------------------------


def test_below_threshold_candidate_is_not_returned(fake_redis, monkeypatch):
    monkeypatch.setattr(sc, "extract_entities", lambda q: frozenset({"condor"}))
    _stub_embedder(monkeypatch, {
        "how many condors": [1.0, 0.0, 0.0],
        "totally unrelated question": [0.0, 1.0, 0.0],
    })
    sc.write("how many condors", "SELECT count(*) FROM detections", "conn-a")
    hit = sc.lookup("totally unrelated question", "conn-a")
    assert hit is None


# ---------------------------------------------------------------------------
# Real client/model construction (not stubbed via `fake_redis`/`_stub_embedder`)
# ---------------------------------------------------------------------------


def test_redis_client_constructs_real_client_and_caches_singleton(monkeypatch):
    """redis.Redis.from_url() only parses the URL and builds a lazy client
    object — it does not connect — so this needs no live Redis server."""
    import redis as redis_pkg

    monkeypatch.setenv("CANOPY_REDIS_URL", "redis://localhost:6379/0")
    sc._reset_redis_client()
    try:
        client = sc._redis_client()
        assert isinstance(client, redis_pkg.Redis)
        assert sc._redis_client() is client  # cached singleton, not reconstructed
    finally:
        sc._reset_redis_client()


def test_embedding_model_constructs_and_caches_singleton(monkeypatch):
    """Stubs the SentenceTransformer class itself (not the module function) so
    the real _embedding_model() construction/caching logic runs without
    triggering an actual model download."""
    import sentence_transformers

    built = []

    class _StubModel:
        def encode(self, text, normalize_embeddings=True):
            return [0.0]

    def _fake_ctor(name):
        built.append(name)
        return _StubModel()

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _fake_ctor)
    sc._reset_embedding_model()
    try:
        model = sc._embedding_model()
        assert isinstance(model, _StubModel)
        assert sc._embedding_model() is model  # cached singleton
        assert built == ["all-MiniLM-L6-v2"]
    finally:
        sc._reset_embedding_model()


def test_cosine_zero_norm_returns_zero():
    assert sc._cosine([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 0.0
    assert sc._cosine([1.0, 0.0, 0.0], [0.0, 0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# extract_entities — real implementation (gate tests above stub this out)
# ---------------------------------------------------------------------------


@pytest.fixture
def _fuzzy_values(monkeypatch):
    """Route fuzzy_match's DB-backed value lookup to fixed test data, and
    clear its module-level TTL cache before/after so this doesn't leak into
    test_fuzzy_match.py's own tests in the same session."""
    from canopy.query.executor import QueryResult
    from canopy.query.fuzzy_match import _cache as fuzzy_value_cache

    fuzzy_value_cache._values.clear()
    fuzzy_value_cache._fetched_at.clear()

    def _fake_execute_query(sql):
        if "species" in sql:
            values = ("Grallaria gigantea", "Tinamus major")
        elif "sites" in sql:
            values = ("Reserva Narupa",)
        else:
            values = ()
        return QueryResult(columns=("v",), rows=tuple((v,) for v in values), row_count=len(values))

    monkeypatch.setattr("canopy.query.fuzzy_match.execute_query", _fake_execute_query)
    yield
    fuzzy_value_cache._values.clear()
    fuzzy_value_cache._fetched_at.clear()


def test_extract_entities_full_string_match(_fuzzy_values):
    entities = sc.extract_entities("how many Grallaria gigantea were seen at Reserva Narupa")
    assert "grallaria gigantea" in entities
    assert "reserva narupa" in entities


def test_extract_entities_word_level_match(_fuzzy_values):
    """A fragment of a multi-word value (e.g. just the genus) still matches —
    mirrors fuzzy_match.py's own word-level tolerance rationale."""
    entities = sc.extract_entities("how many tinamus were detected")
    assert "tinamus major" in entities


def test_extract_entities_numeric_literal(_fuzzy_values):
    entities = sc.extract_entities("show me the top 5 species")
    assert "5" in entities


def test_extract_entities_no_match_returns_only_numbers(_fuzzy_values):
    entities = sc.extract_entities("what is the weather today")
    assert entities == frozenset()


def test_extract_entities_survives_value_lookup_failure(monkeypatch):
    """A broken column-value lookup for one registered column must not crash
    extraction — it's skipped, other columns and numeric literals still work."""
    class _BrokenCache:
        def get(self, key, spec):
            raise RuntimeError("DB unreachable")

    monkeypatch.setattr("canopy.query.fuzzy_match._cache", _BrokenCache())
    entities = sc.extract_entities("how many species in 2023")
    assert entities == frozenset({"2023"})


# ---------------------------------------------------------------------------
# write()/lookup() edge branches
# ---------------------------------------------------------------------------


def test_write_skips_empty_sql(fake_redis, monkeypatch):
    _stub_embedder(monkeypatch, {})
    sc.write("some question", "", "conn-a")
    assert fake_redis.smembers(sc._ids_key("conn-a")) == set()


def test_lookup_skips_id_with_missing_entry(fake_redis, monkeypatch):
    """An id present in the ids-set but whose entry key has expired/vanished
    (e.g. a TTL race) must be skipped, not raise."""
    monkeypatch.setattr(sc, "extract_entities", lambda q: frozenset({"condor"}))
    _stub_embedder(monkeypatch, {"how many condors": [1.0, 0.0, 0.0]})

    fake_redis.sadd(sc._ids_key("conn-a"), "ghost-entry-id")
    hit = sc.lookup("how many condors", "conn-a")
    assert hit is None
