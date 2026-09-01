# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Convert ProgramBench instances into Harbor task directories.

Harbor (harborframework.com) runs each task in a container: the agent works in
``/workspace``, then ``tests/test.sh`` runs and writes a reward under
``/logs/verifier/``. One ProgramBench instance maps to one Harbor task — the
environment is the black-box ``task_cleanroom_v6`` image, and the verifier
re-builds the agent's ``compile.sh`` (offline) and runs each active branch's
behavioral suite, reporting the fractional pass rate.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

from jinja2 import Environment, PackageLoader

from programbench.constants import (
    DOCKER_CP_TIMEOUT,
    DOCKER_EXECUTABLE,
    DOCKER_RUN_TIMEOUT,
    TASKS_DIR,
    image_name_from_instance_id,
)
from programbench.utils.load_data import get_active_branches, load_all_instances

CLEANROOM_TAG = "task_cleanroom_v6"
PACKAGE_ROOT = Path(__file__).resolve().parent
HARBOR_DATA = PACKAGE_ROOT / "data" / "harbor"
ORACLE_PAYLOAD_NAME = "reference.gz"

# ProgramBench execution policy, mirrored onto Harbor's config. DOCKER_CPUS sits
# below ProgramBench's 10 on purpose: the comparison harness runs 8 trials at
# once, and 8 x 10 oversubscribes a 64-core host.
DOCKER_CPUS = 8
COMPILE_TIMEOUT_SEC = 900.0
BRANCH_TIMEOUT_SEC = 3600.0
BUILD_TIMEOUT_SEC = 1800.0
# 3 hours. These reverse-engineering tasks need more than the earlier 1-hour cap
# to finish; a per-run cost limit (see experiment.py) keeps the larger budget
# bounded. AgentTimeoutError stays in Harbor's exclude_exceptions (not retried).
AGENT_TIMEOUT_SEC = 10800.0
KOTLIN_VERSION = "2.4.10"
KOTLIN_COMPILER_SHA256 = "473dd66c7a3ef4b182065b3da670466c1bf2773a9dbb0ed8b33a39fe9d4f876d"
# The agent phase can reach model APIs and the package, build-tool, and
# toolchain documentation hosts needed to assemble an offline JVM submission.
# Source-hosting and agent-installer hosts remain absent: fetching upstream
# source would defeat the black-box task, while agent setup runs outside the
# agent-phase policy under the `[environment]` public baseline. Parameterized
# per runner (override with `harbor export --allowed-host`).
DEFAULT_AGENT_ALLOWED_HOSTS = [
    "api.openai.com",
    "api.anthropic.com",
    "repo.maven.apache.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "services.gradle.org",
    "downloads.gradle.org",
    "maven.google.com",
    "maven.pkg.jetbrains.space",
    "maven.reposilite.com",
    "oss.sonatype.org",
    "kotlinlang.org",
    "*.kotlinlang.org",
    "kotl.in",
    "*.jetbrains.com",
    "docs.gradle.org",
    "docs.gradle.com",
    "gradle.com",
    "gradle.org",
    "scans.gradle.com",
    "help.gradle.org",
    "developer.android.com",
    "schemas.android.com",
    "pub.dartlang.org",
    "pub.dev",
    "pnpm.js.org",
    "ant.apache.org",
]
# The cleanroom image's uid-1000 user. It owns /workspace but, unlike root,
# cannot read the execute-only reference binary at /workspace/executable.
AGENT_USER = "agent"

_env = Environment(loader=PackageLoader("kotlinai", "data/templates"), autoescape=False)


