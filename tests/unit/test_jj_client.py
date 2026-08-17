from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_stack.errors import (
    EXIT_AMBIGUOUS,
    EXIT_USAGE,
    CliError,
    resolve_exit_code,
)
from jj_stack.jj.client import (
    _COMMIT_TEMPLATE,
    JjClient,
    JjCommandError,
    ReviewRefUpdate,
    StaleWorkspaceError,
    _membership_scan_template,
)
from jj_stack.models.stack import LocalRevision
from tests.support.revision_helpers import make_revision

_REPO_GIT_DIR = str(Path("/repo/.git"))


def _revision_line(
    *,
    commit_id: str,
    parents: list[str],
    change_id: str,
    description: str,
    conflict: bool = False,
    empty: bool = False,
    divergent: bool = False,
    hidden: bool = False,
    working_copy: bool = False,
    working_copy_workspaces: list[str] | None = None,
    immutable: bool = False,
) -> str:
    return (
        json.dumps(
            {
                "change_id": change_id,
                "commit_id": commit_id,
                "conflict": conflict,
                "current_working_copy": working_copy,
                "description": description,
                "divergent": divergent,
                "empty": empty,
                "hidden": hidden,
                "immutable": immutable,
                "parents": parents,
                "working_copy_workspaces": working_copy_workspaces
                if working_copy_workspaces is not None
                else (["default"] if working_copy else []),
            },
            separators=(",", ":"),
        )
        + "\n"
    )


class _AmbiguousRevsetClient(JjClient):
    def _query_revisions(
        self,
        revset: str,
        *,
        limit: int | None = None,
    ) -> list[LocalRevision]:
        return [
            make_revision(commit_id="one", change_id="one-change", description="one\n"),
            make_revision(commit_id="two", change_id="two-change", description="two\n"),
        ]


class _InvalidRevsetClient(JjClient):
    def _query_revisions(
        self,
        revset: str,
        *,
        limit: int | None = None,
    ) -> list[LocalRevision]:
        raise JjCommandError("jj log failed: Error: Failed to parse revset: unexpected token")


def test_resolve_revision_reports_ambiguous_revsets_with_ambiguous_exit_code() -> None:
    client = _AmbiguousRevsetClient(Path("/repo"))

    with pytest.raises(CliError) as excinfo:
        client.resolve_revision("heads(all())")

    assert resolve_exit_code(excinfo.value) == EXIT_AMBIGUOUS


def test_resolve_revision_reports_invalid_revsets_with_usage_exit_code() -> None:
    client = _InvalidRevsetClient(Path("/repo"))

    with pytest.raises(CliError) as excinfo:
        client.resolve_revision("bad(")

    assert resolve_exit_code(excinfo.value) == EXIT_USAGE


def test_membership_query_preserves_stale_workspace_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "Error: The working copy is stale (not updated since operation abc123).\n"
                "Hint: Run `jj workspace update-stale` to update it.\n"
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(StaleWorkspaceError, match="workspace is stale") as excinfo:
        JjClient(Path("/repo")).query_revisions_with_membership(
            "trunk()",
            membership_revsets=("trunk()",),
        )

    assert "jj workspace update-stale" in str(excinfo.value.hint)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], str],
) -> JjClient:
    monkeypatch.setattr(subprocess, "run", _runner(responses))
    return JjClient(Path("/repo"))


def test_resolve_color_when_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "ui.color"): "debug\n",
    }
    configured = _client(monkeypatch, responses)

    # An explicit jj config value is honored.
    assert configured.resolve_color_when(stdout_is_tty=True) == "debug"
    # A CLI override beats the jj config, and CLI "auto" maps to terminal capability.
    assert configured.resolve_color_when(cli_color="never", stdout_is_tty=True) == "never"
    assert configured.resolve_color_when(cli_color="auto", stdout_is_tty=False) == "never"
    assert configured.resolve_color_when(cli_color="auto", stdout_is_tty=True) == "always"

    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == (
            "jj",
            "--ignore-working-copy",
            "config",
            "get",
            "ui.color",
        )
        assert Path(kwargs["cwd"]) == Path("/repo")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="no config\n")

    monkeypatch.setattr(subprocess, "run", run)
    unconfigured = JjClient(Path("/repo"))

    # Missing config falls back to terminal capability.
    assert unconfigured.resolve_color_when(stdout_is_tty=True) == "always"
    assert unconfigured.resolve_color_when(stdout_is_tty=False) == "never"


