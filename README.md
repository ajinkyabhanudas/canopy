# canopy

A natural language query tool for a bioacoustic species-monitoring
database. Ask a question in plain English (or Spanish), canopy turns it
into a SQL query, runs it read-only against the dataset, and returns the
result alongside a plain-language interpretation.

## What it does

- Accepts a natural language question about species detections, sites,
  validation records, and related metadata.
- Generates and executes a read-only SQL query against the dataset.
- Returns the underlying data, the relevant record history, and a
  plain-language summary, never a population trend or conservation status
  conclusion (that requires a formal review process, not an automated
  inference).
- Talks to a single model interface, so the underlying language model can
  be swapped by adding a new backend, not by rewriting the tool.

## Requirements

- Python 3.11+
- An Anthropic API key

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

## Usage

`scripts/smoke_test.py` is a manual check that the API key and model in
`.env` actually work. It makes a real API call, run it by hand after
setup:

```bash
python scripts/smoke_test.py
```

The natural language query loop itself is still in progress.

## Architecture

Every model call goes through one interface
(`src/canopy/models/base.py`). Today there is one adapter, Claude, called
directly through Anthropic's API. Adding a different model or provider
means writing a new adapter and registering it in
`src/canopy/models/registry.py`. Nothing else in the codebase needs to
change.

## Tests

```bash
pytest tests/
```

## Status

Early build. The model connection is wired up. The query loop, guardrails,
and evaluation suite are still in progress.
