#!/usr/bin/env python3
"""Run opt-in pre-release smoke tests against a disposable real GitHub repo."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUDGET_SECONDS = 15 * 60
DEFAULT_COMMAND_TIMEOUT_SECONDS = 90
MERGE_TIMEOUT_SECONDS = 11 * 60
DISPOSABLE_PREFIX = "jj-stack-prerelease-"
_MARKER_PATTERN = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}")


class LiveTestError(RuntimeError):
    """A live pre-release assertion or command failed."""


@dataclass(frozen=True)
class DisposableRepo:
    owner: str
    name: str
    marker: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def validate_deletion_target(self) -> None:
        expected_name = f"{DISPOSABLE_PREFIX}{self.marker}"
        if self.name != expected_name:
            raise LiveTestError(
                f"refusing to delete unexpected repository name {self.name!r}; "
                f"expected {expected_name!r}"
            )
        if not self.owner or "/" in self.owner or _MARKER_PATTERN.fullmatch(self.marker) is None:
            raise LiveTestError("refusing to delete an invalid disposable repository target")


def _run(
    command: Sequence[str],
    *,
    deadline: float | None,
    env: Mapping[str, str] | None = None,
    cwd: Path = REPO_ROOT,
    capture: bool = False,
    expected: tuple[int, ...] = (0,),
    max_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    timeout = max_seconds
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveTestError(f"time budget expired before {shlex.join(command)}")
        timeout = min(timeout, remaining)
    print(f"    $ {shlex.join(command)}", flush=True)
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            cwd=cwd,
            env=None if env is None else dict(env),
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise LiveTestError(
            f"command exceeded its {timeout:.0f}s time budget: {shlex.join(command)}"
        ) from error
    if completed.returncode not in expected:
        raise LiveTestError(
            f"command exited {completed.returncode}: {shlex.join(command)}"
            f"{_captured_failure_detail(completed)}"
        )
    return completed


class LiveGithubSuite:
    def __init__(
        self,
        *,
        deadline: float,
        env: Mapping[str, str],
        repo: DisposableRepo,
        root: Path,
    ) -> None:
        self.deadline = deadline
        self.env = dict(env)
        self.repo = repo
        self.primary = root / "primary"
        self.checkout = root / "checkout"

    def execute(self) -> None:
        try:
            self._create_remote()
            self._run_scenarios()
        finally:
            self._delete_remote()

    def _run_scenarios(self) -> None:
        self._section("static CLI and isolated repo setup")
        self._run_command(("uv", "run", "jj-stack", "help", "--all"))
        self._run_command(("uv", "run", "jj-stack", "completion", "bash"))
        self._initialize_primary_repo()
        self._jj_stack(self.primary, "doctor", "--fix")
        self._jj_stack(self.primary, "in-use", expected=(1,))

        self._section("submit, inspect, unstack, relink, and checkout")
        bottom_change = self._commit_change(
            self.primary,
            title="prerelease bottom change",
            filename="bottom.txt",
        )
        top_change = self._commit_change(
            self.primary,
            title="prerelease top change",
            filename="top.txt",
        )
        self._jj_stack(self.primary, "submit", "--dry-run", top_change)
        self._jj_stack(self.primary, "submit", top_change)
        self._jj_stack(self.primary, "in-use")
        self._jj_stack(self.primary, "view", "--json")
        self._jj_stack(self.primary, "list", "--json")
        pull_requests = self._pull_requests()
        bottom_pr = _find_pr(pull_requests, "prerelease bottom change")
        top_pr = _find_pr(pull_requests, "prerelease top change")

        self._jj_stack(self.primary, "unstack", top_change)
        self._jj_stack(self.primary, "submit", top_change)
        self._jj_stack(self.primary, "unstack", "--local", top_change)
        self._jj_stack(
            self.primary,
            "relink",
            str(bottom_pr["number"]),
            bottom_change,
        )
        self._jj_stack(self.primary, "relink", str(top_pr["number"]), top_change)
        self._jj_stack(self.primary, "submit", top_change)

        self._run_command(
            (
                "jj",
                "git",
                "clone",
                f"https://github.com/{self.repo.full_name}.git",
                str(self.checkout),
            )
        )
        self._jj_stack(
            self.checkout,
            "checkout",
            "--pull-request",
            str(top_pr["number"]),
        )
        self._jj_stack(self.checkout, "in-use")
        self._jj_stack(
            self.checkout,
            "view",
            "--pull-request",
            str(top_pr["number"]),
        )

        self._section("direct stack merge and automatic reconciliation")
        self._jj_stack(
            self.primary,
            "merge",
            "--method",
            "squash",
            top_change,
            max_seconds=MERGE_TIMEOUT_SECONDS,
        )
        merged = self._pull_requests(state="merged")
        _find_pr(merged, "prerelease bottom change")
        _find_pr(merged, "prerelease top change")
        self._jj_stack(self.primary, "sync", "--all")

        self._section("external merge followed by selected sync")
        external_change = self._commit_change(
            self.primary,
            title="prerelease external merge",
            filename="external.txt",
        )
        self._jj_stack(self.primary, "submit", external_change)
        external_pr = _find_pr(self._pull_requests(), "prerelease external merge")
        self._run_command(
            (
                "gh",
                "pr",
                "merge",
                str(external_pr["number"]),
                "--repo",
                self.repo.full_name,
                "--squash",
                "--match-head-commit",
                str(external_pr["headRefOid"]),
            ),
            max_seconds=120,
        )
        self._jj_stack(self.primary, "sync", external_change)

        self._section("explicit close and cleanup")
        cleanup_change = self._commit_change(
            self.primary,
            title="prerelease cleanup",
            filename="cleanup.txt",
        )
        self._jj_stack(self.primary, "submit", cleanup_change)
        cleanup_pr = _find_pr(self._pull_requests(), "prerelease cleanup")
        self._jj_stack(
            self.primary,
            "cleanup",
            "--pull-request",
            str(cleanup_pr["number"]),
            "--close",
        )
        _find_pr(self._pull_requests(state="closed"), "prerelease cleanup")

    def _create_remote(self) -> None:
        self._section("create private disposable repository")
        self._run_command(
            (
                "gh",
                "repo",
                "create",
                self.repo.full_name,
                "--private",
                "--description",
                self.repo.marker,
                "--disable-issues",
                "--disable-wiki",
            )
        )

    def _initialize_primary_repo(self) -> None:
        self._run_command(("jj", "git", "init", str(self.primary)))
        (self.primary / "README.md").write_text(
            "jj-stack live prerelease test\n",
            encoding="utf-8",
        )
        self._run_command(
            ("jj", "commit", "-m", "initialize live prerelease test"),
            cwd=self.primary,
        )
        self._run_command(
            ("jj", "bookmark", "create", "main", "-r", "@-"),
            cwd=self.primary,
        )
        self._run_command(
            (
                "jj",
                "git",
                "remote",
                "add",
                "origin",
                f"https://github.com/{self.repo.full_name}.git",
            ),
            cwd=self.primary,
        )
        self._run_command(
            ("jj", "git", "push", "--remote", "origin", "--bookmark", "main"),
            cwd=self.primary,
        )
        self._run_command(
            (
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repo.full_name}",
                "-F",
                "default_branch=main",
                "-F",
                "allow_merge_commit=false",
                "-F",
                "allow_rebase_merge=false",
                "-F",
                "allow_squash_merge=true",
            )
        )

    def _commit_change(self, repo: Path, *, title: str, filename: str) -> str:
        (repo / filename).write_text(f"{title}\n", encoding="utf-8")
        self._run_command(("jj", "commit", "-m", title), cwd=repo)
        completed = self._run_command(
            ("jj", "log", "--no-graph", "-r", "@-", "-T", 'change_id ++ "\\n"'),
            cwd=repo,
            capture=True,
        )
        change_id = completed.stdout.strip()
        if not change_id:
            raise LiveTestError(f"could not read change ID for {title!r}")
        return change_id

    def _jj_stack(
        self,
        repo: Path,
        *arguments: str,
        expected: tuple[int, ...] = (0,),
        max_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_command(
            ("uv", "run", "jj-stack", "--repository", str(repo), *arguments),
            expected=expected,
            max_seconds=max_seconds,
        )

    def _pull_requests(self, *, state: str = "all") -> tuple[dict[str, object], ...]:
        completed = self._run_command(
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo.full_name,
                "--state",
                state,
                "--limit",
                "100",
                "--json",
                "number,title,state,headRefOid",
            ),
            capture=True,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise LiveTestError("GitHub returned invalid pull request data")
        return tuple(payload)

    def _delete_remote(self) -> None:
        self._section("verify and delete disposable repository")
        self.repo.validate_deletion_target()
        inspection = _run(
            ("gh", "api", f"repos/{self.repo.full_name}"),
            deadline=None,
            env=self.env,
            capture=True,
            expected=(0, 1),
            max_seconds=30,
        )
        if inspection.returncode == 1:
            if _is_not_found(inspection):
                print("    disposable repository is already absent", flush=True)
                return
            raise LiveTestError(
                "could not inspect the disposable repository before deletion"
                f"{_captured_failure_detail(inspection)}"
            )
        metadata = json.loads(inspection.stdout)
        if (
            metadata.get("full_name") != self.repo.full_name
            or metadata.get("private") is not True
            or metadata.get("description") != self.repo.marker
        ):
            raise LiveTestError(
                "refusing to delete a repository whose identity, privacy, or marker does not "
                "match this test run"
            )
        _run(
            ("gh", "repo", "delete", self.repo.full_name, "--yes"),
            deadline=None,
            env=self.env,
            max_seconds=45,
        )
        verification = _run(
            ("gh", "api", f"repos/{self.repo.full_name}"),
            deadline=None,
            env=self.env,
            capture=True,
            expected=(0, 1),
            max_seconds=30,
        )
        if verification.returncode == 0 or not _is_not_found(verification):
            raise LiveTestError(
                f"could not verify deletion of {self.repo.full_name}; inspect it manually"
            )

    def _run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path = REPO_ROOT,
        capture: bool = False,
        expected: tuple[int, ...] = (0,),
        max_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            command,
            deadline=self.deadline,
            env=self.env,
            cwd=cwd,
            capture=capture,
            expected=expected,
            max_seconds=max_seconds,
        )

    @staticmethod
    def _section(name: str) -> None:
        print(f"\n==> {name}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(
        prog="tests/run_live_github.py",
        description=(
            "Run every jj-stack command through short user-like flows in a disposable private "
            "GitHub repository. Requires authenticated gh access with repo deletion permission."
        ),
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_BUDGET_SECONDS,
        help="Time budget for setup and scenarios; cleanup always runs afterward (default: 900).",
    )
    args = parser.parse_args(argv)
    if args.budget_seconds <= 0:
        parser.error("--budget-seconds must be positive")

    started = time.monotonic()
    deadline = started + args.budget_seconds
    try:
        owner, token = _authenticate(deadline)
        marker = _new_marker()
        repo = DisposableRepo(owner=owner, name=f"{DISPOSABLE_PREFIX}{marker}", marker=marker)
        with tempfile.TemporaryDirectory(prefix="jj-stack-live-") as temporary:
            root = Path(temporary)
            env = _isolated_environment(root, token=token)
            print(
                f"Live GitHub pre-release run: {repo.full_name} "
                f"({args.budget_seconds:.0f}s budget)",
                flush=True,
            )
            LiveGithubSuite(deadline=deadline, env=env, repo=repo, root=root).execute()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (LiveTestError, OSError, json.JSONDecodeError) as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"\nPASS: live GitHub pre-release checks completed in {elapsed:.1f}s", flush=True)
    return 0


def _authenticate(deadline: float) -> tuple[str, str]:
    for executable in ("gh", "jj", "uv"):
        if shutil.which(executable) is None:
            raise LiveTestError(f"required executable is not installed: {executable}")
    token = _run(
        ("gh", "auth", "token", "--hostname", "github.com"),
        deadline=deadline,
        capture=True,
        max_seconds=30,
    ).stdout.strip()
    if not token:
        raise LiveTestError("`gh auth token` returned an empty token")
    owner = _run(
        ("gh", "api", "--hostname", "github.com", "user", "--jq", ".login"),
        deadline=deadline,
        capture=True,
        max_seconds=30,
    ).stdout.strip()
    if not owner or "/" in owner:
        raise LiveTestError("could not determine the authenticated GitHub login")
    headers = _run(
        (
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "HEAD",
            "--include",
            "user",
        ),
        deadline=deadline,
        capture=True,
        max_seconds=30,
    ).stdout
    for line in headers.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.lower() == "x-oauth-scopes" and value.strip():
            scopes = {scope.strip() for scope in value.split(",")}
            if "delete_repo" not in scopes:
                raise LiveTestError(
                    "the active classic OAuth token lacks permission to delete the test repo"
                )
            break
    return owner, token


def _isolated_environment(root: Path, *, token: str) -> dict[str, str]:
    gh_config = root / "gh-config"
    gh_config.mkdir()
    jj_config = root / "jj-config.toml"
    jj_config.write_text(
        "\n".join(
            (
                "[revset-aliases]",
                '"trunk()" = "latest(present(main) | root())"',
                "",
                "[ui]",
                'color = "never"',
                'pager = "cat"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    git_config = root / "gitconfig"
    git_config.write_text(
        '[credential "https://github.com"]\n\thelper =\n\thelper = !gh auth git-credential\n',
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_ENTERPRISE_TOKEN", "GH_HOST", "GITHUB_ENTERPRISE_TOKEN", "VIRTUAL_ENV"}
    }
    env.update(
        {
            "GH_CONFIG_DIR": str(gh_config),
            "GH_HOST": "github.com",
            "GH_TOKEN": token,
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "JJ_CONFIG": str(jj_config),
            "JJ_EMAIL": "jj-stack-prerelease@example.invalid",
            "JJ_USER": "jj-stack prerelease",
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
        }
    )
    return env


def _new_marker() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _find_pr(
    pull_requests: Sequence[dict[str, object]],
    title: str,
) -> dict[str, object]:
    matches = [
        pull_request for pull_request in pull_requests if pull_request.get("title") == title
    ]
    if len(matches) != 1:
        raise LiveTestError(
            f"expected exactly one pull request titled {title!r}, found {len(matches)}"
        )
    return matches[0]


def _is_not_found(completed: subprocess.CompletedProcess[str]) -> bool:
    return completed.returncode != 0 and (
        "HTTP 404" in completed.stderr or "Not Found" in completed.stderr
    )


def _captured_failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(
        part.strip() for part in (completed.stdout or "", completed.stderr or "") if part.strip()
    )
    return f"\n{output[-4000:]}" if output else ""


if __name__ == "__main__":
    raise SystemExit(main())
