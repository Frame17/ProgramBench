# kotlinai — Harbor export for ProgramBench

Converts ProgramBench reverse-engineering instances into
[Harbor](https://harborframework.com) tasks, each shipping a **runnable golden
solution** so Harbor's oracle agent can verify the task end to end.

One ProgramBench instance maps to one Harbor task. The environment is the
black-box `task_cleanroom_v6` image (reference binary installed, source
removed). Kotlin exports add OpenJDK 21 and the Kotlin 2.4.10 compiler to both
the agent and separate verifier images; Java exports add OpenJDK 21. Both JVM
(Java and Kotlin) exports also bake in Gradle 8.14 — verified against a pinned
SHA-256 during the public-network image build — and prime its wrapper
distribution cache under a baked `GRADLE_USER_HOME`, so `gradle` and a
project-generated `./gradlew` both work fully offline without reaching the
`services.gradle.org`→GitHub redirect that the agent allowlist deliberately
blocks. The agent can therefore build and test without spending benchmark time
installing its toolchain. The verifier rebuilds the submission's `compile.sh`
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
# Export a portable task for trusted storage and non-root oracle runs
uv run programbench harbor export ./out <id> --include-oracle-payload
```

The agent phase runs as the cleanroom image's non-root `agent` user, so the
mode-111 reference binary at `/workspace/executable` can be executed but not
read, copied, or statically inspected; the generated agent image also drops the
image's NOPASSWD sudo grant, which would otherwise hand root back. The default
agent network allowlist covers model APIs plus Maven, Gradle, Kotlin, Android,
and related build-tool hosts so dependencies can be stored in `/workspace` for
the offline verifier build. Source-hosting hosts remain blocked. Harbor installs
the agent CLI during *setup*, before the agent-phase policy applies, so its
installer hosts do not belong on the task allowlist.

## Language comparison experiment

`scripts/language_comparison/run_language_comparison.sh` reads the languages,
task parallelism, agent, task list, and shared Harbor options from the adjacent
`config.yaml`. It exports a separate task set for each configured language, runs
all language-task trials in one Harbor job, and writes Markdown and JSON
comparisons of reward, cost, agent steps, and tokens. The wrapper includes
oracle payloads in both generated task sets, so its experiment directories
contain reference binaries and must remain in trusted storage.

```bash
./scripts/language_comparison/run_language_comparison.sh
./scripts/language_comparison/run_language_comparison.sh \
  --output-dir experiments/miniserve-comparison
./scripts/language_comparison/run_language_comparison.sh \
  --config path/to/comparison-config.yaml

# Rebuild reports from existing Harbor results without running agents
./scripts/language_comparison/run_language_comparison.sh \
  --output-dir experiments/miniserve-comparison --report-only
```

The config must contain exactly two languages, one agent, positive parallelism,
and an explicit `tasks.task_names` list. `parallelism` controls concurrent trials
globally across both languages; keep `parallelism * harbor.DOCKER_CPUS` inside
the host's core count. Trials are submitted with the languages interleaved task
by task, so drift in host load or model serving over a long run cannot align with
the language contrast. Each run also resolves and pins one agent CLI version for
all of its trials, recorded in `manifest.json` and the derived job config; set
`agent.kwargs.version` yourself to skip the registry lookup. Report deltas are
the second language minus the first, and `report.md` carries a paired-difference
interval over the per-task deltas.
Each experiment keeps its generated tasks, derived configs, Harbor jobs,
`manifest.json`, `report.md`, and `report.json` under its output directory.

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
    reference.gz       # optional oracle-only payload for trusted storage
```

## The golden solution

ProgramBench's environment ships no source, but a working reference build sits
at the canonical path the tests target — `/workspace/executable` — execute-only
so a non-root agent can't read its bytes. An export created with
`--include-oracle-payload` also stores a compressed copy at
`solution/reference.gz`. Stock Harbor uploads `solution/` only for its oracle
agent, so both oracle and model agents can use the same task while the task's
configured agent user remains non-root. The oracle reconstructs `./executable`
with **no network** and no per-language vendoring:

1. `solve.sh` copies `solution/reference.gz` into
   `/workspace/.programbench_oracle_reference.gz`.
2. It drops `oracle_compile.sh` in as `/workspace/compile.sh`.
3. Harbor hands the agent's `/workspace` (minus `./executable`) to the separate
   verifier, which runs `compile.sh` offline — `gzip -dc` restores
   `./executable` — and scores every active branch.

Because the stash is compressed (a different sha256 from the raw binary), it
survives the verifier's clean-hash scrub; `./executable` is produced only after
that scrub. A correct oracle resolves every non-ignored, non-flaky test. Exports
without the payload retain the older root-readable reference fallback.

The payload contains the reference binary and therefore the benchmark answer.
Use it only in trusted storage. Model agents do not receive `/solution`, but
anyone who can read the task archive can extract the payload.

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
  agent, oracle, and offline compilation run without public internet. Harbor
  still needs network access while building the task images.

```bash
uv run programbench harbor export ./out <instance_id> --include-oracle-payload
harbor trials start --path ./out/<instance_id> --agent oracle
```

## Known limitation

A literal 100% oracle pass is not reachable for every task in a single-container
Harbor run — some tests fail on the true reference binary itself in the sandbox
(missing OS facilities, TUI hangs, snapshot date-drift).
