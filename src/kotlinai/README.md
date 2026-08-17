# kotlinai — Harbor export for ProgramBench

Converts ProgramBench reverse-engineering instances into
[Harbor](https://harborframework.com) tasks, each shipping a **runnable golden
solution** so Harbor's oracle agent can verify the task end to end.

One ProgramBench instance maps to one Harbor task. The environment is the
black-box `task_cleanroom_v6` image (reference binary installed, source
removed); the verifier rebuilds a submission's `compile.sh` offline and runs
every active behavioral branch against it, reporting the fractional pass rate.

## Layout

```
kotlinai/
  harbor.py                 # core: convert_instance / convert_all
  cli/harbor.py             # `programbench harbor export` (typer)
  data/
    templates/              # jinja templates rendered per instance
      harbor_task.toml.j2
      harbor_instruction.md.j2
      harbor_environment.Dockerfile.j2
      harbor_solve.sh.j2    # the golden solution (clone + vendor + compile.sh)
    harbor/                 # static files copied verbatim into every task
      test.sh               # Harbor verifier entry point
      run_verifier.py       # scores JUnit XML -> reward
```

The CLI is registered on the main app as a subcommand
(`programbench harbor`, see `src/programbench/cli/main.py`).

## Usage

```bash
# Export selected instances (or all matching filters) into OUT_DIR
uv run programbench harbor export ./out jesseduffield__lazygit.1d0db51
uv run programbench harbor export ./out --filter '^svenstaro__' --slice 0:5
```

Each instance produces a standard Harbor task directory:

```
<instance_id>/
  task.toml            # metadata, timeouts
  instruction.md       # the agent-facing prompt (reverse-engineer the binary)
  environment/Dockerfile   # FROM <cleanroom image>; WORKDIR /workspace
  tests/
    test.sh            # verifier: compile offline, run each branch, score
    run_verifier.py    # JUnit XML -> reward.json / reward.txt / report.json
    tests.json         # per-branch test lists + ignored_tests
    branches/<hash>/   # each active branch: full reference tree + eval/ + helpers
  solution/
    solve.sh           # golden solution (see below)
    compile.sh         # upstream build recipe, packed as-is
```

## The golden solution

ProgramBench's environment ships no source, so the gold is reconstructed from
the **real upstream repository**. At export time `convert_instance` packs the
upstream build recipe (`build.sh`, identical across a task's branches) as
`solution/compile.sh`. At oracle time `solve.sh`:

1. clones `https://github.com/<repository>` at the reference `commit` into
   `/workspace`;
2. **vendors dependencies while the network is still available**
   (`cargo vendor` for Rust, `go mod vendor` for Go) so the offline build can
   resolve them — the same thing a real submission must do;
3. drops the packed `compile.sh` into `/workspace`.

`tests/test.sh` then rebuilds `./executable` from that `compile.sh` with the
network blocked and scores every active branch — so a correct oracle resolves
every non-ignored, non-flaky test.

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
- Network during the **agent/solve phase** (for the clone + vendor step). The
  compile phase inside `test.sh` is always offline by design.

```bash
harbor trials start --path ./out/<instance_id> --agent oracle
```

## Known limitation

A literal 100% oracle pass is not reachable for every task in a single-container
Harbor run — some tests fail on the true reference binary itself in the sandbox
(missing OS facilities, TUI hangs, snapshot date-drift). See
[`todo.md`](todo.md) for the analysis and the options for closing the gap.
