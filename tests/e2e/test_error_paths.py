"""E2E browser tests for Canopy UI error paths and happy path.

Each test submits a question via the real Gradio UI and asserts what Jajean
sees in the browser — verifying that error messages are rendered, not just
that the Python handler returns the right tuple values.

Run:  make e2e  (not included in make check — requires playwright browsers)
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

# Partial match against the placeholder text — robust across locale changes.
_PLACEHOLDER = "e.g. How many confirmed"
_RUN_BTN = "Run Query"
_DB_TAB = "Database query"
_TIMEOUT = 15_000  # ms — allows for Gradio hydration + mock handler
# Consistent viewport so tabs are not obscured by overlapping elements in CI.
_VIEWPORT = {"width": 1280, "height": 800}


def _submit(page: Page, canopy_url: str, question: str) -> None:
    """Navigate to the app, fill the question box, and click Run."""
    page.set_viewport_size(_VIEWPORT)
    page.goto(canopy_url)
    page.wait_for_selector(f"[placeholder*='{_PLACEHOLDER}']")
    page.fill(f"[placeholder*='{_PLACEHOLDER}']", question)
    page.click(f"button:has-text('{_RUN_BTN}')")


# ---------------------------------------------------------------------------
# Guard error paths
# ---------------------------------------------------------------------------


def test_guard_names_blocked_operation(page: Page, canopy_url: str) -> None:
    """DELETE triggers a guard error; response names 'DELETE is not permitted'."""
    _submit(page, canopy_url, "e2e-delete all detections")
    expect(page.get_by_text("DELETE is not permitted", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_guard_blocked_sql_appears_in_database_tab(page: Page, canopy_url: str) -> None:
    """Blocked SQL is shown in the Database query tab so the user can inspect it."""
    _submit(page, canopy_url, "e2e-delete all detections")
    page.wait_for_selector("text=DELETE is not permitted", timeout=_TIMEOUT)
    page.get_by_role("tab", name=_DB_TAB).click()
    expect(page.get_by_text("DELETE FROM detections", exact=False)).to_be_visible(
        timeout=5_000
    )


# ---------------------------------------------------------------------------
# Technical error paths
# ---------------------------------------------------------------------------


def test_statement_timeout_shows_actionable_message(page: Page, canopy_url: str) -> None:
    """Statement timeout: user sees 'too long' with a suggestion to narrow the query."""
    _submit(page, canopy_url, "e2e-timeout large data query")
    expect(page.get_by_text("too long", exact=False)).to_be_visible(timeout=_TIMEOUT)


def test_loop_exhaustion_shows_actionable_message(page: Page, canopy_url: str) -> None:
    """MAX_ITERATIONS: user sees 'too many steps' with a suggestion to split the question."""
    _submit(page, canopy_url, "e2e-overflow complex question")
    expect(page.get_by_text("too many steps", exact=False)).to_be_visible(timeout=_TIMEOUT)


def test_db_connection_error_shows_actionable_message(page: Page, canopy_url: str) -> None:
    """DB connection lost: user sees 'reach the database' — unique to the error message."""
    _submit(page, canopy_url, "e2e-disconnect database test")
    # "reach the database" appears only in the error message, not in any tab label.
    expect(page.get_by_text("reach the database", exact=False)).to_be_visible(timeout=_TIMEOUT)


# ---------------------------------------------------------------------------
# Language gate
# ---------------------------------------------------------------------------


def test_language_gate_rejects_french_question(page: Page, canopy_url: str) -> None:
    """French question (>30 chars) is rejected by app-layer gate before the model is called.

    Validates DECISIONS.md § S5 primary enforcement layer: the UI shows the
    unsupported-language error and no SQL is generated (no API call made).
    """
    _submit(page, canopy_url, "Combien d'espèces ont été détectées en 2023?")
    expect(page.get_by_text("English or Spanish", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_language_gate_status_indicator_shown(page: Page, canopy_url: str) -> None:
    """Language gate rejection sets the status indicator — user sees the ⚠ banner."""
    _submit(page, canopy_url, "Combien d'espèces ont été détectées en 2023?")
    expect(page.get_by_text("Language not yet supported", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )


# ---------------------------------------------------------------------------
# Guardrail — conservation decline
# ---------------------------------------------------------------------------


def test_guardrail_response_shows_decline_language(page: Page, canopy_url: str) -> None:
    """Guardrail response renders in the Answer tab with conservation-decline language.

    Validates that when the model declines a conservation/trend inference request,
    the UI renders the full model_text — not an error state.
    """
    _submit(page, canopy_url, "e2e-guardrail check this query please")
    expect(page.get_by_text("cannot assess conservation trends", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_guardrail_response_has_no_sql_tab_content(page: Page, canopy_url: str) -> None:
    """Guardrail responses produce no SQL — the Database query tab is empty."""
    _submit(page, canopy_url, "e2e-guardrail check this query please")
    page.wait_for_selector("text=cannot assess conservation trends", timeout=_TIMEOUT)
    page.get_by_role("tab", name=_DB_TAB).click()
    # No SQL should be present — the guardrail declined before executing any query.
    expect(page.get_by_text("SELECT", exact=False)).not_to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_renders_model_answer(page: Page, canopy_url: str) -> None:
    """Successful query: model_text appears in the Answer tab."""
    _submit(page, canopy_url, "how many detections are there")
    expect(page.get_by_text("42 detections", exact=False)).to_be_visible(timeout=_TIMEOUT)
