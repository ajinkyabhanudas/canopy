# Canopy — Known Limitations and Data Inconsistencies

> This document is intended for the science team and handover recipients. It records
> the gaps, inconsistencies, and boundaries of v1 that a user or administrator should
> understand before relying on Canopy outputs for decision-making.
>
> Last updated: 2026-06-27. Update this file when any limitation is resolved.

---

## Data Inconsistencies

### 1. Validation status values differ from early documentation

**Severity:** High — affects correctness of any query that filters by validation status.

The original schema documentation described three validation statuses:
- `validated_true` — human-confirmed detection
- `validated_false` — human-rejected detection
- `unvalidated` — awaiting review

**The actual database uses different values:**

| Status | Count | Meaning |
|---|---|---|
| `pending` | 22,757 | AI detection awaiting human validation |
| `approved` | 14,060 | Human expert confirmed genuine detection |

There is no rejection status in the current dataset. Detections not yet reviewed remain `pending` indefinitely.

**Impact:** Any query filtering on `validated_true` returned 0 rows. This was discovered during Playwright UI testing when the model self-flagged the discrepancy. `schema.py` has been corrected to use `approved`/`pending`.

**Action required:** If the Jocotoco team introduces a third status (e.g. `rejected` or `validated_false`), `schema.py` must be updated to reflect it. The system prompt default filter (`ALWAYS filter on validation_status = 'approved'`) will not pick up new statuses automatically.

**Long-term fix:** A CI integration test should query `SELECT DISTINCT validation_status FROM detections` and assert the results match what `schema.py` documents. See DECISIONS.md § D1.

---

### 2. No common name / taxonomic group support

**Severity:** Medium — affects usability for non-scientific users.

The `species` table contains only `scientific_name` (binomial). There is no:
- Common name column (e.g. "Jocotoco antpitta" → `Grallaria ridgelyi`)
- Taxonomic order, family, or class column (e.g. "bird", "frog", "mammal")

**Impact:** A question like "which sites had the most birds?" cannot be answered by group. Canopy will show a sample of available species names and ask the user to specify a scientific name.

**Action required:** Either (a) add a `common_name` or `taxonomic_class` column to the `species` table, or (b) integrate a lookup from IUCN/eBird. Deferred to post-v1.

---

### 3. Missing-year detection gaps not labelled

**Severity:** Low — affects interpretation of time-series outputs.

When detections are queried per year, years with zero activity at a site return no row rather than a row with count = 0. Canopy will show years with data but silently omit years with no data.

**Impact:** A user comparing 2023 vs 2024 detection counts at a site that had no detections in 2024 would see only a 2023 row — not a "2024: 0" row. This could be interpreted as the site simply not being queried.

**Action required:** The system prompt instructs the model to note gaps explicitly ("note if years are missing"). This relies on model compliance — it is not enforced in SQL. Step 9 in the build plan addresses this.

---

### 4. No validated_false / rejection status in current data

**Severity:** Low — affects completeness of validation workflow reporting.

All non-approved detections remain in `pending` status. There is currently no way to distinguish "awaiting review" from "reviewed and rejected" using the database alone.

**Impact:** Queries asking "how many detections were rejected?" will return 0. This is technically correct but may confuse users who expect a rejection workflow to be recorded.

**Action required:** If Jocotoco's validation platform introduces a rejection status, it should be documented in `schema.py` and this file updated.

---

## Tool Scope Limitations

### 5. No population trend or conservation status inference

Canopy reports what was acoustically detected and when. It does not assess whether a population is stable, growing, or declining. Trend analysis requires a formally designed monitoring protocol, multi-year abundance modelling, and expert scientific review.

IUCN threat categories (Endangered, Vulnerable, etc.) are held in the IUCN Red List — a separate system not integrated with Canopy in v1. See DECISIONS.md § S4 and build step 7.

### 6. No common name lookup

Common names (e.g. "giant antpitta", "Jocotoco antpitta") are not stored in this database. All species queries must use scientific binomial names.

### 7. Single-turn query — no conversational memory