def test_first_post_bootstrap_jj_call_uses_normal_snapshot_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        observed_commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    client = JjClient(Path("/repo"))

    client.read_jj_stack_config_list_output()
    client.enable_initial_working_copy_snapshot()
    client.list_git_remotes()
    client.list_git_remotes()

    assert observed_commands == [
        ("jj", "--ignore-working-copy", "config", "list", "jj-stack"),
        ("jj", "git", "remote", "list"),
        ("jj", "--ignore-working-copy", "git", "remote", "list"),
    ]


def test_find_private_commits_returns_matching_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "git.private-commits"): "description(private)\n",
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "(description(private)) & ('head' | 'parent')",
            "-T",
            _template(),
        ): _revision_line(
            commit_id="head",
            parents=["parent"],
            change_id="head-change",
            description="head\n",
        ),
    }

    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="head\n"),
        make_revision(commit_id="parent", change_id="parent-change", description="parent\n"),
    )
    result = _client(monkeypatch, responses).find_private_commits(revisions)

    assert len(result) == 1
    assert result[0].commit_id == "head"


def test_list_remote_branches_resolves_jj_remote_name_to_fetch_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation == ("jj", "--ignore-working-copy", "git", "remote", "list"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "origin https://github.test/octo-org/repo.git "
                    "(push: git@github.test:octo-org/repo.git)\n"
                ),
                stderr="",
            )
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(command, 0, stdout="/repo/.git\n", stderr="")
        if invocation == (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "ls-remote",
            "--refs",
            "https://github.test/octo-org/repo.git",
            "refs/heads/jj-stack/feat",
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="abc123\trefs/heads/jj-stack/feat\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)
    result = JjClient(Path("/repo")).list_remote_branches(
        remote="origin", patterns=("refs/heads/jj-stack/feat",)
    )

    assert result == {"jj-stack/feat": "abc123"}
    assert seen_commands == [
        ("jj", "--ignore-working-copy", "git", "remote", "list"),
        ("jj", "--ignore-working-copy", "git", "root"),
        (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "ls-remote",
            "--refs",
            "https://github.test/octo-org/repo.git",
            "refs/heads/jj-stack/feat",
        ),
    ]


def test_list_remote_branches_rejects_an_unconfigured_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        seen_commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(JjCommandError, match="missing.*not configured"):
        JjClient(Path("/repo")).list_remote_branches(
            remote="missing",
            patterns=("refs/heads/jj-stack/feat",),
        )

    assert seen_commands == [("jj", "--ignore-working-copy", "git", "remote", "list")]


def test_remote_failure_redacts_http_userinfo_without_changing_subprocess_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_url = "https://alice:top-secret@github.test/octo-org/repo.git"
    scp_url = "git@github.test:octo-org/repo.git"
    seen_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation == ("jj", "--ignore-working-copy", "git", "remote", "list"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"origin {remote_url} (push: {scp_url})\n",
                stderr="",
            )
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="/repo/.git\n",
                stderr="",
            )
        if invocation == (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "ls-remote",
            "--refs",
            remote_url,
            "refs/heads/jj-stack/feat",
        ):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr=f"could not access {remote_url}; push URL remains {scp_url}",
            )
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)

    with pytest.raises(JjCommandError) as raised:
        JjClient(Path("/repo")).list_remote_branches(
            remote="origin",
            patterns=("refs/heads/jj-stack/feat",),
        )

    rendered = str(raised.value)
    assert "alice" not in rendered
    assert "top-secret" not in rendered
    assert "https://github.test/octo-org/repo.git" in rendered
    assert scp_url in rendered
    assert remote_url in seen_commands[-1]


