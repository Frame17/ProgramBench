# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Run and report a two-language ProgramBench comparison experiment."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from kotlinai.harbor import convert_all

REPORT_SCHEMA_VERSION = 2
RESERVED_HARBOR_FIELDS = {
    "agents",
    "datasets",
    "job_name",
    "jobs_dir",
    "n_concurrent_trials",
    "target_language",
    "tasks",
}


class ComparisonError(ValueError):
    """Raised when an experiment cannot guarantee a fair comparison."""


@dataclass(frozen=True)
class ComparisonSettings:
    languages: tuple[str, str]
    parallelism: int
    agent: dict[str, Any]
    task_names: list[str]
    harbor: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_experiment_dir(root: Path = Path("experiments")) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d__%H-%M-%S")
    return root / timestamp


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ComparisonError(f"Config file does not exist: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ComparisonError(f"Expected a YAML object in {path}")
    return data


def _language_key(language: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", language.strip().lower()).strip("-")


def _validate_languages(value: Any) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) for item in value):
        raise ComparisonError("languages must contain exactly two names")
    languages = (value[0].strip(), value[1].strip())
    if any(not language for language in languages):
        raise ComparisonError("language names cannot be empty")
    keys = tuple(_language_key(language) for language in languages)
    if any(not key for key in keys) or len(set(keys)) != 2:
        raise ComparisonError("languages must have two distinct filesystem-safe names")
    return languages


def validate_comparison_config(config: dict[str, Any]) -> ComparisonSettings:
    """Validate and normalize the comparison-specific configuration."""
    allowed_fields = {"languages", "parallelism", "agent", "tasks", "harbor"}
    unknown_fields = sorted(set(config) - allowed_fields)
    if unknown_fields:
        raise ComparisonError(f"Unknown comparison config field(s): {unknown_fields}")

    languages = _validate_languages(config.get("languages"))
    parallelism = config.get("parallelism")
    if isinstance(parallelism, bool) or not isinstance(parallelism, int) or parallelism < 1:
        raise ComparisonError("parallelism must be an integer greater than zero")

    agent = config.get("agent")
    if not isinstance(agent, dict):
        raise ComparisonError("agent must be an object")
    if not isinstance(agent.get("name"), str) or not agent["name"].strip():
        raise ComparisonError("agent.name must be a non-empty string")
    if not isinstance(agent.get("model_name"), str) or not agent["model_name"].strip():
        raise ComparisonError("agent.model_name must be a non-empty string")

    tasks = config.get("tasks")
    if not isinstance(tasks, dict):
        raise ComparisonError("tasks must be an object")
    unknown_task_fields = sorted(set(tasks) - {"task_names"})
    if unknown_task_fields:
        raise ComparisonError(f"Unknown tasks field(s): {unknown_task_fields}")
    task_names = tasks.get("task_names")
    if not isinstance(task_names, list) or not task_names or not all(isinstance(name, str) for name in task_names):
        raise ComparisonError("tasks.task_names must be a non-empty list")
    if any(not name.strip() for name in task_names):
        raise ComparisonError("task names cannot be empty")
    if any(any(marker in name for marker in "*?[") for name in task_names):
        raise ComparisonError("task_names must be exact task IDs; glob patterns are not supported")
    if len(set(task_names)) != len(task_names):
        raise ComparisonError("task_names contains duplicate task IDs")

    harbor = config.get("harbor", {})
    if not isinstance(harbor, dict):
        raise ComparisonError("harbor must be an object")
    conflicting_fields = sorted(set(harbor) & RESERVED_HARBOR_FIELDS)
    if conflicting_fields:
        raise ComparisonError(f"harbor contains generated field(s): {conflicting_fields}")
    return ComparisonSettings(
        languages=languages,
        parallelism=parallelism,
        agent=copy.deepcopy(agent),
        task_names=list(task_names),
        harbor=copy.deepcopy(harbor),
    )


