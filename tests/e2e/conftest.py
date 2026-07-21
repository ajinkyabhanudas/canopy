"""Session-scoped Gradio server fixture for Canopy E2E tests.

Starts the real Gradio app on 127.0.0.1:7862 with run_query mocked via a
keyword-routing stub, so no live database or Anthropic API key is required.

Trigger keywords (all prefixed e2e- to avoid collisions with real queries):
  e2e-delete    → SQLGuardError (DELETE statement)
  e2e-timeout   → psycopg2 QueryCanceled (statement_timeout)
  e2e-overflow  → RuntimeError (MAX_ITERATIONS exhausted)
  e2e-disconnect→ psycopg2 OperationalError (connection lost)
  anything else → LoopResult success with model_text mentioning "42 detections"
"""
from __future__ import annotations

import time
import urllib.request
import warnings
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import pytest

from canopy.query.executor import SQLGuardError
from canopy.query.fuzzy_match import FuzzyMatch
from canopy.query.loop import LoopResult
from canopy.ui.app import build_app

# Suppress third-party deprecation warnings emitted by Gradio's internals.
# These originate in the background server thread so pytest's filterwarnings
# in pyproject.toml does not reach them — the global Python filter must be
# set here, before the session fixture starts the server.
warnings.filterwarnings("ignore", message=".*HTTP_422_UNPROCESSABLE_ENTITY.*")
warnings.filterwarnings("ignore", message=".*no_silent_downcasting.*")
warnings.filterwarnings("ignore", message=".*copy keyword is deprecated.*")

_PORT = 7862
_BASE_URL = f"http://127.0.0.1:{_PORT}"


# Safe subclasses — bypass psycopg2 C-extension constructors which require
# a live connection to build properly.
class _QueryCanceled(psycopg2.errors.QueryCanceled):
    def __init__(self) -> None:
        Exception.__init__(self, "canceling statement due to statement timeout")


class _OperationalError(psycopg2.OperationalError):
    def __init__(self) -> None:
        Exception.__init__(self, "connection to server lost")


_SUCCESS = LoopResult(
    question="test",
    sql="SELECT COUNT(*) FROM detections",
    columns=("count",),
    rows=((42,),),
    row_count=1,
    model_text="There are 42 detections in the database.",
    timing={
        "total_s": 0.5,
        "cache_hit": False,
        "llm_s": 0.4,
        "llm_calls": 1,
        "db_s": 0.05,
        "db_calls": 1,
    },
)

_TYPO_MATCH = LoopResult(
    question="test",
    sql="SELECT * FROM species WHERE scientific_name ILIKE '%Gralari gigantae%'",
    columns=("scientific_name",),
    rows=(),
    row_count=0,
    model_text="I found 0 rows for that species name.",
    timing={
        "total_s": 0.6,
        "cache_hit": False,
        "llm_s": 0.5,
        "llm_calls": 1,
        "db_s": 0.05,
        "db_calls": 1,
    },
    fuzzy_matches=(
        FuzzyMatch(
            literal="Gralari gigantae",
            candidates=("Grallaria gigantea", "Grallaria ridgelyi"),
            label_key="species",
        ),
    ),
)

_SITE_TYPO_MATCH = LoopResult(
    question="test",
    sql="SELECT * FROM sites WHERE name ILIKE '%Buenaventuraa%'",
    columns=("name",),
    rows=(),
    row_count=0,
    model_text="I found 0 rows for that site name.",
    timing={
        "total_s": 0.6,
        "cache_hit": False,
        "llm_s": 0.5,
        "llm_calls": 1,
        "db_s": 0.05,
        "db_calls": 1,
    },
    fuzzy_matches=(
        FuzzyMatch(
            literal="Buenaventuraa",
            candidates=("Reserva Buenaventura",),
            label_key="site",
        ),
    ),
)

_TWO_TYPOS_MATCH = LoopResult(
    question="test",
    sql=(
        "SELECT * FROM species sp JOIN sites si ON sp.site_id = si.id "
        "WHERE sp.scientific_name ILIKE '%Gralari gigantae%' "
        "AND si.name ILIKE '%Buenaventuraa%'"
    ),
    columns=("scientific_name", "name"),
    rows=(),
    row_count=0,
    model_text="I found 0 rows for that species/site combination.",
    timing={
        "total_s": 0.7,
        "cache_hit": False,
        "llm_s": 0.6,
        "llm_calls": 1,
        "db_s": 0.05,
        "db_calls": 1,
    },
    fuzzy_matches=(
        FuzzyMatch(
            literal="Gralari gigantae",
            candidates=("Grallaria gigantea",),
            label_key="species",
        ),
        FuzzyMatch(
            literal="Buenaventuraa",
            candidates=("Reserva Buenaventura",),
            label_key="site",
        ),
    ),
)

