# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for the two-language Harbor comparison harness."""

import json
from pathlib import Path

import pytest
import yaml

from kotlinai.experiment import (
    DEFAULT_AGENT_MAX_BUDGET_USD,
    ComparisonError,
    apply_agent_cost_limit,
    build_comparison_job_config,
    build_comparison_report,
    render_markdown_report,
    run_comparison_experiment,
    validate_comparison_config,
    write_comparison_reports,
)


def _base_config() -> dict:
    return {
        "languages": ["Java", "Kotlin"],
        "parallelism": 3,
        "agent": {"name": "codex", "model_name": "openai/test-model", "kwargs": {"x": 1}},
        "tasks": {"task_names": ["task-a", "task-b"]},
        "harbor": {"n_attempts": 2, "retry": {"max_retries": 1}},
    }


def test_comparison_job_config_contains_both_languages_and_shared_parameters(tmp_path):
    base = _base_config()
    settings = validate_comparison_config(base)
    config = build_comparison_job_config(
        settings,
        task_dirs={
            "java": tmp_path / "tasks" / "java",
            "kotlin": tmp_path / "tasks" / "kotlin",
        },
        jobs_dir=tmp_path / "jobs",
        job_name="comparison",
    )

    assert base == _base_config()
    assert config["job_name"] == "comparison"
    assert config["n_concurrent_trials"] == 3
    assert config["n_attempts"] == 2
    assert config["retry"] == {"max_retries": 1}
    assert config["agents"] == [base["agent"]]

    # Languages alternate task by task, so neither runs entirely before the other.
    # Single-task datasets (not Harbor's `tasks` list) keep the job's metric
    # registry seeded per language; `tasks` aborts the job with an IndexError.
    assert "tasks" not in config
    assert [(Path(dataset["path"]).name, dataset["task_names"]) for dataset in config["datasets"]] == [
        ("java", ["task-a"]),
        ("kotlin", ["task-a"]),
        ("java", ["task-b"]),
        ("kotlin", ["task-b"]),
    ]


def test_comparison_job_config_injects_default_retry_when_unset(tmp_path):
    base = _base_config()
    base["harbor"].pop("retry")
    settings = validate_comparison_config(base)
    config = build_comparison_job_config(
        settings,
        task_dirs={
            "java": tmp_path / "tasks" / "java",
            "kotlin": tmp_path / "tasks" / "kotlin",
        },
        jobs_dir=tmp_path / "jobs",
        job_name="comparison",
    )

    # Transient agent API errors are retried; genuine timeouts/limits stay
    # non-retried via Harbor's default exclude_exceptions.
    assert config["retry"] == {"max_retries": 2}


def test_comparison_job_config_keeps_user_supplied_retry(tmp_path):
    base = _base_config()
    base["harbor"]["retry"] = {"max_retries": 5}
    settings = validate_comparison_config(base)
    config = build_comparison_job_config(
        settings,
        task_dirs={
            "java": tmp_path / "tasks" / "java",
            "kotlin": tmp_path / "tasks" / "kotlin",
        },
        jobs_dir=tmp_path / "jobs",
        job_name="comparison",
    )

    assert config["retry"] == {"max_retries": 5}


def test_validate_comparison_config_rejects_non_object_retry():
    config = _base_config()
    config["harbor"]["retry"] = 3
    with pytest.raises(ComparisonError, match="harbor.retry"):
        validate_comparison_config(config)


def test_apply_agent_cost_limit_defaults_claude_code_budget():
    agent = {"name": "claude-code", "model_name": "claude-sonnet-5"}
    apply_agent_cost_limit(agent)
    assert agent["kwargs"]["max_budget_usd"] == DEFAULT_AGENT_MAX_BUDGET_USD


def test_apply_agent_cost_limit_keeps_user_supplied_budget():
    agent = {"name": "claude-code", "model_name": "claude-sonnet-5", "kwargs": {"max_budget_usd": "50"}}
    apply_agent_cost_limit(agent)
    assert agent["kwargs"]["max_budget_usd"] == "50"


def test_apply_agent_cost_limit_is_noop_for_other_agents():
    agent = {"name": "codex", "model_name": "openai/test-model"}
    apply_agent_cost_limit(agent)
    assert "max_budget_usd" not in agent.get("kwargs", {})


