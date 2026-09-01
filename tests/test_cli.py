# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Smoke tests for CLI subcommands."""

from pathlib import Path

import yaml
from typer.testing import CliRunner

from programbench.cli.main import app

runner = CliRunner()


def test_top_level_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "eval" in result.output
    assert "blob" in result.output
    assert "info" in result.output


def test_info_help():
    result = runner.invoke(app, ["info", "--help"])
    assert result.exit_code == 0
    assert "run-dir" in result.output.lower() or "run_dir" in result.output.lower()


def test_blob_help():
    result = runner.invoke(app, ["blob", "--help"])
    assert result.exit_code == 0
    assert "sync" in result.output


def test_blob_sync_help():
    result = runner.invoke(app, ["blob", "sync", "--help"])
    assert result.exit_code == 0
    assert "instance" in result.output.lower()


def test_harbor_compare_help():
    result = runner.invoke(app, ["harbor", "compare", "--help"])
    assert result.exit_code == 0
    assert "output-dir" in result.output.lower()
    assert "report-only" in result.output.lower()
    assert "include-oracle-payload" in result.output.lower()


def test_harbor_compare_forwards_oracle_payload(tmp_path, monkeypatch):
    calls = []

    def fake_run(config, output_dir, comparison_runner, *, include_oracle_payload):
        calls.append((config, output_dir, comparison_runner, include_oracle_payload))
        return 0

    monkeypatch.setattr("kotlinai.experiment.run_comparison_experiment", fake_run)
    config = tmp_path / "config.yaml"
    output_dir = tmp_path / "experiment"
    result = runner.invoke(
        app,
        [
            "harbor",
            "compare",
            "--config",
            str(config),
            "--output-dir",
            str(output_dir),
            "--include-oracle-payload",
        ],
    )

    assert result.exit_code == 0
    assert calls == [(config, output_dir, Path("scripts/run_agent.sh"), True)]


def test_harbor_export_config_uses_job_dataset(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "target_language": "Kotlin",
                "datasets": [{"path": str(tmp_path / "tasks"), "task_names": ["task-a", "task-b"]}],
            }
        )
    )
    calls = []

    def fake_convert(
        out_dir, *, instance_ids, target_language, agent_user, include_oracle_payload, on_error=None
    ):
        calls.append((out_dir, instance_ids, target_language, agent_user, include_oracle_payload))
        return [out_dir / instance_id for instance_id in instance_ids]

    monkeypatch.setattr("kotlinai.harbor.convert_all", fake_convert)
    result = runner.invoke(app, ["harbor", "export-config", str(config)])

    assert result.exit_code == 0
    assert calls == [(tmp_path / "tasks", ["task-a", "task-b"], "Kotlin", "agent", False)]
    assert "Exported 2 Harbor task(s)" in result.output


def test_harbor_export_config_forwards_root_agent_user(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "target_language": "Kotlin",
                "datasets": [{"path": str(tmp_path / "tasks"), "task_names": ["task-a"]}],
            }
        )
    )
    calls = []

    def fake_convert(
        out_dir, *, instance_ids, target_language, agent_user, include_oracle_payload, on_error=None
    ):
        calls.append((out_dir, instance_ids, target_language, agent_user, include_oracle_payload))
        return [out_dir / instance_id for instance_id in instance_ids]

    monkeypatch.setattr("kotlinai.harbor.convert_all", fake_convert)
    result = runner.invoke(
        app,
        ["harbor", "export-config", str(config), "--agent-user", "root", "--include-oracle-payload"],
    )

    assert result.exit_code == 0
    assert calls == [(tmp_path / "tasks", ["task-a"], "Kotlin", "root", True)]


def test_harbor_export_forwards_oracle_payload_flag(tmp_path, monkeypatch):
    calls = []

    def fake_convert(out_dir, **kwargs):
        calls.append((out_dir, kwargs))
        return [out_dir / "task-a"]

    monkeypatch.setattr("kotlinai.harbor.convert_all", fake_convert)
    result = runner.invoke(
        app,
        ["harbor", "export", str(tmp_path / "tasks"), "task-a", "--include-oracle-payload"],
    )

    assert result.exit_code == 0
    assert calls[0][0] == tmp_path / "tasks"
    assert calls[0][1]["instance_ids"] == ["task-a"]
    assert calls[0][1]["include_oracle_payload"] is True


def test_submit_help():
    result = runner.invoke(app, ["submit", "--help"])
    assert result.exit_code == 0
    assert all(cmd in result.output for cmd in ("package", "publish", "verify", "register", "recombine"))


def test_submit_package_help():
    result = runner.invoke(app, ["submit", "package", "--help"])
    assert result.exit_code == 0
    assert "upload" in result.output.lower()


def test_submit_register_help():
    result = runner.invoke(app, ["submit", "register", "--help"])
    assert result.exit_code == 0
    assert "registry" in result.output.lower()


def test_submit_publish_help():
    result = runner.invoke(app, ["submit", "publish", "--help"])
    assert result.exit_code == 0
    assert "owner" in result.output.lower()