_MU_TYPO_MATCH = LoopResult(
    question="test",
    sql="SELECT * FROM detections WHERE management_unit ILIKE '%Waman%'",
    columns=("management_unit",),
    rows=(),
    row_count=0,
    model_text="I found 0 rows for that management unit.",
    timing={
        "total_s": 0.6,
        "cache_hit": False,
        "llm_s": 0.5,
        "llm_calls": 1,
        "db_s": 0.05,
        "db_calls": 1,
    },
    fuzzy_matches=(
        FuzzyMatch(
            literal="Waman",
            candidates=("Wamani", "Wamaní"),
            label_key="management_unit",
        ),
    ),
)

_GUARDRAIL = LoopResult(
    question="test",
    sql=None,
    columns=(),
    rows=(),
    row_count=0,
    model_text=(
        "I cannot assess conservation trends or population status from detection counts alone. "
        "That requires formal scientific review by a qualified expert. "
        "I can show you the raw detection data — would that help?"
    ),
    timing={
        "total_s": 1.1,
        "cache_hit": False,
        "llm_s": 0.9,
        "llm_calls": 1,
        "db_s": 0.0,
        "db_calls": 0,
    },
)


def _smart_mock(question: str, status_cb=None) -> LoopResult:
    """Route by keyword so one mocked server exercises all error paths.

    Trigger keywords (all prefixed e2e- to avoid collisions with real queries):
      e2e-delete      → SQLGuardError (DELETE statement)
      e2e-timeout     → psycopg2 QueryCanceled (statement_timeout)
      e2e-overflow    → RuntimeError (MAX_ITERATIONS exhausted)
      e2e-disconnect  → psycopg2 OperationalError (connection lost)
      e2e-guardrail   → LoopResult with conservation-decline model_text (no SQL)
      e2e-typo        → LoopResult with 0 rows + species fuzzy_matches candidate
      e2e-site-typo   → LoopResult with 0 rows + site fuzzy_matches candidate
      e2e-mu-typo     → LoopResult with 0 rows + management_unit fuzzy_matches
                        candidates (real near-duplicate pair: Wamani/Wamaní)
      e2e-two-typos   → LoopResult with 0 rows + BOTH species and site
                        fuzzy_matches candidates (two simultaneous typos)
      anything else   → LoopResult success with model_text mentioning "42 detections"
    """
    q = question.lower()
    if "e2e-delete" in q:
        raise SQLGuardError(
            "Only SELECT queries are permitted",
            sql="DELETE FROM detections WHERE id = 1",
        )
    if "e2e-timeout" in q:
        raise _QueryCanceled()
    if "e2e-overflow" in q:
        raise RuntimeError("Query loop exceeded maximum iterations")
    if "e2e-disconnect" in q:
        raise _OperationalError()
    if "e2e-guardrail" in q:
        return _GUARDRAIL
    if "e2e-two-typos" in q:
        return LoopResult(**{**_TWO_TYPOS_MATCH.__dict__, "question": question})
    if "e2e-site-typo" in q:
        return LoopResult(**{**_SITE_TYPO_MATCH.__dict__, "question": question})
    if "e2e-mu-typo" in q:
        return LoopResult(**{**_MU_TYPO_MATCH.__dict__, "question": question})
    if "e2e-typo" in q:
        return LoopResult(**{**_TYPO_MATCH.__dict__, "question": question})
    return _SUCCESS


@pytest.fixture(scope="session")
def canopy_url():
    """Start a mocked Gradio server and yield its URL for the whole test session."""
    with patch("canopy.ui.app.run_query", side_effect=_smart_mock):
        app = build_app()
        app.launch(
            server_name="127.0.0.1",
            server_port=_PORT,
            prevent_thread_lock=True,
            quiet=True,
        )
        # Poll until the server is accepting connections (up to 10 s).
        for _ in range(20):
            try:
                urllib.request.urlopen(_BASE_URL, timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        yield _BASE_URL
        app.close()
