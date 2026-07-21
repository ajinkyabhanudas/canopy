"""Deterministic fuzzy-match fallback for mistyped column literals.

The LLM has no direct read access to real database values, so a typo in a
species name or site name (e.g. "Gralari gigantae" instead of the stored
"Grallaria gigantea") cannot be corrected by ILIKE — substring matching has
no edit-distance tolerance — and no fuzzy Postgres extension (pg_trgm,
similarity(), levenshtein()) is installed in this stack. This module closes
that gap without depending on the LLM: when a query against a registered
column returns 0 rows, it fuzzy-matches the literal the model used against a
cached list of real values for that column and surfaces close candidates.

Uses stdlib difflib rather than a third-party fuzzy-matching library — no
new dependency required, and difflib's SequenceMatcher ratio is adequate for
short name strings against small candidate lists (hundreds, not millions, of
distinct values per column).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

from canopy.query.executor import QueryResult, execute_query

_log = logging.getLogger("canopy.fuzzy_match")

_DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6h — column value lists change rarely
_MAX_CANDIDATES = 3
_DEFAULT_THRESHOLD = 0.72  # difflib SequenceMatcher.ratio() scale (0.0-1.0)

# Matches an aggregate query with no GROUP BY: COUNT(*), COUNT(col), SUM(...),
# etc. Such queries always return exactly 1 row regardless of how many
# underlying records matched — a "how many detections of X" question that
# matches nothing still comes back as row_count=1 with the single value 0,
# not row_count=0. Live-LLM testing (Docker verification) showed this is the
# SQL shape the model reaches for by default on "how many" questions — the
# single most common phrasing for exactly the kind of query this module
# exists to help with — so treating row_count==0 as the only "nothing found"
# signal missed the majority of real typo cases entirely.
_AGGREGATE_NO_GROUP_BY_RE = re.compile(
    r"^\s*SELECT\b(?:(?!\bGROUP\s+BY\b).)*\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(",
    re.IGNORECASE | re.DOTALL,
)


def is_empty_result(sql: str, result: QueryResult) -> bool:
    """Return True if `result` represents "nothing found" for fuzzy-match purposes.

    Two shapes both count as empty:
    - row_count == 0 — the ordinary "no rows returned" case (SELECT * ... WHERE
      no match).
    - An aggregate query (COUNT/SUM/AVG/MIN/MAX, no GROUP BY) that returns its
      one mandatory row with a falsy (0/None) value — the aggregate-query
      equivalent of "nothing found," which row_count alone cannot detect since
      such queries always return exactly 1 row.
    """
    if result.row_count == 0:
        return True
    if (
        result.row_count == 1
        and len(result.columns) == 1
        and _AGGREGATE_NO_GROUP_BY_RE.search(sql) is not None
    ):
        value = result.rows[0][0]
        return not value  # 0, None, or any other falsy aggregate result
    return False


def effective_count(sql: str, result: QueryResult) -> int:
    """Return the count that actually matters for a "Found N" style message.

    For an ordinary row-returning query this is just row_count. For an
    aggregate query (COUNT/SUM/AVG/MIN/MAX, no GROUP BY) row_count is always
    1 regardless of how many records matched — the real number is the
    aggregate's own value, not the row count. Without this distinction,
    "SELECT COUNT(*) FROM detections WHERE species_id = 12" returning 100
    real matches would report "Found 1 detection" instead of "Found 100".
    """
    if (
        result.row_count == 1
        and len(result.columns) == 1
        and _AGGREGATE_NO_GROUP_BY_RE.search(sql) is not None
    ):
        value = result.rows[0][0]
        return int(value) if isinstance(value, (int, float)) else result.row_count
    return result.row_count


@dataclass(frozen=True)
class FuzzyMatch:
    """A resolved fuzzy match: the literal the model used and close candidates.

    `literal` is exposed (not just `candidates`) so a caller — the UI — can
    substring-swap the exact mistyped text within the user's original
    question when a candidate is picked, rather than discarding the rest of
    the question's context (date ranges, site filters, etc).

    `label_key` is a stable machine-readable identifier for the matched
    column (e.g. "species", "site") — not display text. This module has no
    i18n dependency (backend query logic should not import UI-layer
    concerns), so callers — the UI — map this key to a translated,
    human-readable label. It exists so a caller can distinguish multiple
    simultaneous matches: a question can have typos in more than one
    fuzzy-checkable column at once, and each needs its own labeled
    suggestion group in the UI.
    """

    literal: str
    candidates: tuple[str, ...]
    label_key: str


@dataclass(frozen=True)
class _ColumnSpec:
    """A column eligible for fuzzy fallback matching."""

    table: str
    column: str
    label_key: str
    values_sql: str


FUZZY_COLUMNS: dict[str, _ColumnSpec] = {
    "species.scientific_name": _ColumnSpec(
        table="species",
        column="scientific_name",
        label_key="species",
        values_sql="SELECT DISTINCT scientific_name FROM species",
    ),
    "sites.name": _ColumnSpec(
        table="sites",
        column="name",
        label_key="site",
        values_sql="SELECT DISTINCT name FROM sites",
    ),
    "detections.management_unit": _ColumnSpec(
        table="detections",
        column="management_unit",
        label_key="management_unit",
        values_sql=(
            "SELECT DISTINCT management_unit FROM detections "
            "WHERE management_unit IS NOT NULL"
        ),
    ),
}


def _ttl_seconds() -> int:
    return int(os.environ.get("CANOPY_FUZZY_CACHE_TTL_SECONDS", _DEFAULT_TTL_SECONDS))


class _ValueCache:
    """In-process TTL cache of distinct column values, one entry per registered column."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, tuple[str, ...]] = {}
        self._fetched_at: dict[str, float] = {}

    def get(self, key: str, spec: _ColumnSpec) -> tuple[str, ...]:
        now = time.monotonic()
        with self._lock:
            fetched_at = self._fetched_at.get(key)
            if fetched_at is not None and (now - fetched_at) < _ttl_seconds():
                return self._values[key]

        result = execute_query(spec.values_sql)
        values = tuple(row[0] for row in result.rows if row[0] is not None)
        with self._lock:
            self._values[key] = values
            self._fetched_at[key] = now
        return values


