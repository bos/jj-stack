from __future__ import annotations

import jj_stack.console as console
import jj_stack.github.resolution as github_resolution
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands.cleanup.command import cleanup_tracked_reviews
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest, GithubStack, GithubStackPullRequest
from jj_stack.review.convergence import dependent_path_commands
from jj_stack.review.finish import (
    FinishContext,
    finish_reviews,
    render_finish_results,
)
from jj_stack.review.github_stack_sync import observe_github_stacks
from jj_stack.review.trunk_evidence import (
    CommitAncestry,
    TrackedReview,
    classify_commit_ancestries,
    classify_exact_snapshot,
    classify_rewritten_result,
)
from jj_stack.ui import Message


async def run_global_recovery(*, context: CommandContext, dry_run: bool) -> int:
    target = github_resolution.resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, github_resolution.GithubTarget):
        raise CliError(
            target.github_repository_error or "Could not resolve GitHub target.",
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
    context.jj_client.fetch_remote(
        remote=target.remote.name,
    )
    trunk = context.jj_client.resolve_revision("trunk()")
    state = context.state_store.load()
    had_failure = False
    all_candidates = state.tracked_reviews()
    ancestry_by_commit_id = classify_commit_ancestries(
        commit_ids=tuple(candidate.submitted_baseline.commit_id for candidate in all_candidates),
        context=context,
        trunk_commit_id=trunk.commit_id,
    )
    exact_candidates = tuple(
        candidate
        for candidate in all_candidates
        if ancestry_by_commit_id[candidate.submitted_baseline.commit_id] == "on_trunk"
    )
    async with build_github_client(repository=target.repository) as github:
        repository_state = await github.get_repository()
        trunk_branch, _trunk_targets = github_resolution.resolve_trunk_branch(
            client=context.jj_client,
            github_repository_state=repository_state,
            remote=target.remote,
            trunk_commit_id=trunk.commit_id,
        )
        try:
            pull_requests = await github.get_pull_requests_by_numbers(
                pull_numbers=tuple(
                    candidate.review_identity.pr_number for candidate in all_candidates
                )
            )
        except GithubClientError as error:
            raise CliError("Could not inspect tracked pull requests") from error
        github_stacks = await observe_github_stacks(github=github) if exact_candidates else ()
        merge_ancestry = classify_commit_ancestries(
            commit_ids=tuple(
                pull_request.merge_commit_sha
                for pull_request in pull_requests.values()
                if pull_request is not None and pull_request.merge_commit_sha is not None
            ),
            context=context,
            trunk_commit_id=trunk.commit_id,
        )
        for candidate in all_candidates:
            ancestry = ancestry_by_commit_id[candidate.submitted_baseline.commit_id]
            if ancestry == "on_trunk":
                continue
            pull_request = pull_requests.get(candidate.review_identity.pr_number)
            had_failure = (
                _report_global_nonexact_candidate(
                    ancestry=ancestry,
                    candidate=candidate,
                    context=context,
                    merge_ancestry=merge_ancestry,
                    pull_request=pull_request,
                    repository=target.repository,
                )
                or had_failure
            )
        eligible_exact, terminal_required = _eligible_exact_candidates(
            candidates=exact_candidates,
            github_stacks=github_stacks,
            pull_requests=pull_requests,
            repository=target.repository,
            tracked_pull_numbers=frozenset(
                identity.pr_number
                for identity in state.review_identities.values()
                if identity.repository_key == target.repository.repository_key
            ),
        )
        had_failure = had_failure or len(eligible_exact) != len(exact_candidates)
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
            change_ids=tuple(
                result.candidate.change_id for result in results if result.outcome != "skipped"
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
    return 1 if blocked else 0


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
    context: CommandContext,
    merge_ancestry: dict[str, CommitAncestry],
    pull_request: GithubPullRequest | None,
    repository: github_resolution.GithubRepoAddress,
) -> bool:
    if pull_request is None:
        return _warn_global_preserved(
            candidate,
            t"GitHub no longer reports PR #{candidate.review_identity.pr_number}",
        )
    rewritten = classify_rewritten_result(
        candidate=candidate,
        merge_result_ancestry=merge_ancestry.get(pull_request.merge_commit_sha or ""),
        pull_request=pull_request,
        repository=repository,
    )
    if rewritten.on_trunk:
        try:
            commands = dependent_path_commands(
                ancestor_commit_id=candidate.submitted_baseline.commit_id,
                context=context,
            )
        except JjCommandError as error:
            return _warn_global_preserved(
                candidate, t"could not inspect other local stacks: {error}"
            )
        if commands is None:
            return _warn_global_preserved(
                candidate, "no local stack is available to finish cleanup"
            )
        console.warning(
            t"Leave {ui.change_id(candidate.change_id)} tracked: GitHub merged it as a "
            t"different commit; {commands}."
        )
        return False
    if rewritten.review_mismatch and rewritten.reason is not None:
        return _warn_global_preserved(candidate, rewritten.reason)
    if pull_request.normalize_state().state != "open" and rewritten.reason is not None:
        return _warn_global_preserved(candidate, rewritten.reason)
    if ancestry == "unresolved":
        return _warn_global_preserved(candidate, "the submitted commit is unavailable locally")
    return False


def _warn_global_preserved(candidate: TrackedReview, reason: Message) -> bool:
    console.warning(t"Leave {ui.change_id(candidate.change_id)} tracked: {reason}.")
    return True
