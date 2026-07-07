from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_stack.errors import EXIT_AMBIGUOUS, EXIT_USAGE, CliError, resolve_exit_code
from jj_stack.jj.client import (
    JjClient,
    JjCommandError,
    StaleWorkspaceError,
    UnsupportedStackError,
)
from jj_stack.models.stack import LocalRevision
from tests.support.revision_helpers import make_revision


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
    immutable: bool = False,
) -> str:
    import json

    fields = [
        json.dumps(change_id),
        json.dumps(commit_id),
        json.dumps(description),
        json.dumps(parents),
        "true" if empty else "false",
        "true" if divergent else "false",
        "true" if working_copy else "false",
        "true" if hidden else "false",
        "true" if immutable else "false",
        "true" if conflict else "false",
    ]
    return "\t".join(fields) + "\n"


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


_TRUNK = _revision_line(
    commit_id="trunk", parents=["root"], change_id="trunk-change", description="main\n"
)
_ROOT = _revision_line(
    commit_id="root",
    parents=[],
    change_id="root-change",
    description="\n",
    empty=True,
    immutable=True,
)
_EMPTY_WORKING_COPY = _revision_line(
    commit_id="wc",
    parents=["head"],
    change_id="wc-change",
    description="\n",
    empty=True,
    working_copy=True,
)
_HEAD = _revision_line(
    commit_id="head", parents=["parent"], change_id="head-change", description="head\n"
)
_HEAD_ON_IMMUTABLE_PARENT = _revision_line(
    commit_id="head",
    parents=["immutable-parent"],
    change_id="head-change",
    description="head\n",
)
_PARENT = _revision_line(
    commit_id="parent", parents=["trunk"], change_id="parent-change", description="parent\n"
)
_MERGE = _revision_line(
    commit_id="merge",
    parents=["left", "right"],
    change_id="merge-change",
    description="merge\n",
)
_DIVERGENT = _revision_line(
    commit_id="divergent",
    parents=["trunk"],
    change_id="div-change",
    description="divergent\n",
    divergent=True,
)
_IMMUTABLE_PARENT = _revision_line(
    commit_id="immutable-parent",
    parents=["trunk"],
    change_id="immutable-parent-change",
    description="immutable parent\n",
    immutable=True,
)
_HIDDEN = _revision_line(
    commit_id="hidden",
    parents=["trunk"],
    change_id="hidden-change/1",
    description="hidden predecessor\n",
    hidden=True,
)


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


def _client(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], str],
) -> JjClient:
    monkeypatch.setattr(subprocess, "run", _runner(responses))
    return JjClient(Path("/repo"))


def test_discover_review_stack_returns_empty_revisions_when_head_is_trunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("trunk"): _selection_scan_response((_TRUNK, True, True)),
    }

    stack = _client(monkeypatch, responses).discover_review_stack("trunk")

    assert stack.revisions == ()
    assert stack.head.commit_id == "trunk"


def test_discover_review_stack_uses_parent_of_empty_working_copy_as_default_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trunk_scan = _revision_with_flag_line(_TRUNK, is_trunk=True)
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "trunk() | @ | @-",
            "-T",
            _trunk_scan_template(),
        ): (
            trunk_scan
            + _revision_with_flag_line(_EMPTY_WORKING_COPY, is_trunk=False)
            + _revision_with_flag_line(_HEAD, is_trunk=False)
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head') & ::'trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            _HEAD + _PARENT + _TRUNK
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack()

    assert stack.selected_revset == "@-"
    assert [revision.subject for revision in stack.revisions] == ["parent", "head"]


def test_discover_review_stack_uses_non_empty_working_copy_as_default_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_copy_head = _revision_line(
        commit_id="head",
        parents=["parent"],
        change_id="head-change",
        description="head\n",
        working_copy=True,
    )
    trunk_scan = _revision_with_flag_line(_TRUNK, is_trunk=True)
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "trunk() | @ | @-",
            "-T",
            _trunk_scan_template(),
        ): (
            trunk_scan
            + _revision_with_flag_line(working_copy_head, is_trunk=False)
            + _revision_with_flag_line(_PARENT, is_trunk=False)
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head') & ::'trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            working_copy_head + _PARENT + _TRUNK
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack()

    assert stack.selected_revset == "@"
    assert [revision.subject for revision in stack.revisions] == ["parent", "head"]


def test_discover_review_stack_default_head_includes_merged_side_branch_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_trunk = _revision_line(
        commit_id="current-trunk",
        parents=["old-trunk", "merged"],
        change_id="trunk-change",
        description="main\n",
    )
    merged = _revision_line(
        commit_id="merged",
        parents=["old-trunk"],
        change_id="merged-change",
        description="merged\n",
        immutable=True,
    )
    head = _revision_line(
        commit_id="head-3",
        parents=["merged"],
        change_id="head-3-change",
        description="head 3\n",
        working_copy=True,
    )
    old_trunk = _revision_line(
        commit_id="old-trunk",
        parents=["root"],
        change_id="old-trunk-change",
        description="old trunk\n",
        immutable=True,
    )
    trunk_scan = (
        _revision_with_flag_line(current_trunk, is_trunk=True)
        + _revision_with_flag_line(head, is_trunk=False)
        + _revision_with_flag_line(merged, is_trunk=False)
    )
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "trunk() | @ | @-",
            "-T",
            _trunk_scan_template(),
        ): trunk_scan,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head-3') & ::'current-trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): merged,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "children('merged') & merges() & ::'current-trunk'",
            "-T",
            _template(),
        ): current_trunk,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "'merged'::'head-3'",
            "-T",
            _template(),
        ): (head + merged),
        ("jj", "log", "--no-graph", "-r", "old-trunk", "-T", _template(), "--limit", "2"): (
            old_trunk
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack(
        allow_immutable=True,
    )

    assert stack.selected_revset == "@"
    assert [revision.subject for revision in stack.revisions] == ["merged", "head 3"]


def test_discover_review_stack_rejects_root_fallback_trunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head"): _selection_scan_response(
            (_ROOT, True, False),
            (_HEAD, False, True),
        ),
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(UnsupportedStackError) as exc:
        client.discover_review_stack("head")

    assert exc.value.reason == "trunk_resolved_to_root"
    assert exc.value.hint is not None


