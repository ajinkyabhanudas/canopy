# Canopy — Known Limitations and Data Inconsistencies

> Intended for the science team and handover recipients. Records gaps,
> inconsistencies, and design boundaries that a user or administrator
> should understand before relying on Canopy outputs for decision-making.
>
> Last updated: 2026-07-11. Update this file when a limitation is resolved or a new one is found.

---

## Data Inconsistencies

### 1. Validation status values differ from early documentation

**Severity:** High — affects correctness of any query that filters by validation status.

The original schema documentation described three statuses (`validated_true`,
`validated_false`, `unvalidated`). The actual database uses:

| Status | Count | Meaning |
|---|---|---|
| `pending` | 22,757 | AI detection awaiting human validation |
| `approved` | 14,060 | Human expert confirmed genuine detection |

There is no rejection status in the current dataset.

**Action required:** If the Jocotoco team introduces a third status (e.g. `rejected`),
`schema.py` must be updated. The system prompt default filter
(`ALWAYS filter on validation_status = 'approved'`) will not pick up new statuses
automatically.

**Long-term fix:** A CI integration test should query
`SELECT DISTINCT validation_status FROM detections` and assert the results match
what `schema.py` documents. See DECISIONS.md § D1.

---

### 2. No common name or taxonomic group support

**Severity:** Medium — affects usability for non-scientific users.

The `species` table contains only `scientific_name` (binomial). There is no common
name column and no taxonomic order, family, or class. A question like "which sites
had the most birds?" cannot be answered by group. Canopy will show available species
names and ask the user to specify a scientific name.

**Action required:** Either add a `common_name` / `taxonomic_class` column, or
integrate a lookup from IUCN/eBird. Deferred to post-v1.

---

### 3. Missing-year detection gaps not labelled

**Severity:** Low — affects interpretation of time-series outputs.

Years with zero activity at a site return no row rather than a row with count = 0.
A user comparing 2023 vs 2024 counts at a site that had no 2024 detections would
see only a 2023 row — not a "2024: 0" row.

The system prompt instructs the model to note gaps explicitly. This relies on model
compliance — it is not enforced in SQL.

---

### 4. No rejection status in current data

**Severity:** Low — affects completeness of validation workflow reporting.

All non-approved detections remain `pending` indefinitely. There is currently no way
to distinguish "awaiting review" from "reviewed and rejected." Queries asking "how
many detections were rejected?" will return 0.

---

## Tool Scope Limitations

### 5. No population trend or conservation status inference

Canopy reports what was acoustically detected and when. It does not assess whether
a population is stable, growing, or declining. Trend analysis requires a formally
designed monitoring protocol, multi-year abundance modelling, and expert scientific
review.

IUCN threat categories are held in the IUCN Red List — not integrated with Canopy
in v1. See DECISIONS.md § S4 and build step 7.

### 6. No common name lookup

Common names (e.g. "Jocotoco antpitta") are not stored in this database. All
species queries must use scientific binomial names.

### 7. Single-turn query — no conversational memory

Each question is answered independently. Canopy does not maintain conversation
history between queries. A follow-up like "tell me more about those sites" will
not carry context from the previous answer.

### 8. Coordinate data withheld from AI layer

Latitude and longitude columns are stripped before the AI model processes results.
Spatial queries (e.g. "which detections were within 5 km of reserve boundary")
cannot be answered by Canopy.

### 9. Language instruction compliance — secondary layer only (Azure models)

**Severity:** Low — does not affect UI users; affects direct `run_query()` callers only.

The primary language gate in `app.py` rejects non-English/Spanish questions before any
model call is made. A secondary instruction in `schema.py` tells the model to respond in
English if it detects another language — this is the fallback for code paths that call
`run_query()` directly (scripts, integrations, future CLI).

Claude Sonnet 4.6 followed this secondary instruction reliably. Both current Azure models
(gpt-5.1-codex-mini, gpt-5.1-2) do not — a French question submitted via `run_query()`
directly returns a French-language answer despite the instruction. This is confirmed by
eval case A09 across multiple benchmark runs.

**Who is affected:** UI users are fully protected — the primary gate fires first. Only
callers that bypass `app.py` (direct `run_query()` calls) are exposed to this gap.

**Workaround:** All queries routed through the Gradio UI are unaffected. For programmatic
use, callers should enforce language on the input side before calling `run_query()`.

**Long-term fix:** Add a language normalisation guard inside `run_query()` itself, upstream
of the model call, so the secondary layer becomes structural rather than instructional.
See DECISIONS.md § M1.

---

### 10. Cache staleness for live-count and time-anchored queries

Responses are cached for 24 hours, keyed on question text. Queries whose correct
answer changes within that window will return the same answer for up to 24 hours
even if the database has been updated. This affects:

- **Live-count queries** — "How many detections are awaiting review?" returns a
  cached count that becomes stale as validators approve records throughout the day.
- **Time-anchored queries** — "Show me the most recent detection" is stale from the
  moment any new data is uploaded.
- **Week/month boundary queries** — "Which sites had the most activity this week?"
  cached on Monday returns Monday's result on Tuesday.

The ⚡ indicator in the timing footer is the only signal to the user that a cached
answer is being served.

**Long-term fix candidates:** Per-query TTL based on detected time-relative language;
cache invalidation webhook on data upload; shorter TTL for high-churn query patterns.

---

## Open Eval Coverage Gaps

| Gap | Priority | Status |
|---|---|---|
| No eval case checks that `validation_status = 'approved'` filter appears in SQL | High | ✅ Closed — Q31 added 2026-06-30 |
| A09 secondary-layer language compliance fails on both Azure models | Medium | ✅ Documented — primary gate protects UI; secondary-layer gap acknowledged in DECISIONS.md § M1 |
| Q27 guardrail soft-bypass fails on gpt-5.1-codex-mini (conservation priority framing) | Medium | Open — model-compliance issue; gpt-5.1-2 passes; no code fix available |
| No eval case for common-name group queries (birds, frogs) | Medium | Open |
| No eval case that verifies missing-year gaps are noted explicitly in model response | Low | Open |
| Cache staleness handling for time-relative queries untested at the UI level | Medium | Open — E2E mock suite added 2026-07-07, live cache test deferred |
| No E2E test covering the language gate UI path (French question rejected) | Medium | ✅ Closed — added 2026-07-07 |
| No E2E test covering guardrail response (conservation decline) | Medium | ✅ Closed — added 2026-07-07 |
