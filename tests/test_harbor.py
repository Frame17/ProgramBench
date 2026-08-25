# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for the Harbor task exporter and its verifier scoring."""

import importlib.util
import tomllib

import pytest

import kotlinai.harbor as harbor
from kotlinai.harbor import HARBOR_DATA, convert_instance
from programbench.eval.eval import EvaluationResult, _process_branch_xml
from programbench.utils.load_data import load_all_instances

_spec = importlib.util.spec_from_file_location("run_verifier", HARBOR_DATA / "run_verifier.py")
run_verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_verifier)


def _calc_instance() -> dict:
    return next(i for i in load_all_instances() if i["instance_id"] == "testorg__calculator.abc1234")


def _xml(*cases: tuple[str, str]) -> str:
    rows = "".join(
        f'<testcase classname="{cn}" name="{n}">{body}</testcase>'
        for cn, n, body in ((c.rsplit(".", 1)[0], c.rsplit(".", 1)[1], b) for c, b in cases)
    )
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{rows}</testsuite></testsuites>'


def test_convert_instance_emits_full_harbor_layout(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path)
    for rel in [
        "task.toml",
        "instruction.md",
        "environment/Dockerfile",
        "tests/Dockerfile",
        "tests/test.sh",
        "tests/run_verifier.py",
        "tests/tests.json",
        "tests/branches/33128f6b8600/eval/run.sh",
        "tests/branches/33128f6b8600/eval/tests/test_calculator.py",
        "solution/solve.sh",
        "solution/compile.sh",
    ]:
        assert (out / rel).is_file(), rel
    assert "programbench/testorg_1776_calculator.abc1234:task_cleanroom_v6" in (
        out / "environment" / "Dockerfile"
    ).read_text()
    environment_dockerfile = (out / "environment" / "Dockerfile").read_text()
    assert "archive.ubuntu.com/ubuntu" in environment_dockerfile
    assert "apt-get install -y --no-install-recommends file ripgrep xxd" in environment_dockerfile

    # The separate verifier image is FROM the cleanroom, wipes /workspace, and
    # bakes the tests dir into /tests (skip_tests_upload=True in separate mode).
    verifier_dockerfile = (out / "tests" / "Dockerfile").read_text()
    assert "programbench/testorg_1776_calculator.abc1234:task_cleanroom_v6" in verifier_dockerfile
    assert "rm -rf /workspace" in verifier_dockerfile
    assert "COPY . /tests" in verifier_dockerfile

    # The oracle is the network-free reference-binary stash, not a clone/vendor.
    solve = (out / "solution" / "solve.sh").read_text()
    assert "git clone" not in solve
    assert ".programbench_oracle_reference" in solve
    assert (out / "solution" / "compile.sh").read_text() == (HARBOR_DATA / "oracle_compile.sh").read_text()


def test_convert_instance_instructs_agent_to_use_target_language(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, target_language="Kotlin")
    instruction = (out / "instruction.md").read_text()

    assert "You MUST implement the deliverable in Kotlin." in instruction
    assert "Write all source code in Kotlin." in instruction
    assert "Do not reimplement the program in another language." in instruction


def test_convert_instance_adds_kotlin_toolchain_to_agent_and_verifier(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, target_language=" Kotlin ")

    for dockerfile in [out / "environment" / "Dockerfile", out / "tests" / "Dockerfile"]:
        contents = dockerfile.read_text()
        assert "openjdk-21-jdk-headless" in contents
        assert f"kotlin-compiler-{harbor.KOTLIN_VERSION}.zip" in contents
        assert harbor.KOTLIN_COMPILER_SHA256 in contents
        assert "ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64" in contents
        assert "ENV KOTLIN_HOME=/opt/kotlinc" in contents
        assert 'ENV PATH="${KOTLIN_HOME}/bin:${PATH}"' in contents
        assert "apt-get update" in contents


def test_convert_instance_adds_only_jdk_for_java_target(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, target_language=" Java ")

    for dockerfile in [out / "environment" / "Dockerfile", out / "tests" / "Dockerfile"]:
        contents = dockerfile.read_text()
        assert "openjdk-21-jdk-headless" in contents
        assert "ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64" in contents
        assert "kotlin-compiler" not in contents
        assert "KOTLIN_HOME" not in contents


def test_convert_instance_does_not_add_jvm_toolchain_for_other_targets(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, target_language="Rust")

    for dockerfile in [out / "environment" / "Dockerfile", out / "tests" / "Dockerfile"]:
        contents = dockerfile.read_text()
        assert "openjdk-21-jdk-headless" not in contents
        assert "kotlin-compiler" not in contents


def test_default_agent_allowlist_does_not_expose_toolchain_or_source_hosts(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, target_language="Kotlin")
    allowed_hosts = tomllib.loads((out / "task.toml").read_text())["agent"]["allowed_hosts"]

    assert "archive.ubuntu.com" not in allowed_hosts
    assert "repo.maven.apache.org" not in allowed_hosts
    assert "services.gradle.org" not in allowed_hosts
    # Agent setup installs the CLI before the agent-phase policy applies, so
    # these are not needed here and would let the agent fetch upstream source.
    assert "raw.githubusercontent.com" not in allowed_hosts
    assert "nodejs.org" not in allowed_hosts
    assert "registry.npmjs.org" not in allowed_hosts


