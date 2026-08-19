#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

exec harbor run \
  --config "${HARBOR_CONFIG:-$ROOT/scripts/config.yaml}" \
  --agent oracle \
  --yes \
  "$@"
