# canopy

A natural language query tool for Jocotoco's bioacoustic species-monitoring
database. Ask a question in plain English, canopy translates it into a SQL
query, executes it read-only against the database, and returns a plain-English
answer alongside the SQL for inspection.

## What it does

- Accepts a natural language question about species detections, recording sites,
  validation records, and related metadata.
- Uses Claude to generate a PostgreSQL SELECT query — never guesses results.
- Executes read-only against PostgreSQL and returns a plain-English answer
  alongside the data table and SQL for inspection.
- Shows live progress while the query runs — what the model understood, which
  stage the pipeline is at, how many records were found.
- Persists query history to disk (last 20 queries surfaced in the UI sidebar).
- Never infers population trends or conservation status — that requires a formal
  scientific review process, not automated inference.
- Precise species coordinates are filtered before any data reaches the AI layer,
  keeping sensitive biodiversity locations out of the model context.
- Vendor-neutral model interface: swapping the LLM means adding one adapter file.

## Requirements

- Python 3.11+ (local) or Docker (recommended for deployment)
- An Anthropic API key
- PostgreSQL credentials for the VAJocotoco database

---

## Quickstart — Docker (recommended)

### 1. Build the image

```bash
docker build -t canopy:dev .
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in all required values. Never commit `.env`.

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `ANTHROPIC_MODEL` | No | Model ID (default: `claude-sonnet-4-6`) |
| `MODEL_BACKEND` | No | Backend (default: `anthropic`) |
| `PG_HOST` | Yes | PostgreSQL host |
| `PG_PORT` | Yes | PostgreSQL port (usually `5432`) |
| `PG_DBNAME` | Yes | Database name |
| `PG_USER` | Yes | Database user (read-only) |
| `PG_PASSWORD` | Yes | Database password |
| `ANTHROPIC_TIMEOUT` | No | API timeout in seconds (default: `60`) |
| `CANOPY_DATA_DIR` | No | History file location — Docker only, do not set locally |

### 3. Run

```bash
./scripts/docker_run.sh
```

Open **http://localhost:7860** in a browser.

> **Why not `--env-file`?** Docker's `--env-file` passes surrounding quotes
> literally. `docker_run.sh` sources `.env` via shell so quotes are stripped
> correctly before the container starts.

### 4. Stop

```bash
docker stop $(docker ps -q --filter "ancestor=canopy:dev")
```

---

## Quickstart — Local (no Docker)

```bash
pip install -e ".[dev]"
cp .env.example .env   # fill in values
python scripts/run_ui.py
```

Open **http://localhost:7860**.

---

## Manual checks (CLI, no UI)

### Verify the API key

One billable call to confirm credentials and model are configured:

```bash
python scripts/smoke_test.py
```

### Verify the database connection

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

### Run a query from the command line

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

### Inspect the system prompt

```bash
python -c "from canopy.schema import build_system_prompt; print(build_system_prompt())"
```

---

## Tests

```bash
# All unit tests (no DB or API key needed)
pytest tests/ --cov=canopy --cov-report=term-missing

# Linting
ruff check src/ tests/
```

Expected: **144 passed, 1 skipped**, ~87% coverage.

The skipped test is a live DB integration test — it runs automatically when
`PG_*` vars are present.

## Ground-truth eval

Runs 20 questions against the live database and validates SQL structure and
result shape:

```bash
python scripts/run_eval.py
```

Pass threshold: ≥85% (17/20). Requires `ANTHROPIC_API_KEY` and `PG_*` vars.

---

## Architecture

```
src/canopy/
├── config.py          # Env var loading — ModelConfig, DBConfig, get_data_dir()
├── schema.py          # DB schema constant + build_system_prompt()
├── history.py         # append_history, load_history, clear_history (JSONL)
├── models/
│   ├── base.py        # ModelClient ABC — vendor-neutral interface
│   ├── anthropic.py   # Claude adapter (only backend today)
│   └── registry.py    # get_model_client() — reads MODEL_BACKEND
├── db/
│   └── connection.py  # get_connection() — psycopg2, read-only
├── query/
│   ├── executor.py    # execute_query() — SELECT-only guard + execution
│   └── loop.py        # run_query() — agentic loop, returns LoopResult
└── ui/
    └── app.py         # build_app() — Gradio two-panel UI

scripts/
├── docker_run.sh      # Docker launcher (handles .env quote stripping)
├── run_ui.py          # Local UI launcher
├── smoke_test.py      # API key / model config check
└── run_eval.py        # Ground-truth eval runner

tests/
└── eval/
    └── queries.py     # 20 EvalCase entries + check_fn predicates

Dockerfile             # python:3.11-slim, non-root user, /data volume
```

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
| Ground-truth eval set (20 queries) | Done |
| Query history (JSONL, Docker-safe) | Done |
| Production hardening (logging, timeout, Dockerfile) | Done |
| Gradio UI with streaming progress | Done |
| Live intent explanation (model states its understanding) | Done |
| Coordinate filtering (lat/lon never sent to AI layer) | Done |
| Read-only DB connection enforcement | Done |
| Resilient query history | Done |
| IUCN API integration | Deferred (needs API key) |