def test_convert_instance_task_toml_enforces_programbench_policy(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, allowed_hosts=["api.example.com"])
    cfg = tomllib.loads((out / "task.toml").read_text())

    # Inference is offline; only the model API host is reachable in the agent phase.
    assert cfg["environment"]["network_mode"] == "public"
    assert cfg["agent"]["network_mode"] == "allowlist"
    assert cfg["agent"]["allowed_hosts"] == ["api.example.com"]

    # The agent runs non-root so it cannot read the execute-only reference binary.
    assert cfg["agent"]["user"] == harbor.AGENT_USER != "root"

    # parallelism x cpus must stay inside the host's core count.
    assert harbor.DOCKER_CPUS == 8

    # The verifier runs in its own container; it keeps network for test
    # execution (test.sh blocks only around compile.sh), matching ProgramBench.
    assert cfg["verifier"]["environment_mode"] == "separate"
    assert cfg["verifier"]["environment"]["network_mode"] == "public"
    assert cfg["verifier"]["env"]["PYTEST_ADDOPTS"] == "--max-worker-restart=4"
    assert cfg["environment"]["cpus"] == cfg["verifier"]["environment"]["cpus"] == harbor.DOCKER_CPUS

    # The whole workspace is handed off with no tar exclusions: exclusions push
    # Harbor onto a transfer path that rejects absolute symlinks, and test.sh
    # already drops ./executable before compiling.
    assert cfg["artifacts"] == ["/workspace"]
    assert "rm -f ./executable" in (harbor.HARBOR_DATA / "test.sh").read_text()


def test_agent_image_ships_inspection_tools_and_drops_sudo(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, target_language="Kotlin")
    dockerfile = (out / "environment" / "Dockerfile").read_text()

    # Every trial of the prior run reached for `file`; several reached for `xxd`.
    assert " file " in dockerfile
    assert " xxd " in dockerfile
    # Without this the non-root agent can regain root and read the reference.
    assert "rm -f /etc/sudoers.d/agent-packages" in dockerfile


def test_convert_instance_exports_root_agent_for_oracle_verification(tmp_path):
    out = convert_instance(_calc_instance(), tmp_path, agent_user="root")

    assert tomllib.loads((out / "task.toml").read_text())["agent"]["user"] == "root"


def test_convert_instance_overwrites_existing(tmp_path):
    convert_instance(_calc_instance(), tmp_path)
    stale = tmp_path / "testorg__calculator.abc1234" / "tests" / "stale.txt"
    stale.write_text("x")
    convert_instance(_calc_instance(), tmp_path)
    assert not stale.exists()


def test_compute_reward_matches_evaluation_result_score(tmp_path):
    tests_json = {
        "branches": {
            "b1": {
                "ignored": False,
                "tests": ["m.t_pass", "m.t_fail", "m.t_missing", "m.t_ign"],
                "ignored_tests": [{"name": "m.t_ign"}],
            },
            "b2": {"ignored": True, "tests": ["m.x"], "ignored_tests": []},
        }
    }
    xml = _xml(("m.t_pass", ""), ("m.t_fail", "<failure/>"), ("m.t_ign", ""))
    (tmp_path / "b1").mkdir()
    (tmp_path / "b1" / "results.xml").write_text(xml)

    report = run_verifier.compute_reward(tests_json, tmp_path)

    # Faithful cross-check against the ProgramBench scorer for the one active branch.
    results, _ = _process_branch_xml(xml, "b1", {"b1": tests_json["branches"]["b1"]["tests"]}, ignored_tests={"b1/m.t_ign"})
    expected_score = EvaluationResult(test_results=results).without_ignored({"b1/m.t_ign"}).score

    assert report["reward"] == expected_score == 1 / 3
    assert (report["resolved"], report["total"]) == (1, 3)
    assert "b2" not in report["branches"]


def test_compute_reward_all_pass_and_compile_failure(tmp_path):
    tests_json = {"branches": {"b1": {"ignored": False, "tests": ["m.a", "m.b"], "ignored_tests": []}}}
    (tmp_path / "b1").mkdir()
    (tmp_path / "b1" / "results.xml").write_text(_xml(("m.a", ""), ("m.b", "")))
    assert run_verifier.compute_reward(tests_json, tmp_path)["reward"] == 1.0

    # No results dir at all (compile failed): every expected test is unresolved.
    assert run_verifier.compute_reward(tests_json, tmp_path / "empty")["reward"] == 0.0


