# Scripts

Run commands from the repository root.

## Agent evaluation

Add `LITELLM_API_KEY` and `LITELLM_BASE_URL` to `.env`, then run:

```bash
./scripts/run_agent.sh
```

Extra arguments are passed to `harbor run`. Edit `config.yaml` to select the
model, tasks, language, and concurrency.

## Oracle verification

```bash
./scripts/run_oracle_verification.sh
```

This runs the tasks in `config.yaml` with Harbor's oracle agent.
Before the run, it exports those tasks with an oracle-only reference payload.
The task's agent user remains non-root, so the same export can be used with a
model agent.

Set `HARBOR_CONFIG` to use another job config. Set `HARBOR_SKIP_EXPORT=1` to
reuse tasks that were already exported. Reused tasks need
`solution/reference.gz` for a non-root oracle run. This file contains the
reference binary and must only be stored in trusted storage.

## Language comparison

```bash
./scripts/language_comparison/run_language_comparison.sh
```

Edit `language_comparison/config.yaml` to select languages, tasks, and the
agent. Set `LANGUAGE_COMPARISON_CONFIG` to use another config file.
