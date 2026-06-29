# Canopy — Architectural Decisions

> This document records every significant design choice made in Canopy: what was
> decided, why, what was rejected, and whether the reasoning holds under scrutiny.
> It is maintained as a **living record** — each new decision must be written here
> before implementation begins, not after.
>
> Each decision has been reviewed by a second pass (the **audit**) that argues
> against the original reasoning and tests whether consensus survives the challenge.
> Where it does not, the weakness is documented explicitly.

---

## Legend

| Badge | Meaning |
|---|---|
| ✅ **Sound** | Reasoning survives scrutiny; no known gaps |
| ⚠️ **Caveat** | Sound in principle; a specific condition or gap limits it |
| 🔄 **Revisit** | Correct now; a named trigger should prompt review |
| ❌ **Gap** | Known weakness without a current fix — technical debt |

---

## Decision map

### 🔒 Security & Privacy

| # | Decision | Chosen approach | Verdict |
|---|---|---|---|
| S1 | Architecture boundary | Model generates SQL; PostgreSQL executes it | ✅ Sound |
| S2 | Mutation prevention | Dual-layer: regex guard + read-only session | ✅ Sound |
| S3 | Coordinate privacy | Lat/lon stripped before model sees results | ⚠️ Caveat |
| S4 | Validation-status default | Always filter `approved` in system prompt | ⚠️ Caveat |

### 🏗️ Core Architecture

| # | Decision | Chosen approach | Verdict |
|---|---|---|---|
| A1 | Agentic loop safety | `MAX_ITERATIONS = 5` hard cap | ⚠️ Caveat |
| A2 | Tool surface | Single `execute_sql` tool only | ✅ Sound |
| A3 | Model abstraction | Vendor-neutral `ModelClient` ABC | ✅ Sound |
| A4 | Concurrency model | Worker thread + queue, not async | ✅ Sound |
| A5 | Data immutability | `frozen=True` dataclasses throughout | ⚠️ Caveat |

### 💾 Data & Persistence

| # | Decision | Chosen approach | Verdict |
|---|---|---|---|
| D1 | Schema representation | Static constant in `schema.py`, not DB-fetched | ❌ Gap |
| D2 | Query result cache | Exact-match SHA-256 key, 24 h TTL, 200-entry LRU | 🔄 Revisit |
| D3 | Persistence layer | File-based JSONL history + JSON cache | 🔄 Revisit |

### 🧪 Testing & Eval

| # | Decision | Chosen approach | Verdict |
|---|---|---|---|
| T1 | Adversarial eval design | Separate suite, 100% threshold, SQLGuardError = PASS | ✅ Sound |
| T2 | Faithfulness testing | Verbatim DB value in model_text; vacuous pass when precondition unverifiable | ⚠️ Caveat |

### 🎨 Interface & UX

| # | Decision | Chosen approach | Verdict |
|---|---|---|---|
| U1 | UI framework | Gradio Blocks | 🔄 Revisit |

### ⚙️ Operations

| # | Decision | Chosen approach | Verdict |
|---|---|---|---|
| O1 | Configuration access | `config.py` owns all env vars; frozen dataclasses | ✅ Sound |
| O2 | Database connections | Per-query connection, no pooling | 🔄 Revisit |
| O3 | Container security | Non-root user `canopy`; persistent `/data` volume | ✅ Sound |

---

## 🔒 Security & Privacy

---

### S1 — Architecture boundary

> **Files:** `src/canopy/schema.py` · `src/canopy/query/loop.py` · `src/canopy/query/executor.py`

**Decision:** The LLM never has direct database access. It generates a SQL statement; the application executes it and returns only the result. The model sees: the schema description, its own prior messages, and tool call results. It never sees a live database connection.

**Why:** Jocotoco's stated policy is to share schemas and documentation with models — not underlying records. This is also consistent with OWASP LLM Top 10 guidance on sensitive data exposure (LLM02, LLM06). Biodiversity data contains precise species coordinates, study-site locations, and researcher observations. Granting a model unrestricted DB access would mean every query exposes the full record set to the model provider's logging infrastructure.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Direct DB agent (LangChain pattern) | Model connects to DB autonomously. Violates coordinate privacy; removes application-layer control over what data reaches the model context window. |
| Full table dumps in system prompt | Embeds a sample of the data for context. Same exposure problem; cannot represent live query results; doesn't scale. |

**Consequences:**
- The model can only retrieve data by constructing a valid SELECT query — it cannot browse tables freely.
- The application controls row limits (200 rows to model), column filtering (coordinates stripped), and which tool calls are permitted.
- Model provider logs contain only natural-language questions, SQL strings, and truncated result subsets — not full dataset records.

> **Audit verdict — ✅ Sound**
>
> The reasoning directly implements Jocotoco's stated architectural principle and is the correct default for any system handling conservation data. One gap not explicitly documented: the 200-row model display limit (in `loop.py`) is part of this boundary — it should be cross-referenced here, because an unrestricted `SELECT *` on a large table would otherwise send thousands of rows into the model's context window. See **S3** and **A2** for related controls.

---

### S2 — Mutation prevention

> **Files:** `src/canopy/query/executor.py` · `src/canopy/db/connection.py`

