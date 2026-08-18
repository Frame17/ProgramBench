# kotlinai — Harbor export for ProgramBench

Converts ProgramBench reverse-engineering instances into
[Harbor](https://harborframework.com) tasks, each shipping a **runnable golden
solution** so Harbor's oracle agent can verify the task end to end.

One ProgramBench instance maps to one Harbor task. The environment is the
black-box `task_cleanroom_v6` image (reference binary installed, source
removed). The agent works offline (only the model API is reachable); a
**separate** verifier container then rebuilds the submission's `compile.sh`
with no network and runs every active behavioral branch against it, reporting
the fractional pass rate.

## Layout

```
kotlinai/
  harbor.py                 # core: convert_instance / convert_all
  cli/harbor.py             # `programbench harbor export` (typer)
  data/
    templates/              # jinja templates rendered per instance
      harbor_task.toml.j2
      harbor_instruction.md.j2
      harbor_environment.Dockerfile.j2       # agent env: FROM cleanroom
      harbor_verifier.Dockerfile.j2          # separate verifier env: wipe /workspace, own /tests
      harbor_solve.sh.j2    # the golden solution (reference-binary stash)
    harbor/                 # static files copied verbatim into every task
      test.sh               # Harbor verifier entry point
      oracle_compile.sh     # oracle compile.sh: restore ./executable from stash
      run_verifier.py       # scores JUnit XML -> reward
```

The CLI is registered on the main app as a subcommand
(`programbench harbor`, see `src/programbench/cli/main.py`).

## Usage

```bash
# Export selected instances (or all matching filters) into OUT_DIR
uv run programbench harbor export ./out jesseduffield__lazygit.1d0db51
uv run programbench harbor export ./out --filter '^svenstaro__' --slice 0:5
# Require the agent to implement every selected task in Kotlin
uv run programbench harbor export ./out --target-language Kotlin <id>
# Restrict the agent's network allowlist to a specific model API host
uv run programbench harbor export ./out <id> --allowed-host api.openai.com
```

Each instance produces a standard Harbor task directory:

```
<instance_id>/
  task.toml            # metadata, network policy, separate verifier, artifact handoff
  instruction.md       # the agent-facing prompt (reverse-engineer the binary)
  environment/Dockerfile   # agent env: FROM <cleanroom image>; WORKDIR /workspace
  tests/
    Dockerfile         # separate verifier env: FROM cleanroom, wipe /workspace, COPY . /tests
    test.sh            # verifier: compile offline, run each branch, score
    run_verifier.py    # JUnit XML -> reward.json / reward.txt / report.json
    tests.json         # per-branch test lists + ignored_tests (faithful to source)
    branches/<hash>/   # each active branch: full reference tree + eval/ + helpers
  solution/
    solve.sh           # golden solution: stash the reference binary (see below)
    compile.sh         # oracle_compile.sh: restore ./executable from the stash
```

## The golden solution

ProgramBench's environment ships no source, but a working reference build sits
at the canonical path the tests target — `/workspace/executable` — execute-only
so a non-root agent can't read its bytes. The oracle (which runs as root)
reconstructs `./executable` from it with **no network** and no per-language
vendoring:

1. `solve.sh` base64-encodes `/workspace/executable` into
   `/workspace/.programbench_oracle_reference`.
2. It drops `oracle_compile.sh` in as `/workspace/compile.sh`.
3. Harbor hands the agent's `/workspace` (minus `./executable`) to the separate
   verifier, which runs `compile.sh` offline — `base64 -d` restores
   `./executable` — and scores every active branch.

Because the stash is base64 (a different sha256 from the raw binary), it
survives the verifier's clean-hash scrub; `./executable` is produced only after
that scrub. A correct oracle resolves every non-ignored, non-flaky test.

> Note: `eval_clean_hashes` is **not** the installed reference's hash — it's a
> gold-build hash from a different toolchain, so the binary can't be located by
> it. The canonical `/workspace/executable` path (the same one `conftest.py`
> uses: `EXECUTABLE = REPO_ROOT / "executable"`) is the reliable source.

### Verifier scoring

`run_verifier.py` reproduces ProgramBench's per-instance score: for each active
branch the expected set is `tests[]` minus `ignored_tests`; a test passes only
if the JUnit XML marks it passed (absent counts as unresolved). It writes:

- `reward.txt` / `reward.json` — the scalar reward (Harbor reads these; the
  JSON is numeric-only to satisfy Harbor's `VerifierResult` schema);
- `report.json` — the full per-branch breakdown, including the names of any
  expected tests that failed (for debugging).

## Prerequisites for oracle verification

- `docker` and the `harbor` CLI.
- The task's cleanroom image (`docker pull programbench/<mangled_id>:task_cleanroom_v6`).
- A `dynamic_network_policy` environment backend (Docker/OrbStack on Linux with
  nftables) so the agent-phase network allowlist override is accepted. The
  entire task — agent, oracle, and verifier — runs without public internet.

```bash
harbor trials start --path ./out/<instance_id> --agent oracle
```

## Known limitation

A literal 100% oracle pass is not reachable for every task in a single-container
Harbor run — some tests fail on the true reference binary itself in the sandbox
(missing OS facilities, TUI hangs, snapshot date-drift). See
[`status.md`](status.md) for the analysis and why those must be fixed in the
source dataset rather than by editing exported tasks.
