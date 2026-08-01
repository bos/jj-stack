from __future__ import annotations

import jj_stack.console as console
import jj_stack.github.resolution as github_resolution
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest, GithubStack, GithubStackPullRequest
from jj_stack.review.convergence import dependent_path_commands
from jj_stack.review.finish import (
    FinishContext,
    finish_exit_code,
    finish_reviews,
    render_finish_results,
    retire_reviews,
)
from jj_stack.review.native_sync import observe_native_stacks
from jj_stack.review.observation import duplicate_review_claim_change_ids
from jj_stack.review.trunk_evidence import (
    CommitAncestry,
    TrackedReview,
    classify_commit_ancestries,
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
        dry_run=dry_run,
        on_isolation_change=report_fetch_isolation,
    )
    trunk = context.jj_client.resolve_revision("trunk()")
    state = context.state_store.load()
    had_failure = bool(state.record_issues)
    for issue in state.record_issues:
        condition = "incomplete" if issue.validation_error.endswith(" is missing.") else "invalid"
        component = (
            "pull request details are"
            if issue.record_type == "review_identity"
            else "last submitted commit is"
        )
        console.warning(
            t"Skip {ui.change_id(issue.change_id)}: its saved {component} {condition}; "
            t"repair the tracking with {ui.cmd('relink')}."
        )
    all_candidates = state.tracked_reviews()
    duplicate_change_ids = duplicate_review_claim_change_ids(state.review_identities)
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
        pull_requests = await github.get_pull_requests_by_numbers_independently(
            pull_numbers=tuple(
                candidate.review_identity.pr_number for candidate in all_candidates
            )
        )
        native_stacks = await observe_native_stacks(github=github) if exact_candidates else ()
        merge_ancestry = classify_commit_ancestries(
            commit_ids=tuple(
                pull_request.merge_commit_sha
                for pull_request in pull_requests.values()
                if isinstance(pull_request, GithubPullRequest)
                and pull_request.merge_commit_sha is not None
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
                    duplicate=candidate.change_id in duplicate_change_ids,
                    merge_ancestry=merge_ancestry,
                    pull_request=pull_request,
                    repository=target.repository,
                )
                or had_failure
            )
        eligible_exact, terminal_required = _eligible_exact_candidates(
            candidates=exact_candidates,
            native_stacks=native_stacks,
            pull_requests=pull_requests,
            tracked_pull_numbers=frozenset(
                identity.pr_number
                for identity in state.review_identities.values()
                if identity.repository_key == target.repository.repository_key
            ),
        )
        had_failure = had_failure or len(eligible_exact) != len(exact_candidates)
        finish_context = FinishContext(
            command=context,
            dry_run=dry_run,
            github=github,
            remote_name=target.remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk.commit_id,
        )
        results = await finish_reviews(
            candidates=eligible_exact,
            finish=finish_context,
            skip_finish=terminal_required,
        )
        results = await retire_reviews(
            evidence={candidate.change_id: "exact" for candidate in eligible_exact},
            finish_results=results,
            finish=finish_context,
            terminal_required=terminal_required,
        )
    render_finish_results(dry_run=dry_run, results=results)
    blocked = had_failure or any(result.outcome == "skipped" for result in results)
    return finish_exit_code(base=1 if blocked else 0, results=results)


def _eligible_exact_candidates(
    candidates: tuple[TrackedReview, ...],
    native_stacks: tuple[GithubStack, ...],
    pull_requests: dict[int, GithubPullRequest | GithubClientError | None],
    tracked_pull_numbers: frozenset[int],
) -> tuple[tuple[TrackedReview, ...], frozenset[str]]:
    members = [member for stack in native_stacks for member in stack.pull_requests]
    eligible: list[TrackedReview] = []
    terminal_required: set[str] = set()
    for candidate in candidates:
        number = candidate.review_identity.pr_number
        matching = [member for member in members if member.number == number]
        if not matching:
            eligible.append(candidate)
            continue
        reason = _github_stack_blocker(
            matching=matching,
            native_stacks=native_stacks,
            number=number,
            pull_request=pull_requests.get(number),
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
    native_stacks: tuple[GithubStack, ...],
    number: int,
    pull_request: object,
    tracked_pull_numbers: frozenset[int],
) -> Message | None:
    """Explain why a GitHub stack member cannot be retired repository-wide.

    Each cause is reported on its own. Naming them together left the reader to guess which of
    them applied when the code already knew.
    """

    if not matching[0].is_historical:
        return t"GitHub still lists PR #{number} as an active member of its stack"
    if not isinstance(pull_request, GithubPullRequest):
        return t"GitHub did not return usable data for PR #{number}"
    if pull_request.normalize_state().state != "merged":
        return t"GitHub does not report PR #{number} merged"
    if any(
        number in stack.pull_request_numbers
        and not set(stack.active_pull_request_numbers).isdisjoint(tracked_pull_numbers)
        for stack in native_stacks
    ):
        return t"PR #{number} is in a GitHub stack that still has active members tracked here"
    return None


def _report_global_nonexact_candidate(
    *,
    ancestry: CommitAncestry,
    candidate: TrackedReview,
    context: CommandContext,
    duplicate: bool,
    merge_ancestry: dict[str, CommitAncestry],
    pull_request: GithubPullRequest | GithubClientError | None,
    repository: github_resolution.GithubRepoAddress,
) -> bool:
    if duplicate:
        return _warn_global_preserved(candidate, "another tracked change claims the same review")
    if not isinstance(pull_request, GithubPullRequest):
        reason = (
            t"could not inspect its current review: {pull_request}"
            if isinstance(pull_request, GithubClientError)
            else t"GitHub no longer reports PR #{candidate.review_identity.pr_number}"
        )
        return _warn_global_preserved(candidate, reason)
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