**Decision:** Two independent layers block the model from issuing mutating SQL.

| Layer | Where | Mechanism |
|---|---|---|
| 1 | `executor.py` | Comments stripped (`-- ...`, `/* ... */`), then first token checked — only `SELECT` or `WITH` (CTEs) allowed. `SQLGuardError` raised before any DB contact. |
| 2 | `connection.py` | `conn.set_session(readonly=True)` — PostgreSQL rejects any mutation at the server level, regardless of what the application sends. |

**Why:** A single enforcement point is a single point of failure. If the regex guard is bypassed by an unusual query form, the DB-level guard still applies independently. If a future code path creates a connection that skips the executor, the session flag still applies.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Regex guard only | One bypass vector succeeds. Not acceptable. |
| DB read-only user only | Relies on correct role configuration in every environment. Produces an opaque DB error rather than a structured `SQLGuardError` with the rejected SQL attached. |
| ORM / query builder (SQLAlchemy) | Would prevent raw SQL injection at the construction level but requires schema binding and adds a large dependency. Rejected for this stage. |

**Consequences:**
- `SQLGuardError` carries the rejected SQL so the UI can show it to the user in the SQL tab — aiding debugging without exposing internal tracebacks.
- Defence-in-depth: both layers must be bypassed simultaneously for a mutation to succeed.

> **Audit verdict — ✅ Sound**
>
> **Challenge raised and resolved:** `SELECT INTO` in PostgreSQL creates a new table (equivalent to `CREATE TABLE AS SELECT`). The regex guard allows it because the first token is `SELECT`. Layer 2 (`readonly=True`) blocks it at the server — PostgreSQL treats table creation as a write operation. The dual-layer design correctly handles this edge case even though the guard alone does not. No change needed; this validates the two-layer approach rather than undermining it.

---

### S3 — Coordinate privacy

> **Files:** `src/canopy/query/loop.py` — `_SENSITIVE_COLUMNS`, `_format_result()`

**Decision:** `latitude` and `longitude` are removed from query results before they are formatted into the model's context, even if the generated SQL explicitly requests them. The user-facing Results tab still shows full data (including coordinates), which is appropriate — the human researcher already has authorised access to this information.

**Why:** Precise species coordinates are operationally sensitive — they can reveal nesting sites, protected individuals, or research locations. The risk being addressed is model-provider log access to these values. A researcher querying "how many detections?" does not need the model to reason over GPS coordinates.

**Implementation:**
```python
_SENSITIVE_COLUMNS = frozenset({"latitude", "longitude"})
# applied in _format_result() before the tool result is appended to messages
```

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Remove from `schema.py` only | The model might infer column names from context or database conventions and include them anyway. Stripping at result time is a hard guarantee. |
| Strip at UI layer | Model's context window already received the coordinates. The problem is in the prompt, not the display. |
| Reduce coordinate precision (fuzzy rounding) | More nuanced — the model could still reason about regions. Not implemented because the use case (answering species count questions) has no need for coordinates at all. |

**Consequences:**
- The model can never reason over precise coordinates, even if it generates SQL that includes them.
- `_SENSITIVE_COLUMNS` is a **hardcoded Python set**. Adding a new sensitive column (e.g. `observer_name`, `recording_device_id`) requires a code change.

> **Audit verdict — ⚠️ Caveat**
>
> The design is correct. The caveat is real and unaddressed: the scope of "sensitive" is not formally defined. Observer names, recording device IDs (which may imply location by association), and timestamp precision could all be argued as sensitive depending on context. The current implementation protects against the most obvious risk (GPS coordinates) but has no mechanism for systematically identifying future sensitive columns without a code change.
>
> **Recommended fix:** Move `_SENSITIVE_COLUMNS` to a config value (env var or `.env`) so it can be updated without a code change. Longer term, drive it from a database column annotation (`information_schema` custom comment or a `sensitive_columns` config table). Until then, document the current set explicitly and add a test that verifies each column in the set is actually present in the schema.

---

### S4 — Validation-status default filter

> **Files:** `src/canopy/schema.py` — `_GUARDRAILS`

**Decision:** The system prompt instructs the model to always filter `validation_status = 'approved'` unless the user explicitly asks for pending or unvalidated records.

**Why:** The monitoring database contains detections in two validation states: `approved` (human expert confirmed) and `pending` (AI detection awaiting human review). Including pending records in conservation queries would produce misleading counts that do not represent confirmed species observations.

**Actual DB values (verified 2026-06-27 via direct query):**

| Status | Count | Meaning |
|---|---|---|
| `pending` | 22,757 | AI detection awaiting human validation |
| `approved` | 14,060 | Human expert confirmed genuine detection |

There is no explicit rejection status in the current dataset. Detections not approved remain `pending` indefinitely.