def test_discover_review_stack_rejects_merge_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("merge"): _selection_scan_response(
            (_TRUNK, True, False),
            (_MERGE, False, True),
        ),
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(UnsupportedStackError, match="merge commits are not supported"):
        client.discover_review_stack("merge")


def test_discover_review_stack_rejects_divergent_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("divergent"): _selection_scan_response(
            (_TRUNK, True, False),
            (_DIVERGENT, False, True),
        ),
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(UnsupportedStackError, match="divergent changes are not supported") as exc:
        client.discover_review_stack("divergent")

    assert exc.value.change_id == "div-change"
    assert exc.value.reason == "divergent_change"


def test_discover_review_stack_allows_divergent_ancestor_for_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    divergent_parent = _revision_line(
        commit_id="div-parent",
        parents=["parent"],
        change_id="div-parent-change",
        description="div parent\n",
        divergent=True,
    )
    head = _revision_line(
        commit_id="head-2",
        parents=["div-parent"],
        change_id="head-2-change",
        description="head 2\n",
    )
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head-2"): _selection_scan_response(
            (_TRUNK, True, False),
            (head, False, True),
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head-2') & ::'trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head-2'", "-T", _template()): (
            head + divergent_parent + _PARENT + _TRUNK
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack(
        "head-2",
        allow_divergent=True,
    )

    assert [revision.subject for revision in stack.revisions] == [
        "parent",
        "div parent",
        "head 2",
    ]


def test_discover_review_stack_rejects_immutable_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head"): _selection_scan_response(
            (_TRUNK, True, False),
            (_HEAD_ON_IMMUTABLE_PARENT, False, True),
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head') & ::'trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            _HEAD_ON_IMMUTABLE_PARENT + _IMMUTABLE_PARENT + _TRUNK
        ),
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(UnsupportedStackError, match="immutable commits are not reviewable"):
        client.discover_review_stack("head")


def test_discover_review_stack_allows_immutable_ancestor_for_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head"): _selection_scan_response(
            (_TRUNK, True, False),
            (_HEAD_ON_IMMUTABLE_PARENT, False, True),
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head') & ::'trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            _HEAD_ON_IMMUTABLE_PARENT + _IMMUTABLE_PARENT + _TRUNK
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack(
        "head",
        allow_immutable=True,
    )

    assert [revision.subject for revision in stack.revisions] == [
        "immutable parent",
        "head",
    ]