def build_comparison_job_config(
    settings: ComparisonSettings,
    *,
    task_dirs: dict[str, Path],
    jobs_dir: Path,
    job_name: str,
) -> dict[str, Any]:
    """Create one Harbor job containing every language-specific dataset."""
    config = copy.deepcopy(settings.harbor)
    config["job_name"] = job_name
    config["jobs_dir"] = str(jobs_dir.resolve())
    config["n_concurrent_trials"] = settings.parallelism
    config["agents"] = [copy.deepcopy(settings.agent)]
    config["datasets"] = [
        {
            "path": str(task_dirs[_language_key(language)].resolve()),
            "task_names": list(settings.task_names),
        }
        for language in settings.languages
    ]
    return config


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _config_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_agent(runner: Path, config_path: Path, *, cwd: Path) -> int:
    env = os.environ.copy()
    env["HARBOR_CONFIG"] = str(config_path.resolve())
    env["HARBOR_SKIP_EXPORT"] = "1"
    return subprocess.run([str(runner.resolve())], cwd=cwd, env=env, check=False).returncode


def run_comparison_experiment(
    config_path: Path,
    output_dir: Path,
    runner: Path,
    *,
    exporter: Callable[..., list[Path]] = convert_all,
    agent_runner: Callable[[Path, Path], int] | None = None,
) -> int:
    """Export both languages and run all trials in one parallel Harbor job."""
    config_path = config_path.resolve()
    runner = runner.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ComparisonError(f"Experiment directory already exists: {output_dir}")
    if not runner.is_file():
        raise ComparisonError(f"Agent runner does not exist: {runner}")

    settings = validate_comparison_config(_read_yaml(config_path))
    output_dir.mkdir(parents=True)
    jobs_dir = output_dir / "jobs"
    repo_root = runner.parent.parent
    manifest: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "running",
        "base_config": str(config_path),
        "base_config_sha256": _config_digest(config_path),
        "runner": str(runner),
        "agent": {"name": settings.agent.get("name"), "model_name": settings.agent.get("model_name")},
        "language_order": list(settings.languages),
        "parallelism": settings.parallelism,
        "task_names": settings.task_names,
        "languages": {},
    }
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)

    exit_code = 0
    interrupted = False
    task_dirs: dict[str, Path] = {}
    job_name = "comparison"
    derived_config_path = output_dir / "configs" / f"{job_name}.yaml"
    job_dir = jobs_dir / job_name
    manifest["job"] = {
        "status": "pending",
        "config": str(derived_config_path),
        "job_dir": str(job_dir),
        "exit_code": None,
    }
    for language in settings.languages:
        key = _language_key(language)
        task_dir = output_dir / "tasks" / key
        language_state = {
            "language": language,
            "status": "exporting",
            "source": key,
            "task_dir": str(task_dir),
            "config": str(derived_config_path),
            "job_dir": str(job_dir),
            "exit_code": None,
        }
        manifest["languages"][key] = language_state
        manifest["updated_at"] = _utc_now()
        _write_json(manifest_path, manifest)

        try:
            exported = exporter(task_dir, instance_ids=settings.task_names, target_language=language)
            exported_names = sorted(path.name for path in exported)
            if exported_names != sorted(settings.task_names):
                raise ComparisonError(
                    f"{language} export produced {exported_names}, expected {sorted(settings.task_names)}"
                )
            task_dirs[key] = task_dir
            language_state["status"] = "exported"
        except KeyboardInterrupt:
            language_state["status"] = "interrupted"
            language_state["exit_code"] = 130
            exit_code = 130
            interrupted = True
        except Exception as exc:
            language_state["status"] = "failed"
            language_state["error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 1
        finally:
            manifest["updated_at"] = _utc_now()
            _write_json(manifest_path, manifest)
        if interrupted:
            break

    if not interrupted and len(task_dirs) == len(settings.languages):
        comparison_config = build_comparison_job_config(
            settings,
            task_dirs=task_dirs,
            jobs_dir=jobs_dir,
            job_name=job_name,
        )
        _write_yaml(derived_config_path, comparison_config)
        manifest["job"]["status"] = "running"
        for state in manifest["languages"].values():
            state["status"] = "running"
        manifest["updated_at"] = _utc_now()
        _write_json(manifest_path, manifest)

        try:
            if agent_runner is None:
                job_exit_code = _run_agent(runner, derived_config_path, cwd=repo_root)
            else:
                job_exit_code = agent_runner(runner, derived_config_path)
            exit_code = 0 if job_exit_code == 0 else 1
            job_status = "completed" if job_exit_code == 0 else "failed"
            manifest["job"]["exit_code"] = job_exit_code
            manifest["job"]["status"] = job_status
            for state in manifest["languages"].values():
                state["exit_code"] = job_exit_code
                state["status"] = job_status
        except KeyboardInterrupt:
            interrupted = True
            exit_code = 130
            manifest["job"]["exit_code"] = 130
            manifest["job"]["status"] = "interrupted"
            for state in manifest["languages"].values():
                state["exit_code"] = 130
                state["status"] = "interrupted"
        except Exception as exc:
            exit_code = 1
            error = f"{type(exc).__name__}: {exc}"
            manifest["job"]["status"] = "failed"
            manifest["job"]["error"] = error
            for state in manifest["languages"].values():
                state["status"] = "failed"
                state["error"] = error
        finally:
            manifest["updated_at"] = _utc_now()
            _write_json(manifest_path, manifest)
    elif not interrupted:
        exit_code = 1
        manifest["job"]["status"] = "skipped"

    manifest["status"] = "completed" if exit_code == 0 else ("interrupted" if interrupted else "failed")
    manifest["updated_at"] = _utc_now()
    _write_json(manifest_path, manifest)
    write_comparison_reports(output_dir)
    return exit_code


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _sum_contexts(result: dict[str, Any]) -> dict[str, int | float | None]:
    agent_result = result.get("agent_result")
    if isinstance(agent_result, dict):
        contexts = [agent_result]
    else:
        contexts = []
        for step in result.get("step_results") or []:
            if isinstance(step, dict) and isinstance(step.get("agent_result"), dict):
                contexts.append(step["agent_result"])

    fields = ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd")
    totals: dict[str, int | float | None] = {}
    for field in fields:
        values = [_numeric(context.get(field)) for context in contexts]
        present = [value for value in values if value is not None]
        totals[field] = sum(present) if present else None
    return totals


def _primary_reward(result: dict[str, Any]) -> int | float | None:
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    if not isinstance(rewards, dict):
        return None
    reward = _numeric(rewards.get("reward"))
    if reward is not None:
        return reward
    values = [_numeric(value) for value in rewards.values()]
    present = [value for value in values if value is not None]
    return present[0] if len(present) == 1 else None


def _trajectory_steps(trial_dir: Path) -> int | None:
    trajectory = _read_json(trial_dir / "agent" / "trajectory.json")
    if trajectory is None:
        return None
    final_metrics = trajectory.get("final_metrics")
    if isinstance(final_metrics, dict):
        total_steps = _numeric(final_metrics.get("total_steps"))
        if isinstance(total_steps, int) and total_steps >= 0:
            return total_steps
    steps = trajectory.get("steps")
    return len(steps) if isinstance(steps, list) else None


def _load_trials(job_dir: Path, language: str, *, source: str | None = None) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    if not job_dir.is_dir():
        return trials
    for result_path in sorted(job_dir.glob("*/result.json")):
        result = _read_json(result_path)
        if result is None or not isinstance(result.get("task_name"), str):
            continue
        if source is not None and result.get("source") != source:
            continue
        context = _sum_contexts(result)
        n_input = context["n_input_tokens"]
        n_output = context["n_output_tokens"]
        total_tokens = n_input + n_output if n_input is not None and n_output is not None else None
        exception = result.get("exception_info")
        exception_type = exception.get("exception_type") if isinstance(exception, dict) else None
        trials.append(
            {
                "language": language,
                "task_name": result["task_name"],
                "trial_name": result.get("trial_name", result_path.parent.name),
                "status": "failed" if exception_type else "completed",
                "exception_type": exception_type,
                "reward": _primary_reward(result),
                "cost_usd": context["cost_usd"],
                "steps": _trajectory_steps(result_path.parent),
                "n_input_tokens": n_input,
                "n_cache_tokens": context["n_cache_tokens"],
                "n_output_tokens": n_output,
                "n_total_tokens": total_tokens,
            }
        )
    return trials


def _sum_present(records: list[dict[str, Any]], field: str) -> int | float | None:
    values = [_numeric(record.get(field)) for record in records]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    rewards = [_numeric(record.get("reward")) for record in records]
    present_rewards = [reward for reward in rewards if reward is not None]
    failures = [record["exception_type"] for record in records if record.get("exception_type")]
    return {
        "attempts": len(records),
        "completed": sum(record.get("status") == "completed" for record in records),
        "scored": len(present_rewards),
        "failures": len(failures),
        "failure_types": sorted(set(failures)),
        "reward_mean": sum(present_rewards) / len(present_rewards) if present_rewards else None,
        "reward_sum": sum(present_rewards) if present_rewards else None,
        "cost_usd": _sum_present(records, "cost_usd"),
        "steps": _sum_present(records, "steps"),
        "n_input_tokens": _sum_present(records, "n_input_tokens"),
        "n_cache_tokens": _sum_present(records, "n_cache_tokens"),
        "n_output_tokens": _sum_present(records, "n_output_tokens"),
        "n_total_tokens": _sum_present(records, "n_total_tokens"),
    }


def _delta(second: dict[str, Any], first: dict[str, Any]) -> dict[str, int | float | None]:
    fields = (
        "attempts",
        "completed",
        "scored",
        "failures",
        "reward_mean",
        "reward_sum",
        "cost_usd",
        "steps",
        "n_input_tokens",
        "n_cache_tokens",
        "n_output_tokens",
        "n_total_tokens",
    )
    result: dict[str, int | float | None] = {}
    for field in fields:
        left = _numeric(second.get(field))
        right = _numeric(first.get(field))
        result[field] = left - right if left is not None and right is not None else None
    return result


def build_comparison_report(experiment_dir: Path) -> dict[str, Any]:
    experiment_dir = experiment_dir.resolve()
    manifest = _read_json(experiment_dir / "manifest.json")
    if manifest is None:
        raise ComparisonError(f"Missing or invalid experiment manifest: {experiment_dir / 'manifest.json'}")
    task_names = manifest.get("task_names")
    if not isinstance(task_names, list) or not all(isinstance(name, str) for name in task_names):
        raise ComparisonError("Experiment manifest has no valid task_names list")
    languages = _validate_languages(manifest.get("language_order"))
    language_specs = [{"name": language, "key": _language_key(language)} for language in languages]
    first_key = language_specs[0]["key"]
    second_key = language_specs[1]["key"]

    trials_by_language: dict[str, list[dict[str, Any]]] = {}
    for language_spec in language_specs:
        language = language_spec["name"]
        key = language_spec["key"]
        state = (manifest.get("languages") or {}).get(key, {})
        job_dir_value = state.get("job_dir") if isinstance(state, dict) else None
        job_dir = Path(job_dir_value) if isinstance(job_dir_value, str) else experiment_dir / "jobs" / key
        source = state.get("source") if isinstance(state, dict) else None
        trials_by_language[key] = _load_trials(
            job_dir,
            language,
            source=source if isinstance(source, str) else None,
        )

    tasks = []
    for task_name in task_names:
        results = {
            key: _aggregate([record for record in records if record["task_name"] == task_name])
            for key, records in trials_by_language.items()
        }
        tasks.append(
            {
                "task_name": task_name,
                "results": results,
                "delta": _delta(results[second_key], results[first_key]),
            }
        )

    total_results = {key: _aggregate(records) for key, records in trials_by_language.items()}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "experiment_dir": str(experiment_dir),
        "experiment_status": manifest.get("status"),
        "base_config": manifest.get("base_config"),
        "agent": manifest.get("agent"),
        "parallelism": manifest.get("parallelism"),
        "languages": language_specs,
        "delta": {"from": first_key, "to": second_key},
        "task_count": len(task_names),
        "tasks": tasks,
        "totals": {
            "results": total_results,
            "delta": _delta(total_results[second_key], total_results[first_key]),
        },
        "trials": trials_by_language,
        "language_runs": manifest.get("languages", {}),
    }


