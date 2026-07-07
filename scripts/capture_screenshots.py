"""
Capture fresh UI screenshots against the live Canopy app.

Usage:
    python scripts/capture_screenshots.py [--url http://localhost:7860]

Requires: playwright browsers installed (make playwright-install)
Saves to: docs/screenshots/
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

_OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
_VIEWPORT = {"width": 1280, "height": 800}
_ANSWER_TAB = "Answer"
_SQL_TAB = "Database query"
_TABLE_TAB = "Full data table"
_RUN_BTN = "Run Query"
_PLACEHOLDER = "e.g. How many confirmed"

# Max wait for a live LLM answer (real network call — up to 60s)
_LIVE_TIMEOUT = 90_000
# Wait for cache hits or UI transitions
_FAST_TIMEOUT = 10_000


def _submit(page, question: str) -> None:
    page.goto(page.url.rstrip("/") + "/")
    page.wait_for_selector(f"[placeholder*='{_PLACEHOLDER}']", timeout=_FAST_TIMEOUT)
    page.fill(f"[placeholder*='{_PLACEHOLDER}']", question)
    page.click(f"button:has-text('{_RUN_BTN}')")


def _wait_for_answer(page) -> None:
    """Wait until the timing footer appears — signals the answer is fully rendered."""
    # Poll until either timing footer variant is visible (live answer or cache hit)
    deadline = time.monotonic() + _LIVE_TIMEOUT / 1000
    while time.monotonic() < deadline:
        live = page.locator("text=Answer ready").count()
        cached = page.locator("text=From your recent queries").count()
        if live or cached:
            break
        time.sleep(1)
    else:
        raise TimeoutError(f"Answer footer not visible after {_LIVE_TIMEOUT/1000:.0f}s")
    time.sleep(0.5)


def capture(url: str) -> None:
    _OUT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=_VIEWPORT)
        page = ctx.new_page()
        page.set_default_timeout(_LIVE_TIMEOUT)

        base = url.rstrip("/")
        page.goto(base + "/")

        # ------------------------------------------------------------------
        # 01 — Idle state (no query submitted)
        # ------------------------------------------------------------------
        print("01-idle …", end=" ", flush=True)
        page.goto(base + "/")
        page.wait_for_selector(f"[placeholder*='{_PLACEHOLDER}']", timeout=_FAST_TIMEOUT)
        time.sleep(0.5)
        page.screenshot(path=str(_OUT / "01-idle.png"))
        print("done")

        # ------------------------------------------------------------------
        # 02 — English answer: confirmed species per reserve in 2023
        # ------------------------------------------------------------------
        print("02-english-count-answer …", end=" ", flush=True)
        _submit(page, "How many confirmed species were detected at each reserve in 2023?")
        _wait_for_answer(page)
        page.screenshot(path=str(_OUT / "02-english-count-answer.png"))
        print("done")

        # ------------------------------------------------------------------
        # 03 — Same query, Database query tab (SQL view)
        # ------------------------------------------------------------------
        print("03-english-count-sql …", end=" ", flush=True)
        page.get_by_role("tab", name=_SQL_TAB).click()
        time.sleep(0.3)
        page.screenshot(path=str(_OUT / "03-english-count-sql.png"))
        print("done")

        # ------------------------------------------------------------------
        # 04 — English answer: sites with most activity (Answer tab)
        # ------------------------------------------------------------------
        print("04-english-sites-answer …", end=" ", flush=True)
        _submit(page, "Which sites had the most validated detections last year?")
        _wait_for_answer(page)
        page.get_by_role("tab", name=_ANSWER_TAB).click()
        time.sleep(0.3)
        page.screenshot(path=str(_OUT / "04-english-sites-answer.png"))
        print("done")

        # ------------------------------------------------------------------
        # 05 — Spanish answer: unique species with at least one validated detection
        # ------------------------------------------------------------------
        print("05-spanish-species-answer …", end=" ", flush=True)
        _submit(page, "¿Cuántas especies únicas tienen al menos una detección validada?")
        _wait_for_answer(page)
        page.get_by_role("tab", name=_ANSWER_TAB).click()
        time.sleep(0.3)
        page.screenshot(path=str(_OUT / "05-spanish-species-answer.png"))
        print("done")

        # ------------------------------------------------------------------
        # 06 — Spanish answer: pending detections per site (Answer tab)
        # Per-site query — use GROUP BY phrasing to get multiple rows
        # ------------------------------------------------------------------
        print("06-spanish-pending-answer …", end=" ", flush=True)
        _submit(page, "¿Cuántas detecciones pendientes de revisión hay en cada uno de los sitios?")
        _wait_for_answer(page)
        page.get_by_role("tab", name=_ANSWER_TAB).click()
        time.sleep(0.3)
        page.screenshot(path=str(_OUT / "06-spanish-pending-answer.png"))
        print("done")

        # ------------------------------------------------------------------
        # 07 — Same query, Full data table tab (should show per-site rows)
        # ------------------------------------------------------------------
        print("07-spanish-pending-table …", end=" ", flush=True)
        page.get_by_role("tab", name=_TABLE_TAB).click()
        time.sleep(0.3)
        page.screenshot(path=str(_OUT / "07-spanish-pending-table.png"))
        print("done")

        browser.close()

    print(f"\nAll screenshots saved to {_OUT}/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:7860")
    args = parser.parse_args()
    capture(args.url)


if __name__ == "__main__":
    main()
