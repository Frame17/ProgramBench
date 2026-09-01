#!/bin/bash
# Oracle compile.sh: rebuild ./executable from the stashed reference binary.
#
# Runs offline in the separate verifier (network policy = no-network). solve.sh
# copied a gzip-compressed reference into this stash; here we decompress it
# back into ./executable. The stash's own sha256 differs from the raw binary's,
# so it is not removed by the verifier's clean-hash scrub, and ./executable is
# produced only after it.
set -euo pipefail

STASH=".programbench_oracle_reference.gz"
if [ ! -f "$STASH" ]; then
  echo "FATAL: oracle stash $STASH missing from /workspace" >&2
  exit 1
fi

gzip -dc "$STASH" > ./executable
chmod +x ./executable
