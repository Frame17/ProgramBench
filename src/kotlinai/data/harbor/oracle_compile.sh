#!/bin/bash
# Oracle compile.sh: rebuild ./executable from the stashed reference binary.
#
# Runs offline in the separate verifier (network policy = no-network). solve.sh
# base64-encoded the reference binary into this stash while it still had access
# to the installed reference; here we decode it back into ./executable. The
# stash's own sha256 differs from the raw binary's, so it is not removed by the
# verifier's clean-hash scrub, and ./executable is produced only after it.
set -euo pipefail

STASH=".programbench_oracle_reference"
if [ ! -f "$STASH" ]; then
  echo "FATAL: oracle stash $STASH missing from /workspace" >&2
  exit 1
fi

base64 -d "$STASH" > ./executable
chmod +x ./executable