def _format_value(value: Any, kind: str, *, delta: bool = False) -> str:
    value = _numeric(value)
    if value is None:
        return "N/A"
    prefix = "+" if delta and value > 0 else ""
    if kind == "reward":
        return f"{prefix}{value:.4f}"
    if kind == "cost":
        return f"{prefix}${value:.6f}" if not (delta and value < 0) else f"-${abs(value):.6f}"
    if isinstance(value, float) and not value.is_integer():
        return f"{prefix}{value:,.2f}"
    return f"{prefix}{int(value):,}"


def _status_text(aggregate: dict[str, Any]) -> str:
    if aggregate["attempts"] == 0:
        return "missing"
    if aggregate["failures"]:
        return f"{aggregate['completed']}/{aggregate['attempts']} completed"
    if aggregate["scored"] != aggregate["attempts"]:
        return f"{aggregate['scored']}/{aggregate['attempts']} scored"
    return "complete"


def _markdown_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown_report(report: dict[str, Any]) -> str:
    first, second = report["languages"]
    first_name = _markdown_text(first["name"])
    second_name = _markdown_text(second["name"])
    first_key = first["key"]
    second_key = second["key"]
    lines = [
        f"# {first_name}/{second_name} ProgramBench Comparison",
        "",
        f"- Experiment status: `{report.get('experiment_status')}`",
        f"- Tasks: {report['task_count']}",
        f"- Base config: `{report.get('base_config')}`",
        f"- Agent: `{(report.get('agent') or {}).get('name')}`",
        f"- Model: `{(report.get('agent') or {}).get('model_name')}`",
        f"- Trial parallelism: {report.get('parallelism')}",
        f"- Delta convention: {second_name} minus {first_name}",
        "- Total tokens: input plus output; cached tokens are already part of input tokens",
        "",
        "## Language runs",
        "",
        "| Language | Status | Exit code | Error |",
        "|---|---|---:|---|",
    ]
    language_runs = report.get("language_runs") or {}
    for language_spec in report["languages"]:
        language = language_spec["name"]
        key = language_spec["key"]
        state = language_runs.get(key, {}) if isinstance(language_runs, dict) else {}
        status = state.get("status", "missing") if isinstance(state, dict) else "missing"
        exit_code = state.get("exit_code") if isinstance(state, dict) else None
        error = state.get("error", "") if isinstance(state, dict) else ""
        lines.append(
            f"| {_markdown_text(language)} | {status} | {_format_value(exit_code, 'count')} | "
            f"{_markdown_text(error)} |"
        )
    lines.extend(
        [
            "",
            "## Per-task comparison",
            "",
            f"| Task | {first_name} reward | {second_name} reward | Delta | {first_name} cost | "
            f"{second_name} cost | Delta | {first_name} steps | {second_name} steps | Delta | "
            f"{first_name} tokens | {second_name} tokens | Delta | Status |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for task in report["tasks"]:
        first_result = task["results"][first_key]
        second_result = task["results"][second_key]
        delta = task["delta"]
        status = f"{first_name}: {_status_text(first_result)}; {second_name}: {_status_text(second_result)}"
        lines.append(
            "| {task} | {jr} | {kr} | {dr} | {jc} | {kc} | {dc} | {js} | {ks} | {ds} | "
            "{jt} | {kt} | {dt} | {status} |".format(
                task=_markdown_text(task["task_name"]),
                jr=_format_value(first_result["reward_mean"], "reward"),
                kr=_format_value(second_result["reward_mean"], "reward"),
                dr=_format_value(delta["reward_mean"], "reward", delta=True),
                jc=_format_value(first_result["cost_usd"], "cost"),
                kc=_format_value(second_result["cost_usd"], "cost"),
                dc=_format_value(delta["cost_usd"], "cost", delta=True),
                js=_format_value(first_result["steps"], "count"),
                ks=_format_value(second_result["steps"], "count"),
                ds=_format_value(delta["steps"], "count", delta=True),
                jt=_format_value(first_result["n_total_tokens"], "count"),
                kt=_format_value(second_result["n_total_tokens"], "count"),
                dt=_format_value(delta["n_total_tokens"], "count", delta=True),
                status=status,
            )
        )

    totals = report["totals"]
    lines.extend(
        [
            "",
            "## Totals",
            "",
            f"| Metric | {first_name} | {second_name} | Delta |",
            "|---|---:|---:|---:|",
        ]
    )
    total_metrics = (
        ("Attempts", "attempts", "count"),
        ("Completed", "completed", "count"),
        ("Scored", "scored", "count"),
        ("Failures", "failures", "count"),
        ("Mean reward", "reward_mean", "reward"),
        ("Reward sum", "reward_sum", "reward"),
        ("Cost", "cost_usd", "cost"),
        ("Steps", "steps", "count"),
        ("Input tokens", "n_input_tokens", "count"),
        ("Cached tokens", "n_cache_tokens", "count"),
        ("Output tokens", "n_output_tokens", "count"),
        ("Total tokens", "n_total_tokens", "count"),
    )
    for label, field, kind in total_metrics:
        lines.append(
            f"| {label} | {_format_value(totals['results'][first_key][field], kind)} | "
            f"{_format_value(totals['results'][second_key][field], kind)} | "
            f"{_format_value(totals['delta'][field], kind, delta=True)} |"
        )

    lines.extend(
        [
            "",
            "## Token breakdown by task",
            "",
            f"| Task | {first_name} input | {first_name} cached | {first_name} output | "
            f"{second_name} input | {second_name} cached | {second_name} output |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for task in report["tasks"]:
        first_result = task["results"][first_key]
        second_result = task["results"][second_key]
        lines.append(
            f"| {_markdown_text(task['task_name'])} | {_format_value(first_result['n_input_tokens'], 'count')} | "
            f"{_format_value(first_result['n_cache_tokens'], 'count')} | "
            f"{_format_value(first_result['n_output_tokens'], 'count')} | "
            f"{_format_value(second_result['n_input_tokens'], 'count')} | "
            f"{_format_value(second_result['n_cache_tokens'], 'count')} | "
            f"{_format_value(second_result['n_output_tokens'], 'count')} |"
        )

    failures = [record for records in report["trials"].values() for record in records if record["exception_type"]]
    if failures:
        lines.extend(["", "## Failures", ""])
        for record in failures:
            lines.append(
                f"- `{record['language']}` / `{record['task_name']}` / `{record['trial_name']}`: "
                f"`{record['exception_type']}`"
            )
    return "\n".join(lines) + "\n"


def write_comparison_reports(experiment_dir: Path) -> tuple[Path, Path]:
    report = build_comparison_report(experiment_dir)
    json_path = experiment_dir / "report.json"
    markdown_path = experiment_dir / "report.md"
    _write_json(json_path, report)
    markdown_path.write_text(render_markdown_report(report))
    return markdown_path, json_path
