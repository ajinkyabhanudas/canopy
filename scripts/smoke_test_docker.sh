#!/usr/bin/env bash
# smoke_test_docker.sh
#
# Builds the Docker image and validates the container's runtime behaviour:
#   1. Gradio UI serves HTTP 200
#   2. /data volume is writable by the canopy user
#   3. No unexpected UserWarning in startup logs
#
# Exits 0 if all checks pass, 1 if any fail.
# Requires: docker, curl
#
# Usage:
#   ./scripts/smoke_test_docker.sh
#   ./scripts/smoke_test_docker.sh --skip-build   # reuse canopy:smoke if already built

set -euo pipefail

IMAGE="canopy:smoke"
CONTAINER_NAME="canopy-smoke-$$"
VOLUME="canopy-smoke-vol-$$"
PORT=17860   # avoids conflict with a running dev instance on 7860
SKIP_BUILD=0

for arg in "$@"; do
    [[ "$arg" == "--skip-build" ]] && SKIP_BUILD=1
done

PASS=0
FAIL=0

_pass() { echo "    PASS  $*"; PASS=$((PASS + 1)); }
_fail() { echo "    FAIL  $*"; FAIL=$((FAIL + 1)); }

cleanup() {
    docker stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker rm   "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME"    >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo ""
echo "canopy Docker smoke test"
echo "========================"
echo ""

# ── Build ──────────────────────────────────────────────────────────────────
if [[ $SKIP_BUILD -eq 0 ]]; then
    echo "==> Building image ($IMAGE)..."
    docker build -t "$IMAGE" . -q
    echo "    Built."
else
    echo "==> Skipping build (--skip-build)"
fi
echo ""

# ── Start container ────────────────────────────────────────────────────────
echo "==> Starting container..."
docker volume create "$VOLUME" >/dev/null
docker run -d \
    --name "$CONTAINER_NAME" \
    -p "$PORT:7860" \
    -v "$VOLUME:/data" \
    -e ANTHROPIC_API_KEY=smoke-test-dummy \
    -e PG_HOST=localhost \
    -e PG_PORT=5432 \
    -e PG_DBNAME=canopy \
    -e PG_USER=canopy \
    -e PG_PASSWORD=canopy \
    "$IMAGE" >/dev/null
echo "    Container $CONTAINER_NAME started."
echo ""

# ── Wait for Gradio ────────────────────────────────────────────────────────
echo "==> Waiting for Gradio (up to 30s)..."
READY=0
for i in $(seq 1 30); do
    if curl -sf "http://localhost:$PORT" >/dev/null 2>&1; then
        echo "    Ready after ${i}s."
        READY=1
        break
    fi
    sleep 1
done

if [[ $READY -eq 0 ]]; then
    echo "    FAIL  Server did not start within 30 seconds"
    echo ""
    echo "Container logs:"
    docker logs "$CONTAINER_NAME"
    echo ""
    echo "========================"
    echo "Smoke test: FAILED (server did not start)"
    exit 1
fi
echo ""

# ── Check 1: HTTP 200 ──────────────────────────────────────────────────────
echo "==> Check 1: HTTP 200 on /"
STATUS=$(curl -so /dev/null -w "%{http_code}" "http://localhost:$PORT")
if [[ "$STATUS" == "200" ]]; then
    _pass "HTTP $STATUS"
else
    _fail "HTTP $STATUS (expected 200)"
fi

# ── Check 2: /data writable ────────────────────────────────────────────────
echo "==> Check 2: /data writable by canopy user"
if docker exec "$CONTAINER_NAME" touch /data/.smoke_write_check 2>/dev/null; then
    docker exec "$CONTAINER_NAME" rm /data/.smoke_write_check
    _pass "/data is writable"
else
    _fail "/data not writable — Dockerfile missing chown before USER switch (see LESSONS.md L1)"
fi

# ── Check 3: No unexpected UserWarning in startup logs ────────────────────
echo "==> Check 3: No UserWarning in startup logs"
WARNINGS=$(docker logs "$CONTAINER_NAME" 2>&1 | grep "UserWarning:" || true)
if [[ -z "$WARNINGS" ]]; then
    _pass "No UserWarning in logs"
else
    _fail "Unexpected UserWarning in startup logs:"
    echo "$WARNINGS" | sed 's/^/          /'
fi

# ── Check 4: models.yaml is present and loadable ──────────────────────────
echo "==> Check 4: models.yaml present and loadable inside container"
if docker exec "$CONTAINER_NAME" python -c "
import sys; sys.path.insert(0, '/app/src')
from canopy.config import load_model_connections
conns = load_model_connections()
assert len(conns) > 0, 'no connections loaded'
print(f'  {len(conns)} connection(s) loaded: {[c.id for c in conns]}')
" 2>&1; then
    _pass "models.yaml loaded successfully"
else
    _fail "models.yaml missing or unparseable inside container — check Dockerfile COPY"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "========================"
echo "Smoke test: $PASS/4 passed"
echo ""
[[ $FAIL -eq 0 ]]
