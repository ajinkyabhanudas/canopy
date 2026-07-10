<img src="docs/banner.svg" alt="CANOPY" width="100%"/>

[![CI](https://github.com/ajinkyabhanudas/canopy/actions/workflows/ci.yml/badge.svg)](https://github.com/ajinkyabhanudas/canopy/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/ajinkyabhanudas/canopy/branch/main/graph/badge.svg)](https://codecov.io/gh/ajinkyabhanudas/canopy)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://docs.python.org/3.11/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](Dockerfile)
[![DB: read-only](https://img.shields.io/badge/database-read--only-brightgreen)](DECISIONS.md)

A natural language query tool for Jocotoco's bioacoustic species-monitoring
database. Ask a question in plain English (or Spanish), canopy translates it
into a SQL query, executes it read-only against the database, and returns a
plain-language answer alongside the SQL for inspection.

## Example

```
Q: How many confirmed species were detected at each reserve in 2023?

Answer:
  In 2023, confirmed species were detected across 14 recording sites.

  Key findings:
  • Buenaventura led with 63 confirmed species.
  • El Pambilar recorded 41 species.
  • La Hesperia recorded 38 species.

  ⚠️ Data notes: Figures show detections with validation_status = 'approved'.
  For external reports, ask the science team to verify these figures.

SQL (shown in "Database query" tab):
  SELECT si.name AS site, COUNT(DISTINCT d.species_id) AS species_count
  FROM detections d
    JOIN sites si ON d.site_id = si.id
  WHERE d.validation_status = 'approved'
    AND EXTRACT(YEAR FROM d.recorded_at) = 2023
  GROUP BY si.name
  ORDER BY species_count DESC
```

---

| Idle — question input, history sidebar, example prompts | Result — structured answer with headline, findings, and data notes |
|---|---|
| ![Canopy idle state](docs/screenshots/01-idle.png) | ![English query result](docs/screenshots/04-english-sites-answer.png) |

---

## What it does

- Accepts natural language questions in **English or Spanish** — responds in
  whichever language you write in, without any configuration.
- Uses Claude to generate a PostgreSQL SELECT query — never guesses results.
- Executes read-only against PostgreSQL and returns a structured answer
  (headline → key findings → data notes) alongside the data table and SQL.
- Caches results for 24 hours by question text so repeated queries return
  instantly without an LLM or DB call.
- Streams live progress while the query runs — what the model understood, which
  pipeline stage is active, how many records were found.
- Persists query history to disk (last 20 queries surfaced in the UI sidebar);
  clicking a history item auto-runs the query from cache.
- Never infers population trends or conservation status — that requires a formal
  scientific review process, not automated inference.
- Precise species coordinates are filtered before any data reaches the AI layer,
  keeping sensitive biodiversity locations out of the model context.
- Vendor-neutral model interface: swapping the LLM means adding one adapter file.

## Requirements

- Python 3.11+ (local) or Docker (recommended for deployment)
- At least one model API key (Anthropic and/or Azure AI Foundry)
- PostgreSQL credentials for the VAJocotoco database

---

## Quickstart — Docker (recommended)

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in all required values. Never commit `.env`.

**Model connections** are declared in `models.yaml` at the project root (safe to commit —
no secrets). Each connection points to an API key env var by name:

```yaml
connections:
  - id: gpt-5.1-codex-mini     # matches MODEL_BACKEND value
    backend: azure
    api_style: openai-responses
    active: true
    endpoint: https://your-resource.services.ai.azure.com/openai/v1/
    api_key_env: AZURE_CAPA_API_KEY
    models: [gpt-5.1-codex-mini]

  - id: claude-sonnet           # re-enable when Anthropic API credits are available
    backend: anthropic
    api_key_env: ANTHROPIC_API_KEY
    active: false
    models: [claude-sonnet-4-6]
```

To add a second Azure resource: add a new `connections` entry to `models.yaml` and add the
corresponding `AZURE_<NAME>_API_KEY` to `.env`. No code changes needed.

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | If using Anthropic | Anthropic API key (requires separate API credits at console.anthropic.com) |
| `AZURE_CAPA_API_KEY` | If using Azure | Key for all capa connections in models.yaml |
| `MODEL_BACKEND` | No | Active connection `id` from models.yaml (default: `gpt-5.1-codex-mini`) |
| `PG_HOST` | Yes | PostgreSQL host |
| `PG_PORT` | Yes | PostgreSQL port (usually `5432`) |
| `PG_DBNAME` | Yes | Database name |
| `PG_USER` | Yes | Database user (read-only) |
| `PG_PASSWORD` | Yes | Database password |
| `CANOPY_DATA_DIR` | No | History + cache file location — Docker only, do not set locally |
| `CANOPY_CACHE_TTL_HOURS` | No | Cache TTL in hours (default: `24`) |
| `CANOPY_UI_LANG` | No | UI label language: `en` (default) or `es` (Spanish). Questions must be in English or Spanish — other languages are rejected before reaching the model. This env var only controls UI labels (buttons, tabs, error messages). |

### 2. Build and run

```bash
make run
```

Open **http://localhost:7860** in a browser.

> **Why not `--env-file`?** Docker's `--env-file` passes surrounding quotes
> literally. `docker_run.sh` sources `.env` via shell so quotes are stripped
> correctly before the container starts.

### 3. Stop

```bash
docker stop $(docker ps -q --filter "ancestor=canopy:dev")
```

---

## Quickstart — Local (no Docker)

```bash
pip install -e ".[dev]"
cp .env.example .env   # fill in values
make ui
```

Open **http://localhost:7860**.

---

## Developer commands

All common tasks are available via `make`. Run `make` (no target) to see the full list.

| Command | What it does |
|---|---|
| `make check` | Lint + unit tests — run before every commit |
| `make lint` | `ruff check src/ tests/ scripts/` |
| `make test` | `pytest tests/ -q` |
| `make ui` | Start the app locally (needs `.env`) |
| `make build` | Build Docker image (`canopy:dev`) |
| `make run` | Build and run in Docker (needs `.env`) |
| `make smoke` | Docker smoke test — validates runtime behaviour unit tests can't catch |
| `make eval` | Ground-truth + adversarial eval (needs live DB + API key) |
| `make eval-es` | Spanish language variant eval |
| `make benchmark` | Run all connections from `models.yaml`, print comparison table (needs live DB + all API keys) |
| `make playwright-install` | Install Chromium for E2E tests (run once) |
| `make e2e` | Browser-level E2E tests — verifies error messages render in the UI (no DB or API key needed) |
| `make clean` | Remove build artefacts and caches |

### Manual checks (CLI, no UI)

#### Verify the API key

```bash
python scripts/smoke_test.py
```

#### Verify the database connection

```bash
python -c "
from canopy.db import get_connection
conn = get_connection()
cur = conn.cursor()
cur.execute('SELECT 1')
print('DB connected:', cur.fetchone())
conn.close()
"
```

#### Run a query from the command line

```bash
python -c "
from canopy.query import run_query
result = run_query('What species have been validated at any site?')
print('SQL:', result.sql)
print('Rows:', result.row_count)
print()
print(result.model_text)
"
```

#### Inspect the system prompt

```bash
python -c "from canopy.schema import build_system_prompt; print(build_system_prompt())"
```

---

## Tests

```bash
make check          # lint + unit tests
make test           # unit tests only
make smoke          # Docker runtime validation (requires Docker)
```

Expected unit test result: **413 passed**, 100% coverage.

The smoke test validates what `pytest` cannot: Docker volume permissions, Gradio
startup warnings, and HTTP availability. Run it after any Dockerfile or Gradio change.

## Multi-model benchmark

`make benchmark` runs all active connections declared in `models.yaml` against the full
eval suite and prints a comparison table:

| Connection | GT% | ADV% | Lat(s) | Tokens | $ (41 cases) | $/1K in | $/1K out |
|---|---|---|---|---|---|---|---|
| gpt-5.1-codex-mini | **94%** | **100%** | 12.3s | 305,687 | **$0.308** | $0.00075 | $0.003 |
| gpt-5.1-2 | 97% | 80% | 11.5s | 327,934 | $1.140 | $0.003 | $0.012 |

> **codex-mini is the default** — 3.7× cheaper ($0.308 vs $1.140 on this 41-case run) and
> scores higher on adversarial cases (100% vs 80%). gpt-5.1-2 scores 3% higher on
> ground-truth but fails two adversarial cases: A02 (SQL injection) and A09 (language gate
> bypass via French). The single shared failure is Q27 ("informal planning notes" framing —
> both models reason about trends instead of declining). Run `make benchmark` to refresh; the
> cache is now cleared before each model run so token and cost totals reflect live API usage.

Connections marked `active: false` in `models.yaml` are skipped. Currently inactive:
`claude-sonnet` (Anthropic API credits required — re-enable at console.anthropic.com),
`phi-4`, `qwen-3-4b` (pending admin deployment activation).

Columns: **GT%** = ground-truth pass rate (31 cases, target ≥85%), **ADV%** = adversarial
pass rate (10 cases, target 100%), **Lat(s)** = average latency per case, **Tokens** = total
prompt+completion tokens across all 41 cases, **$** = estimated cost at published Azure rates.

Results are also written to `benchmark_results/benchmark_<timestamp>.json` and `.csv`
for reproducible records and trend tracking.

**Model cap:** at most 5 models are benchmarked per connection. If a connection has more
deployments available, the extras are listed in the JSON output under `available_not_tested`
and printed at the end of the run — they are not benchmarked but are tracked as a running
record of what is deployed on each resource.

### Available models — gpt-5.1-2

<!-- Updated automatically by make benchmark. Do not edit by hand. -->
| Status | Model |
|--------|-------|
| tested | `gpt-5.1-2` |

### Available models — gpt-5.1-codex-mini

<!-- Updated automatically by make benchmark. Do not edit by hand. -->
| Status | Model |
|--------|-------|
| tested | `gpt-5.1-codex-mini` |



### Key design decisions

- **SELECT-only guard + read-only connection** — `execute_query()` rejects
  non-SELECT statements before touching the DB. The psycopg2 connection is also
  opened with `readonly=True` as belt-and-suspenders.
- **Coordinate filtering** — `latitude` and `longitude` are stripped from query
  results before they reach the model. The user's UI sees the full dataset; the
  AI layer never does. Complies with the principle of not granting agents direct
  access to sensitive biodiversity records.
- **Progressive feedback** — the UI streams live status above the output tabs
  (always visible regardless of which tab is active). The model states what it
  understood from the question before executing SQL, so users can catch
  misinterpretations before waiting 90 seconds.
- **Parallel tool calls** — if Claude returns multiple `tool_use` blocks,
  all are executed and their results are bundled into a single user message
  (Anthropic API requirement).
- **System prompt is a constant** — `SCHEMA_CONTEXT` is a module-level string.
  `build_system_prompt()` is a function so runtime context (language preference,
  etc.) can be injected later without touching the schema constant.
- **Resilient history** — query history is written to `CANOPY_DATA_DIR` in
  Docker (mounted as a named volume) and falls back to `~/.canopy` locally.
  If the configured path can't be created (e.g. `/data` set in a local `.env`),
  the app falls back gracefully rather than silently losing history.
- **Cache round-trip type safety** — `datetime`/`date` columns serialised to ISO
  strings on cache write are reconstructed back to `datetime` objects on read,
  so downstream code sees the same types whether a result is live or cached.

See `LIMITATIONS.md` for known data gaps, cache staleness windows, and UI
behaviour boundaries.

---

## Status

| Component | State |
|---|---|
| Model client interface + Claude adapter | Done |
| DB connection factory | Done |
| Schema context + system prompt | Done |
| SQL executor with SELECT-only guard | Done |
| Agentic query loop | Done |
| Parallel tool call handling | Done |
| Ground-truth eval set (31 queries) | Done |
| Query history (JSONL, Docker-safe) | Done |
| Production hardening (logging, timeout, Dockerfile) | Done |
| Gradio UI with streaming progress | Done |
| Live intent explanation (model states its understanding) | Done |
| Coordinate filtering (lat/lon never sent to AI layer) | Done |
| Read-only DB connection enforcement | Done |
| Resilient query history | Done |
| Faithfulness + adversarial evals (31 GT + 10 adversarial) | Done |
| Query result cache (SHA-256+NFC, TTL, LRU, model-scoped key) | Done |
| Spanish language support (auto-detect responses + UI labels) | Done |
| Spanish eval suite (8 GT parallel cases) | Done |
| Azure AI Foundry adapter (OpenAI-compatible) | Done |
| models.yaml connection registry (named connections, auto-discover) | Done |
| Multi-model benchmark runner (`make benchmark`) | Done |
| IUCN API integration | Deferred (needs API key) |