_cache = _ValueCache()


def _column_pattern(column: str) -> re.Pattern[str]:
    # Matches `<qualifier.>column ILIKE '%literal%'` or `<qualifier.>column = 'literal'`,
    # tolerating an optional table alias prefix (e.g. `sp.scientific_name`).
    #
    # The leading \b is load-bearing: without it, a short registered column
    # name like "name" would match as a substring of an unrelated longer
    # identifier ending in "name" (e.g. "scientific_name"), because "_" is a
    # word character and (?:\w+\.)? doesn't require the match to start at an
    # identifier boundary. \b anchors the match to the start of the actual
    # column token instead of anywhere within a longer one.
    return re.compile(
        rf"(?:\w+\.)?\b{re.escape(column)}\b\s*(?:ILIKE|ilike|=)\s*'%?([^'%]+)%?'"
    )


def _best_word_score(literal_cf: str, candidate: str) -> float:
    """Score `literal_cf` (already casefolded) against `candidate` and each of
    its individual whitespace-separated words, returning the best ratio.

    Live-LLM testing showed the model often writes an ILIKE fragment covering
    only part of a multi-word value — just the species epithet ("ridgeleyi"
    instead of "Grallaria ridgelyi"), or just the genus — rather than the full
    binomial. Comparing that fragment only against the full candidate string
    scores far below threshold even for an obvious match (e.g. 0.59 for
    "ridgeleyi" vs "Grallaria ridgelyi"), while the correct word-level
    comparison scores 0.94. Checking every word (not just the last one) is
    required: a mistyped genus only matches well against the first word, a
    mistyped epithet only matches well against the last word.
    """
    candidates_to_check = [candidate, *candidate.split()]
    return max(
        SequenceMatcher(a=literal_cf, b=c.casefold()).ratio() for c in candidates_to_check
    )


def find_candidates(
    sql: str, threshold: float = _DEFAULT_THRESHOLD, limit: int = _MAX_CANDIDATES
) -> tuple[FuzzyMatch, ...]:
    """Return one FuzzyMatch per registered column with a resolvable mistyped literal.

    Checks every column in FUZZY_COLUMNS — not just the first match — so a
    question with typos in two different columns (e.g. a mistyped species
    name AND a mistyped site name in the same query) surfaces suggestions
    for both rather than silently dropping the second. Returns an empty
    tuple if no registered column is referenced, no literal can be
    extracted, or nothing scores above `threshold` for any column (a
    genuinely absent value should not produce noisy suggestions).
    """
    matches: list[FuzzyMatch] = []
    for key, spec in FUZZY_COLUMNS.items():
        if spec.column not in sql:
            continue
        match = _column_pattern(spec.column).search(sql)
        if match is None:
            continue
        literal = match.group(1).strip()
        if not literal:
            continue

        try:
            values = _cache.get(key, spec)
        except Exception:
            _log.warning("fuzzy candidate lookup failed for %s", key, exc_info=True)
            continue
        if not values:
            continue

        literal_cf = literal.casefold()
        scored = [(_best_word_score(literal_cf, v), v) for v in values]
        scored = [(score, v) for score, v in scored if score >= threshold]
        if scored:
            scored.sort(key=lambda sv: sv[0], reverse=True)
            matches.append(
                FuzzyMatch(
                    literal=literal,
                    candidates=tuple(v for _, v in scored[:limit]),
                    label_key=spec.label_key,
                )
            )

    return tuple(matches)
