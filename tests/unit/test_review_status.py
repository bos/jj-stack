from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review import status as status_module
from jj_stack.review.status import (
    PreparedStatus,
    prepare_stack_for_status,
    prepare_status,
    stream_status_async,
)
from jj_stack.state.store import ReviewStateStore
from tests.support.revision_helpers import make_revision


def test_untracked_status_omits_branch_and_skips_remote_and_github_discovery(
    monkeypatch,
) -> None:
    revision = make_revision(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    client = _PrepareStatusClient(_stack_for_status(revision))
    prepared = prepare_stack_for_status(
        context=_context(client=client, state=ReviewState()),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(revision),
        state=ReviewState(),
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
        selected_revset="@",
        base_parent_subject="base",
    )

    async def fail_github_inspection(**_kwargs):
        if False:
            yield None
        raise AssertionError("untracked revisions must not trigger GitHub inspection")

    monkeypatch.setattr(
        "jj_stack.review.status._iter_status_revisions_with_github",
        fail_github_inspection,
    )

    result = asyncio.run(
        stream_status_async(
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert client.list_calls == []
    assert result.revisions[0].branch is None
    assert result.revisions[0].remote_target is None


def test_prepare_status_observes_only_exact_saved_review_branches() -> None:
    first = make_revision(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    second = make_revision(
        commit_id="commit-2",
        description="feature 2",
        change_id="bbbbbbbb5678",
    )
    state = ReviewState(
        review_identities={
            first.change_id: _identity(head_ref="jj-stack/feature-1-aaaaaaaa"),
        }
    )
    client = _PrepareStatusClient(
        _stack_for_status(first, second),
        remote_targets={"jj-stack/feature-1-aaaaaaaa": first.commit_id},
    )

    prepared = prepare_status(
        context=_context(client=client, state=state),
        revset=None,
    )

    assert client.list_calls == [("refs/heads/jj-stack/feature-1-aaaaaaaa",)]
    assert prepared.prepared.status_revisions[0].branch == "jj-stack/feature-1-aaaaaaaa"
    assert prepared.prepared.status_revisions[1].branch is None
    assert prepared.prepared.remote_targets == {"jj-stack/feature-1-aaaaaaaa": first.commit_id}


def test_prepare_status_reloads_saved_branch_after_fetch() -> None:
    revision = make_revision(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    stale_state = ReviewState(
        review_identities={revision.change_id: _identity(head_ref="jj-stack/stale", pr_number=1)}
    )
    refreshed_state = ReviewState(
        review_identities={
            revision.change_id: _identity(head_ref="jj-stack/refreshed", pr_number=2)
        }
    )
    client = _PrepareStatusClient(
        _stack_for_status(revision),
        remote_targets={"jj-stack/refreshed": revision.commit_id},
    )
    state_store = _StateStoreStub(stale_state, refreshed_state)

    prepared = prepare_status(
        context=_context(client=client, state_store=state_store),
        fetch_remote_state=True,
        revset=None,
    )

    assert state_store.loads == 2
    assert client.fetches == ["origin"]
    assert client.list_calls == [("refs/heads/jj-stack/refreshed",)]
    assert prepared.prepared.status_revisions[0].branch == "jj-stack/refreshed"


def test_stream_status_falls_back_to_local_data_after_github_abort(monkeypatch) -> None:
    revision = make_revision(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    state = ReviewState(
        review_identities={
            revision.change_id: _identity(
                head_ref="jj-stack/feature-1-aaaaaaaa",
                pr_number=1,
            )
        }
    )
    client = _PrepareStatusClient(
        _stack_for_status(revision),
        remote_targets={"jj-stack/feature-1-aaaaaaaa": revision.commit_id},
    )
    prepared = prepare_stack_for_status(
        context=_context(client=client, state=state),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(revision),
        state=state,
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
        selected_revset="@",
        base_parent_subject="base",
    )
    streamed: list[tuple[str, bool]] = []

    async def abort_github_inspection(**_kwargs):
        if False:
            yield None
        raise CliError("GitHub lookup failed")

    monkeypatch.setattr(
        "jj_stack.review.status._iter_status_revisions_with_github",
        abort_github_inspection,
    )

    result = asyncio.run(
        stream_status_async(
            on_revision=lambda item, github_available: streamed.append(
                (item.change_id, github_available)
            ),
            prepared_status=prepared_status,
        )
    )

    assert streamed == [(revision.change_id, False)]
    assert result.github_error == "GitHub lookup failed"
    assert result.incomplete is True
    assert result.revisions[0].branch == "jj-stack/feature-1-aaaaaaaa"
    assert result.revisions[0].remote_target == revision.commit_id


def test_pull_request_lookup_falls_back_to_exact_remembered_pr_number() -> None:
    class FakeGithubClient:
        repository = GithubRepoAddress(
            owner="octo-org",
            repo="stacked-review",
        )

        async def get_pull_requests_by_head_refs(self, *, head_refs):
            assert head_refs == ("jj-stack/old-branch",)
            return {"jj-stack/old-branch": ()}

        async def get_pull_requests_by_numbers(self, *, pull_numbers):
            assert pull_numbers == (7,)
            return {
                7: GithubPullRequest.model_validate(
                    {
                        "base": {"ref": "jj-stack/base"},
                        "head": {
                            "label": "octo-org:jj-stack/old-branch",
                            "ref": "jj-stack/old-branch",
                        },
                        "html_url": "https://github.test/octo-org/stacked-review/pull/7",
                        "merged_at": "2026-03-16T12:00:00Z",
                        "number": 7,
                        "state": "closed",
                        "title": "feature 7",
                    }
                )
            }

    prepared_revision = SimpleNamespace(
        branch="jj-stack/old-branch",
        review_identity=_identity(
            head_ref="jj-stack/old-branch",
            pr_number=7,
        ),
    )

    lookups = asyncio.run(
        status_module._discover_pull_request_lookups(
            github_client=cast(Any, FakeGithubClient()),
            prepared_revisions=cast(Any, (prepared_revision,)),
        )
    )

    lookup = lookups["jj-stack/old-branch"]
    assert lookup.source == "remembered"
    assert lookup.state == "closed"
    assert lookup.pull_request is not None
    assert lookup.pull_request.number == 7
    assert lookup.pull_request.state == "merged"


def test_pull_request_lookup_ignores_draft_review_decision() -> None:
    lookup = status_module._pull_request_lookup_from_discovered(
        head_label="octo-org:jj-stack/draft",
        pull_requests=(
            GithubPullRequest(
                base={"ref": "main"},
                draft=True,
                head={"ref": "jj-stack/draft"},
                html_url="https://github.test/octo-org/stacked-review/pull/3",
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
    fetch_url="git@github.com:octo-org/stacked-review.git",
    push_url="git@github.com:octo-org/stacked-review.git",
)


def _github_target() -> GithubTarget:
    return GithubTarget(
        remote=_STATUS_REMOTE,
        repository=GithubRepoAddress(
            owner="octo-org",
            repo="stacked-review",
        ),
    )


def _identity(
    *,
    head_ref: str = "jj-stack/change",
    pr_number: int = 1,
) -> ReviewIdentity:
    return ReviewIdentity(
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref=head_ref,
    )


def _stack_for_status(*revisions: LocalRevision) -> LocalStack:
    trunk = make_revision(
        commit_id="trunk",
        description="base",
        change_id="trunkchangeid",
    )
    return LocalStack(
        base_parent=trunk,
        head=revisions[-1],
        revisions=tuple(revisions),
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
        self.fetches: list[str] = []
        self.list_calls: list[tuple[str, ...]] = []
        self.remote_targets = remote_targets or {}
        self.stack = stack

    def query_revisions_with_membership(
        self,
        _revset,
        *,
        membership_revsets,
        **_kwargs,
    ):
        flag_count = len(membership_revsets)
        observed = tuple(
            revision.model_copy(
                update={
                    "parents": (
                        self.stack.trunk.commit_id
                        if index == 0
                        else self.stack.revisions[index - 1].commit_id,
                    )
                }
            )
            for index, revision in enumerate(self.stack.revisions)
        )
        return (
            (self.stack.trunk, (True, False, *([False] * (flag_count - 3)), True)),
            *(
                (
                    revision,
                    (
                        False,
                        revision.commit_id == self.stack.head.commit_id,
                        *([False] * (flag_count - 2)),
                    ),
                )
                for revision in observed
            ),
        )

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        return (_STATUS_REMOTE,)

    def fetch_remote(self, *, remote: str, **_kwargs) -> None:
        self.fetches.append(remote)

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
    def __init__(self, *states: ReviewState) -> None:
        self.loads = 0
        self.states = states

    def load(self) -> ReviewState:
        state = self.states[min(self.loads, len(self.states) - 1)]
        self.loads += 1
        return state


def _context(
    *,
    client: _PrepareStatusClient,
    state: ReviewState | None = None,
    state_store: _StateStoreStub | None = None,
) -> CommandContext:
    store = state_store or _StateStoreStub(state or ReviewState())
    return cast(
        CommandContext,
        SimpleNamespace(
            jj_client=cast(JjClient, client),
            state_store=cast(ReviewStateStore, store),
        ),
    )