def _write_oracle_payload(image_ref: str, destination: Path) -> None:
    """Copy and compress the reference binary from a cleanroom image."""
    create = subprocess.run(
        [DOCKER_EXECUTABLE, "create", image_ref],
        capture_output=True,
        text=True,
        timeout=DOCKER_RUN_TIMEOUT,
    )
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create temporary container from {image_ref}: {create.stderr.strip()}")

    container_id = create.stdout.strip()
    if not container_id:
        raise RuntimeError(f"Docker returned no container ID for {image_ref}")

    try:
        with tempfile.TemporaryDirectory(prefix="programbench-oracle-") as temp_dir:
            reference = Path(temp_dir) / "executable"
            copy = subprocess.run(
                [DOCKER_EXECUTABLE, "cp", f"{container_id}:/workspace/executable", str(reference)],
                capture_output=True,
                text=True,
                timeout=DOCKER_CP_TIMEOUT,
            )
            if copy.returncode != 0:
                raise RuntimeError(
                    f"Failed to copy /workspace/executable from {image_ref}: {copy.stderr.strip()}"
                )
            if not reference.is_file() or reference.stat().st_size == 0:
                raise RuntimeError(f"Reference binary in {image_ref} is missing or empty")
            # docker cp preserves the source's execute-only mode. The temporary
            # host copy belongs to this process, so make it readable before
            # compression; the image and final task permissions are unchanged.
            reference.chmod(0o600)

            with reference.open("rb") as source, destination.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                    shutil.copyfileobj(source, compressed)
            destination.chmod(0o644)
    finally:
        try:
            subprocess.run(
                [DOCKER_EXECUTABLE, "rm", "-f", container_id],
                capture_output=True,
                text=True,
                timeout=DOCKER_RUN_TIMEOUT,
            )
        except Exception:
            pass


def _lay_down_branch(task_dir: Path, branch: str, dest: Path, blob_dir: Path | None) -> None:
    """Copy one branch's ``eval/`` test tree into ``dest``.

    Prefers an already-extracted tree in the task dir (the local fixture);
    otherwise extracts the HuggingFace blob ``tests/<branch>.tar.gz``. A
    task-level build script is used when the branch does not provide one.
    """
    on_disk = task_dir / "tests" / branch
    if on_disk.is_dir():
        shutil.copytree(on_disk, dest)
    else:
        if blob_dir is None:
            from programbench.utils.blob_store import get_blob_dir

            blob_dir = get_blob_dir(task_dir.name)
        tar = blob_dir / "tests" / f"{branch}.tar.gz" if blob_dir else None
        if tar is None or not tar.exists():
            raise FileNotFoundError(f"No test tree for {task_dir.name}/{branch} (looked in {on_disk} and blob {tar})")
        dest.mkdir(parents=True)
        with tarfile.open(tar) as tf:
            tf.extractall(dest)

    build_script = task_dir / "build.sh"
    if not (dest / "build.sh").exists() and build_script.is_file():
        shutil.copy(build_script, dest / "build.sh")


