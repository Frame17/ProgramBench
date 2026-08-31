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

Set `HARBOR_CONFIG` to use another job config. Set `HARBOR_SKIP_EXPORT=1` to
reuse tasks that were already exported.

## Language comparison

```bash
./scripts/language_comparison/run_language_comparison.sh
```

Edit `language_comparison/config.yaml` to select languages, tasks, and the
agent. Set `LANGUAGE_COMPARISON_CONFIG` to use another config file.