@pytest.mark.parametrize("languages", [[], ["Java"], ["Java", "Kotlin", "Go"], ["Java", "java"], ["", "Kotlin"]])
def test_validate_comparison_config_requires_two_distinct_languages(languages):
    config = _base_config()
    config["languages"] = languages
    with pytest.raises(ComparisonError):
        validate_comparison_config(config)


@pytest.mark.parametrize("parallelism", [None, True, 0, -1, 1.5])
def test_validate_comparison_config_requires_positive_integer_parallelism(parallelism):
    config = _base_config()
    config["parallelism"] = parallelism
    with pytest.raises(ComparisonError, match="parallelism"):
        validate_comparison_config(config)


@pytest.mark.parametrize("task_names", [[], ["task-*"], ["task-a", "task-a"], [""]])
def test_validate_comparison_config_requires_explicit_unique_tasks(task_names):
    config = _base_config()
    config["tasks"]["task_names"] = task_names
    with pytest.raises(ComparisonError):
        validate_comparison_config(config)


@pytest.mark.parametrize("field", ["job_name", "jobs_dir", "n_concurrent_trials", "agents", "datasets"])
def test_validate_comparison_config_rejects_generated_harbor_fields(field):
    config = _base_config()
    config["harbor"][field] = "conflict"
    with pytest.raises(ComparisonError, match="generated field"):
        validate_comparison_config(config)


def test_experiment_uses_configured_language_order_and_reports_after_failure(tmp_path):
    config = _base_config()
    config["languages"] = ["Go", "Rust"]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    runner = tmp_path / "repo" / "scripts" / "run_agent.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\n")
    output_dir = tmp_path / "experiment"
    calls = []

    def fake_export(out_dir: Path, *, instance_ids: list[str], target_language: str):
        calls.append(("export", target_language))
        paths = []
        for instance_id in instance_ids:
            path = out_dir / instance_id
            path.mkdir(parents=True)
            paths.append(path)
        return paths

    def fake_run(_runner: Path, derived_config: Path) -> int:
        derived = yaml.safe_load(derived_config.read_text())
        calls.append(("run", derived["job_name"]))
        assert derived["n_concurrent_trials"] == 3
        assert [Path(d["path"]).name for d in derived["datasets"]] == ["go", "rust", "go", "rust"]
        return 2

    exit_code = run_comparison_experiment(
        config_path,
        output_dir,
        runner,
        exporter=fake_export,
        agent_runner=fake_run,
        version_resolver=lambda package: "1.2.3",
    )

    assert exit_code == 1
    assert calls == [("export", "Go"), ("export", "Rust"), ("run", "comparison")]
    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["language_order"] == ["Go", "Rust"]
    assert manifest["parallelism"] == 3
    assert manifest["languages"]["go"]["status"] == "failed"
    assert manifest["languages"]["rust"]["status"] == "failed"
    assert manifest["job"]["status"] == "failed"
    assert manifest["languages"]["go"]["job_dir"] == manifest["languages"]["rust"]["job_dir"]
    assert Path(manifest["languages"]["go"]["job_dir"]).name == "comparison"
    assert (output_dir / "report.md").is_file()
    assert (output_dir / "report.json").is_file()
    report = json.loads((output_dir / "report.json").read_text())
    assert report["languages"] == [{"key": "go", "name": "Go"}, {"key": "rust", "name": "Rust"}]
    assert report["delta"] == {"from": "go", "to": "rust"}
    assert (output_dir / "report.md").read_text().startswith("# Go/Rust ProgramBench Comparison")


