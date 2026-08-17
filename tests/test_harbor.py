# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for the Harbor task exporter and its verifier scoring."""

import importlib.util

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
        "tests/test.sh",
        "tests/run_verifier.py",
        "tests/tests.json",
        "tests/branches/33128f6b8600/eval/run.sh",
        "tests/branches/33128f6b8600/eval/tests/test_calculator.py",
        "solution/solve.sh",
        "solution/compile.sh",
    ]:
        assert (out / rel).is_file(), rel
    dockerfile = (out / "environment" / "Dockerfile").read_text()
    assert "programbench/testorg_1776_calculator.abc1234:task_cleanroom_v6" in dockerfile
    assert "testorg/calculator" in (out / "task.toml").read_text()

    # The gold solution clones the real reference repo at its commit and reuses
    # the upstream build recipe as compile.sh.
    solve = (out / "solution" / "solve.sh").read_text()
    assert "https://github.com/testorg/calculator" in solve
    assert "abc1234" in solve
    assert (out / "solution" / "compile.sh").read_text() == (
        out / "tests" / "branches" / "33128f6b8600" / "build.sh"
    ).read_text()


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
