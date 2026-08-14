#!/bin/bash
# Harbor verifier for a ProgramBench reverse-engineering task.
#
# Runs once, after the agent, in the black-box container. Re-builds the agent's
# solution offline and runs every active test branch against it, then writes the
# fractional pass rate to /logs/verifier/reward.json. Ported from the
# ProgramBench Evaluator (src/programbench/eval/eval.py) into a single container:
# the multi-container isolation is emulated by stashing the built binary and
# restoring it (with a hash check) before each branch.
#
# Deliberately does NOT `set -e`: a failing compile or test run must still reach
# run_verifier.py so the reward reflects reality instead of aborting silently.
set -u

TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE=/workspace
STASH=/opt/programbench-stashed-executable-do-not-modify
RESULTS_ROOT="$(mktemp -d)"
mkdir -p /logs/verifier
cd "$WORKSPACE" || exit 1

# --- anti-cheat: drop any prebuilt binary and byte-identical gold copies ---
rm -f ./executable
if [ -s "$TESTS_DIR/clean_hashes.txt" ]; then
  pat="$(tr '\n' '|' < "$TESTS_DIR/clean_hashes.txt" | sed 's/|$//')"
  find "$WORKSPACE" -type f -exec sha256sum {} + 2>/dev/null \
    | grep -E "^(${pat})  " | cut -c67- | xargs -r -I% rm -fv %
fi

# --- deterministic synthetic git repo (builds that need a work-tree) ---
if [ ! -d .git ]; then
  export GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z'
  git -c init.defaultBranch=gold init -q \
    && git -c user.email=gold@local -c user.name=gold -c commit.gpgsign=false add -A \
    && git -c user.email=gold@local -c user.name=gold -c commit.gpgsign=false commit -q --allow-empty -m gold
  unset GIT_AUTHOR_DATE GIT_COMMITTER_DATE
fi

# --- compile with the internet blocked (DNS blackhole) ---
compile_rc=1
if [ -f ./compile.sh ]; then
  cp -f /etc/resolv.conf /etc/resolv.conf.harbor-bak 2>/dev/null || true
  if ! printf 'nameserver 0.0.0.0\n' > /etc/resolv.conf 2>/dev/null; then
    echo "FATAL: could not blackhole DNS for offline build" >&2
    exit 1
  fi
  chmod +x ./compile.sh
  ./compile.sh
  compile_rc=$?
  [ -f /etc/resolv.conf.harbor-bak ] && cat /etc/resolv.conf.harbor-bak > /etc/resolv.conf && rm -f /etc/resolv.conf.harbor-bak
else
  echo "no compile.sh in /workspace" >&2
fi

# --- run each active branch against the stashed binary ---
if [ "$compile_rc" -eq 0 ] && [ -f ./executable ]; then
  cp -f ./executable "$STASH"
  expected_hash="$(sha256sum "$STASH" | awk '{print $1}')"
  for bdir in "$TESTS_DIR"/branches/*/; do
    [ -d "$bdir" ] || continue
    branch="$(basename "$bdir")"
    rm -f ./executable && cp -f "$STASH" ./executable && chmod +x ./executable
    got="$(sha256sum ./executable | awk '{print $1}')"
    [ "$got" = "$expected_hash" ] || echo "WARN: executable hash drift in branch $branch" >&2
    rm -rf ./eval ./results.xml
    cp -a "$bdir/eval" ./eval
    chmod +x ./eval/run.sh 2>/dev/null || true
    sed -i 's/--timeout-method=thread/--timeout-method=signal/g' ./eval/run.sh 2>/dev/null || true
    ./eval/run.sh
    mkdir -p "$RESULTS_ROOT/$branch"
    [ -f ./eval/results.xml ] && cp ./eval/results.xml "$RESULTS_ROOT/$branch/results.xml"
  done
else
  echo "compile failed (rc=$compile_rc) or ./executable missing; scoring as all-unresolved" >&2
fi

python3 "$TESTS_DIR/run_verifier.py" \
  --tests-json "$TESTS_DIR/tests.json" \
  --results-dir "$RESULTS_ROOT" \
  --reward-file /logs/verifier/reward.json
