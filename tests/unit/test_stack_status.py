from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.stack import status as status_module
from jj_stack.stack.path import SelectedStackPath
from jj_stack.stack.status import (
    PreparedStatus,
    prepare_stack_for_status,
    prepare_status,
    stream_status_async,
)
from jj_stack.state.store import TrackingStore
from tests.support.change_helpers import make_change


def test_untracked_status_omits_branch_and_skips_remote_and_github_discovery(
    monkeypatch,
) -> None:
    change = make_change(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    client = _PrepareStatusClient(_stack_for_status(change))
    prepared = prepare_stack_for_status(
        context=_context(client=client, state=TrackingState()),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(change),
        state=TrackingState(),
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
    )

    async def fail_github_inspection(**_kwargs):
        if False:
            yield None
        raise AssertionError("untracked changes must not trigger GitHub inspection")

    monkeypatch.setattr(
        "jj_stack.stack.status._iter_status_changes_with_github",
        fail_github_inspection,
    )

    result = asyncio.run(
        stream_status_async(
            on_change=None,
            prepared_status=prepared_status,
        )
    )

    assert client.list_calls == []
    assert result.changes[0].branch is None
    assert result.changes[0].remote_target is None


def test_prepare_status_observes_only_exact_saved_pr_branches(monkeypatch) -> None:
    first = make_change(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    second = make_change(
        commit_id="commit-2",
        description="feature 2",
        change_id="bbbbbbbb5678",
    )
    state = TrackingState(
        pr_identities={
            first.change_id: _identity(head_ref="jj-stack/feature-1-aaaaaaaa"),
        },
        submitted_baselines={first.change_id: SubmittedBaseline(commit_id=first.commit_id)},
    )
    client = _PrepareStatusClient(
        _stack_for_status(first, second),
        remote_targets={"jj-stack/feature-1-aaaaaaaa": first.commit_id},
    )
    _patch_selected_path(monkeypatch, client=client, state=state)

    prepared = prepare_status(
        context=_context(client=client, state=state),
        revset=None,
    )

    assert client.list_calls == [("refs/heads/jj-stack/feature-1-aaaaaaaa",)]
    assert prepared.prepared.status_changes[0].branch == "jj-stack/feature-1-aaaaaaaa"
    assert prepared.prepared.status_changes[1].branch is None
    assert prepared.prepared.remote_targets == {"jj-stack/feature-1-aaaaaaaa": first.commit_id}


def test_stream_status_falls_back_to_local_data_after_github_abort(monkeypatch) -> None:
    change = make_change(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    state = TrackingState(
        pr_identities={
            change.change_id: _identity(
                head_ref="jj-stack/feature-1-aaaaaaaa",
                pr_number=1,
            )
        },
        submitted_baselines={change.change_id: SubmittedBaseline(commit_id=change.commit_id)},
    )
    client = _PrepareStatusClient(
        _stack_for_status(change),
        remote_targets={"jj-stack/feature-1-aaaaaaaa": change.commit_id},
    )
    prepared = prepare_stack_for_status(
        context=_context(client=client, state=state),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(change),
        state=state,
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
    )
    streamed: list[tuple[str, bool]] = []

    async def abort_github_inspection(**_kwargs):
        if False:
            yield None
        raise CliError("GitHub lookup failed")

    monkeypatch.setattr(
        "jj_stack.stack.status._iter_status_changes_with_github",
        abort_github_inspection,
    )

    result = asyncio.run(
        stream_status_async(
            on_change=lambda item, github_available: streamed.append(
                (item.change_id, github_available)
            ),
            prepared_status=prepared_status,
        )
    )

    assert streamed == [(change.change_id, False)]
    assert result.github_error == "GitHub lookup failed"
    assert result.incomplete is True
    assert result.changes[0].branch == "jj-stack/feature-1-aaaaaaaa"
    assert result.changes[0].remote_target == change.commit_id


def test_pr_lookup_falls_back_to_exact_remembered_pr_number() -> None:
    class FakeGithubClient:
        repo = GithubRepoAddress(
            owner="octo-org",
            repo="stacked-prs",
        )

        async def get_open_prs_by_head_refs(self, *, head_refs):
            assert head_refs == ("jj-stack/old-branch",)
            return {"jj-stack/old-branch": ()}

        async def get_prs_by_numbers(self, *, pr_numbers):
            assert pr_numbers == (7,)
            return {
                7: GithubPR.model_validate(
                    {
                        "base": {"ref": "jj-stack/base"},
                        "head": {
                            "label": "octo-org:jj-stack/old-branch",
                            "ref": "jj-stack/old-branch",
                        },
                        "html_url": "https://github.test/octo-org/stacked-prs/pull/7",
                        "merged_at": "2026-03-16T12:00:00Z",
                        "number": 7,
                        "state": "closed",
                        "title": "feature 7",
                    }
                )
            }

    prepared_change = SimpleNamespace(
        branch="jj-stack/old-branch",
        pr_identity=_identity(
            head_ref="jj-stack/old-branch",
            pr_number=7,
        ),
    )

    lookups = asyncio.run(
        status_module._discover_pr_lookups(
            github_client=cast(Any, FakeGithubClient()),
            prepared_changes=cast(Any, (prepared_change,)),
        )
    )

    lookup = lookups["jj-stack/old-branch"]
    assert lookup.source == "remembered"
    assert lookup.state == "closed"
    assert lookup.pr is not None
    assert lookup.pr.number == 7
    assert lookup.pr.state == "merged"


def test_pr_lookup_ignores_draft_review_decision() -> None:
    lookup = status_module._pr_lookup_from_discovered(
        head_label="octo-org:jj-stack/draft",
        prs=(
            GithubPR(
                base={"ref": "main"},
                draft=True,
                head={"ref": "jj-stack/draft"},
                html_url="https://github.test/octo-org/stacked-prs/pull/3",
                number=3,
                review_decision="approved",
                state="open",
                title="draft",
            ),
        ),
    )

    assert lookup.review_decision is None


_STATUS_REMOTE = GitRemote(
    name="origin",
    fetch_url="git@github.com:octo-org/stacked-prs.git",
    push_url="git@github.com:octo-org/stacked-prs.git",
)


def _github_target() -> GithubTarget:
    return GithubTarget(
        remote=_STATUS_REMOTE,
        repo=GithubRepoAddress(
            owner="octo-org",
            repo="stacked-prs",
        ),
    )


def _identity(
    *,
    head_ref: str = "jj-stack/change",
    pr_number: int = 1,
) -> PRIdentity:
    return PRIdentity(
        repo_owner="octo-org",
        repo_name="stacked-prs",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref=head_ref,
    )


def _stack_for_status(*changes: LocalCommit) -> LocalStack:
    trunk = make_change(
        commit_id="trunk",
        description="base",
        change_id="trunkchangeid",
    )
    return LocalStack(
        base_parent=trunk,
        head=changes[-1],
        changes=tuple(changes),
        selected_revset="@",
        trunk=trunk,
    )


class _PrepareStatusClient:
    def __init__(
        self,
        stack: LocalStack,
        *,
        remote_targets: dict[str, str] | None = None,
    ) -> None:
        self.list_calls: list[tuple[str, ...]] = []
        self.remote_targets = remote_targets or {}
        self.stack = stack

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        return (_STATUS_REMOTE,)

    def list_remote_branches(
        self,
        *,
        remote: str,
        patterns: tuple[str, ...],
    ) -> dict[str, str]:
        assert remote == "origin"
        self.list_calls.append(patterns)
        requested = {pattern.removeprefix("refs/heads/") for pattern in patterns}
        return {
            branch: target
            for branch, target in self.remote_targets.items()
            if branch in requested
        }


class _StateStoreStub:
    def __init__(self, state: TrackingState) -> None:
        self.state = state

    def load(self) -> TrackingState:
        return self.state


def _patch_selected_path(
    monkeypatch,
    *,
    client: _PrepareStatusClient,
    state: TrackingState,
) -> None:
    selected_path = SelectedStackPath(
        is_maximal=True,
        stack=client.stack,
    )
    monkeypatch.setattr(
        status_module,
        "select_stack_path",
        lambda **_kwargs: selected_path,
    )


def _context(
    *,
    client: _PrepareStatusClient,
    state: TrackingState | None = None,
) -> CommandContext:
    return cast(
        CommandContext,
        SimpleNamespace(
            jj_client=cast(JjClient, client),
            state_store=cast(TrackingStore, _StateStoreStub(state or TrackingState())),
        ),
    )