Each question is answered independently. Canopy does not maintain conversation history between queries. A follow-up like "tell me more about those sites" will not carry context from the previous answer.

### 8. Coordinate data withheld from AI layer

Latitude and longitude columns are stripped before the AI model processes results. Spatial queries (e.g. "which detections were within 5 km of reserve boundary") cannot be answered by Canopy. The science team can run spatial analysis directly against the database.

### 9. Cache staleness for live-count and time-anchored queries

Responses are cached for 24 hours, keyed on question text. Queries whose correct answer changes within that window will return the same answer for up to 24 hours even if the database has been updated. This affects:

- **Live-count queries** — "How many detections are awaiting human review?" returns a count that changes as validators approve records throughout the day. The cached count from 09:00 will be served unchanged at 22:00.
- **Time-anchored queries** — "Show me the most recent detection" is stale from the moment any new data is uploaded.
- **Week/month boundary queries** — "Which sites had the most activity this week?" cached on Monday returns Monday's result on Tuesday, even though "this week" now means a different day range.

**Impact:** A non-technical user (e.g. Jajean) has no indication that they are seeing a cached answer. The ⚡ indicator in the timing footer is the only signal.

**Action required:** For current figures before submitting to donors or grant bodies, ask the science team to verify, or allow 24 hours between queries. The system prompt model instruction advises noting this in ⚠️ Data notes for live-count queries.

**Long-term fix candidates:** Per-query TTL based on detected time-relative language; cache invalidation webhook on data upload; shorter TTL for high-churn query patterns.

---

## Known UI Bugs (as of 2026-06-27)

| # | Bug | Status |
|---|---|---|
| B1 | Eval test runs pollute the history sidebar | Fixed — `tests/conftest.py` now isolates `CANOPY_DATA_DIR` per test |
| B2 | History click did not restore previous result | Fixed — `.then()` chain auto-runs the cached query |
| B3 | History sidebar selection did not deselect after new query | Fixed — final yield passes `value=None` |
| B4 | Queries using `EXTRACT(YEAR ...)` not saved to history/cache | Fixed — custom JSON encoder handles `Decimal` from psycopg2 |
| B5 | System prompt allowed model to ask unanswerable follow-up questions | Fixed — explicit pick-and-run instruction added |
| B6 | datetime columns returned as str on cache hit (type mismatch vs live query) | Fixed — `_maybe_datetime` reconstructs ISO strings in row values on cache read |
| B7 | `_empty_result` called `_history_choices()` in error path — double-fault if data dir unavailable | Fixed — silent fallback to empty list |
| B8 | `_history_choices()` called 3–4× per query on every intermediate status yield | Fixed — snapshot once before query loop, refresh only on final yield |
| B9 | `/data` volume owned by root — `canopy` user gets EACCES on every cache/history write in Docker | Fixed — Dockerfile now `chown canopy:canopy /data` before `USER canopy` so the volume initialises with correct ownership |
| B10 | Dark theme persists in real browser despite CSS `color-scheme: light` — Gradio re-adds `.dark` after one-shot JS removal | Fixed — replaced one-shot `classList.remove('dark')` with a `MutationObserver` that fires on every class-attribute change |

---

## Eval Coverage Gaps

| Gap | Priority | Notes |
|---|---|---|
| No eval case checks that `validation_status = 'approved'` filter appears in SQL | High | Model could omit filter on ambiguous queries |
| No eval case for common-name group queries (birds, frogs) | Medium | Model should run broad species list, not ask follow-up |
| No eval case that verifies missing-year gaps are noted explicitly | Low | Step 9 of build plan |
| No eval case for live-count queries (pending, most-recent) | Medium | Added Q28–Q30 in queries.py |
| Cache staleness for time-relative queries untested in Playwright protocol | Medium | Added Test 9 in playwright-protocol.md |
| Spanish eval coverage | Closed | 8 Spanish parallel cases added to queries.py (run with `--spanish`); SQL structure checks inherited; language soft-check via Spanish character presence |
| Cache miss on Spanish accent variants (NFC vs NFD composition) | Closed | unicodedata.normalize("NFC") added to `_make_key()` |
