# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from pathlib import Path

import typer
import yaml

app = typer.Typer(
    name="harbor",
    help="Export and compare tasks in the Harbor task format (harborframework.com).",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.command()
def export(
    out_dir: Path = typer.Argument(..., help="Directory to write Harbor task directories into."),
    instances: list[str] = typer.Argument(None, help="Instance IDs to export (omit for all matching filters)."),
    filter_spec: str = typer.Option("", "--filter", help="Restrict to instance IDs matching this regex."),
    slice_spec: str = typer.Option("", "--slice", help="Slice specification (e.g. '0:5')."),
    allowed_host: list[str] = typer.Option(
        None, "--allowed-host", help="Model API host the agent may reach (repeatable); defaults to common LLM APIs."
    ),
    target_language: str | None = typer.Option(
        None,
        "--target-language",
        help="Language the agent must use for its implementation; defaults to the instance language.",
    ),
) -> None:
    """Convert ProgramBench instances into Harbor tasks under OUT_DIR."""
    from kotlinai.harbor import convert_all

    failures: list[tuple[str, Exception]] = []

    def _skip(instance_id: str, exc: Exception) -> None:
        typer.echo(f"WARNING: skipping {instance_id}: {type(exc).__name__}: {exc}", err=True)
        failures.append((instance_id, exc))

    paths = convert_all(
        out_dir,
        instance_ids=instances or None,
        filter_spec=filter_spec,
        slice_spec=slice_spec,
        allowed_hosts=allowed_host or None,
        target_language=target_language,
        on_error=_skip,
    )
    for p in paths:
        typer.echo(p)
    summary = f"Exported {len(paths)} Harbor task(s) to {out_dir}"
    if failures:
        summary += f", skipped {len(failures)} ({', '.join(iid for iid, _ in failures)})"
    typer.echo(summary)


@app.command("export-config")
def export_config(
    config: Path = typer.Argument(..., help="Harbor job config containing target_language and datasets."),
) -> None:
    """Export the task datasets selected by a Harbor job config."""
    from kotlinai.harbor import convert_all

    data = yaml.safe_load(config.read_text())
    if not isinstance(data, dict):
        raise typer.BadParameter("Harbor config must contain a YAML object")

    target_language = data.get("target_language")
    if not isinstance(target_language, str) or not target_language.strip():
        raise typer.BadParameter("Harbor config must define a non-empty target_language")

    datasets = data.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise typer.BadParameter("Harbor config must define at least one dataset")

    total = 0
    failures: list[tuple[str, Exception]] = []

    def _skip(instance_id: str, exc: Exception) -> None:
        typer.echo(f"WARNING: skipping {instance_id}: {type(exc).__name__}: {exc}", err=True)
        failures.append((instance_id, exc))

    for dataset in datasets:
        if not isinstance(dataset, dict):
            raise typer.BadParameter("Each Harbor dataset must be an object")
        out_dir = dataset.get("path")
        task_names = dataset.get("task_names")
        if not isinstance(out_dir, str) or not out_dir.strip():
            raise typer.BadParameter("Each Harbor dataset must define a non-empty path")
        if task_names is not None and (
            not isinstance(task_names, list)
            or not task_names
            or not all(isinstance(name, str) and name for name in task_names)
        ):
            raise typer.BadParameter("Harbor dataset task_names must be a non-empty list of strings")

        paths = convert_all(
            Path(out_dir), instance_ids=task_names or None, target_language=target_language, on_error=_skip
        )
        total += len(paths)
        for path in paths:
            typer.echo(path)

    # Per-task conversion failures are skipped (not fatal) so the surviving tasks
    # still flow into `harbor run`; the exit code stays 0 for the runner's
    # `set -euo pipefail`. Genuinely fatal config errors already raised above.
    summary = f"Exported {total} Harbor task(s) from {config}"
    if failures:
        summary += f", skipped {len(failures)} ({', '.join(iid for iid, _ in failures)})"
    typer.echo(summary)


@app.command()
def compare(
    config: Path = typer.Option(
        Path("scripts/language_comparison/config.yaml"),
        "--config",
        help="Comparison config containing languages, parallelism, agent, tasks, and shared Harbor options.",
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        help="Experiment directory; defaults to experiments/<UTC timestamp>.",
    ),
    runner: Path = typer.Option(
        Path("scripts/run_agent.sh"),
        "--runner",
        help="Path to run_agent.sh.",
    ),
    report_only: bool = typer.Option(
        False,
        "--report-only",
        help="Regenerate report.md and report.json from an existing experiment.",
    ),
) -> None:
    """Run the same ProgramBench tasks in two languages and compare them."""
    from kotlinai.experiment import (
        ComparisonError,
        default_experiment_dir,
        run_comparison_experiment,
        write_comparison_reports,
    )

    try:
        if report_only:
            if output_dir is None:
                raise ComparisonError("--output-dir is required with --report-only")
            markdown_path, json_path = write_comparison_reports(output_dir)
            typer.echo(markdown_path)
            typer.echo(json_path)
            return

        target_dir = output_dir or default_experiment_dir()
        exit_code = run_comparison_experiment(config, target_dir, runner)
        typer.echo(target_dir.resolve() / "report.md")
        typer.echo(target_dir.resolve() / "report.json")
        if exit_code:
            raise typer.Exit(exit_code)
    except ComparisonError as exc:
        raise typer.BadParameter(str(exc)) from exc
