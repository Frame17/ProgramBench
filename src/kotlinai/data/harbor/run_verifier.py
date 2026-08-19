#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Score a ProgramBench Harbor task and write the reward.

Runs inside the black-box container (stdlib only — programbench is not
installed there). Reproduces ProgramBench's per-instance score: for every active
branch, the expected test set is ``tests[]`` minus that branch's
``ignored_tests``; a test passes only if the JUnit XML marks it passed, and an
expected test absent from the XML counts as unresolved. Reward is the fraction
of non-ignored tests that pass across all active branches.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def parse_statuses(results_xml: str) -> dict[str, str]:
    """Map ``classname.name`` -> status for every testcase in JUnit XML.

    A testcase with no failure/error/skipped child passed; otherwise it takes
    that child's kind. Mirrors ``parse_test_results`` in eval.py.
    """
    statuses: dict[str, str] = {}
    root = ET.fromstring(results_xml)
    for case in root.iter("testcase"):
        classname, name = case.get("classname"), case.get("name")
        if not name:
            continue
        full = f"{classname}.{name}" if classname else name
        kinds = {child.tag for child in case}
        if "failure" in kinds:
            statuses[full] = "failure"
        elif "error" in kinds:
            statuses[full] = "error"
        elif "skipped" in kinds:
            statuses[full] = "skipped"
        else:
            statuses[full] = "passed"
    return statuses


def compute_reward(tests_json: dict, results_dir: Path) -> dict:
    """Return ``{reward, resolved, total, branches}`` for the instance."""
    resolved = total = 0
    per_branch: dict[str, dict] = {}
    for branch, info in tests_json.get("branches", {}).items():
        if info.get("ignored"):
            continue
        ignored = {t["name"] for t in info.get("ignored_tests") or []}
        declared = set(info.get("tests", []))
        expected = [t for t in info.get("tests", []) if t not in ignored]
        xml = results_dir / branch / "results.xml"
        statuses = parse_statuses(xml.read_text()) if xml.exists() else {}
        # Parity with ProgramBench (eval.py:_process_branch_xml): JUnit cases not
        # declared in tests.json (and not ignored) stay in the EvaluationResult,
        # so they count toward both resolved and total. One expected-pass plus
        # one unexpected-fail is 1/2, not 1/1.
        unexpected = sorted(n for n in statuses if n not in declared and n not in ignored)
        scored = expected + unexpected
        n_pass = sum(statuses.get(t) == "passed" for t in scored)
        resolved += n_pass
        total += len(scored)
        failed = [t for t in scored if statuses.get(t) != "passed"]
        per_branch[branch] = {"resolved": n_pass, "total": len(scored), "failed": failed}
    return {
        "reward": resolved / total if total else 0.0,
        "resolved": resolved,
        "total": total,
        "branches": per_branch,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests-json", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--reward-file", type=Path, required=True)
    args = parser.parse_args()

    report = compute_reward(json.loads(args.tests_json.read_text()), args.results_dir)
    args.reward_file.parent.mkdir(parents=True, exist_ok=True)
    # Harbor's VerifierResult only accepts numeric reward values, so reward.json
    # carries scalars only; the per-branch breakdown goes to report.json.
    args.reward_file.write_text(json.dumps({"reward": report["reward"]}))
    (args.reward_file.parent / "reward.txt").write_text(str(report["reward"]))
    (args.reward_file.parent / "report.json").write_text(json.dumps(report, indent=2))
    print(f"reward={report['reward']:.4f} ({report['resolved']}/{report['total']} tests)")


if __name__ == "__main__":
    main()
