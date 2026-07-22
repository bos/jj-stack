"""Fresh policy-free facts used to authorize review mutations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

import jj_stack.github.resolution as github_resolution
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.bookmarks import GitRemote
from jj_stack.models.github import GithubPullRequest, GithubRepository
from jj_stack.models.review_state import ReviewIdentity, SubmittedBaseline
from jj_stack.models.stack import LocalRevision


@dataclass(frozen=True, slots=True)
class ReviewObservation:
    """Fresh facts for one change ID; no field is an authorization decision."""

    baseline: SubmittedBaseline | None
    head_pull_requests: tuple[GithubPullRequest, ...]
    identity: ReviewIdentity | None
    local_revision: LocalRevision | None
    pull_request: GithubPullRequest | None
    remote_review_target: str | None


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    """One fresh repository observation shared by a single mutation policy."""

    configured_repository: GithubRepoAddress | None
    duplicate_claim_change_ids: frozenset[str]
    fetched_trunk: LocalRevision | None
    github_repository: GithubRepository
    remote: GitRemote | None
    remote_trunk_target: str | None
    reviews: Mapping[str, ReviewObservation]


async def observe_review_mutation(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
    trunk_branch: str,
) -> RepositoryObservation:
    """Reload facts needed by one immediately following review mutation."""

    ordered_change_ids = tuple(dict.fromkeys(change_ids))
    remotes = {remote.name: remote for remote in context.jj_client.list_git_remotes()}
    remote = remotes.get(remote_name)
    configured_repository = (
        github_resolution.parse_github_repo(remote) if remote is not None else None
    )
    state = context.state_store.load()
    claims = tuple(
        (change_id, repository, kind, target)
        for change_id, identity in state.review_identities.items()
        for repository in (
            f"{identity.github_host.casefold()}/{identity.repository_owner.casefold()}/"
            f"{identity.repository_name.casefold()}",
        )
        for kind, target in (
            ("pr", str(identity.pr_number)),
            ("head", f"{identity.head_owner.casefold()}:{identity.head_ref}"),
        )
    )
    claim_counts = Counter((repository, kind, target) for _, repository, kind, target in claims)
    duplicate_claim_change_ids = frozenset(
        change_id
        for change_id, repository, kind, target in claims
        if claim_counts[(repository, kind, target)] > 1
    )
    identities = {
        change_id: state.review_identities.get(change_id) for change_id in ordered_change_ids
    }
    head_refs = tuple(
        dict.fromkeys(
            identity.head_ref for identity in identities.values() if identity is not None
        )
    )
    pull_numbers = tuple(
        dict.fromkeys(
            identity.pr_number for identity in identities.values() if identity is not None
        )
    )

    github_repository = await github_client.get_repository()
    pull_requests = await github_client.get_pull_requests_by_numbers(
        pull_numbers=pull_numbers,
    )
    pull_requests_by_head = await github_client.get_pull_requests_by_head_refs(
        head_refs=head_refs,
    )

    remote_targets: dict[str, str] = {}
    if remote is not None:
        remote_targets = context.jj_client.list_remote_branches(
            remote=remote.url,
            patterns=tuple(f"refs/heads/{ref}" for ref in (trunk_branch, *head_refs)),
        )
    reviews: dict[str, ReviewObservation] = {}
    for change_id in ordered_change_ids:
        identity = identities[change_id]
        local_revision: LocalRevision | None
        try:
            local_revision = context.jj_client.resolve_revision(change_id)
        except CliError:
            local_revision = None
        head_ref = identity.head_ref if identity is not None else None
        reviews[change_id] = ReviewObservation(
            baseline=state.submitted_baselines.get(change_id),
            head_pull_requests=(
                pull_requests_by_head.get(head_ref, ()) if head_ref is not None else ()
            ),
            identity=identity,
            local_revision=local_revision,
            pull_request=(
                pull_requests.get(identity.pr_number) if identity is not None else None
            ),
            remote_review_target=(remote_targets.get(head_ref) if head_ref is not None else None),
        )

    try:
        fetched_trunk = context.jj_client.resolve_revision("trunk()")
    except CliError:
        fetched_trunk = None
    return RepositoryObservation(
        configured_repository=configured_repository,
        duplicate_claim_change_ids=duplicate_claim_change_ids,
        fetched_trunk=fetched_trunk,
        github_repository=github_repository,
        remote=remote,
        remote_trunk_target=remote_targets.get(trunk_branch),
        reviews=reviews,
    )
