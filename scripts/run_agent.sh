#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
. "$ROOT/.env"
set +a
export OPENAI_API_KEY="$LITELLM_API_KEY"
export OPENAI_BASE_URL="$LITELLM_BASE_URL"

LITELLM_BASE_HOST="${LITELLM_BASE_URL#*://}"
LITELLM_BASE_HOST="${LITELLM_BASE_HOST%%/*}"

exec harbor run \
  --config "${HARBOR_CONFIG:-$ROOT/scripts/config.yaml}" \
  --agent-kwarg "api_base=$LITELLM_BASE_URL" \
  --allow-agent-host "$LITELLM_BASE_HOST" \
  --env-file "$ROOT/.env" \
  --yes \
  "$@"