> ⚠️ **Schema drift incident (2026-06-27):** The original `schema.py` documented `validated_true`, `validated_false`, and `unvalidated` — values that do not exist in the database. This was discovered during Playwright UI testing when the model self-flagged "a technical discrepancy was found." Queries filtering on `validated_true` returned 0 rows despite 14,060 approved detections existing. Fixed by updating `schema.py` to use `approved`/`pending`. Root cause: the schema constant was written from design documentation rather than verified against the live database.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| No default filter | Requires every user to know validation states exist and specify them explicitly. Non-technical users will not. Rejected: default-safe is essential. |
| Hard-inject `WHERE validation_status = 'approved'` at executor level | Hides the filter from the model. The model's answer may not match what was queried, producing confusing discrepancies. Also prevents legitimate queries about pending data. |
| Separate endpoints for validated/all data | Forces users to choose before they understand the question. Rejected: wrong UX for a natural-language interface. |

**Consequences:**
- Non-technical users get scientifically correct answers by default.
- The model can handle exceptions ("show me pending detections for review") because the instruction is conditional, not absolute.
- **This is soft enforcement (a prompt instruction), not hard enforcement (a SQL constraint).** The model could theoretically omit the filter.
- **Schema drift risk remains** — if the DB validation status values change, `schema.py` must be updated manually. The correct long-term fix is a CI test that queries `information_schema` and asserts the documented values match actual DB values. See also **D1**.

> **Audit verdict — ⚠️ Caveat**
>
> The soft enforcement is the right design choice (see "hard inject" rejection above). But there is no test that verifies the model actually follows this instruction in practice. The ground-truth eval set (`tests/eval/`) should include at least one case: "How many detections are there?" → result must include `validation_status = 'approved'` in the generated SQL. Without this, the guarantee is aspirational, not verified.
>
> **Recommended fix:** Add an eval case that checks the SQL for the presence of the validation filter on ambiguous queries. Flag in CI if the filter is absent.

---

## 🏗️ Core Architecture

---

### A1 — Agentic loop safety

> **Files:** `src/canopy/query/loop.py` — `MAX_ITERATIONS = 5`

**Decision:** The query loop runs for at most 5 model–tool–result cycles. If the model has not produced a final answer by then, a `RuntimeError` is raised.

**Why:** An unbounded loop risks both infinite execution and runaway API costs. NL-to-SQL for a single well-documented schema rarely needs more than two iterations (generate SQL → execute → write answer). Three handles error recovery (bad SQL → refine → re-execute). Five is a generous ceiling.

**Cost ceiling:**

| Model | Per-call cost (est.) | Max per query |
|---|---|---|
| Claude Sonnet 4.6 | ~$0.01 | ~$0.05 |
| Claude Opus 4.8 | ~$0.15 | ~$0.75 |

**Consequences:**
- A malformed or genuinely unanswerable question raises `RuntimeError` after 5 attempts.
- The UI shows a human-readable error; the exception is logged.
- The limit is not configurable without a code change.

> **Audit verdict — ⚠️ Caveat**
>
> **Challenge:** The number 5 has no empirical basis. It was chosen without measurement. The eval set (`tests/eval/`) contains 30+ ground-truth queries but does not currently log iteration counts. It is possible that real queries never need more than 3, which means 5 is safe-but-opaque, or that some legitimate complex queries need 4–5, which means the ceiling is tighter than it appears.
>
> **Recommended fix:** Log `iteration` count at INFO level for every completed query (already present in `_log.info`). After 20–30 real-world queries, inspect the distribution. If P99 ≤ 3, lower `MAX_ITERATIONS` to 4. If any query hits 5 and fails, raise it to 6. Make the number data-driven.

---

### A2 — Tool surface

> **Files:** `src/canopy/query/loop.py` — `EXECUTE_SQL_TOOL`

**Decision:** The model has access to exactly one tool: `execute_sql`. It cannot list tables, introspect columns at runtime, call external APIs, or search history via a tool call.

**Why:** Minimising the tool surface minimises the attack surface. The static `SCHEMA_CONTEXT` in `schema.py` already provides the model with the database structure it needs — a `describe_table` tool would add a round-trip without new information. Each additional tool is a new surface for prompt injection and a new result-handling code path.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| `describe_table` tool | Redundant with `SCHEMA_CONTEXT`; adds a round-trip; complicates the loop. |
| `search_iucn` / `call_earthranger` | Multi-source queries. In scope as future work but each requires: (1) security review of the new data source, (2) analysis of what sensitive data that source returns, (3) a new `_SENSITIVE_COLUMNS`-equivalent for that source. Not appropriate to add without that review. |
| `list_recent_history` | Useful for UX but raises privacy questions (whose history?). Deferred. |

**Consequences:**
- Questions that require data from other sources (IUCN conservation status, EarthRanger patrol sightings) correctly result in "I don't have that data" rather than hallucination.
- Adding a future tool requires the same security analysis as `execute_sql`: what data does it return? What is the sensitive-column equivalent? Can the result be prompt-injected?

> **Audit verdict — ✅ Sound**
>
> Correct. The single-tool design is the right conservative starting point. Adding tools is easy; removing them after users depend on them is hard.

---

### A3 — Vendor-neutral model interface

> **Files:** `src/canopy/models/base.py` · `src/canopy/models/anthropic.py` · `src/canopy/models/__init__.py`

**Decision:** All model interaction goes through the `ModelClient` abstract base class. The Anthropic SDK is fully encapsulated in `models/anthropic.py`. `get_model_client()` reads `MODEL_BACKEND` and returns the appropriate concrete class. Adding a new backend means adding one file — not modifying the loop.

