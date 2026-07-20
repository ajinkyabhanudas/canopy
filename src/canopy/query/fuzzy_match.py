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

from canopy.query.executor import execute_query

_log = logging.getLogger("canopy.fuzzy_match")

_DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6h — column value lists change rarely
_MAX_CANDIDATES = 3
_DEFAULT_THRESHOLD = 0.72  # difflib SequenceMatcher.ratio() scale (0.0-1.0)


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
        scored = [
            (SequenceMatcher(a=literal_cf, b=v.casefold()).ratio(), v) for v in values
        ]
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
