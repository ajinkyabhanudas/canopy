#!/usr/bin/env bash
# Run the canopy Docker image, sourcing .env so shell strips surrounding quotes
# from values before passing them to the container.
#
# Usage: ./scripts/docker_run.sh [extra docker args...]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE" >&2
  exit 1
fi

# Source the file so shell evaluates and strips quotes
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

# Build -e flags for every non-comment, non-empty var in .env
env_args=()
while IFS= read -r line; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  key="${line%%=*}"
  [[ -z "$key" ]] && continue
  env_args+=("-e" "${key}")
done < "$ENV_FILE"

exec docker run --rm -p 7860:7860 \
  "${env_args[@]}" \
  -v canopy-data:/data \
  "$@" \
  canopy:dev
