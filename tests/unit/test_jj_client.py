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
    DriftError,
    resolve_exit_code,
)
from jj_stack.jj.client import (
    JjClient,
    JjCommandError,
    ReviewFetchIsolationRequired,
    ReviewRefUpdate,
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
    working_copy_workspaces: list[str] | None = None,
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
        json.dumps(
            working_copy_workspaces
            if working_copy_workspaces is not None
            else (["default"] if working_copy else [])
        ),
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


class _DivergentChangeIdRevsetClient(JjClient):
    def _query_revisions(
        self,
        revset: str,
        *,
        limit: int | None = None,
    ) -> list[LocalRevision]:
        raise JjCommandError(
            "jj log failed: Error: Change ID `zqrozzrmllru` is divergent\n"
            "Hint: Use change offset to select single revision: "
            "zqrozzrmllru/0, zqrozzrmllru/1"
        )


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
_UNDESCRIBED_WORKING_COPY = _revision_line(
    commit_id="wc",
    parents=["head"],
    change_id="wc-change",
    description="",
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


def test_resolve_revision_reports_divergent_selected_change_as_unsupported_stack() -> None:
    # Fetching a foreign branch that points at a rewritten change's old commit
    # resurrects the predecessor, so the change ID itself no longer resolves.
    # The raw jj error must become the same targeted divergent-change
    # diagnostic the stack walk produces, not an unadorned subprocess failure.
    client = _DivergentChangeIdRevsetClient(Path("/repo"))

    with pytest.raises(UnsupportedStackError, match="divergent changes are not supported") as exc:
        client.resolve_revision("zqrozzrmllru")

    assert exc.value.change_id == "zqrozzrmllru"
    assert exc.value.reason == "divergent_change"


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


def test_discover_review_stack_skips_an_undescribed_working_copy(
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
            + _revision_with_flag_line(_UNDESCRIBED_WORKING_COPY, is_trunk=False)
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


def test_discover_review_stack_includes_side_branch_boundary_merged_into_trunk(
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
            'NOT_JSON\t"commit-id"\t"desc"\t[]\tfalse\tfalse\tfalse\t[]\tfalse'
            "\tfalse\tfalse\tfalse\ttrue\n",
            "invalid JSON",
            id="invalid-json",
        ),
        pytest.param(
            '"change-id"\t'
            '"commit-id"\t'
            '"desc"\t'
            '"not-a-list"\t'
            "false\tfalse\tfalse\t[]\tfalse\tfalse\tfalse\tfalse\ttrue\n",
            "unexpected field types",
            id="wrong-parent-field-type",
        ),
        pytest.param(
            '"change-id"\t'
            '"commit-id"\t'
            '"desc"\t'
            "[]\t"
            'false\tfalse\tfalse\t"not-a-list"\tfalse\tfalse\tfalse\tfalse\ttrue\n',
            "unexpected field types",
            id="wrong-workspace-field-type",
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


@pytest.mark.landing_recovery
def test_query_present_commit_ancestor_membership_distinguishes_absent_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []
    on_trunk = _revision_line(
        commit_id="on-trunk",
        parents=["root"],
        change_id="on-trunk-change",
        description="on trunk\n",
    ).rstrip("\n")
    off_trunk = _revision_line(
        commit_id="off-trunk",
        parents=["root"],
        change_id="off-trunk-change",
        description="off trunk\n",
    ).rstrip("\n")

    def runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        seen_commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{on_trunk}\ttrue\n{off_trunk}\tfalse\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", runner)
    result = JjClient(Path("/repo")).query_present_commit_ancestor_membership(
        ("on-trunk", "off-trunk", "absent", "bad'commit"),
        descendant_commit_id="fetched-trunk",
    )

    assert result == {"on-trunk": True, "off-trunk": False}
    assert len(seen_commands) == 1
    invocation = seen_commands[0]
    revset = invocation[invocation.index("-r") + 1]
    assert "present('on-trunk')" in revset
    assert "present('off-trunk')" in revset
    assert "present('absent')" in revset
    assert """present("bad'commit")""" in revset
    template = invocation[invocation.index("-T") + 1]
    assert "contained_in" in template
    assert "fetched-trunk" in template

    def failing_runner(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert Path(kwargs["cwd"]) == Path("/repo")
        seen_commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="query failed")

    monkeypatch.setattr(subprocess, "run", failing_runner)
    failed = JjClient(Path("/repo")).query_present_commit_ancestor_membership(
        ("one", "two", "three"),
        descendant_commit_id="fetched-trunk",
    )
    assert failed == {}
    assert len(seen_commands) == 2, "a failed batch must not fan out into per-commit queries"


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
            "/repo/.git",
            "ls-remote",
            "--refs",
            "https://github.test/octo-org/repo.git",
            "refs/heads/review/feat",
        ):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="abc123\trefs/heads/review/feat\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {invocation!r}")

    monkeypatch.setattr(subprocess, "run", runner)
    result = JjClient(Path("/repo")).list_remote_branches(
        remote="origin", patterns=("refs/heads/review/feat",)
    )

    assert result == {"review/feat": "abc123"}
    assert seen_commands == [
        ("jj", "--ignore-working-copy", "git", "remote", "list"),
        ("jj", "--ignore-working-copy", "git", "root"),
        (
            "git",
            "--git-dir",
            "/repo/.git",
            "ls-remote",
            "--refs",
            "https://github.test/octo-org/repo.git",
            "refs/heads/review/feat",
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
            patterns=("refs/heads/review/feat",),
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
            "/repo/.git",
            "ls-remote",
            "--refs",
            remote_url,
            "refs/heads/review/feat",
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
            patterns=("refs/heads/review/feat",),
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
    changes = []

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
            "/repo/.git",
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
    with pytest.raises(ReviewFetchIsolationRequired):
        JjClient(Path("/repo")).ensure_review_fetch_isolation(
            remote="origin",
            dry_run=True,
            on_change=changes.append,
        )

    assert len(changes) == 1
    assert changes[0].status == "required"
    assert all("config --add" not in " ".join(command) for command in seen_commands)


def test_review_fetch_isolation_normalizes_duplicate_exclusions_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_refspec = "^refs/heads/review/*"
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
            "/repo/.git",
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
            "/repo/.git",
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
    changes = []

    def record_change(change) -> None:
        events.append("callback")
        changes.append(change)

    result = JjClient(Path("/repo")).ensure_review_fetch_isolation(
        remote="origin",
        on_change=record_change,
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
            "/repo/.git",
            "config",
            "--fixed-value",
            "--replace-all",
            "remote.origin.fetch",
            review_refspec,
            review_refspec,
        )
    ]
    assert events == ["initial-read", "replace", "post-read", "callback"]
    assert result.status == "applied"
    assert changes == [result]


def test_review_fetch_isolation_reports_the_effective_override_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        invocation = tuple(command)
        assert invocation[:4] == ("jj", "--ignore-working-copy", "config", "list")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='"repo"\t"/repo/.jj/repo/config.toml"\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", runner)

    with pytest.raises(CliError) as raised:
        JjClient(Path("/repo")).ensure_review_fetch_isolation(remote="origin")

    assert "/repo/.jj/repo/config.toml" in str(raised.value)
    assert "jj config unset --repo" in str(raised.value)


def test_imported_review_bookmark_scan_reports_every_reserved_namespace_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        json.dumps({"name": "review/not-managed", "target": ["one"]})
        + "\n"
        + json.dumps({"name": "review/feature-abcdefgh", "target": ["two"]})
        + "\n"
    )

    def runner(command: Sequence[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", runner)

    assert JjClient(Path("/repo")).list_imported_review_bookmarks() == (
        "review/feature-abcdefgh",
        "review/not-managed",
    )


def test_remote_review_ref_mutation_uses_one_atomic_exact_lease_push_and_rejects_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_commands: list[tuple[str, ...]] = []
    observed_target = "old"
    old_branch = "review/old-aaaaaaaa"
    new_branch = "review/new-bbbbbbbb"
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
                command, 0, stdout="^refs/heads/review/*\n", stderr=""
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
        if invocation[3:6] == (
            "ls-remote",
            "--refs",
            "https://github.test/octo-org/repo.git",
        ) and invocation[6:] in ((old_ref, new_ref), (old_ref,)):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{observed_target}\t{old_ref}\n",
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

    observed_target = "stale"
    with pytest.raises(DriftError, match="changed before the atomic push"):
        client.mutate_remote_review_refs(
            remote="origin",
            updates=(
                ReviewRefUpdate(
                    branch=old_branch,
                    expected_target="old",
                    desired_target="updated-again",
                ),
            ),
        )
    assert len([command for command in seen_commands if command[3:4] == ("push",)]) == 1


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
                command, 0, stdout="^refs/heads/review/*\n", stderr=""
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
    return (
        r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
        r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
        r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
        r'json(current_working_copy) ++ "\t" ++ '
        r'json(working_copies.map(|wc| wc.name())) ++ "\t" ++ '
        r'json(self.hidden()) ++ "\t" ++ '
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
