#!/usr/bin/env python3
"""Install and smoke-test built jj-stack distributions outside the source tree."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND_TIMEOUT_SECONDS = 180


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    rendered = " ".join(command)
    print(f"==> {rendered}", flush=True)
    completed = subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != expected_exit:
        raise RuntimeError(
            f"{rendered} exited {completed.returncode}, expected {expected_exit}:\n"
            f"{completed.stdout}"
        )
    return completed


def _smoke_artifact(artifact: Path, *, scratch: Path, base_env: dict[str, str]) -> None:
    label = "wheel" if artifact.suffix == ".whl" else "source"
    artifact_root = scratch / label
    venv = artifact_root / "venv"
    repo = artifact_root / "repo"
    artifact_root.mkdir()

    _run(
        ("uv", "venv", "--python", sys.executable, str(venv)),
        cwd=artifact_root,
        env=base_env,
    )
    python = venv / "bin" / "python"
    executable = venv / "bin" / "jj-stack"
    _run(
        ("uv", "pip", "install", "--python", str(python), "--no-cache", str(artifact)),
        cwd=artifact_root,
        env=base_env,
    )
    version = _run((str(executable), "--version"), cwd=artifact_root, env=base_env)
    if not version.stdout.startswith("jj-stack "):
        raise RuntimeError(f"unexpected version output from {artifact.name}: {version.stdout!r}")

    _run(("jj", "git", "init", str(repo)), cwd=artifact_root, env=base_env)
    _run((str(executable), "in-use"), cwd=repo, env=base_env, expected_exit=1)
    print(f"PASS: {artifact.name}", flush=True)


def _distributions(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("jj_stack-*.whl"))
    source_archives = sorted(dist_dir.glob("jj_stack-*.tar.gz"))
    if len(wheels) != 1 or len(source_archives) != 1:
        raise ValueError(
            f"expected one jj-stack wheel and one source archive in {dist_dir}; "
            f"found {len(wheels)} wheel(s) and {len(source_archives)} source archive(s)"
        )
    return wheels[0], source_archives[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        description="Install and smoke-test the wheel and source distribution in clean venvs."
    )
    parser.add_argument(
        "dist_dir",
        nargs="?",
        type=Path,
        default=REPO_ROOT / "dist",
        help="directory containing exactly one jj-stack wheel and source archive",
    )
    args = parser.parse_args(argv)
    try:
        artifacts = _distributions(args.dist_dir.resolve())
        if shutil.which("uv") is None or shutil.which("jj") is None:
            raise RuntimeError("uv and jj must be available on PATH")
        with tempfile.TemporaryDirectory(prefix="jj-stack-release-") as temporary:
            scratch = Path(temporary)
            base_env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
            }
            base_env["XDG_CONFIG_HOME"] = str(scratch / "config")
            base_env["XDG_STATE_HOME"] = str(scratch / "state")
            for artifact in artifacts:
                _smoke_artifact(artifact, scratch=scratch, base_env=base_env)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"release artifact check failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
