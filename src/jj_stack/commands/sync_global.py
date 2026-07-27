from __future__ import annotations

import jj_stack.console as console
import jj_stack.github.resolution as github_resolution
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._fetch_isolation import report_fetch_isolation
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.jj.client import JjCommandError
from jj_stack.models.github import GithubPullRequest, GithubStack
from jj_stack.review.convergence import dependent_path_commands
from jj_stack.review.landed import (
    FinalizationContext,
    LandedReviewResult,
    finalize_landed_reviews,
    landed_exit_code,
    render_landed_results,
    retire_landed_reviews,
)
from jj_stack.review.landed_evidence import (
    CommitAncestry,
    LandedReviewCandidate,
    classify_commit_ancestries,
    classify_rewritten_result,
    complete_review_candidates,
)
from jj_stack.review.native_sync import observe_native_stacks
from jj_stack.review.observation import duplicate_review_claim_change_ids
from jj_stack.ui import Message


async def run_global_recovery(*, context: CommandContext, dry_run: bool) -> int:
    target = github_resolution.resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, github_resolution.GithubTarget):
        raise CliError(target.github_repository_error or "Could not resolve GitHub target.")
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
    all_candidates = complete_review_candidates(state)
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
        native_stacks = (
            await observe_native_stacks(context=context, dry_run=dry_run, github=github)
            if exact_candidates
            else ()
        )
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
        authorized_exact, terminal_required = _authorize_exact_candidates(
            candidates=exact_candidates,
            native_stacks=native_stacks,
            pull_requests=pull_requests,
            tracked_pull_numbers=frozenset(
                identity.pr_number
                for identity in state.review_identities.values()
                if identity.repository_key == target.repository.repository_key
            ),
        )
        had_failure = had_failure or len(authorized_exact) != len(exact_candidates)
        finalizer = FinalizationContext(
            command=context,
            dry_run=dry_run,
            github=github,
            remote_name=target.remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=trunk.commit_id,
        )
        legacy_exact = tuple(
            candidate
            for candidate in authorized_exact
            if candidate.change_id not in terminal_required
        )
        legacy_results = iter(
            await finalize_landed_reviews(
                candidates=legacy_exact,
                finalizer=finalizer,
            )
        )
        results = tuple(
            LandedReviewResult(candidate=candidate, outcome="already_terminal")
            if candidate.change_id in terminal_required
            else next(legacy_results)
            for candidate in authorized_exact
        )
        results = await retire_landed_reviews(
            evidence={candidate.change_id: "exact" for candidate in authorized_exact},
            finalization_results=results,
            finalizer=finalizer,
            terminal_required=terminal_required,
        )
    render_landed_results(dry_run=dry_run, results=results)
    blocked = had_failure or any(result.outcome == "skipped" for result in results)
    return landed_exit_code(base=1 if blocked else 0, results=results)


def _authorize_exact_candidates(
    candidates: tuple[LandedReviewCandidate, ...],
    native_stacks: tuple[GithubStack, ...],
    pull_requests: dict[int, GithubPullRequest | GithubClientError | None],
    tracked_pull_numbers: frozenset[int],
) -> tuple[tuple[LandedReviewCandidate, ...], frozenset[str]]:
    members = [member for stack in native_stacks for member in stack.pull_requests]
    authorized: list[LandedReviewCandidate] = []
    terminal_required: set[str] = set()
    for candidate in candidates:
        number = candidate.review_identity.pr_number
        matching = [member for member in members if member.number == number]
        if not matching:
            authorized.append(candidate)
            continue
        pull_request = pull_requests.get(number)
        if (
            len(matching) != 1
            or not matching[0].is_historical
            or not isinstance(pull_request, GithubPullRequest)
            or pull_request.normalize_state().state != "merged"
            or any(
                number in stack.pull_request_numbers
                and not set(stack.active_pull_request_numbers).isdisjoint(tracked_pull_numbers)
                for stack in native_stacks
            )
        ):
            _warn_global_preserved(
                candidate,
                t"native member PR #{number} is not terminally merged, has ambiguous "
                t"membership, or has tracked active members",
            )
            continue
        authorized.append(candidate)
        terminal_required.add(candidate.change_id)
    return tuple(authorized), frozenset(terminal_required)


def _report_global_nonexact_candidate(
    *,
    ancestry: CommitAncestry,
    candidate: LandedReviewCandidate,
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
    if rewritten.state == "landed":
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
    if rewritten.state in {"head_mismatch", "identity_mismatch"}:
        return _warn_global_preserved(candidate, rewritten.reason or rewritten.state)
    if pull_request.normalize_state().state != "open":
        return _warn_global_preserved(
            candidate,
            rewritten.reason
            or t"PR #{pull_request.number} is {pull_request.normalize_state().state} "
            t"without a result on trunk",
        )
    if ancestry == "unresolved":
        return _warn_global_preserved(candidate, "the submitted commit is unavailable locally")
    return False


def _warn_global_preserved(candidate: LandedReviewCandidate, reason: Message) -> bool:
    console.warning(t"Leave {ui.change_id(candidate.change_id)} tracked: {reason}.")
    return True