def convert_instance(
    instance: dict,
    out_root: Path,
    *,
    blob_dir: Path | None = None,
    allowed_hosts: list[str] | None = None,
    target_language: str | None = None,
    agent_user: str = AGENT_USER,
    include_oracle_payload: bool = False,
) -> Path:
    """Write the Harbor task directory for one instance and return its path."""
    iid = instance["instance_id"]
    active = get_active_branches(instance)
    if not active:
        raise ValueError(f"{iid}: no active test branches to convert")

    image_name = image_name_from_instance_id(iid)
    normalized_target_language = (target_language or "").strip().lower()
    install_jdk = normalized_target_language in {"java", "kotlin"}
    install_kotlin = normalized_target_language == "kotlin"
    task_dir = TASKS_DIR / iid
    out = out_root / iid
    if out.exists():
        shutil.rmtree(out)
    (out / "environment").mkdir(parents=True)
    tests_out = out / "tests"
    tests_out.mkdir()
    (out / "solution").mkdir()

    (out / "task.toml").write_text(
        _env.get_template("harbor_task.toml.j2").render(
            instance_id=iid,
            repository=instance["repository"],
            commit=instance["commit"],
            language=instance["language"],
            difficulty=instance.get("difficulty", ""),
            allowed_hosts=allowed_hosts or DEFAULT_AGENT_ALLOWED_HOSTS,
            agent_user=agent_user,
            env_cpus=DOCKER_CPUS,
            verifier_cpus=DOCKER_CPUS,
            build_timeout=BUILD_TIMEOUT_SEC,
            agent_timeout=AGENT_TIMEOUT_SEC,
            # One overall budget covering the offline compile plus every branch,
            # since the separate verifier runs them sequentially in one container.
            verifier_timeout=COMPILE_TIMEOUT_SEC + BRANCH_TIMEOUT_SEC * len(active),
        )
    )
    (out / "instruction.md").write_text(
        _env.get_template("harbor_instruction.md.j2").render(
            target_language=target_language,
            use_gradle=install_jdk,
        )
    )
    (out / "environment" / "Dockerfile").write_text(
        _env.get_template("harbor_environment.Dockerfile.j2").render(
            image_name=image_name,
            image_tag=CLEANROOM_TAG,
            install_jdk=install_jdk,
            install_kotlin=install_kotlin,
            kotlin_version=KOTLIN_VERSION,
            kotlin_compiler_sha256=KOTLIN_COMPILER_SHA256,
        )
    )
    # The separate verifier builds its own image from the tests/ dir (its build
    # context); the Dockerfile there wipes /workspace and bakes /tests in.
    (tests_out / "Dockerfile").write_text(
        _env.get_template("harbor_verifier.Dockerfile.j2").render(
            image_name=image_name,
            image_tag=CLEANROOM_TAG,
            install_jdk=install_jdk,
            install_kotlin=install_kotlin,
            kotlin_version=KOTLIN_VERSION,
            kotlin_compiler_sha256=KOTLIN_COMPILER_SHA256,
        )
    )

    shutil.copy(HARBOR_DATA / "test.sh", tests_out / "test.sh")
    shutil.copy(HARBOR_DATA / "run_verifier.py", tests_out / "run_verifier.py")
    # Faithfully copy the official metadata — no post-export ignore convergence,
    # which would change the benchmark score away from ProgramBench's.
    (tests_out / "tests.json").write_text(json.dumps({"branches": instance["branches"]}, indent=2, sort_keys=True))
    if instance.get("eval_clean_hashes"):
        (tests_out / "clean_hashes.txt").write_text("\n".join(instance["eval_clean_hashes"]) + "\n")

    for branch in active:
        _lay_down_branch(task_dir, branch, tests_out / "branches" / branch, blob_dir)

    # The oracle assumes every active branch was built the same way. Fail loudly
    # if they disagree (e.g. canop__broot.d6c798e ships divergent build.sh, one
    # of which never creates ./executable) rather than silently picking one.
    build_scripts = {(tests_out / "branches" / b / "build.sh").read_text() for b in active}
    if len(build_scripts) != 1:
        raise ValueError(f"{iid}: active branches disagree on build.sh ({len(build_scripts)} distinct recipes)")

    # Pack the gold solution: a network-free, language-agnostic oracle. A
    # portable task carries a compressed reference under solution/, which
    # Harbor uploads only for its oracle agent. The root-readable in-container
    # reference remains a compatibility fallback when no payload is requested.
    shutil.copy(HARBOR_DATA / "oracle_compile.sh", out / "solution" / "compile.sh")
    (out / "solution" / "solve.sh").write_text(_env.get_template("harbor_solve.sh.j2").render())
    if include_oracle_payload:
        try:
            _write_oracle_payload(
                f"{image_name}:{CLEANROOM_TAG}",
                out / "solution" / ORACLE_PAYLOAD_NAME,
            )
        except Exception:
            shutil.rmtree(out)
            raise
    return out


def convert_all(
    out_root: Path,
    *,
    instance_ids: list[str] | None = None,
    filter_spec: str = "",
    slice_spec: str = "",
    allowed_hosts: list[str] | None = None,
    target_language: str | None = None,
    agent_user: str = AGENT_USER,
    include_oracle_payload: bool = False,
    on_error: Callable[[str, Exception], None] | None = None,
) -> list[Path]:
    """Convert selected instances into Harbor tasks under ``out_root``.

    When ``on_error`` is supplied, a failure converting one instance is reported
    through it and that instance is skipped so the remaining ones still convert;
    otherwise the first failure aborts the whole batch (the strict default).
    """
    from programbench.utils.instance_filters import filter_instances

    instances = load_all_instances()
    if instance_ids:
        wanted = set(instance_ids)
        instances = [i for i in instances if i["instance_id"] in wanted]
        missing = wanted - {i["instance_id"] for i in instances}
        if missing:
            raise ValueError(f"Unknown instance_id(s): {sorted(missing)}")
    instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, has_test_branch=True)
    paths: list[Path] = []
    for instance in instances:
        try:
            paths.append(
                convert_instance(
                    instance,
                    out_root,
                    allowed_hosts=allowed_hosts,
                    target_language=target_language,
                    agent_user=agent_user,
                    include_oracle_payload=include_oracle_payload,
                )
            )
        except Exception as exc:
            if on_error is None:
                raise
            on_error(instance["instance_id"], exc)
    return paths
