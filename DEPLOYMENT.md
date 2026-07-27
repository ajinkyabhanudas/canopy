# Deployment Runbook

Operational procedures for whoever inherits this repo: deploying a fresh
instance, restarting safely, rotating credentials, and changing the schema.

This document covers **operations**. For setup/quickstart (first-time
install), see [README.md](README.md#quickstart). For *why* things are built
this way, see [DECISIONS.md](DECISIONS.md). For known gaps and their
severity, see [LIMITATIONS.md](LIMITATIONS.md).

There is no automated deploy pipeline — CI (`.github/workflows/ci.yml`)
only lints and tests on pull requests. It does not build, push, or deploy
anything. Deployment is manual, on whatever host runs the container.

---

## 1. Fresh deploy

1. Clone the repo on the target host.
2. `cp .env.example .env` and fill in every value:
   - `PG_HOST`, `PG_PORT`, `PG_DBNAME`, `PG_USER`, `PG_PASSWORD` — required,
     `db/connection.py` raises `ValueError` naming whichever are missing.
   - At least one model API key: `ANTHROPIC_API_KEY` and/or
     `AZURE_CAPA_API_KEY` (see Section 3 for which backends are actually usable).
   - Never commit `.env`.
3. `make run` — builds the `canopy:dev` image, then runs
   `scripts/docker_run.sh`, which reads `.env` and starts the container with
   a named volume `canopy-data:/data` (not a host bind mount).
4. Open `http://localhost:7860`.
5. Verify the deploy:
   - `make smoke` — validates container mechanics only (HTTP 200, `/data`
     writable, no startup warnings, `models.yaml` parses). Uses a throwaway
     image/container/volume on port 17860; does **not** touch the real DB or
     make a billable API call.
   - `python scripts/smoke_test.py` — makes one real (billable) call against
     the active model connection. Run this to confirm the API key actually
     works, not just that it's present.
   - DB connectivity (run inside the container or locally with the same
     `.env`):
     ```python
     from canopy.db import get_connection
     conn = get_connection()
     conn.cursor().execute("SELECT 1")
     conn.close()
     ```
6. Stop: `docker stop $(docker ps -q --filter "ancestor=canopy:dev")` — the
   container has no `--name`, so it must be found by ancestor image.

**Local (no Docker) path** — for development, not recommended for a
persistent deployment: `pip install -e ".[dev]"`, `cp .env.example .env`,
`make ui`. Do **not** set `CANOPY_DATA_DIR` locally — it defaults to
`~/.canopy`; it's a Docker/cloud-only var (see Section 2).

### Env vars not in `.env.example`

`config.py` is documented as the single place env vars are read, but three
are read directly by other modules and are missing from `.env.example`:

| Var | Default | Read in |
|---|---|---|
| `CANOPY_CACHE_TTL_HOURS` | 24 | `cache.py:41` |
| `CANOPY_SENSITIVE_COLUMNS` | `latitude,longitude,hashed_password` | `query/loop.py:89` |
| `CANOPY_FUZZY_CACHE_TTL_SECONDS` | 6h (21600s) | `query/fuzzy_match.py:164` |

All three have working defaults — only set them to override. Worth adding
to `.env.example` as commented-out entries in a future pass (not done here
to keep this change docs-only).

---

## 2. Restart

Standard `docker stop` + `make run` is safe: history (`history.jsonl`) and
the query cache (`cache.json`) live in the `canopy-data` named volume, not
in the container filesystem, so a same-host restart preserves both
automatically. No manual backup step needed for a routine restart.

**Risks:**
- `docker volume rm canopy-data` (or losing the host it lives on) destroys
  history permanently — there is no backup strategy. Cache is regenerable;
  history is not.
- The app is single-instance only against a given `/data` volume — running
  two containers against the same volume causes write races on
  `history.jsonl`/`cache.json`. Don't scale horizontally without changing
  the persistence layer first (see DECISIONS.md's D3 section).
- No documented graceful-shutdown handling — `docker stop` sends SIGTERM
  then SIGKILL after Docker's default grace period; in-flight queries are
  not drained.

**Config changes require a restart to take effect.** `models.yaml` and
`.env` are parsed once at process start and cached in-process
(`config.py`'s `_connections_cache` is keyed by file path, not content).
Editing either while the container is running has no effect until restart.

---

## 3. Credential rotation

There is no automated rotation tooling or documented rotation cadence in
this repo — key expiry is not tracked anywhere in code. The mechanism,
inferred from how config is loaded (see Section 2's caching note): **update the
value in `.env`, then restart the container.** A running process will not
pick up a changed value.

| Credential | Env var | Used by |
|---|---|---|
| Anthropic API key | `ANTHROPIC_API_KEY` | `claude-sonnet` connection (currently inactive — see Section 4) |
| Azure AI Foundry key | `AZURE_CAPA_API_KEY` | All 4 Azure connections (`gpt-5.1-codex-mini`, `gpt-5.1-2`, `phi-4`, `qwen-3-4b`) — one key shared across the `capa-4249-resource` resource |
| Postgres credentials | `PG_HOST/PORT/DBNAME/USER/PASSWORD` | `db/connection.py` |

Steps:
1. Obtain the new credential value (Azure portal for `AZURE_CAPA_API_KEY`,
   console.anthropic.com for `ANTHROPIC_API_KEY`, DB admin for `PG_*`).
2. Update `.env` on the host — do not commit it.
3. Restart the container (`docker stop ...` then `make run`).
4. Re-run `python scripts/smoke_test.py` to confirm the new credential
   actually authenticates before considering rotation complete.

`models.yaml` itself holds no secrets — only `api_key_env` *names* — so it
does not need to change during a routine key rotation. It only changes if
you add a new model connection or new Azure resource (README.md's
"Multi-model benchmark" section covers that).

---

## 4. Switching model backends

Two Azure connections are active and interchangeable today with **no code
change**: `gpt-5.1-codex-mini` and `gpt-5.1-2`. To switch between them, set
`MODEL_BACKEND` in `.env` to the connection name and restart.

**Re-enabling Claude Sonnet is not a config-only change**, despite
DECISIONS.md's M1 section reading that way at a glance. Flipping
`active: false → true` in `models.yaml` and topping up
console.anthropic.com credits is necessary but not sufficient:
`registry.py`'s `get_llm()` unconditionally raises `NotImplementedError` for
`backend: anthropic` — a LlamaIndex `FunctionCallingLLM` subclass for
Anthropic does not exist yet (follow the pattern in `azure_responses_llm.py`
or `llamaindex_compat.py`). Budget this as a real (small) coding task, not a
flag flip, if Claude needs to come back online.

`phi-4` and `qwen-3-4b` are inactive for an unrelated reason — pending
Azure-side deployment activation, not a code gap.

---

## 5. Schema changes

There is no migrations system. The schema the model sees is a hand-maintained
string constant, `SCHEMA_CONTEXT` in `src/canopy/schema.py` — it does not
introspect the live database. Per DECISIONS.md's D1 section, every schema change
(new column, renamed field, new table) requires:

1. Edit `SCHEMA_CONTEXT` in `schema.py` manually to match the new schema.
2. Run `tests/test_schema_drift.py` against the live DB to verify it
   matches — **this test requires live `PG_*` credentials and self-skips
   without them.** It is not currently run in CI (no `PG_*` secrets are
   configured there — see LIMITATIONS.md's note on this), so it must be run
   manually against a real database as part of this procedure:
   ```
   PG_HOST=... PG_PORT=... PG_DBNAME=... PG_USER=... PG_PASSWORD=... \
     pytest tests/test_schema_drift.py -v
   ```
3. Rebuild the Docker image (`make build`).
4. Redeploy (Section 1).

**A new `validation_status` value** (e.g. `rejected`) needs extra care: the
system prompt's default filter (`ALWAYS filter on validation_status =
'approved'`) will not pick it up automatically. Update both
`SCHEMA_CONTEXT` and the S4 guardrail filter in `schema.py`, and update
`test_schema_drift.py`'s `_DOCUMENTED_STATUSES` set — the test will fail
loudly if a live status isn't in that set, which is the point (this
categories of drift caused a real incident on 2026-06-27 — see LIMITATIONS.md).

**Not covered by any drift test:** `FUZZY_COLUMNS` in
`src/canopy/query/fuzzy_match.py` is a second hand-maintained list of
fuzzy-checkable columns. Renaming or dropping one of those columns fails
silently — no test catches it today. If you rename `sites.name` or
`detections.management_unit`, check `FUZZY_COLUMNS` manually.

---

## 6. Incident / rollback

No automated rollback tooling exists. If a bad deploy needs reverting:

1. `git checkout <last-known-good-commit>` (or `main` if the bad change
   hasn't merged).
2. `make build && make run` to rebuild and redeploy from that commit.
3. History/cache in the `canopy-data` volume are untouched by a code
   rollback — no data migration needed for a pure code revert.

If the incident is a schema mismatch (model querying columns/values that no
longer exist), see Section 5 — this is the failure mode `test_schema_drift.py`
exists to catch before it reaches production.

---

## 7. Escalation

- **Repo/CI:** GitHub branch protection for `main` (required checks: `check`,
  `e2e`) is a manual repo setting, not codified — verify it's actually
  configured in Settings → Branches (see the comment at the top of
  `.github/workflows/ci.yml`).
- **Network:** VPN/firewall deployment restrictions are discussed in
  DECISIONS.md's U1 section.
- **Data questions:** LIMITATIONS.md documents known data gaps (IUCN
  categories, common names, missing-year handling) — check there before
  assuming a query result is wrong.