def test_compute_reward_counts_unexpected_junit_cases(tmp_path):
    # A JUnit case not listed in tests.json still counts, matching ProgramBench:
    # one expected-pass + one unexpected-fail is 1/2, not 1/1.
    tests_json = {"branches": {"b1": {"ignored": False, "tests": ["m.expected"], "ignored_tests": []}}}
    xml = _xml(("m.expected", ""), ("m.surprise", "<failure/>"))
    (tmp_path / "b1").mkdir()
    (tmp_path / "b1" / "results.xml").write_text(xml)

    report = run_verifier.compute_reward(tests_json, tmp_path)

    results, _ = _process_branch_xml(xml, "b1", {"b1": ["m.expected"]}, ignored_tests=set())
    expected_score = EvaluationResult(test_results=results).score

    assert report["reward"] == expected_score == 1 / 2
    assert (report["resolved"], report["total"]) == (1, 2)
    assert "m.surprise" in report["branches"]["b1"]["failed"]


def _fake_branch(task_dir, branch: str, build_sh: str) -> None:
    (task_dir / "tests" / branch / "eval" / "tests").mkdir(parents=True)
    (task_dir / "tests" / branch / "build.sh").write_text(build_sh)
    (task_dir / "tests" / branch / "eval" / "run.sh").write_text("#!/bin/bash\n")
    (task_dir / "tests" / branch / "eval" / "tests" / "test_x.py").write_text("")


def test_divergent_build_scripts_raise(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    iid = "org__tool.deadbeef"
    _fake_branch(tasks / iid, "aaaaaaaaaaaa", "build recipe A\n")
    _fake_branch(tasks / iid, "bbbbbbbbbbbb", "build recipe B\n")
    monkeypatch.setattr(harbor, "TASKS_DIR", tasks)
    instance = {
        "instance_id": iid,
        "repository": "org/tool",
        "commit": "deadbeef",
        "language": "rust",
        "branches": {
            "aaaaaaaaaaaa": {"ignored": False, "tests": [], "ignored_tests": []},
            "bbbbbbbbbbbb": {"ignored": False, "tests": [], "ignored_tests": []},
        },
    }
    with pytest.raises(ValueError, match="disagree on build.sh"):
        convert_instance(instance, tmp_path / "out")


def test_missing_branch_build_sh_backfilled_from_task_level(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    iid = "org__tool.deadbeef"
    # One branch ships a build.sh; the other lacks one (like mdbook's c8cbe415e02d).
    _fake_branch(tasks / iid, "aaaaaaaaaaaa", "shared recipe\n")
    no_build = tasks / iid / "tests" / "bbbbbbbbbbbb" / "eval" / "tests"
    no_build.mkdir(parents=True)
    (tasks / iid / "tests" / "bbbbbbbbbbbb" / "eval" / "run.sh").write_text("#!/bin/bash\n")
    (no_build / "test_x.py").write_text("")
    # Task-level fallback recipe, byte-identical to the branch that ships one.
    (tasks / iid / "build.sh").write_text("shared recipe\n")
    monkeypatch.setattr(harbor, "TASKS_DIR", tasks)
    instance = {
        "instance_id": iid,
        "repository": "org/tool",
        "commit": "deadbeef",
        "language": "rust",
        "branches": {
            "aaaaaaaaaaaa": {"ignored": False, "tests": [], "ignored_tests": []},
            "bbbbbbbbbbbb": {"ignored": False, "tests": [], "ignored_tests": []},
        },
    }

    out = convert_instance(instance, tmp_path / "out")

    # The branch without a build.sh is backfilled from the task-level recipe, so
    # all active branches agree and the divergence check passes.
    backfilled = out / "tests" / "branches" / "bbbbbbbbbbbb" / "build.sh"
    assert backfilled.read_text() == "shared recipe\n"


def test_convert_all_skips_failing_instance(tmp_path, monkeypatch):
    good = {"instance_id": "org__good.1111111", "branches": {"aaaaaaaaaaaa": {"ignored": False}}}
    bad = {"instance_id": "org__bad.2222222", "branches": {"bbbbbbbbbbbb": {"ignored": False}}}
    monkeypatch.setattr(harbor, "load_all_instances", lambda: [good, bad])

    def fake_convert(instance, out_root, **kwargs):
        if instance["instance_id"] == bad["instance_id"]:
            raise ValueError("boom")
        path = out_root / instance["instance_id"]
        path.mkdir(parents=True)
        return path

    monkeypatch.setattr(harbor, "convert_instance", fake_convert)

    errors: list[tuple[str, Exception]] = []
    paths = harbor.convert_all(tmp_path / "out", on_error=lambda iid, exc: errors.append((iid, exc)))

    # The good instance is still exported; the bad one is skipped and reported.
    assert [p.name for p in paths] == ["org__good.1111111"]
    assert [iid for iid, _ in errors] == ["org__bad.2222222"]


def test_convert_all_reraises_without_error_handler(tmp_path, monkeypatch):
    bad = {"instance_id": "org__bad.2222222", "branches": {"bbbbbbbbbbbb": {"ignored": False}}}
    monkeypatch.setattr(harbor, "load_all_instances", lambda: [bad])

    def fake_convert(instance, out_root, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(harbor, "convert_instance", fake_convert)

    # Without on_error the batch stays fail-fast (experiment.py relies on this).
    with pytest.raises(ValueError, match="boom"):
        harbor.convert_all(tmp_path / "out")