**Interface:**
```python
class ModelClient(ABC):
    def generate(self, system_prompt, messages, tools) -> ModelResponse: ...
    def format_assistant_turn(self, response) -> dict: ...
    def format_tool_results(self, results: list[tuple[str, str]]) -> dict: ...
```

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Anthropic SDK directly in the loop | Simplest today. Tight coupling makes future migration or testing expensive. |
| LiteLLM | Unified interface library. Large dependency; adds version risk; provides what the ABC gives for free. |

**Consequences:**
- Tests use `MagicMock` of `ModelClient` without importing the Anthropic SDK.
- `format_tool_results()` returns a message dict — the internal format is vendor-specific (Anthropic's `tool_result` block), but the caller (`loop.py`) is insulated from it.

> **Audit verdict — ✅ Sound**
>
> **Challenge raised:** `format_tool_results()` returns a dict in Anthropic's specific `{"role": "user", "content": [{"type": "tool_result", ...}]}` format. An OpenAI backend would return a different structure. Does the abstraction leak?
>
> **Resolution:** No. The caller (`loop.py`) passes the returned dict directly to `messages.append()` without inspecting its contents. Each backend is responsible for producing a dict its `generate()` method understands on the next call. The caller never interprets the format. The abstraction holds.

---

### A4 — Concurrency model

> **Files:** `src/canopy/ui/app.py` — `threading.Thread`, `queue.Queue`

**Decision:** `_run_query_handler` spawns a daemon worker thread to run `run_query()`. A `queue.Queue` passes status messages from the worker back to the generator, which yields them to Gradio. No `asyncio`.

**Why:** Gradio 6's generator protocol is synchronous. A generator function `yield`s values; Gradio streams each to the browser. `run_query()` blocks for 10–90 seconds. A worker thread decouples the blocking work from the generator's ability to yield status updates. `queue.Queue` is thread-safe and correct for one-producer / one-consumer.

**Why not async:**
- `psycopg2` is synchronous by design. True async would require migrating to `asyncpg` — a significant DB layer change for no net gain.
- Gradio 6 does not natively support async generators in the Blocks event handler pattern used here.
- The `queue.Queue` approach is simpler, debuggable, and has no dependency on event loop state.

**Consequences:**
- The `daemon=True` flag means the worker is abandoned (not cleanly stopped) if the Gradio process exits mid-query. The PostgreSQL connection will eventually time out server-side.
- The `None` sentinel in the queue guarantees the generator waits for the worker to complete before reading `result_holder`.

> **Audit verdict — ✅ Sound**
>
> **Challenge raised:** A daemon thread killed mid-query leaves a psycopg2 connection open server-side until it times out. PostgreSQL's default `statement_timeout` is infinite. This is a resource leak on abrupt shutdown.
>
> **Partial mitigation:** The executor closes the connection in a `finally` block — if the thread is killed before the DB call completes, the OS will reclaim the file descriptor. The real exposure is a long-running SQL query that is already executing when the process exits. At current query volumes this is unlikely to cause connection exhaustion, but it should be acknowledged.
>
> **Recommended fix (low priority):** Set `statement_timeout` in the psycopg2 connection options (e.g. `options="-c statement_timeout=30000"`) to bound any runaway SQL to 30 seconds.

---

### A5 — Data immutability

> **Files:** `src/canopy/config.py` · `src/canopy/query/executor.py` · `src/canopy/query/loop.py`

**Decision:** `ModelConfig`, `DBConfig`, `QueryResult`, and `LoopResult` are all `frozen=True` dataclasses. Setting any field after construction raises `FrozenInstanceError` immediately.

**Why:** Configuration and result objects passed between modules should not be mutated after construction. Accidental mutation (e.g. middleware appending to a results list) is a silent bug that frozen dataclasses catch at the language level.

**Consequences:**
- Any accidental `result.model_text = "..."` fails loudly.
- `LoopResult.timing` is a `dict` — it is not frozen. Its contents can be mutated even though the reference cannot be replaced.
- `LoopResult.rows` is a `list[tuple]` — the list itself can be mutated with `.append()`. Assigning a new list is blocked; appending to the existing list is not.

> **Audit verdict — ⚠️ Caveat**
>
> The frozen constraint on the reference is sound. The mutable container content is a real gap: `result.rows.append(("injected",))` succeeds silently. For a research tool at current scale this is unlikely to matter — there is no multi-tenant shared state where one user's result could be contaminated by another's. But it is worth knowing the immutability guarantee is shallower than it appears.
>
> **If this matters:** Change `rows: list[tuple]` to `rows: tuple[tuple, ...]` in `LoopResult` and update all call sites to produce tuples. This is a straightforward change that would make the guarantee complete. Not blocking, but the right long-term posture.

---

## 💾 Data & Persistence

---

### D1 — Schema representation

> **Files:** `src/canopy/schema.py` — `SCHEMA_CONTEXT`

**Decision:** The database schema, business context, join patterns, and guardrails are written by hand as a Python string constant. There is no runtime call to `information_schema`. The constant is computed once at import time and reused across every model call.

**Why:** `information_schema` provides column names and types — but not business context. The model needs: what does `validation_status` mean? Which join is canonical? Which columns are sensitive? Which data sources are out of scope? None of this can be derived from the database itself. A hand-written schema description is the only way to encode this semantic layer.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Fetch `information_schema` at startup | Provides accurate structure but no business context. Model would generate worse SQL without the guidance in `SCHEMA_CONTEXT`. Adds DB dependency at startup. |
| Hybrid: fetch structure + manual annotation overlay | More accurate column lists. Added complexity without proportionate benefit at current table count. |
| External documentation file (YAML/JSON schema) | Would decouple schema from code. Worth considering when the schema stabilises. Currently the schema is still evolving. |

**Consequences:**
- Startup is fast; schema context is available before a DB connection is established.
- **Schema drift is certain, not hypothetical.** When columns are added or renamed in PostgreSQL, `schema.py` will not update automatically. The model will generate SQL against a description that no longer matches reality.
- There is no automated check that `schema.py` matches the live database.

> **Audit verdict — ❌ Gap**
>
> This is the only decision in the document with a known, unmitigated failure mode that will definitely occur as the project matures. A "process requirement" (update `schema.py` when the DB changes) is not enforcement — it is a social contract that breaks under time pressure.
>
> **Required fix before production:** Add a CI/integration test that connects to a test database and verifies:
> 1. Every table name mentioned in `SCHEMA_CONTEXT` exists in the DB.
> 2. Every column name mentioned in `SCHEMA_CONTEXT` exists in its table.
> 3. Every column in `_SENSITIVE_COLUMNS` exists in the DB.
>
> This test can run against the real DB in a staging environment. It converts a social contract into an automated gate. Until this exists, schema drift will produce silently wrong SQL that is hard to debug.

---

### D2 — Query result cache

> **Files:** `src/canopy/cache.py` · `src/canopy/query/loop.py`

**Decision:** Identical questions (after normalisation) return cached results without hitting the model or database. Key = SHA-256 of lowercased, whitespace-collapsed question (16 hex chars). TTL 24 h (configurable). Max 200 entries, evicting by age. Atomic writes via `.tmp` rename.

**Normalisation:**
```python
q = unicodedata.normalize("NFC", question)       # added: Spanish accent variant safety
normalised = re.sub(r'\s+', ' ', q.casefold().strip())
key = hashlib.sha256(normalised.encode()).hexdigest()[:16]
```
"Which birds?" and `"  which birds?  "` → same cache key. "Which birds?" and "Which mammals?" → different keys. "¿Cuántas?" typed NFC vs NFD composition → same key. "¿Cuántas?" (Spanish) and "How many?" (English) → different keys, by design: `LoopResult.model_text` is language-specific; sharing a cache entry would serve an English-language answer to a Spanish asker.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| In-memory cache (dict) | Lost on restart. Cross-session reuse is a key benefit. |
| Redis | Adds infrastructure dependency. Overkill for single-instance; revisit with multi-instance scaling. |
| Semantic / embedding-based cache | Would catch paraphrases. Requires an embedding model call on every question (adds latency and cost). Highest-value hits (copy-paste, history re-runs) are covered by exact match. Correct next step once usage patterns are known. |
| Event-driven cache invalidation | Clear cache when new data is loaded, not after N hours. Requires a hook from the data pipeline — not in scope for the current architecture. |

**Consequences:**
- Cache hits return in < 100 ms; UI shows "⚡ Cached result · Xh ago."
- Rephrased questions ("Which birds?" vs "What bird species showed up?") miss the cache and pay full cost.
- The cache file lives in `$CANOPY_DATA_DIR/cache.json`. In Docker: the persistent `/data` volume. Locally: `~/.canopy/cache.json`.

> **Audit verdict — 🔄 Revisit**
>
> **Challenge 1 — TTL mismatch with data update frequency.** The 24 h TTL assumes biodiversity data changes slowly. But if a batch of detections is validated at 15:00, answers cached at 09:00 become wrong immediately. The right invalidation strategy is event-driven (clear cache when new data is loaded), not time-based. Until the data pipeline has a cache-invalidation hook, the 24 h TTL is an approximation. Operators should be aware: set `CANOPY_CACHE_TTL_HOURS` to match the actual data import cadence.
>
> **Challenge 2 — Entry size.** The cache stores full result rows. A query returning 50,000 rows (capped at 200 for the model but stored in full in `LoopResult.rows`) can produce a large cache entry. There is no per-entry size limit. Monitor `cache.json` file size in production.
>
> **Trigger for revisit:** (a) When data import frequency is established, align TTL or implement event-driven invalidation. (b) When users report stale results. (c) When `cache.json` exceeds 10 MB.

---

### D3 — Persistence layer

> **Files:** `src/canopy/history.py` · `src/canopy/cache.py` · `src/canopy/config.py`

**Decision:** Query history uses append-only JSONL (`history.jsonl`); the result cache uses a JSON dict (`cache.json`). Both live in `CANOPY_DATA_DIR` (default: `~/.canopy`).

**Format rationale:**
- **JSONL history:** append-only — no read-before-write on every query. A corrupt entry on one line doesn't affect others. Easy to tail / stream.
- **JSON cache:** single dict — O(1) key lookup. Read-modify-write is acceptable because writes are infrequent and atomic (`.tmp` rename prevents corruption).

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| SQLite | Reasonable middle ground. Adds schema migration management and a dependency for marginal benefit at current scale. Reconsider if history becomes a reporting asset. |
| PostgreSQL table | Would require the app DB to be writable — contradicts the read-only connection design (S2). Rejected. |
| Redis | Correct for multi-instance cache. Overkill for single-instance. See scaling caveat below. |

**Consequences:**
- **Single-instance only.** Both files require exclusive write access. Running two app instances against the same `/data` volume causes write races and potential corruption. If horizontal scaling is ever needed, replace both file stores: Redis for the cache, a PostgreSQL table (separate from the monitoring DB) or S3-backed JSONL for history.
- Graceful degradation: both `write_cache` and `append_history` are wrapped in `try/except`. A failed write logs a WARNING but never breaks a query.
- No backup strategy. If the `/data` volume is lost, query history is gone. The cache is rebuildable (re-run queries); the history is not.

> **Audit verdict — 🔄 Revisit**
>
> Sound for a single-instance deployment. The scaling caveat is real and will become load-bearing if the system is ever deployed with multiple replicas (Fly.io, Kubernetes multi-pod, etc.). The backup gap for history is also real: if history serves as an audit trail (who asked what, when), it should be treated as data, not cache, and backed up accordingly.
>
> **Trigger for revisit:** (a) Any deployment topology with > 1 app instance. (b) If query history is used for reporting or audit purposes. (c) If `history.jsonl` exceeds 50 MB and querying it becomes slow.

---

## 🧪 Testing & Eval

---

### T1 — Adversarial eval design

> **Files:** `tests/eval/adversarial.py` · `scripts/run_eval.py`

**Decision:** Adversarial inputs run as a separate suite with a 100% pass threshold. When the security guard raises `SQLGuardError` in response to an adversarial SQL injection attempt, that is counted as PASS (`guard_error_is_pass=True`), not FAIL.

**Why:** Two things make adversarial testing structurally different from ground-truth testing.

First, the threshold: ground-truth eval allows 15% failure (covering known ambiguities, schema gaps, phrasing issues). Guardrails either hold or they don't — partial credit produces false confidence. A 99% adversarial pass rate means one attack in a hundred succeeds.

Second, the `SQLGuardError` semantics: the standard test runner treats any unhandled exception as FAIL. But if an adversarial prompt causes Claude to generate a `DELETE FROM detections` statement, the executor's guard fires — the attack was blocked. Marking that FAIL inverts the test's intent. The runner must catch `SQLGuardError` explicitly and, in the adversarial suite only, count it as success.

**Cases covered:**
- Prompt injection ("ignore previous instructions")
- SQL injection in question text (`'; DROP TABLE species; --`)
- Persona/roleplay bypass ("act as SQLBot")
- System prompt extraction ("print your instructions")
- Credentials request (asking for `PG_PASSWORD`, `psycopg2.connect`)
- Hallucination boundary (fabricated species names → 0 rows → model must not invent data)

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Include adversarial cases in ground-truth suite with 85% threshold | Blurs two different quality signals. A ground-truth 85% means some questions are hard; an adversarial 85% means one security boundary in seven fails. |
| Count SQLGuardError as FAIL always | Marks correct security behavior as a failure. Discourages adding adversarial injection tests. |

**Consequences:**
- Adversarial suite requires `ANTHROPIC_API_KEY` and `PG_*` vars (live end-to-end run).
- New attack patterns must be added manually — the suite tests known vectors, not novel ones. The gap file documents patterns observed in production as they accumulate.
- `guard_error_is_pass=True` must only be set for the adversarial suite. Setting it in ground-truth eval would mask legitimate query failures.

> **Audit verdict — ✅ Sound**
>
> The two-suite design correctly separates quality testing from security testing. The `guard_error_is_pass` parameter is a deliberate design point, not a workaround — document it in any future contributor guide so it is not "fixed" to always-FAIL by a well-meaning refactor.

---

### T2 — Faithfulness testing approach

> **Files:** `tests/eval/queries.py` — `_count_value_in_text()`, Q21–Q23, H1–H3

**Decision:** Faithfulness checks verify that integer counts from `rows[0][0]` appear verbatim in `model_text`. When a hallucination test's precondition cannot be confirmed (the fabricated species name actually exists in the DB), the check returns `True` (vacuous pass) rather than `False`.

**Why:** Two problems make LLM faithfulness testing harder than ML eval.

First, ground truth is dynamic. We cannot pre-compute expected model outputs because the DB content changes. A test that hardcodes "35741" will break after the next data import. The only reliable check is: whatever the DB returned (`rows[0][0]`), that value must appear in `model_text`. This works regardless of DB state.

Second, hallucination tests depend on the DB not having the test entity. "Fictus imaginarius" is intended to be a non-existent species. If it somehow exists (a researcher added it, a test fixture left it, a future import included it), asserting 0 rows produces a false positive failure with no relation to model behavior. Returning `True` (skip) is honest: "we cannot test this right now" is better than "the model hallucinated" when the data changed.

**What this catches:** A model that says "there are 450 species" when the DB returned 423. A model that makes up detections when the query returns 0 rows.

**What this does not catch:** Semantic faithfulness — "approximately 35,000 detections" is arguably correct when the count is 35,741, but fails the verbatim check. Closing this gap requires an LLM-as-judge (Claude evaluating Claude's output). RAGAS and DeepEval are the relevant libraries. Not implemented; tracked as a gap.

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| Hardcode expected values per query | Breaks on every data import. Not maintainable. |
| Return False (FAIL) when hallucination precondition unmet | Produces misleading red CI unrelated to model behavior. |
| LLM-as-judge for all faithfulness | Correct long-term. Adds cost and latency; adds a recursive dependency (model evaluating itself). Deferred. |

**Consequences:**
- Hallucination tests (H1–H3) may vacuously pass in perpetuity if the fabricated species names are ever imported. Monitor: if H1–H3 always return True in production runs, the test cases need new fabricated names.
- The verbatim number check will fail if the model rounds or abbreviates counts ("over 35,000" instead of "35,741"). This is intentional — the model should cite exact DB values, not approximations.

> **Audit verdict — ⚠️ Caveat**
>
> The verbatim check is the right conservative baseline. The caveat is real: it will fail on semantically faithful but non-verbatim answers. Track in production; if false failures accumulate, add a rounding tolerance or move to LLM-as-judge for aggregates.

---

## 🎨 Interface & UX

---

### U1 — UI framework

> **Files:** `src/canopy/ui/app.py`

**Decision:** The application UI is built with Gradio `gr.Blocks`. No custom JavaScript, no separate frontend build step. The entire frontend is written in Python.

**Why:** The primary users are Jocotoco science staff — internal, technically literate, but not requiring a polished public-facing product. Gradio lets a Python developer own the entire stack.

**Detailed comparison — the question the original decision glossed over:**

| Framework | Generator streaming | Layout control | Auth | Multi-user isolation | When to choose |
|---|---|---|---|---|---|
| **Gradio Blocks** | ✅ Native | ✅ Good | ⚠️ Primitive basic auth | ❌ No session isolation | Internal research tool, single developer |
| **Streamlit** | ⚠️ `st.status()` experimental | ⚠️ Limited | ⚠️ Primitive | ⚠️ Per-session state | Data science dashboards, simpler apps |
| **React + FastAPI** | ✅ Via SSE or WebSocket | ✅ Complete | ✅ Proper auth (OAuth, JWT) | ✅ Full isolation | Public-facing app, polished product |
| **Next.js + FastAPI** | ✅ Via streaming fetch | ✅ Complete | ✅ Auth.js, Clerk | ✅ Full isolation | Public SaaS product |

**Why Gradio over Streamlit specifically:**
- Gradio's generator protocol (`yield`-based streaming) integrates naturally with the worker-thread + queue model in `app.py`. Each `yield` from the generator is streamed to the browser with zero additional infrastructure.
- Streamlit's streaming support (`st.status()`, `st.write_stream()`) was experimental when this was built and requires a different event model that would require restructuring the query loop.
- Gradio's `gr.Blocks` gives finer two-panel layout control than Streamlit's top-down column model.

**Why not React + FastAPI today:**
- Requires a JS build pipeline (Webpack/Vite), separate dev server, CORS configuration, and frontend deployment — a full additional development environment.
- Requires a TypeScript/JS developer or Ajinkya to context-switch across the full stack.
- The correct long-term answer if the tool becomes public or user count grows beyond ~10.

**Consequences:**
- UI customisation is bounded by Gradio's component model. Complex interactions (map visualisations, multi-step workflows, drag-and-drop uploads) require hacking around Gradio or are impossible.
- **No authentication.** Gradio offers HTTP basic auth (username/password in `.launch(auth=...)`), but no per-user session isolation. All users of the same instance share one query history, one cache, and see each other's recent queries in the history radio. If Jocotoco deploys this for multiple staff, they will see each other's queries.
- **No audit logging.** There is no record of which user asked which question. JSONL history logs questions but not who asked them.

> **Audit verdict — 🔄 Revisit**
>
> Gradio is the right choice for the current stage: single developer, internal tool, rapid iteration. The decision is sound today. But two gaps must be acknowledged and not papered over:
>
> **Gap 1 — No per-user isolation.** If more than one person uses the same instance simultaneously, their histories are interleaved in the radio list, and the cache is shared (which is actually fine). This is not a security issue (all users are authorised staff) but it is a UX problem. Short-term fix: deploy one instance per user or per team. Medium-term: add Gradio basic auth per user + separate CANOPY_DATA_DIR per user. Long-term: React frontend.
>
> **Gap 2 — No authentication.** Anyone who can reach the URL can use the system. If the instance is not firewalled to the Jocotoco network, it is publicly accessible. Ensure deployment is network-restricted or add Gradio's `auth=` parameter before any semi-public deployment.
>
> **Trigger for revisit:** (a) More than ~5 concurrent users. (b) Any external or public access. (c) Need for user-level audit logging. (d) Need for data visualisations beyond tables.

---

## ⚙️ Operations

---

### O1 — Configuration access

> **Files:** `src/canopy/config.py`

**Decision:** All `os.getenv` calls in the codebase live in `config.py`. Nothing else reads environment variables directly. `ModelConfig` and `DBConfig` are `frozen=True` dataclasses. The getters validate required variables and raise `ValueError` with a precise list of what is missing.

**Why:** Scattered `os.getenv` calls make credential handling impossible to audit without reading every file. A single module that owns all environment access is auditable in one place and makes security review tractable.

**Consequences:**
- Adding a new environment variable requires editing `config.py`, making it visible in code review.
- `.env.example` documents every variable; `config.py` validates the required subset at connection time (not import time).
- The validation produces a message like "Missing required DB config vars: PG_HOST, PG_PASSWORD" rather than a cryptic `KeyError` or `NoneType` connection error.

> **Audit verdict — ✅ Sound**
>
> The design is correct. One nuance worth noting: validation happens at connection time (when `get_db_config()` is called), not at application startup. A misconfigured `MODEL_BACKEND` will not be caught until the first query is attempted. For an internal tool this is acceptable; for a production service, a startup health check that calls all config getters would surface misconfigurations before the first user request.

---

### O2 — Database connections

> **Files:** `src/canopy/db/connection.py` · `src/canopy/query/executor.py`

**Decision:** `get_connection()` opens a new `psycopg2` connection for each query. The executor closes it in a `finally` block regardless of success or failure.

**Why:** At expected load (science staff running individual queries, not automated pipelines), connection setup time is negligible relative to model latency (10–90 seconds). A connection pool would add complexity and state management with no measurable benefit at this scale.

**Connection overhead in context:**
- psycopg2 connection setup: ~5–20 ms
- Model API call: 2,000–20,000 ms
- Ratio: < 1% overhead

**Alternatives considered:**

| Alternative | Why rejected |
|---|---|
| `psycopg2.pool.ThreadedConnectionPool` | Adds pool lifecycle management, size tuning, and connection leak detection. Not justified at < 1 QPS. |
| `asyncpg` with async pool | Requires migrating the entire DB layer to async. See **A4** for why async was rejected. |
| pgbouncer (external proxy) | Infrastructure-level pooling. Correct at high load; overkill for a single-user tool. |

**Load threshold for revisit:**

| Concurrent queries | psycopg2 connections | PostgreSQL backend processes | Action |
|---|---|---|---|
| 1–5 | 1–5 | 1–5 | No action needed |
| 5–20 | 5–20 | 5–20 | Monitor; consider pool |
| > 20 | > 20 | > 20 | Add `ThreadedConnectionPool` |

> **Audit verdict — 🔄 Revisit**
>
> Sound at current load. The threshold table above makes the revisit condition concrete. Monitor PostgreSQL `pg_stat_activity` in production; if concurrent connection count routinely exceeds 10, add a pool. Log connection setup time if it starts appearing in query timing breakdowns.

---

### O3 — Container security

> **Files:** `Dockerfile`

**Decision:** The Docker image creates a `canopy` user (`useradd -m canopy`) and runs the application as that user. Persistent data lives at `/data` as a Docker VOLUME, mapped to `CANOPY_DATA_DIR`.

**Why:**
- **Non-root:** If the container is compromised, a non-root process has a smaller blast radius than a root process. This is a standard container security baseline.
- **Persistent volume:** History and cache must survive container restarts. A VOLUME at `/data` decouples persistent state from the container image lifecycle.

**Dockerfile pattern (volume ownership handled in the build layer):**
```dockerfile
RUN useradd -m canopy && mkdir -p /data && chown canopy:canopy /data
USER canopy
ENV CANOPY_DATA_DIR=/data
VOLUME ["/data"]
```

The `chown` must happen before the `USER` switch: Docker initialises a named volume
from the image layer at that path, so the ownership set in the `RUN` layer propagates
into any freshly-mounted volume. Declaring `VOLUME` after `USER canopy` without the
prior `chown` would create the volume as root:root and deny writes to the non-root process.

**Consequences:**
- **Non-root blast radius:** A compromised container process runs without root privileges.
- **Persistent volume:** History and cache survive container restarts via the `/data` VOLUME.
- **Volume ownership** is handled entirely within the image build — no entrypoint.sh or host-side `chown` is required. The `make smoke` check 2 validates this on every Docker build.

> **Audit verdict — ✅ Sound**
>
> The design is correct and the volume ownership issue has been resolved in the Dockerfile directly (2026-06-27). The previous caveat about entrypoint.sh is no longer applicable.

---

## Maintenance rules

1. **Write before you build.** Add a section here before starting implementation. The discipline of articulating the decision first is the point — it prevents decisions made by inertia or deadline pressure from becoming invisible technical debt.

2. **Audit every entry.** Each decision must have a genuine challenge ("what if this is wrong?") and a documented response. If the challenge wins, the verdict must say so.

3. **Update when superseded.** If a decision is reversed, mark its row in the Decision Map as "Superseded by [#]" and add a note explaining what changed and why the original reasoning no longer holds.

4. **Keep the table honest.** A ✅ that should be 🔄 is more dangerous than an acknowledged ❌. The purpose of this document is institutional honesty, not institutional confidence.