def test_discover_review_stack_excludes_revisions_already_reachable_from_trunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_trunk = _revision_line(
        commit_id="current-trunk",
        parents=["old-trunk", "merged"],
        change_id="trunk-change",
        description="main\n",
    )
    merged = _revision_line(
        commit_id="merged",
        parents=["old-trunk"],
        change_id="merged-change",
        description="merged\n",
        immutable=True,
    )
    head = _revision_line(
        commit_id="head-3",
        parents=["merged"],
        change_id="head-3-change",
        description="head 3\n",
    )
    old_trunk = _revision_line(
        commit_id="old-trunk",
        parents=["root"],
        change_id="old-trunk-change",
        description="old trunk\n",
        immutable=True,
    )
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head-3"): _selection_scan_response(
            (current_trunk, True, False),
            (head, False, True),
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head-3') & ::'current-trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): merged,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "children('merged') & merges() & ::'current-trunk'",
            "-T",
            _template(),
        ): current_trunk,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "'merged'::'head-3'",
            "-T",
            _template(),
        ): (head + merged),
        ("jj", "log", "--no-graph", "-r", "old-trunk", "-T", _template(), "--limit", "2"): (
            old_trunk
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack(
        "head-3",
        allow_immutable=True,
    )

    assert [revision.subject for revision in stack.revisions] == [
        "merged",
        "head 3",
    ]


def test_discover_review_stack_stops_at_recent_shared_trunk_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_trunk = _revision_line(
        commit_id="current-trunk",
        parents=["old-trunk"],
        change_id="trunk-change",
        description="main\n",
    )
    head = _revision_line(
        commit_id="head-4",
        parents=["old-trunk"],
        change_id="head-4-change",
        description="head 4\n",
    )
    old_trunk = _revision_line(
        commit_id="old-trunk",
        parents=["root"],
        change_id="old-trunk-change",
        description="old trunk\n",
        immutable=True,
    )
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head-4"): _selection_scan_response(
            (current_trunk, True, False),
            (head, False, True),
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head-4') & ::'current-trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): old_trunk,
        ("jj", "log", "--no-graph", "-r", "'old-trunk'::'head-4'", "-T", _template()): (
            head + old_trunk
        ),
    }

    stack = _client(monkeypatch, responses).discover_review_stack(
        "head-4",
        allow_immutable=True,
    )

    assert [revision.subject for revision in stack.revisions] == ["head 4"]


def test_discover_review_stack_rejects_root_shared_trunk_ancestor_without_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_trunk = _revision_line(
        commit_id="current-trunk",
        parents=["root"],
        change_id="trunk-change",
        description="main\n",
    )
    head = _revision_line(
        commit_id="head-4",
        parents=["root"],
        change_id="head-4-change",
        description="head 4\n",
    )
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head-4"): _selection_scan_response(
            (current_trunk, True, False),
            (head, False, True),
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "heads(first_ancestors('head-4') & ::'current-trunk')",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _ROOT,
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(UnsupportedStackError, match=r"root commit before trunk\(\)"):
        client.discover_review_stack("head-4")


def test_discover_review_stack_rejects_hidden_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("hidden"): _selection_scan_response(
            (_TRUNK, True, False),
            (_HIDDEN, False, True),
        ),
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(UnsupportedStackError, match="hidden commits are not reviewable"):
        client.discover_review_stack("hidden")


@pytest.mark.parametrize(
    ("malformed_line", "expected_message"),
    [
        pytest.param(
            "not\tenough\n",
            "unexpected format",
            id="wrong-field-count",
        ),
        pytest.param(
            'NOT_JSON\t"commit-id"\t"desc"\t[]\tfalse\tfalse\tfalse\tfalse\tfalse'
            "\tfalse\tfalse\ttrue\n",
            "invalid JSON",
            id="invalid-json",
        ),
        pytest.param(
            '"change-id"\t'
            '"commit-id"\t'
            '"desc"\t'
            '"not-a-list"\t'
            "false\tfalse\tfalse\tfalse\tfalse\tfalse\tfalse\ttrue\n",
            "unexpected field types",
            id="wrong-field-type",
        ),
    ],
)
def test_discover_review_stack_raises_jj_command_error_on_malformed_output(
    malformed_line: str,
    expected_message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: dict[tuple[str, ...], str] = {
        _selection_scan_command("head"): (
            _revision_with_two_flags_line(_TRUNK, is_trunk=True, is_selected=False)
            + malformed_line
        ),
    }

    client = _client(monkeypatch, responses)
    with pytest.raises(JjCommandError, match=expected_message):
        client.discover_review_stack("head")


def test_discover_review_stack_surfaces_stale_workspace_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == (
            "jj",
            "--ignore-working-copy",
            "log",
            "--no-graph",
            "-r",
            "trunk() | (head)",
            "-T",
            _selection_scan_template("head"),
        )
        assert Path(kwargs["cwd"]) == Path("/repo")
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
    client = JjClient(Path("/repo"))
    with pytest.raises(StaleWorkspaceError, match="jj workspace update-stale"):
        client.discover_review_stack("head")


def test_resolve_color_when_honors_explicit_jj_config(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "ui.color"): "debug\n",
    }

    value = _client(monkeypatch, responses).resolve_color_when(stdout_is_tty=True)

    assert value == "debug"


