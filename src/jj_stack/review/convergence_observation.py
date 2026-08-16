"""Observe the additional repository facts needed to plan selected convergence."""

from __future__ import annotations

from dataclasses import replace

from jj_stack.bootstrap import CommandContext
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalRevision
from jj_stack.review.observation import RepositoryObservation, ReviewObservation, observe_reviews
from jj_stack.review.repository import observe_repository_paths


async def complete_sync_observation(
    *,
    context: CommandContext,
    github: GithubClient,
    initial: RepositoryObservation,
    remote_name: str,
    repository: GithubRepoAddress,
    selected: tuple[LocalRevision, ...],
    stacks: tuple[GithubStack, ...],
) -> tuple[RepositoryObservation, tuple[GithubStack, ...], bool]:
    state = context.state_store.load()
    selected_pulls = {
        identity.pr_number
        for revision in selected
        if (identity := state.review_identities.get(revision.change_id)) is not None
    }
    affected = tuple(
        stack for stack in stacks if not selected_pulls.isdisjoint(stack.pull_request_numbers)
    )
    resource_pulls = {number for stack in affected for number in stack.pull_request_numbers}
    tracked_pulls = {
        identity.pr_number
        for identity in state.review_identities.values()
        if identity.repository_key == repository.repository_key
    }
    if not any(
        _changed_review(observed, include_remote_target=False)
        for revision in selected
        if (observed := initial.reviews.get(revision.change_id)) is not None
    ) and (resource_pulls & tracked_pulls).issubset(selected_pulls):
        return initial, (), False
    change_ids = tuple(
        change_id
        for change_id, identity in state.review_identities.items()
        if identity.repository_key == repository.repository_key
        and identity.pr_number in resource_pulls
        and change_id in state.submitted_baselines
    )
    missing_ids = tuple(change_id for change_id in change_ids if change_id not in initial.reviews)
    missing = (
        await observe_reviews(
            change_ids=missing_ids,
            context=context,
            github_client=github,
            github_repository_snapshot=initial.github_repository,
            include_remote_targets=False,
            remote_name=remote_name,
        )
        if missing_ids
        else None
    )
    reviews = {**initial.reviews, **(missing.reviews if missing is not None else {})}
    identities = tuple(item.identity for item in reviews.values() if item.identity is not None)
    remote = initial.remote or (missing.remote if missing is not None else None)
    targets = (
        context.jj_client.list_remote_branches(
            remote=remote.name,
            patterns=tuple(f"refs/heads/{item.head_ref}" for item in identities),
        )
        if remote is not None and identities
        else {}
    )
    observation = replace(
        initial,
        reviews={
            change_id: replace(
                item,
                remote_review_target=(
                    targets.get(item.identity.head_ref) if item.identity is not None else None
                ),
            )
            for change_id, item in reviews.items()
        },
    )
    changed = any(_changed_review(item) for item in observation.reviews.values())
    return observation, stacks if changed else (), changed


def queued_pull_numbers(
    observation: RepositoryObservation,
    selected: tuple[LocalRevision, ...],
) -> tuple[int, ...]:
    return tuple(
        pull_request.number
        for revision in selected
        if (observed := observation.reviews.get(revision.change_id)) is not None
        and (pull_request := observed.pull_request) is not None
        and pull_request.normalize_state().state == "open"
        and pull_request.is_queued
    )


def dependent_path_heads(
    *,
    ancestor_commit_ids: tuple[str, ...],
    context: CommandContext,
    excluded_change_ids: frozenset[str],
) -> dict[str, tuple[LocalRevision, ...]]:
    if not ancestor_commit_ids:
        return {}
    paths = observe_repository_paths(
        jj_client=context.jj_client,
        namespace=context.review_namespace,
        descendant_of=ancestor_commit_ids,
        include_working_copies=True,
        state=context.state_store.load(),
    ).paths
    result: dict[str, tuple[LocalRevision, ...]] = {}
    for ancestor in ancestor_commit_ids:
        heads: dict[str, LocalRevision] = {}
        for path in paths:
            if not any(item.commit_id == ancestor for item in path.stack.revisions):
                continue
            head = next(
                (
                    revision
                    for revision in reversed(path.stack.revisions)
                    if revision.change_id not in excluded_change_ids
                ),
                None,
            )
            if head is not None:
                heads[head.commit_id] = head
        result[ancestor] = tuple(heads.values())
    return result


def _changed_review(
    observed: ReviewObservation,
    *,
    include_remote_target: bool = True,
) -> bool:
    pull_request = observed.pull_request
    if (baseline := observed.baseline) is None:
        return False
    if pull_request is None:
        return True
    return (
        pull_request.normalize_state().state == "merged"
        or pull_request.head.sha != baseline.commit_id
        or (include_remote_target and observed.remote_review_target != baseline.commit_id)
        or any(revision.immutable for revision in observed.local_revisions)
    )