def test_missing_review_fetch_isolation_is_a_shared_dry_run_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation[:4] == ("jj", "--ignore-working-copy", "config", "list"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation[:5] == (
            "jj",
            "--ignore-working-copy",
            "bookmark",
            "list",
            "--all-remotes",
        ):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(command, 0, stdout="/repo/.git\n", stderr="")
        if invocation == (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "config",
            "--get-all",
            "remote.origin.fetch",
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="+refs/heads/*:refs/remotes/origin/*\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)
    result = JjClient(Path("/repo")).ensure_review_fetch_isolation(
        remote="origin",
        dry_run=True,
    )

    assert result.status == "required"
    assert result.problem == "missing"
    assert all("config --add" not in " ".join(command) for command in seen_commands)


def test_review_fetch_isolation_normalizes_duplicate_exclusions_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_refspec = "^refs/heads/jj-stack/*"
    positive_refspec = "+refs/heads/*:refs/remotes/origin/*"
    seen_commands: list[tuple[str, ...]] = []
    events: list[str] = []
    normalized = False

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal normalized
        assert Path(kwargs["cwd"]) == Path("/repo")
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation[:4] == ("jj", "--ignore-working-copy", "config", "list"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation[:5] == (
            "jj",
            "--ignore-working-copy",
            "bookmark",
            "list",
            "--all-remotes",
        ):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="/repo/.git\n",
                stderr="",
            )
        if invocation == (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "config",
            "--get-all",
            "remote.origin.fetch",
        ):
            events.append("post-read" if normalized else "initial-read")
            refspecs = (
                (positive_refspec, review_refspec)
                if normalized
                else (positive_refspec, review_refspec, review_refspec)
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\n".join(refspecs) + "\n",
                stderr="",
            )
        if invocation == (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "config",
            "--fixed-value",
            "--replace-all",
            "remote.origin.fetch",
            review_refspec,
            review_refspec,
        ):
            events.append("replace")
            normalized = True
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)
    result = JjClient(Path("/repo")).ensure_review_fetch_isolation(
        remote="origin",
    )

    replace_commands = [
        command
        for command in seen_commands
        if command[3:7] == ("config", "--fixed-value", "--replace-all", "remote.origin.fetch")
    ]
    assert replace_commands == [
        (
            "git",
            "--git-dir",
            _REPO_GIT_DIR,
            "config",
            "--fixed-value",
            "--replace-all",
            "remote.origin.fetch",
            review_refspec,
            review_refspec,
        )
    ]
    assert events == ["initial-read", "replace", "post-read"]
    assert result.status == "applied"
    assert result.problem == "duplicate"


def test_review_fetch_isolation_reports_the_effective_override_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        invocation = tuple(command)
        assert invocation[:4] == ("jj", "--ignore-working-copy", "config", "list")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"name":"remotes.origin.fetch","value":[],"source":"repo",'
                '"path":"/repo/.jj/repo/config.toml","is_overridden":false}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", runner)

    with pytest.raises(CliError) as raised:
        JjClient(Path("/repo")).ensure_review_fetch_isolation(
            remote="origin",
        )

    assert "/repo/.jj/repo/config.toml" in str(raised.value)
    assert "jj config unset --repo" in str(raised.value)


def test_imported_review_bookmark_scan_reports_every_reserved_namespace_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        json.dumps({"name": "jj-stack/not-managed", "target": ["one"]})
        + "\n"
        + json.dumps({"name": "jj-stack/feature-abcdefgh", "target": ["two"]})
        + "\n"
    )

    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", runner)

    assert JjClient(Path("/repo")).visible_review_bookmark_targets() == {
        "jj-stack/feature-abcdefgh": frozenset({"two"}),
        "jj-stack/not-managed": frozenset({"one"}),
    }


