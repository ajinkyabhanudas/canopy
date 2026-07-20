"""E2E browser tests for the fuzzy "did you mean X?" suggestion flow.

Verifies the recovery path added when a mistyped species/site name matches
a real column value closely enough to suggest a correction: the model's own
0-row answer still renders, suggestion buttons appear with the candidate
names, and clicking one re-runs the corrected question end-to-end through
the real Gradio UI (not just the Python handler contract).

Also verifies the multi-column case: a question with typos in BOTH a
species name AND a site name at once must surface two independent,
separately labeled suggestion groups rather than only the first one found.

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


# ---------------------------------------------------------------------------
# Single typo — species column
# ---------------------------------------------------------------------------


def test_typo_query_shows_did_you_mean_prompt(page: Page, canopy_url: str) -> None:
    """A mistyped species name still gets the model's own 0-row answer, plus
    a labeled 'did you mean' prompt introducing the suggestions."""
    _submit(page, canopy_url, "e2e-typo How many detections of Gralari gigantae are there?")
    expect(page.get_by_text("0 rows for that species", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_text("no exact match found", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_text("Species:", exact=False)).to_be_visible(timeout=_TIMEOUT)


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


def test_clicking_suggestion_removes_original_typo_from_history(
    page: Page, canopy_url: str
) -> None:
    """Clicking a suggestion must drop the original mistyped question from
    the history sidebar, not leave it alongside the corrected one — a
    dead-end entry that would just hit the same 0-row result again if
    clicked. Only the corrected question should remain."""
    _submit(page, canopy_url, "e2e-typo How many detections of Gralari gigantae are there?")
    page.get_by_role("button", name="Grallaria gigantea", exact=True).wait_for(
        state="visible", timeout=_TIMEOUT
    )
    page.get_by_role("button", name="Grallaria gigantea", exact=True).click()

    expect(page.locator(f"[placeholder*='{_PLACEHOLDER}']")).to_have_value(
        "e2e-typo How many detections of Grallaria gigantea are there?", timeout=_TIMEOUT
    )

    expect(
        page.get_by_text(
            "e2e-typo How many detections of Grallaria gigantea are there?", exact=False
        )
    ).to_be_visible(timeout=_TIMEOUT)
    expect(
        page.get_by_text(
            "e2e-typo How many detections of Gralari gigantae are there?", exact=False
        )
    ).not_to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------------
# Single typo — site column (distinct from species; exercises the second
# registered FUZZY_COLUMNS entry end-to-end, not just species every time)
# ---------------------------------------------------------------------------


def test_site_typo_query_shows_did_you_mean_prompt(page: Page, canopy_url: str) -> None:
    """A mistyped site name gets its own labeled suggestion, distinct from
    the species-column wording."""
    _submit(page, canopy_url, "e2e-site-typo How many detections at Buenaventuraa are there?")
    expect(page.get_by_text("0 rows for that site", exact=False)).to_be_visible(timeout=_TIMEOUT)
    expect(page.get_by_text("Site:", exact=False)).to_be_visible(timeout=_TIMEOUT)


def test_site_typo_query_shows_candidate_button(page: Page, canopy_url: str) -> None:
    _submit(page, canopy_url, "e2e-site-typo How many detections at Buenaventuraa are there?")
    expect(page.get_by_role("button", name="Reserva Buenaventura", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_clicking_site_suggestion_reruns_corrected_question(page: Page, canopy_url: str) -> None:
    _submit(page, canopy_url, "e2e-site-typo How many detections at Buenaventuraa are there?")
    page.get_by_role("button", name="Reserva Buenaventura", exact=True).wait_for(
        state="visible", timeout=_TIMEOUT
    )
    page.get_by_role("button", name="Reserva Buenaventura", exact=True).click()
    expect(page.locator(f"[placeholder*='{_PLACEHOLDER}']")).to_have_value(
        "e2e-site-typo How many detections at Reserva Buenaventura are there?", timeout=_TIMEOUT
    )


# ---------------------------------------------------------------------------
# Single typo — management_unit column (third registered FUZZY_COLUMNS
# entry; exercises the real near-duplicate pair found live: Wamani/Wamaní)
# ---------------------------------------------------------------------------


def test_mu_typo_query_shows_did_you_mean_prompt(page: Page, canopy_url: str) -> None:
    """A mistyped management unit name gets its own labeled suggestion,
    distinct from the species/site column wording."""
    _submit(page, canopy_url, "e2e-mu-typo How many detections in Waman are there?")
    expect(page.get_by_text("0 rows for that management unit", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_text("Management unit:", exact=False)).to_be_visible(timeout=_TIMEOUT)


def test_mu_typo_query_shows_both_candidate_buttons(page: Page, canopy_url: str) -> None:
    """Both real near-duplicate candidates (accent divergence) render as
    separate, clickable buttons."""
    _submit(page, canopy_url, "e2e-mu-typo How many detections in Waman are there?")
    expect(page.get_by_role("button", name="Wamani", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_role("button", name="Wamaní", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_clicking_mu_suggestion_reruns_corrected_question(page: Page, canopy_url: str) -> None:
    _submit(page, canopy_url, "e2e-mu-typo How many detections in Waman are there?")
    page.get_by_role("button", name="Wamani", exact=True).wait_for(
        state="visible", timeout=_TIMEOUT
    )
    page.get_by_role("button", name="Wamani", exact=True).click()
    expect(page.locator(f"[placeholder*='{_PLACEHOLDER}']")).to_have_value(
        "e2e-mu-typo How many detections in Wamani are there?", timeout=_TIMEOUT
    )


# ---------------------------------------------------------------------------
# Two simultaneous typos — species AND site mistyped in the same question
# ---------------------------------------------------------------------------


def test_two_simultaneous_typos_show_two_suggestion_groups(page: Page, canopy_url: str) -> None:
    """A question with typos in BOTH a species name AND a site name must
    surface two independent suggestion groups, each with its own label —
    not just a correction for whichever column was checked first."""
    _submit(
        page,
        canopy_url,
        "e2e-two-typos How many detections of Gralari gigantae at Buenaventuraa are there?",
    )
    expect(page.get_by_text("Species:", exact=False)).to_be_visible(timeout=_TIMEOUT)
    expect(page.get_by_text("Site:", exact=False)).to_be_visible(timeout=_TIMEOUT)
    expect(page.get_by_role("button", name="Grallaria gigantea", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_role("button", name="Reserva Buenaventura", exact=True)).to_be_visible(
        timeout=_TIMEOUT
    )


def test_clicking_one_group_in_two_typo_case_only_fixes_that_column(
    page: Page, canopy_url: str
) -> None:
    """Clicking the species suggestion when both columns are mistyped fixes
    only the species literal — the site typo remains in the re-run question,
    since only one correction was chosen."""
    _submit(
        page,
        canopy_url,
        "e2e-two-typos How many detections of Gralari gigantae at Buenaventuraa are there?",
    )
    page.get_by_role("button", name="Grallaria gigantea", exact=True).wait_for(
        state="visible", timeout=_TIMEOUT
    )
    page.get_by_role("button", name="Grallaria gigantea", exact=True).click()

    expect(page.locator(f"[placeholder*='{_PLACEHOLDER}']")).to_have_value(
        "e2e-two-typos How many detections of Grallaria gigantea at Buenaventuraa are there?",
        timeout=_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# No suggestions on non-typo paths — additive-only, no regression
# ---------------------------------------------------------------------------


def test_normal_success_shows_no_suggestion_buttons(page: Page, canopy_url: str) -> None:
    """Golden path (non-zero-row result) never shows the suggestion row —
    additive-only behavior, no regression to the default UI."""
    _submit(page, canopy_url, "how many detections are there")
    expect(page.get_by_text("42 detections", exact=False)).to_be_visible(timeout=_TIMEOUT)
    expect(page.get_by_text("no exact match found", exact=False)).not_to_be_visible(
        timeout=3_000
    )


def test_guardrail_zero_row_response_shows_no_suggestions(page: Page, canopy_url: str) -> None:
    """A 0-row/no-SQL guardrail decline (no fuzzy_matches set) must not show
    suggestion buttons — only an actual fuzzy-match hit triggers a group."""
    _submit(page, canopy_url, "e2e-guardrail check this query please")
    expect(page.get_by_text("cannot assess conservation trends", exact=False)).to_be_visible(
        timeout=_TIMEOUT
    )
    expect(page.get_by_text("no exact match found", exact=False)).not_to_be_visible(
        timeout=3_000
    )
