#!/bin/bash
# Harbor verifier for a ProgramBench reverse-engineering task.
#
# Runs in a SEPARATE verifier container (task.toml: verifier.environment_mode =
# "separate"), built FROM the cleanroom image with /workspace wiped at build
# time. Harbor hands off the agent's /workspace (minus ./executable) as an
# artifact, so this script sees the submitted source but no prebuilt binary. It
# rebuilds ./executable with the network blocked (an in-container DNS blackhole,
# only around compile.sh) and then runs each active branch against it with the
# network restored — mirroring ProgramBench, which blocks the build but leaves
# test-execution online (some suites pip-install plugins or reach the network).
#
# ProgramBench isolates every branch in a fresh container built from the same
# post-compilation image. A single verifier container can't fully reproduce
# that, but we approximate it: snapshot the pristine post-compile workspace and
# restore it before each branch, so root-level fixtures, test-created files, and
# candidate state don't leak between branches. Out-of-workspace/system changes
# within one container are the documented residual difference.
#
# Deliberately does NOT `set -e`: a failing compile or test run must still reach
# run_verifier.py so the reward reflects reality instead of aborting silently.
set -u

TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE=/workspace
STASH=/opt/programbench-stashed-executable-do-not-modify
SNAPSHOT="$(mktemp -d)"
RESULTS_ROOT="$(mktemp -d)"
COMPILE_TIMEOUT=900   # ProgramBench's compile budget
BRANCH_TIMEOUT=3600   # ProgramBench's per-branch budget; enforced per branch so
                      # one slow branch can't starve the ones after it
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

# --- compile with the internet blocked, then restore it for the tests ---
# Same in-container DNS blackhole as programbench/utils/internet_control.py: the
# build can't smuggle pip/cargo/go downloads, but test execution keeps network.
compile_rc=1
if [ -f ./compile.sh ]; then
  cp -f /etc/resolv.conf /etc/resolv.conf.harbor-bak 2>/dev/null || true
  if ! printf 'nameserver 0.0.0.0\n' > /etc/resolv.conf 2>/dev/null; then
    echo "FATAL: could not blackhole DNS for offline build" >&2
    exit 1
  fi
  chmod +x ./compile.sh
  timeout "$COMPILE_TIMEOUT" ./compile.sh
  compile_rc=$?
  [ -f /etc/resolv.conf.harbor-bak ] && cat /etc/resolv.conf.harbor-bak > /etc/resolv.conf && rm -f /etc/resolv.conf.harbor-bak
else
  echo "no compile.sh in /workspace" >&2
fi

# --- run each active branch against the stashed binary ---
if [ "$compile_rc" -eq 0 ] && [ -f ./executable ]; then
  cp -f ./executable "$STASH"
  expected_hash="$(sha256sum "$STASH" | awk '{print $1}')"

  # Snapshot the pristine post-compile workspace (without the binary — each
  # branch restores it from the stash) so every branch starts identical.
  rm -f ./executable
  cp -a "$WORKSPACE/." "$SNAPSHOT/"

  for bdir in "$TESTS_DIR"/branches/*/; do
    [ -d "$bdir" ] || continue
    branch="$(basename "$bdir")"
    mkdir -p "$RESULTS_ROOT/$branch"

    # Best-of retry: a hung TUI test can crash an xdist worker, whereupon xdist
    # raises INTERNALERROR and aborts the session, stranding every not-yet-run
    # test. Attempt 1 runs as authored; on a detected crash we retry serially
    # (-n0), where a per-test timeout fails only the hung test. Each attempt
    # restarts from the pristine snapshot (like a fresh container per retry) and
    # we keep the XML with the most parsed testcases (the aborted attempt has
    # fewer).
    best_n=-1
    for attempt in 1 2; do
      # Reap stray TUI apps / tmux servers, reset the workspace to the snapshot,
      # then overlay this branch's tree — eval/ plus root-level helpers the
      # suite resolves at the workspace root (e.g. ./tui2cli, fixtures) — and
      # restore the canonical binary on top.
      pkill -9 -f "$WORKSPACE/executable" 2>/dev/null || true
      tmux kill-server 2>/dev/null || true
      find "$WORKSPACE" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null
      cp -a "$SNAPSHOT/." "$WORKSPACE/"
      cp -a "$bdir/." "$WORKSPACE/"
      [ -f ./tui2cli ] && chmod +x ./tui2cli
      # rm first: a branch tree may ship its own ./executable (a prebuilt binary
      # or a dangling coverage symlink, e.g. executable -> ./executable_cov);
      # cp -f won't write through a dangling symlink, so drop it before restoring.
      rm -f ./executable && cp -f "$STASH" ./executable && chmod +x ./executable
      got="$(sha256sum ./executable | awk '{print $1}')"
      [ "$got" = "$expected_hash" ] || echo "WARN: executable hash drift in branch $branch" >&2
      chmod +x ./eval/run.sh 2>/dev/null || true
      sed -i 's/--timeout-method=thread/--timeout-method=signal/g' ./eval/run.sh 2>/dev/null || true
      [ "$attempt" -eq 2 ] && sed -i -E 's/-n[= ]*(auto|[0-9]+)/-n0/g' ./eval/run.sh 2>/dev/null

      runlog="$(mktemp)"
      timeout "$BRANCH_TIMEOUT" ./eval/run.sh 2>&1 | tee "$runlog"
      n=0
      [ -f ./eval/results.xml ] && n="$(grep -o '<testcase' ./eval/results.xml | wc -l | tr -d ' ')"
      if [ "$n" -gt "$best_n" ]; then
        best_n="$n"
        cp -f ./eval/results.xml "$RESULTS_ROOT/$branch/results.xml" 2>/dev/null || true
      fi
      crashed=0
      grep -qE "INTERNALERROR|worker .* crashed|Replacing crashed worker" "$runlog" && crashed=1
      rm -f "$runlog"
      { [ -f ./eval/results.xml ] && [ "$crashed" -eq 0 ]; } && break
      [ "$attempt" -eq 1 ] && echo "WARN: xdist crash/abort in branch $branch; retrying serially" >&2
    done
  done
else
  echo "compile failed (rc=$compile_rc) or ./executable missing; scoring as all-unresolved" >&2
fi

python3 "$TESTS_DIR/run_verifier.py" \
  --tests-json "$TESTS_DIR/tests.json" \
  --results-dir "$RESULTS_ROOT" \
  --reward-file /logs/verifier/reward.json