def test_remote_review_ref_mutation_uses_one_atomic_exact_lease_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []
    old_branch = "jj-stack/old-aaaaaaaa"
    new_branch = "jj-stack/new-bbbbbbbb"
    old_ref = f"refs/heads/{old_branch}"
    new_ref = f"refs/heads/{new_branch}"

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation[:4] == ("jj", "--ignore-working-copy", "config", "list"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation[:5] == (
            "jj",
            "--ignore-working-copy",
            "bookmark",
            "list",
            "--all-remotes",
        ):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(command, 0, stdout="/repo/.git\n", stderr="")
        if invocation[-3:] == ("config", "--get-all", "remote.origin.fetch"):
            return subprocess.CompletedProcess(
                command, 0, stdout="^refs/heads/jj-stack/*\n", stderr=""
            )
        if invocation == ("jj", "--ignore-working-copy", "git", "remote", "list"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "origin https://github.test/octo-org/repo.git "
                    "(push: git@github.test:octo-org/repo.git)\n"
                ),
                stderr="",
            )
        if invocation[3:] == (
            "push",
            "--atomic",
            "--no-follow-tags",
            "--no-verify",
            f"--force-with-lease={old_ref}:old",
            f"--force-with-lease={new_ref}:",
            "git@github.test:octo-org/repo.git",
            f"updated:{old_ref}",
            f"created:{new_ref}",
        ):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)
    client = JjClient(Path("/repo"))
    client.mutate_remote_review_refs(
        remote="origin",
        updates=(
            ReviewRefUpdate(
                branch=old_branch,
                expected_target="old",
                desired_target="updated",
            ),
            ReviewRefUpdate(
                branch=new_branch,
                expected_target=None,
                desired_target="created",
            ),
        ),
    )

    pushes = [command for command in seen_commands if command[3:4] == ("push",)]
    assert len(pushes) == 1


def test_remote_change_id_inspection_fetches_an_object_without_creating_a_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []
    commit_id = "a" * 40
    cat_file_calls = 0

    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal cat_file_calls
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation[:4] == ("jj", "--ignore-working-copy", "config", "list"):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation[:5] == (
            "jj",
            "--ignore-working-copy",
            "bookmark",
            "list",
            "--all-remotes",
        ):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(command, 0, stdout="/repo/.git\n", stderr="")
        if invocation[-3:] == ("config", "--get-all", "remote.origin.fetch"):
            return subprocess.CompletedProcess(
                command, 0, stdout="^refs/heads/jj-stack/*\n", stderr=""
            )
        if invocation == ("jj", "--ignore-working-copy", "git", "remote", "list"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="origin https://github.test/octo-org/repo.git\n",
                stderr="",
            )
        if invocation[3:] == ("cat-file", "commit", commit_id):
            cat_file_calls += 1
            if cat_file_calls == 1:
                return subprocess.CompletedProcess(
                    command, 128, stdout="", stderr="object not found"
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"tree {'b' * 40}\n"
                    "author Test <test@example.com> 1 +0000\n"
                    "committer Test <test@example.com> 1 +0000\n"
                    "change-id full-change-id\n\nsubject\n"
                ),
                stderr="",
            )
        if invocation[3:] == (
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "https://github.test/octo-org/repo.git",
            commit_id,
        ):
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)

    change_id = JjClient(Path("/repo")).read_remote_git_change_id(
        remote="origin",
        commit_id=commit_id,
    )

    assert change_id == "full-change-id"
    assert not any(
        "update-ref" in command or "git import" in command for command in seen_commands
    )