def test_experiment_pins_one_agent_version_per_run(tmp_path):
    config_path = tmp_path / "config.yaml"
    runner = tmp_path / "repo" / "scripts" / "run_agent.sh"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/usr/bin/env bash\n")
    resolved = []

    def resolver(package: str) -> str:
        resolved.append(package)
        return "9.9.9"

    def fake_export(out_dir: Path, *, instance_ids: list[str], target_language: str):
        return [(out_dir / instance_id) for instance_id in instance_ids]

    def run(output_dir: Path, config: dict) -> dict:
        config_path.write_text(yaml.safe_dump(config))
        run_comparison_experiment(
            config_path,
            output_dir,
            runner,
            exporter=fake_export,
            agent_runner=lambda _runner, _config: 0,
            version_resolver=resolver,
        )
        return yaml.safe_load((output_dir / "configs" / "comparison.yaml").read_text())

    derived = run(tmp_path / "resolved", _base_config())
    assert resolved == ["@openai/codex"]
    assert derived["agents"][0]["kwargs"]["version"] == "9.9.9"
    assert json.loads((tmp_path / "resolved" / "manifest.json").read_text())["agent"]["version"] == "9.9.9"

    # An explicit pin wins, and no registry lookup happens.
    pinned = _base_config()
    pinned["agent"]["kwargs"]["version"] = "0.1.0"
    assert run(tmp_path / "explicit", pinned)["agents"][0]["kwargs"]["version"] == "0.1.0"
    assert resolved == ["@openai/codex"]


def _write_trial(
    job_dir: Path,
    trial_name: str,
    *,
    task_name: str,
    reward: float | None = None,
    agent_result: dict | None = None,
    step_results: list[dict] | None = None,
    source: str | None = None,
    exception_type: str | None = None,
    total_steps: int | None = None,
    fallback_steps: int | None = None,
) -> None:
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True)
    result = {
        "task_name": task_name,
        "trial_name": trial_name,
        "source": source,
        "agent_result": agent_result,
        "step_results": step_results,
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": {"exception_type": exception_type} if exception_type else None,
    }
    (trial_dir / "result.json").write_text(json.dumps(result))
    if total_steps is not None or fallback_steps is not None:
        trajectory = {
            "steps": [{} for _ in range(fallback_steps or 1)],
            "final_metrics": {"total_steps": total_steps} if total_steps is not None else None,
        }
        (trial_dir / "agent").mkdir()
        (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(trajectory))


def test_report_aggregates_attempts_tokens_steps_and_failures(tmp_path):
    experiment_dir = tmp_path / "experiment"
    job_dir = experiment_dir / "jobs" / "comparison"
    manifest = {
        "status": "failed",
        "base_config": "/repo/scripts/language_comparison/config.yaml",
        "agent": {"name": "codex", "model_name": "openai/test-model"},
        "language_order": ["Java", "Kotlin"],
        "parallelism": 3,
        "task_names": ["task-a", "task-b"],
        "languages": {
            "java": {"job_dir": str(job_dir), "source": "java"},
            "kotlin": {"job_dir": str(job_dir), "source": "kotlin"},
        },
    }
    experiment_dir.mkdir()
    (experiment_dir / "manifest.json").write_text(json.dumps(manifest))

    _write_trial(
        job_dir,
        "java__task-a__1",
        task_name="task-a",
        source="java",
        reward=0.5,
        agent_result={"n_input_tokens": 100, "n_cache_tokens": 60, "n_output_tokens": 20, "cost_usd": 0.1},
        total_steps=4,
    )
    _write_trial(
        job_dir,
        "java__task-a__2",
        task_name="task-a",
        source="java",
        reward=1.0,
        agent_result={"n_input_tokens": 200, "n_cache_tokens": 100, "n_output_tokens": 40, "cost_usd": 0.2},
        fallback_steps=3,
    )
    _write_trial(
        job_dir,
        "java__task-b__1",
        task_name="task-b",
        source="java",
        exception_type="AgentSetupError",
    )
    _write_trial(
        job_dir,
        "kotlin__task-a__1",
        task_name="task-a",
        source="kotlin",
        reward=0.75,
        step_results=[
            {"agent_result": {"n_input_tokens": 80, "n_cache_tokens": 40, "n_output_tokens": 15, "cost_usd": 0.05}},
            {"agent_result": {"n_input_tokens": 20, "n_cache_tokens": 10, "n_output_tokens": 5, "cost_usd": 0.02}},
        ],
        total_steps=6,
    )

    report = build_comparison_report(experiment_dir)
    task_a = report["tasks"][0]
    java = task_a["results"]["java"]
    kotlin = task_a["results"]["kotlin"]
    assert java["attempts"] == 2
    assert java["reward_mean"] == 0.75
    assert java["reward_sum"] == 1.5
    assert java["cost_usd"] == pytest.approx(0.3)
    assert java["steps"] == 7
    assert java["n_total_tokens"] == 360
    assert kotlin["n_input_tokens"] == 100
    assert kotlin["n_cache_tokens"] == 50
    assert kotlin["n_output_tokens"] == 20
    assert kotlin["n_total_tokens"] == 120
    assert task_a["delta"]["n_total_tokens"] == -240
    assert report["delta"] == {"from": "java", "to": "kotlin"}
    assert report["tasks"][1]["results"]["java"]["failure_types"] == ["AgentSetupError"]
    assert report["tasks"][1]["results"]["kotlin"]["attempts"] == 0

    markdown = render_markdown_report(report)
    assert "Kotlin minus Java" in markdown
    assert "## Token breakdown by task" in markdown
    assert "`AgentSetupError`" in markdown

    markdown_path, json_path = write_comparison_reports(experiment_dir)
    assert markdown_path.read_text() == markdown
    persisted = json.loads(json_path.read_text())
    assert persisted["totals"]["results"]["java"]["reward_sum"] == 1.5


