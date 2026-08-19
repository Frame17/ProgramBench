# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from pathlib import Path

import typer

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

    paths = convert_all(
        out_dir,
        instance_ids=instances or None,
        filter_spec=filter_spec,
        slice_spec=slice_spec,
        allowed_hosts=allowed_host or None,
        target_language=target_language,
    )
    for p in paths:
        typer.echo(p)
    typer.echo(f"Exported {len(paths)} Harbor task(s) to {out_dir}")


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