def test_temp_ref_cleanup_removes_raw_ref_when_forgetting_bookmark_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_id = "a" * 40
    raw_ref_present = True
    seen_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal raw_ref_present
        invocation = tuple(command)
        seen_commands.append(invocation)
        if invocation[:5] == (
            "jj",
            "--ignore-working-copy",
            "bookmark",
            "list",
            "-T",
        ):
            payload = json.dumps({"name": "jj-stack-tmp/checkout", "target": [commit_id]})
            return subprocess.CompletedProcess(command, 0, stdout=f"{payload}\n", stderr="")
        if invocation == (
            "jj",
            "--ignore-working-copy",
            "bookmark",
            "forget",
            "jj-stack-tmp/checkout",
        ):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="forget failed")
        if invocation == ("jj", "--ignore-working-copy", "git", "root"):
            return subprocess.CompletedProcess(command, 0, stdout="/repo/.git\n", stderr="")
        if invocation[3:] == (
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/heads/jj-stack-tmp/checkout",
        ):
            return subprocess.CompletedProcess(
                command,
                0 if raw_ref_present else 1,
                stdout=f"{commit_id}\n" if raw_ref_present else "",
                stderr="",
            )
        if invocation[3:] == (
            "update-ref",
            "-d",
            "refs/heads/jj-stack-tmp/checkout",
            commit_id,
        ):
            raw_ref_present = False
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)

    with pytest.raises(JjCommandError, match="forget failed"):
        JjClient(Path("/repo"))._clear_review_temp_ref()

    assert not raw_ref_present
    assert any(command[3:4] == ("update-ref",) for command in seen_commands)


def _template() -> str:
    return _COMMIT_TEMPLATE


def _trunk_scan_template() -> str:
    return _membership_scan_template(("trunk()",))


def _selection_scan_template(selection_revset: str) -> str:
    return _membership_scan_template(("trunk()", selection_revset))


def _revision_with_flag_line(revision_line: str, *, is_trunk: bool) -> str:
    return (
        json.dumps(
            {"revision": json.loads(revision_line), "membership": [is_trunk]},
            separators=(",", ":"),
        )
        + "\n"
    )


def _revision_with_two_flags_line(
    revision_line: str,
    *,
    is_trunk: bool,
    is_selected: bool,
) -> str:
    return (
        json.dumps(
            {
                "revision": json.loads(revision_line),
                "membership": [is_trunk, is_selected],
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def _selection_scan_command(selection_revset: str) -> tuple[str, ...]:
    return (
        "jj",
        "log",
        "--no-graph",
        "-r",
        f"trunk() | ({selection_revset})",
        "-T",
        _selection_scan_template(selection_revset),
    )


def _selection_scan_response(*entries: tuple[str, bool, bool]) -> str:
    return "".join(
        _revision_with_two_flags_line(
            revision_line,
            is_trunk=is_trunk,
            is_selected=is_selected,
        )
        for revision_line, is_trunk, is_selected in entries
    )


def _runner(responses: dict[tuple[str, ...], str]):
    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        response_key = (
            (key[0], *key[2:]) if len(key) > 1 and key[1] == "--ignore-working-copy" else key
        )
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
        assert Path(kwargs["cwd"]) == Path("/repo")
        assert kwargs["text"] is True
        if (
            response_key not in responses
            and len(response_key) == 8
            and response_key[:4] == ("jj", "log", "--no-graph", "-r")
            and response_key[5] == "-T"
            and response_key[6] == _template()
            and response_key[7] == "--limit"
        ):
            # Defensive guard; the boundary probe always includes the limit value.
            raise AssertionError(f"Unexpected truncated command: {key!r}")
        if (
            response_key not in responses
            and len(response_key) == 9
            and response_key[:4] == ("jj", "log", "--no-graph", "-r")
            and response_key[5] == "-T"
            and response_key[6] == _template()
            and response_key[7:] == ("--limit", "2")
        ):
            boundary_revset = response_key[4]
            if boundary_revset.startswith("heads(first_ancestors(") and boundary_revset.endswith(
                "& ::'trunk')"
            ):
                fallback_key = (
                    "jj",
                    "log",
                    "--no-graph",
                    "-r",
                    "trunk()",
                    "-T",
                    _template(),
                    "--limit",
                    "2",
                )
                if fallback_key in responses:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=responses[fallback_key],
                        stderr="",
                    )
        if (
            response_key not in responses
            and len(response_key) == 7
            and response_key[:4] == ("jj", "log", "--no-graph", "-r")
            and response_key[5:] == ("-T", _template())
        ):
            revset = response_key[4]
            if revset.startswith("children(") and ") & merges() & ::" in revset:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if response_key not in responses:
            raise AssertionError(f"Unexpected command: {key!r}")
        return subprocess.CompletedProcess(command, 0, stdout=responses[response_key], stderr="")

    return run
