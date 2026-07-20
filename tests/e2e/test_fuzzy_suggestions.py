"""E2E browser tests for the fuzzy "did you mean X?" suggestion flow.

Verifies the recovery path added when a mistyped species/site name matches
a real column value closely enough to suggest a correction: the model's own
0-row answer still renders, suggestion buttons appear with the candidate
names, and clicking one re-runs the corrected question end-to-end through
the real Gradio UI (not just the Python handler contract).

Run:  make e2e  (not included in make check — requires playwright browsers)
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

_PLACEHOLDER = "e.g. How many confirmed"
_RUN_BTN = "Run Query"
_TIMEOUT = 15_000  # ms — allows for Gradio hydration + mock handler
_VIEWPORT = {"width": 1280, "height": 800}


def _submit(page: Page, canopy_url: str, question: str) -> None:
    page.set_viewport_size(_VIEWPORT)
    page.goto(canopy_url)
    page.wait_for_selector(f"[placeholder*='{_PLACEHOLDER}']")
    page.fill(f"[placeholder*='{_PLACEHOLDER}']", question)
    page.click(f"button:has-text('{_RUN_BTN}')")


def test_typo_query_shows_did_you_mean_prompt(page: Page, canopy_url: str) -> None:
    """A mistyped species name still gets the model's own 0-row answer, plus
    a 'did you mean' prompt introducing the suggestions."""
    _submit(page, canopy_url, "e2e-typo How many detections of Gralari gigantae are there?")
    expect(page.get_by_text("0 rows for that species", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_text("Did you mean", exact=False)).to_be_visible(timeout=_TIMEOUT)


def test_typo_query_shows_candidate_buttons(page: Page, canopy_url: str) -> None:
    """Both fuzzy-match candidates render as visible, clickable buttons."""
    _submit(page, canopy_url, "e2e-typo How many detections of Gralari gigantae are there?")
    expect(page.get_by_role("button", name="Grallaria gigantea", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_role("button", name="Grallaria ridgelyi", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_clicking_suggestion_reruns_corrected_question(page: Page, canopy_url: str) -> None:
    """Clicking a suggestion re-runs the query with the typo swapped for the
    candidate — the question box should show the corrected text after the
    re-run completes (real end-to-end click, not just handler output)."""
    _submit(page, canopy_url, "e2e-typo How many detections of Gralari gigantae are there?")
    page.get_by_role("button", name="Grallaria gigantea", exact=True).wait_for(
        state="visible", timeout=_TIMEOUT
    )
    page.get_by_role("button", name="Grallaria gigantea", exact=True).click()

    # Re-run keeps "e2e-typo" (still routes through the mock) but with the
    # literal corrected — question box reflects the rewritten text.
    expect(page.locator(f"[placeholder*='{_PLACEHOLDER}']")).to_have_value(
        "e2e-typo How many detections of Grallaria gigantea are there?", timeout=_TIMEOUT
    )


def test_normal_success_shows_no_suggestion_buttons(page: Page, canopy_url: str) -> None:
    """Golden path (non-zero-row result) never shows the suggestion row —
    additive-only behavior, no regression to the default UI."""
    _submit(page, canopy_url, "how many detections are there")
    expect(page.get_by_text("42 detections", exact=False)).to_be_visible(timeout=_TIMEOUT)
    expect(page.get_by_text("Did you mean", exact=False)).not_to_be_visible(timeout=3_000)


def test_guardrail_zero_row_response_shows_no_suggestions(page: Page, canopy_url: str) -> None:
    """A 0-row/no-SQL guardrail decline (no fuzzy_match set) must not show
    suggestion buttons — only an actual fuzzy-match hit triggers the row."""
    _submit(page, canopy_url, "e2e-guardrail check this query please")
    expect(page.get_by_text("cannot assess conservation trends", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_text("Did you mean", exact=False)).not_to_be_visible(timeout=3_000)
