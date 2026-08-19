#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CONFIG="${HARBOR_CONFIG:-$ROOT/scripts/config.yaml}"

if [[ "${HARBOR_SKIP_EXPORT:-0}" != "1" ]]; then
  uv run programbench harbor export-config "$CONFIG"
fi

exec harbor run \
  --config "$CONFIG" \
  --agent oracle \
  --yes \
  "$@"