def test_resolve_color_when_maps_auto_to_terminal_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    client = JjClient(Path("/repo"))

    assert client.resolve_color_when(stdout_is_tty=True) == "always"
    assert client.resolve_color_when(stdout_is_tty=False) == "never"


def test_resolve_color_when_cli_override_beats_jj_config(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "ui.color"): "debug\n",
    }

    client = _client(monkeypatch, responses)

    assert client.resolve_color_when(cli_color="never", stdout_is_tty=True) == "never"
    assert client.resolve_color_when(cli_color="auto", stdout_is_tty=False) == "never"
    assert client.resolve_color_when(cli_color="auto", stdout_is_tty=True) == "always"


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
        ): _HEAD,
    }

    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="head\n"),
        make_revision(commit_id="parent", change_id="parent-change", description="parent\n"),
    )
    result = _client(monkeypatch, responses).find_private_commits(revisions)

    assert len(result) == 1
    assert result[0].commit_id == "head"


def test_find_private_commits_skips_empty_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "git.private-commits"): "none()\n",
    }
    revisions = (make_revision(commit_id="head", change_id="head-change", description="head\n"),)

    result = _client(monkeypatch, responses).find_private_commits(revisions)

    assert result == ()


def test_query_paired_ancestor_membership_returns_subjects_in_one_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []
    candidate_a = _revision_line(
        commit_id="cand-a", parents=["trunk"], change_id="a-change", description="a\n"
    )
    candidate_b = _revision_line(
        commit_id="cand-b", parents=["cand-a"], change_id="b-change", description="b\n"
    )

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        seen_commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command, 0, stdout=candidate_a + candidate_b, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", runner)
    result = JjClient(Path("/repo")).query_paired_ancestor_membership(
        (("cand-a", "base-1"), ("cand-b", "base-2"), ("cand-c", "base-3")),
    )

    assert result == {"cand-a", "cand-b"}
    assert len(seen_commands) == 1, "all pairs must land in a single jj invocation"
    invocation = seen_commands[0]
    assert invocation[:4] == ("jj", "--ignore-working-copy", "log", "--no-graph")
    revset = invocation[invocation.index("-r") + 1]
    assert "('cand-a' & ::'base-1')" in revset
    assert "('cand-b' & ::'base-2')" in revset
    assert "('cand-c' & ::'base-3')" in revset


def test_push_bookmarks_issues_one_atomic_jj_invocation_for_a_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        seen_commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", runner)
    JjClient(Path("/repo")).push_bookmarks(
        remote="origin", bookmarks=("review/feat-1", "review/feat-2", "review/feat-3")
    )

    assert len(seen_commands) == 1, "all bookmarks must land in a single jj invocation"
    invocation = seen_commands[0]
    assert invocation[:3] == ("jj", "git", "push")
    assert "--remote" in invocation and "origin" in invocation
    assert invocation.count("--bookmark") == 3
    assert {"review/feat-1", "review/feat-2", "review/feat-3"}.issubset(set(invocation))


def _template() -> str:
    return (
        r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
        r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
        r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
        r'json(current_working_copy) ++ "\t" ++ json(self.hidden()) ++ "\t" ++ '
        r'json(immutable) ++ "\t" ++ json(self.conflict()) ++ "\n"'
    )


def _trunk_scan_template() -> str:
    return _scan_template_prefix() + r'json(self.contained_in("trunk()")) ++ "\n"'


def _selection_scan_template(selection_revset: str) -> str:
    return (
        _scan_template_prefix()
        + r'json(self.contained_in("trunk()")) ++ "\t" ++ json(self.contained_in('
        + json.dumps(selection_revset)
        + r')) ++ "\n"'
    )


def _scan_template_prefix() -> str:
    return _template().removesuffix(r'"\n"') + r'"\t" ++ '


def _revision_with_flag_line(revision_line: str, *, is_trunk: bool) -> str:
    return revision_line.removesuffix("\n") + f"\t{'true' if is_trunk else 'false'}\n"


def _revision_with_two_flags_line(
    revision_line: str,
    *,
    is_trunk: bool,
    is_selected: bool,
) -> str:
    return (
        revision_line.removesuffix("\n")
        + f"\t{'true' if is_trunk else 'false'}\t"
        + f"{'true' if is_selected else 'false'}\n"
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
            (key[0], *key[2:])
            if len(key) > 1 and key[1] == "--ignore-working-copy"
            else key
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
