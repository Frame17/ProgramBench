# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from pathlib import Path

import typer

app = typer.Typer(
    name="harbor",
    help="Export tasks into the Harbor task format (harborframework.com).",
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
