from __future__ import annotations

import asyncio
from dataclasses import dataclass

import jj_stack.console as console
import jj_stack.github.resolution as github_resolution
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands.cleanup.command import cleanup_tracked_reviews
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.models.github import (
    GithubPullRequest,
    GithubRepository,
    GithubStack,
    GithubStackPullRequest,
)
from jj_stack.models.stack import LocalRevision
from jj_stack.review.branches import is_review_branch
from jj_stack.review.finish import (
    FinishContext,
    finish_reviews,
    render_finish_results,
)
from jj_stack.review.github_stack_sync import observe_github_stacks
from jj_stack.review.path import SelectedReviewPath
from jj_stack.review.repository import observe_repository_paths
from jj_stack.review.trunk_evidence import (
    CommitAncestry,
    TrackedReview,
    classify_commit_ancestries,
    classify_exact_snapshot,
    classify_rewritten_result,
)
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class GlobalRecoveryResult:
    exit_code: int
    sync_change_ids: tuple[str, ...]
    trunk_branch: str | None


async def run_global_recovery(*, context: CommandContext, dry_run: bool) -> GlobalRecoveryResult:
    target = github_resolution.resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, github_resolution.GithubTarget):
        raise CliError(
            target.github_repository_error or "Could not resolve GitHub target.",
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
    with console.spinner(description="Fetching trunk") as progress:
        previous_trunk = context.jj_client.resolve_revision("trunk()")
        trunk_branches = tuple(
            branch
            for branch in context.jj_client.remote_bookmarks_at_revision(
                remote=target.remote.name,
                revision=previous_trunk.commit_id,
            )
            if not is_review_branch(branch)
        )
        context.jj_client.fetch_remote(
            branches=trunk_branches,
            remote=target.remote.name,
        )
        progress.update("Comparing pull requests with trunk")
        trunk = context.jj_client.resolve_revision("trunk()")
        state = context.state_store.load()
        had_failure = False
        sync_change_ids: list[str] = []
        all_candidates = state.tracked_reviews()
        ancestry_by_commit_id = classify_commit_ancestries(
            commit_ids=tuple(
                candidate.submitted_baseline.commit_id for candidate in all_candidates
            ),
            context=context,
            trunk_commit_id=trunk.commit_id,
        )
        exact_candidates = tuple(
            candidate
            for candidate in all_candidates
            if ancestry_by_commit_id[candidate.submitted_baseline.commit_id] == "on_trunk"
        )
        nonexact_candidates = tuple(
            candidate
            for candidate in all_candidates
            if ancestry_by_commit_id[candidate.submitted_baseline.commit_id] != "on_trunk"
        )
    local_copies, paths = _observe_local_recovery_paths(
        candidates=all_candidates,
        context=context,
    )
    detached_exact: list[TrackedReview] = []
    for candidate in exact_candidates:
        heads = _candidate_path_heads(candidate, local_copies=local_copies, paths=paths)
        if heads is None:
            if any(
                not revision.immutable or revision.is_working_copy
                for revision in local_copies[candidate.change_id]
            ):
                had_failure = (
                    _warn_global_preserved(candidate, "local history is not a supported stack")
                    or had_failure
                )
            else:
                detached_exact.append(candidate)
        elif heads:
            sync_change_ids.extend(heads)
        else:
            detached_exact.append(candidate)
    async with build_github_client(repository=target.repository) as github:
        pull_request_count = len(nonexact_candidates) + len(detached_exact)
        with console.spinner(
            description=f"Inspecting {pull_request_count} pull requests"
        ) as progress:
            try:
                pull_numbers = tuple(
                    candidate.review_identity.pr_number
                    for candidate in (*nonexact_candidates, *detached_exact)
                )
                if detached_exact:
                    repository_state, pull_requests, github_stacks = await asyncio.gather(
                        github.get_repository(),
                        github.get_pull_requests_by_numbers(pull_numbers=pull_numbers),
                        observe_github_stacks(github=github),
                    )
                else:
                    repository_state, pull_requests = await asyncio.gather(
                        github.get_repository(),
                        github.get_pull_requests_by_numbers(pull_numbers=pull_numbers),
                    )
                    github_stacks = ()
            except GithubClientError as error:
                raise CliError("Could not inspect pull requests") from error
            progress.update("Checking GitHub merge results")
            merge_ancestry = classify_commit_ancestries(
                commit_ids=tuple(
                    pull_request.merge_commit_sha
                    for pull_request in pull_requests.values()
                    if pull_request is not None and pull_request.merge_commit_sha is not None
                ),
                context=context,
                trunk_commit_id=trunk.commit_id,
            )
        rewritten_cleanup: list[str] = []
        for candidate in nonexact_candidates:
            ancestry = ancestry_by_commit_id[candidate.submitted_baseline.commit_id]
            pull_request = pull_requests.get(candidate.review_identity.pr_number)
            had_failure = (
                _report_global_nonexact_candidate(
                    ancestry=ancestry,
                    candidate=candidate,
                    local_copies=local_copies,
                    merge_ancestry=merge_ancestry,
                    paths=paths,
                    pull_request=pull_request,
                    repository=target.repository,
                    rewritten_cleanup=rewritten_cleanup,
                    sync_change_ids=sync_change_ids,
                )
                or had_failure
            )
        trunk_branch = _resolve_global_trunk_branch(
            context=context,
            repository_state=repository_state,
            required=bool(detached_exact or sync_change_ids),
            target=target,
            trunk_commit_id=trunk.commit_id,
        )
        if not detached_exact:
            results = ()
        else:
            assert trunk_branch is not None
            eligible_exact, terminal_required = _eligible_exact_candidates(
                candidates=tuple(detached_exact),
                github_stacks=github_stacks,
                pull_requests=pull_requests,
                repository=target.repository,
                tracked_pull_numbers=frozenset(
                    identity.pr_number
                    for identity in state.review_identities.values()
                    if identity.repository_key == target.repository.repository_key
                ),
            )
            had_failure = had_failure or len(eligible_exact) != len(detached_exact)
            finish_context = FinishContext(
                dry_run=dry_run,
                github=github,
                trunk_branch=trunk_branch,
            )
            results = await finish_reviews(
                candidates=eligible_exact,
                finish=finish_context,
                pull_requests={
                    candidate.review_identity.pr_number: pull_request
                    for candidate in eligible_exact
                    if (pull_request := pull_requests.get(candidate.review_identity.pr_number))
                    is not None
                },
                skip_finish=terminal_required,
            )
            render_finish_results(dry_run=dry_run, results=results)
        cleanup_result = await cleanup_tracked_reviews(
            change_ids=(
                *(
                    result.candidate.change_id
                    for result in results
                    if result.outcome != "skipped"
                ),
                *rewritten_cleanup,
            ),
            context=context,
            dry_run=dry_run,
            github_client=github,
            github_target=target,
            planned_detached_dependents=frozenset(
                result.candidate.review_identity.pr_number for result in results
            ),
        )
    blocked = (
        had_failure
        or any(result.outcome == "skipped" for result in results)
        or any(action.status == "blocked" for action in cleanup_result.actions)
    )
    return GlobalRecoveryResult(
        exit_code=1 if blocked else 0,
        sync_change_ids=tuple(dict.fromkeys(sync_change_ids)),
        trunk_branch=trunk_branch,
    )


def _resolve_global_trunk_branch(
    *,
    context: CommandContext,
    repository_state: GithubRepository,
    required: bool,
    target: github_resolution.GithubTarget,
    trunk_commit_id: str,
) -> str | None:
    if not required:
        return None
    branch, _targets = github_resolution.resolve_trunk_branch(
        client=context.jj_client,
        github_repository_state=repository_state,
        remote=target.remote,
        trunk_commit_id=trunk_commit_id,
    )
    return branch


def _observe_local_recovery_paths(
    *,
    candidates: tuple[TrackedReview, ...],
    context: CommandContext,
) -> tuple[dict[str, tuple[LocalRevision, ...]], tuple[SelectedReviewPath, ...]]:
    local_copies = context.jj_client.query_revisions_by_change_ids(
        tuple(candidate.change_id for candidate in candidates),
        off_trunk=True,
    )
    anchors = tuple(
        revision.commit_id for revisions in local_copies.values() for revision in revisions
    )
    if not anchors:
        return local_copies, ()
    paths = observe_repository_paths(
        jj_client=context.jj_client,
        descendant_of=anchors,
        exclude_trunk_descendants=True,
        include_working_copies=True,
        state=context.state_store.load(),
    ).paths
    return local_copies, paths


def _candidate_path_heads(
    candidate: TrackedReview,
    *,
    local_copies: dict[str, tuple[LocalRevision, ...]],
    paths: tuple[SelectedReviewPath, ...],
) -> tuple[str, ...] | None:
    copy_commit_ids = {revision.commit_id for revision in local_copies[candidate.change_id]}
    if not copy_commit_ids:
        return ()
    heads = tuple(
        path.stack.head.change_id
        for path in paths
        if any(revision.commit_id in copy_commit_ids for revision in path.stack.revisions)
    )
    return heads or None


def _eligible_exact_candidates(
    candidates: tuple[TrackedReview, ...],
    github_stacks: tuple[GithubStack, ...],
    pull_requests: dict[int, GithubPullRequest | None],
    repository: github_resolution.GithubRepoAddress,
    tracked_pull_numbers: frozenset[int],
) -> tuple[tuple[TrackedReview, ...], frozenset[str]]:
    members = [member for stack in github_stacks for member in stack.pull_requests]
    eligible: list[TrackedReview] = []
    terminal_required: set[str] = set()
    for candidate in candidates:
        number = candidate.review_identity.pr_number
        pull_request = pull_requests.get(number)
        if pull_request is None:
            _warn_global_preserved(candidate, t"GitHub no longer reports PR #{number}")
            continue
        evidence = classify_exact_snapshot(
            ancestry="on_trunk",
            candidate=candidate,
            pull_request=pull_request,
            repository=repository,
        )
        if not evidence.on_trunk:
            _warn_global_preserved(
                candidate,
                evidence.reason or "the submitted review no longer matches",
            )
            continue
        matching = [member for member in members if member.number == number]
        if not matching:
            eligible.append(candidate)
            continue
        reason = _github_stack_blocker(
            matching=matching,
            github_stacks=github_stacks,
            number=number,
            tracked_pull_numbers=tracked_pull_numbers,
        )
        if reason is not None:
            _warn_global_preserved(candidate, reason)
            continue
        eligible.append(candidate)
        terminal_required.add(candidate.change_id)
    return tuple(eligible), frozenset(terminal_required)


def _github_stack_blocker(
    *,
    matching: list[GithubStackPullRequest],
    github_stacks: tuple[GithubStack, ...],
    number: int,
    tracked_pull_numbers: frozenset[int],
) -> Message | None:
    """Explain why a GitHub stack member cannot be finished repository-wide.

    Each cause is reported on its own. Naming them together left the reader to guess which of
    them applied when the code already knew.
    """

    if not matching[0].is_historical:
        return t"GitHub still lists PR #{number} as an active member of its stack"
    if any(
        number in stack.pull_request_numbers
        and not set(stack.active_pull_request_numbers).isdisjoint(tracked_pull_numbers)
        for stack in github_stacks
    ):
        return t"PR #{number} is in a GitHub stack that still has active members tracked here"
    return None


def _report_global_nonexact_candidate(
    *,
    ancestry: CommitAncestry,
    candidate: TrackedReview,
    local_copies: dict[str, tuple[LocalRevision, ...]],
    merge_ancestry: dict[str, CommitAncestry],
    paths: tuple[SelectedReviewPath, ...],
    pull_request: GithubPullRequest | None,
    repository: github_resolution.GithubRepoAddress,
    rewritten_cleanup: list[str],
    sync_change_ids: list[str],
) -> bool:
    if ancestry == "on_trunk":
        return False
    if pull_request is None:
        return _warn_global_preserved(
            candidate, t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
        )
    rewritten = classify_rewritten_result(
        candidate=candidate,
        merge_result_ancestry=merge_ancestry.get(pull_request.merge_commit_sha or ""),
        pull_request=pull_request,
        repository=repository,
    )
    if rewritten.on_trunk:
        heads = _candidate_path_heads(candidate, local_copies=local_copies, paths=paths)
        if heads is None:
            return _warn_global_preserved(candidate, "local history is not a supported stack")
        if heads:
            sync_change_ids.extend(heads)
        else:
            rewritten_cleanup.append(candidate.change_id)
        return False
    if rewritten.review_mismatch and rewritten.reason is not None:
        return _warn_global_preserved(candidate, rewritten.reason)
    if pull_request.normalize_state().state != "open" and rewritten.reason is not None:
        return _warn_global_preserved(candidate, rewritten.reason)
    if ancestry == "unresolved":
        return _warn_global_preserved(candidate, "the submitted commit is unavailable locally")
    return False


def _warn_global_preserved(candidate: TrackedReview, reason: Message) -> bool:
    console.warning(
        t"Skipped PR #{candidate.review_identity.pr_number} for "
        t"{ui.change_id(candidate.change_id)}: {reason}."
    )
    return True
