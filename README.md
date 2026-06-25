# canopy

A natural language query tool for Jocotoco's bioacoustic species-monitoring
database. Ask a question in plain English (or Spanish), canopy translates it
into a SQL query, executes it read-only against the dataset, and returns the
raw results alongside the SQL that was run.

## What it does

- Accepts a natural language question about species detections, sites,
  validation records, and related metadata.
- Uses Claude to generate a PostgreSQL SELECT query — never guesses results.
- Executes the query read-only and returns the data with the SQL visible for
  inspection (Pedro's transparency requirement).
- Never infers population trends or conservation status — that requires a
  formal scientific review process, not an automated inference.
- Vendor-neutral model interface: swapping the underlying LLM means adding
  one adapter file, not rewriting the tool.

## Requirements

- Python 3.11+
- An Anthropic API key
- PostgreSQL credentials for the VAJocotoco database

## Install

```bash
pip install -e ".[dev]"
```

## Setup

```bash
cp .env.example .env
```

Edit `.env` and fill in the values below. Never commit `.env`.

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key |
| `ANTHROPIC_MODEL` | No | Model to use (default: `claude-sonnet-4-6`) |
| `MODEL_BACKEND` | No | Backend to use — only `anthropic` exists today (default: `anthropic`) |
| `PG_HOST` | Yes | PostgreSQL host |
| `PG_PORT` | Yes | PostgreSQL port (usually `5432`) |
| `PG_DBNAME` | Yes | Database name |
| `PG_USER` | Yes | Database user (read-only recommended) |
| `PG_PASSWORD` | Yes | Database password |

## Manual checks

### 1. Verify the API key works

Makes one real billable API call to confirm credentials and model are configured:

```bash
python scripts/smoke_test.py
```

### 2. Verify the database connection

Runs a `SELECT 1` against PostgreSQL (requires `PG_*` vars in `.env`):

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

### 3. Run a natural language query

End-to-end test of the full loop — model call → SQL generation → DB execution:

```bash
python -c "
from canopy.query import run_query
result = run_query('What species have been validated at any site?')
print('SQL:', result.sql)
print('Rows returned:', result.row_count)
print('Columns:', result.columns)
print()
print(result.model_text)
"
```

### 4. Inspect the system prompt

Prints the full prompt the model receives on every call:

```bash
python -c "from canopy.schema import build_system_prompt; print(build_system_prompt())"
```

## Tests

```bash
# All tests (unit only — no DB or API key needed)
pytest tests/

# With coverage
pytest tests/ --cov=canopy --cov-report=term-missing

# Single module
pytest tests/test_query_loop.py -v
```

Expected output: 54 passed, 1 skipped (live DB integration test, skipped
when `PG_*` vars are absent).

## Architecture

```
src/canopy/
├── schema.py          # DB schema + system prompt (SCHEMA_CONTEXT constant,
│                      # build_system_prompt())
├── config.py          # All env var loading (ModelConfig, DBConfig)
├── models/
│   ├── base.py        # ModelClient ABC — vendor-neutral interface
│   ├── anthropic.py   # Claude adapter (the only backend today)
│   └── registry.py    # get_model_client() — reads MODEL_BACKEND env var
├── db/
│   └── connection.py  # get_connection() — raw psycopg2, read-only
└── query/
    ├── executor.py    # execute_query(sql) — SELECT-only guard + DB execution
    └── loop.py        # run_query(question) — agentic loop, returns LoopResult
```

### Adding a new model backend

1. Create `src/canopy/models/<provider>.py` implementing `ModelClient`
   (three methods: `generate`, `format_tool_result`, `format_tool_results`,
   `format_assistant_turn`).
2. Register it in `src/canopy/models/registry.py`.
3. Set `MODEL_BACKEND=<provider>` in `.env`. Nothing else changes.

### Key design decisions

- **SELECT-only guard** — `execute_query()` rejects any non-SELECT statement
  before opening a DB connection. Belt-and-suspenders over the LLM guardrail.
- **Parallel tool calls** — if Claude returns multiple tool_use blocks in one
  response, all are executed sequentially and their results are bundled into a
  single user message (Anthropic API requirement). Sequential execution is
  intentional for v1; threading is not needed at this data volume.
- **System prompt is a constant** — `SCHEMA_CONTEXT` is a module-level string,
  not computed at runtime. `build_system_prompt()` is a function so runtime
  context (e.g. language preference) can be injected later without touching
  the schema constant.

## Status

| Component | State |
|---|---|
| Model client interface + Claude adapter | Done |
| DB connection factory | Done |
| Schema context + system prompt | Done |
| SQL executor with SELECT-only guard | Done |
| Agentic query loop | Done |
| Parallel tool call handling | Done |
| Ground-truth eval set (20 queries) | In progress |
| Query history | Planned |
| Gradio UI | Planned |
| Interpretation layer | Future (post-v1) |
| IUCN API integration | Future (post-v1) |
