#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
. "$ROOT/.env"
set +a
export OPENAI_API_KEY="$LITELLM_API_KEY"
export OPENAI_BASE_URL="$LITELLM_BASE_URL"
export ANTHROPIC_API_KEY="$LITELLM_API_KEY"
export ANTHROPIC_BASE_URL="$LITELLM_BASE_URL/anthropic"

LITELLM_BASE_HOST="${LITELLM_BASE_URL#*://}"
LITELLM_BASE_HOST="${LITELLM_BASE_HOST%%/*}"
CONFIG="${HARBOR_CONFIG:-$ROOT/scripts/config.yaml}"

if [[ "${HARBOR_SKIP_EXPORT:-0}" != "1" ]]; then
  uv run programbench harbor export-config "$CONFIG"
fi

exec harbor run \
  --config "$CONFIG" \
  --agent-kwarg "api_base=$LITELLM_BASE_URL" \
  --allow-agent-host "$LITELLM_BASE_HOST" \
  --env-file "$ROOT/.env" \
  --yes \
  "$@"
