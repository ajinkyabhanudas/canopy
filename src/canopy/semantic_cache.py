"""Semantic SQL-plan cache.

Consolidates what were originally framed as two separate ideas — "semantic
caching" and "query-plan caching" — into one mechanism, because loop.py's
Interpretation is parsed post-hoc from the model's own narrative output, not
a separate LLM call: there is no standalone query-planning step to cache
independently of the full agent turn. So this caches the model's *generated
SQL* (not the full result row set), keyed by semantic similarity of the
question, and re-executes it live on a hit — the LLM's SQL-generation step
is what gets skipped, never correctness or freshness.

Design principle (see the approved scaling plan): embeddings are an ML tool
used only to narrow the candidate set. Every candidate must then pass a
chain of deterministic gates before being served — never an LLM judgment
call, so cache-safety stays repeatable and auditable.

Gate order, all of which must pass:
  1. Temporal safety   (write-time)  — sql_is_temporally_safe()
  2. Entity match       (read-time)  — entities_match()
  3. Guardrail filter   (write-time) — sql_touches_sensitive_column()
  4. Connection isolation (read-time, structural — candidates are only ever
     fetched from the calling connection_id's own key namespace)
  5. Schema-version invalidation (structural — the version is baked into the
     key namespace; bumping _SCHEMA_VERSION orphans every prior entry)
  6. Distinct tier-2 messaging — see build_semantic_hit_notice()
  7. Fail-open on storage errors — lookup()/write() never raise; a storage
     error degrades to "no hit" / "write skipped", never breaks run_query()

Storage: plain Redis (not RediSearch/Redis Stack — candidate similarity is
computed in-process over the small number of cached SQL plans a single
connection accumulates, which is simpler to operate and just as fast at
this scale). Uses the same CANOPY_REDIS_URL as canopy.cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from canopy.config import get_redis_url, get_semantic_cache_threshold

if TYPE_CHECKING:
    import redis
    from sentence_transformers import SentenceTransformer

_log = logging.getLogger("canopy.semantic_cache")

# Gate 5 — bump this when schema.py's SCHEMA_CONTEXT changes. Baked into the
# Redis key namespace so a bump orphans every previously cached SQL plan
# without needing explicit invalidation code.
_SCHEMA_VERSION = "v1"
_KEY_PREFIX = f"canopy:semantic:{_SCHEMA_VERSION}:"

# TTL is defense-in-depth, not the correctness mechanism (gates 1-3 are) —
# bounds unbounded growth and caps how long a stale entity vocabulary (Gate 2
# compares against fuzzy_match.py's cached column values, which themselves
# have a TTL) can linger.
_ENTRY_TTL_SECONDS = 7 * 24 * 3600

_SENSITIVE_COLUMNS = frozenset({"latitude", "longitude", "hashed_password"})

_TEMPORAL_KEYWORDS_RE = re.compile(
    r"\b(today|yesterday|this\s+week|this\s+month|this\s+year|recent(?:ly)?|"
    r"latest|last\s+\d+\s+days?|currently|right\s+now|as\s+of\s+now)\b",
    re.IGNORECASE,
)
_RELATIVE_SQL_RE = re.compile(
    r"\b(CURRENT_DATE|CURRENT_TIMESTAMP|NOW\s*\(|INTERVAL|date_trunc\s*\()",
    re.IGNORECASE,
)
_LITERAL_DATE_RE = re.compile(r"'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?'")
_NUMERIC_RE = re.compile(r"\b\d+\b")

_redis_client_singleton = None
_model_singleton = None


def _redis_client() -> redis.Redis:
    global _redis_client_singleton
    if _redis_client_singleton is None:
        import redis  # lazy import — only needed when semantic caching is enabled

        _redis_client_singleton = redis.Redis.from_url(get_redis_url(), socket_timeout=2)
    return _redis_client_singleton


def _reset_redis_client() -> None:
    """Discard the cached client. Used by tests to force a fresh client per test."""
    global _redis_client_singleton
    _redis_client_singleton = None


def _embedding_model() -> SentenceTransformer:
    global _model_singleton
    if _model_singleton is None:
        from sentence_transformers import SentenceTransformer  # lazy — heavy import

        _model_singleton = SentenceTransformer("all-MiniLM-L6-v2")
    return _model_singleton


def _reset_embedding_model() -> None:
    """Discard the cached model. Used by tests to inject a stub embedder."""
    global _model_singleton
    _model_singleton = None


def embed(question: str) -> list[float]:
    """Return a normalized embedding vector for `question` (ML tool — ranking only)."""
    vec = _embedding_model().encode(question, normalize_embeddings=True)
    return list(vec)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Gate 1 — temporal safety (write-time, deterministic)
# ---------------------------------------------------------------------------


def sql_is_temporally_safe(question: str, sql: str) -> bool:
    """Return True if it is safe to cache `sql` for later replay.

    Relative-date SQL functions re-evaluate at execution time, so they stay
    correct on any future day — always safe. A literal date/timestamp paired
    with relative-time language in the question ("today", "this week", ...)
    means the model resolved the relative phrase into a fixed date at
    generation time — replaying that SQL later would silently reapply the
    original day's date. Refuse to cache that combination.
    """
    if _RELATIVE_SQL_RE.search(sql):
        return True
    if _TEMPORAL_KEYWORDS_RE.search(question) and _LITERAL_DATE_RE.search(sql):
        return False
    return True


# ---------------------------------------------------------------------------
# Gate 2 — entity verification (read-time, deterministic)
# ---------------------------------------------------------------------------


def extract_entities(question: str) -> frozenset[str]:
    """Deterministically extract the known entities/numeric literals a question
    references — species names, site names, explicit numbers/years — by
    reusing fuzzy_match.py's cached real column values. No LLM judgment call:
    embedding similarity narrows candidates, this decides whether two
    questions are actually about the same subject.
    """
    from canopy.query.fuzzy_match import FUZZY_COLUMNS
    from canopy.query.fuzzy_match import _cache as _fuzzy_value_cache

    q_cf = question.casefold()
    found: set[str] = set()
    for key, spec in FUZZY_COLUMNS.items():
        try:
            values = _fuzzy_value_cache.get(key, spec)
        except Exception:
            _log.debug("entity extraction: column value lookup failed for %s", key, exc_info=True)
            continue
        for value in values:
            value_cf = value.casefold()
            if value_cf in q_cf:
                found.add(value_cf)
                continue
            for word in value_cf.split():
                if len(word) >= 4 and word in q_cf:
                    found.add(value_cf)
                    break
    found.update(_NUMERIC_RE.findall(question))
    return frozenset(found)


def entities_match(new_question: str, cached_entities: frozenset[str]) -> bool:
    """Gate 2 — reject an embedding-similarity candidate unless the entity sets
    match exactly (e.g. same species/site/number), regardless of cosine score."""
    return extract_entities(new_question) == cached_entities


# ---------------------------------------------------------------------------
# Gate 3 — guardrail / sensitive-column filter (write-time, deterministic)
# ---------------------------------------------------------------------------


def sql_touches_sensitive_column(sql: str) -> bool:
    """Return True if `sql` references a column CANOPY_SENSITIVE_COLUMNS-listed
    as sensitive. Never write such SQL into the semantic cache — a one-time
    guardrail near-miss must not become a standing, replayable cache entry."""
    sql_cf = sql.casefold()
    return any(re.search(rf"\b{re.escape(col)}\b", sql_cf) for col in _SENSITIVE_COLUMNS)


# ---------------------------------------------------------------------------
# Gate 6 — distinct tier-2 messaging
# ---------------------------------------------------------------------------


def build_semantic_hit_notice() -> str:
    """Notice for a semantic-cache-served answer.

    Deliberately different from schema.py's tier-1 "cached for up to 24
    hours" notice: tier-2 always re-executes SQL live, so the numbers are
    current — only the SQL generation step was skipped. Using tier-1's
    wording here would make the *fresher* tier read as staler than it is.
    """
    return "Matched to a similar prior query — the numbers shown are current, freshly queried."


# ---------------------------------------------------------------------------
# Storage + candidate retrieval
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticHit:
    sql: str
    question: str


def _entry_id(question: str) -> str:
    return hashlib.sha256(question.casefold().strip().encode()).hexdigest()[:24]


def _ids_key(connection_id: str) -> str:
    # Gate 4 — connection isolation: candidates are only ever looked up
    # within this connection's own key, never merged or ranked across
    # connections.
    return f"{_KEY_PREFIX}{connection_id}:ids"


def _entry_key(connection_id: str, entry_id: str) -> str:
    return f"{_KEY_PREFIX}{connection_id}:entry:{entry_id}"


def write(question: str, sql: str, connection_id: str) -> None:
    """Store a (question, sql) pair for later semantic reuse.

    Silently skips the write (never raises) if: the SQL is not temporally
    safe to replay (Gate 1), the SQL touches a sensitive column (Gate 3), or
    the storage backend is unreachable (Gate 7 applies to writes too — a
    failed cache write must never break the query that produced the result).
    """
    if not sql:
        return
    if not sql_is_temporally_safe(question, sql):
        _log.debug("semantic cache: skipping write — temporal safety gate failed")
        return
    if sql_touches_sensitive_column(sql):
        _log.debug("semantic cache: skipping write — sensitive-column gate failed")
        return

    try:
        entities = extract_entities(question)
        embedding = embed(question)
        entry_id = _entry_id(question)
        payload = json.dumps(
            {
                "question": question,
                "sql": sql,
                "entities": sorted(entities),
                "embedding": embedding,
            }
        )
        client = _redis_client()
        client.set(_entry_key(connection_id, entry_id), payload, ex=_ENTRY_TTL_SECONDS)
        client.sadd(_ids_key(connection_id), entry_id)
        client.expire(_ids_key(connection_id), _ENTRY_TTL_SECONDS)
    except Exception:
        _log.warning("semantic cache write failed — continuing without it", exc_info=True)


def lookup(question: str, connection_id: str) -> SemanticHit | None:
    """Return a semantically-matched, gate-verified (question, sql) pair, or
    None on a miss, a gate rejection, or any storage error (fail-open —
    Gate 7: a lookup failure here must never break the calling query)."""
    try:
        client = _redis_client()
        ids = client.smembers(_ids_key(connection_id))
        if not ids:
            return None

        query_embedding = embed(question)
        threshold = get_semantic_cache_threshold()

        scored: list[tuple[float, dict]] = []
        for raw_id in ids:
            entry_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            raw_entry = client.get(_entry_key(connection_id, entry_id))
            if raw_entry is None:
                continue
            entry = json.loads(raw_entry)
            score = _cosine(query_embedding, entry["embedding"])
            if score >= threshold:
                scored.append((score, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        for _score, entry in scored:
            cached_entities = frozenset(entry.get("entities", []))
            if entities_match(question, cached_entities):
                return SemanticHit(sql=entry["sql"], question=entry["question"])
        return None
    except Exception:
        _log.warning("semantic cache lookup failed — falling through to full agent", exc_info=True)
        return None