def test_report_paired_difference_summarizes_per_task_deltas(tmp_path):
    experiment_dir = tmp_path / "experiment"
    job_dir = experiment_dir / "jobs" / "comparison"
    experiment_dir.mkdir()
    (experiment_dir / "manifest.json").write_text(
        json.dumps(
            {
                "language_order": ["Java", "Kotlin"],
                "task_names": ["task-a", "task-b", "task-c", "task-d"],
                "languages": {
                    "java": {"job_dir": str(job_dir), "source": "java"},
                    "kotlin": {"job_dir": str(job_dir), "source": "kotlin"},
                },
            }
        )
    )
    rewards = {
        "task-a": {"java": 0.5, "kotlin": 0.75},  # +0.25
        "task-b": {"java": 1.0, "kotlin": 0.5},  # -0.5
        "task-c": {"java": 0.25, "kotlin": 0.25},  # tie
        "task-d": {"java": 0.4, "kotlin": None},  # unpaired, excluded
    }
    for task_name, by_language in rewards.items():
        for language, reward in by_language.items():
            _write_trial(job_dir, f"{language}__{task_name}", task_name=task_name, source=language, reward=reward)

    paired = build_comparison_report(experiment_dir)["paired"]

    assert paired["n"] == 3
    assert paired["mean"] == pytest.approx(-0.25 / 3)
    assert paired["sd"] == pytest.approx(0.3818813, abs=1e-6)
    # t(df=2) = 4.303, so a three-task interval is far too wide to exclude zero.
    assert paired["ci_low"] == pytest.approx(-1.0320565, abs=1e-6)
    assert paired["ci_high"] == pytest.approx(0.8653898, abs=1e-6)
    assert (paired["wins"], paired["losses"], paired["ties"]) == (1, 1, 1)

    markdown = render_markdown_report(build_comparison_report(experiment_dir))
    assert "## Paired difference" in markdown
    assert "| Mean difference | -0.0833 |" in markdown
    assert "| Task wins (Kotlin / Java / tie) | 1 / 1 / 1 |" in markdown


def test_report_supports_legacy_separate_job_manifest_without_sources(tmp_path):
    experiment_dir = tmp_path / "experiment"
    java_dir = experiment_dir / "jobs" / "java"
    kotlin_dir = experiment_dir / "jobs" / "kotlin"
    manifest = {
        "language_order": ["Java", "Kotlin"],
        "task_names": ["task-a"],
        "languages": {
            "java": {"job_dir": str(java_dir)},
            "kotlin": {"job_dir": str(kotlin_dir)},
        },
    }
    experiment_dir.mkdir()
    (experiment_dir / "manifest.json").write_text(json.dumps(manifest))
    _write_trial(java_dir, "task-a__java", task_name="task-a", reward=0.5)
    _write_trial(kotlin_dir, "task-a__kotlin", task_name="task-a", reward=1.0)

    report = build_comparison_report(experiment_dir)

    assert report["totals"]["results"]["java"]["reward_sum"] == 0.5
    assert report["totals"]["results"]["kotlin"]["reward_sum"] == 1.0


def test_report_rejects_missing_manifest(tmp_path):
    with pytest.raises(ComparisonError):
        build_comparison_report(tmp_path)
