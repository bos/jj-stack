"""Fresh review observations used to check mutations."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

import jj_stack.github.resolution as github_resolution
from jj_stack.bootstrap import CommandContext
from jj_stack.github.client import GithubClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPullRequest, GithubRepository
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import prepare_visible_review_snapshots
from jj_stack.state.store import ReviewStateStore


@dataclass(frozen=True, slots=True)
class ReviewObservation:
    """Fresh facts for one change ID; no field decides whether a mutation is safe."""

    baseline: SubmittedBaseline | None
    head_pull_requests: tuple[GithubPullRequest, ...]
    identity: ReviewIdentity | None
    local_revisions: tuple[LocalRevision, ...]
    pull_request: GithubPullRequest | None
    remote_review_target: str | None


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    """Fresh review facts shared by mutation policies."""

    configured_repository: github_resolution.GithubRepoAddress | None
    fetched_trunk_commit_id: str | None
    github_repository: GithubRepository | None
    open_pull_requests_by_base: Mapping[str, tuple[GithubPullRequest, ...]] | None
    remote: GitRemote | None
    remote_trunk_target: str | None
    repository: github_resolution.GithubRepoAddress
    reviews: Mapping[str, ReviewObservation]


def duplicate_review_claim_change_ids(
    identities: Mapping[str, ReviewIdentity],
) -> frozenset[str]:
    """Return every change participating in a duplicate PR or head claim."""

    values = identities.values()
    pr_claims = Counter((item.repository_key, item.pr_number) for item in values)
    head_claims = Counter(
        (item.repository_key, item.head_owner.casefold(), item.head_ref) for item in values
    )
    return frozenset(
        change_id
        for change_id, item in identities.items()
        if pr_claims[(item.repository_key, item.pr_number)] > 1
        or head_claims[(item.repository_key, item.head_owner.casefold(), item.head_ref)] > 1
    )


async def observe_reviews(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext | None = None,
    github_client: GithubClient,
    include_open_dependents: bool = False,
    remote_name: str | None = None,
    state_store: ReviewStateStore | None = None,
    trunk_branch: str | None = None,
) -> RepositoryObservation:
    """Reload observations needed by an immediately following mutation."""

    remotes = () if context is None else context.jj_client.list_git_remotes()
    remote = next((item for item in remotes if item.name == remote_name), None)
    store = context.state_store if context is not None else state_store
    assert store is not None
    state = store.load()
    identities = {
        change_id: state.review_identities.get(change_id)
        for change_id in dict.fromkeys(change_ids)
    }
    repository = github_client.repository
    known_identities = tuple(identity for identity in identities.values() if identity is not None)
    head_refs = tuple(dict.fromkeys(identity.head_ref for identity in known_identities))
    pull_numbers = tuple(dict.fromkeys(identity.pr_number for identity in known_identities))
    numbered, by_head, by_base, github_repository = await asyncio.gather(
        github_client.get_pull_requests_by_numbers(pull_numbers=pull_numbers),
        github_client.get_pull_requests_by_head_refs(head_refs=head_refs),
        (
            github_client.get_open_pull_requests_by_base_refs(base_refs=head_refs)
            if include_open_dependents
            else asyncio.sleep(0, result=None)
        ),
        github_client.get_repository() if context is not None else asyncio.sleep(0, result=None),
    )
    remote_targets: dict[str, str] = {}
    remote_refs = tuple(
        dict.fromkeys((*((trunk_branch,) if trunk_branch is not None else ()), *head_refs))
    )
    if context is not None and remote is not None and remote_refs:
        remote_targets = context.jj_client.list_remote_branches(
            remote=remote.name,
            patterns=tuple(f"refs/heads/{ref}" for ref in remote_refs),
        )
    if context is None:
        local_revisions = {}
    else:
        prepare_visible_review_snapshots(jj_client=context.jj_client, state=state)
        local_revisions = context.jj_client.query_revisions_by_change_ids(tuple(identities))
    reviews = {
        change_id: ReviewObservation(
            baseline=state.submitted_baselines.get(change_id),
            head_pull_requests=(
                by_head.get(identity.head_ref, ()) if identity is not None else ()
            ),
            identity=identity,
            local_revisions=matches,
            pull_request=(numbered.get(identity.pr_number) if identity is not None else None),
            remote_review_target=(
                remote_targets.get(identity.head_ref) if identity is not None else None
            ),
        )
        for change_id, identity in identities.items()
        for matches in (local_revisions.get(change_id, ()),)
    }

    fetched_trunks = (
        context.jj_client.query_revisions("trunk()", limit=2) if context is not None else ()
    )
    fetched_commit = fetched_trunks[0].commit_id if len(fetched_trunks) == 1 else None
    return RepositoryObservation(
        configured_repository=github_resolution.parse_github_repo(remote) if remote else None,
        fetched_trunk_commit_id=fetched_commit,
        github_repository=github_repository,
        open_pull_requests_by_base=by_base,
        remote=remote,
        remote_trunk_target=remote_targets.get(trunk_branch) if trunk_branch else None,
        repository=repository,
        reviews=reviews,
    )
