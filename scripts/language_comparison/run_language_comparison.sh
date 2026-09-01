#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

CONFIG="${LANGUAGE_COMPARISON_CONFIG:-$SCRIPT_DIR/config.yaml}"

exec uv run programbench harbor compare \
  --config "$CONFIG" \
  --include-oracle-payload \
  "$@"
