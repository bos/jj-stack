"""Connect existing GitHub pull requests to local jj changes.

With --pull-request --fetch, fetch the reviewed commits without moving the working copy or leaving
persistent review bookmarks.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError, UsageError
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.pull_request_refs import parse_repository_pull_request_reference
from jj_stack.github.resolution import (
    GithubRepoAddress,
    require_github_repo,
    select_submit_remote,
)
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.branches import (
    is_review_branch,
    review_branch_matches_change,
    review_namespace,
)
from jj_stack.review.observation import duplicate_review_claim_change_ids
from jj_stack.review.repository import observe_repository_paths
from jj_stack.review.selected import select_review_path
from jj_stack.review.status import status_preparation_cli_error
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Connect jj-stack to an existing stack of pull requests"


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Outcome of attaching one exact pull-request stack."""

    adopted_count: int
    fetched_tip_commit: str | None
    stack: LocalStack


def checkout(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    fetch: bool,
    pick: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `checkout`."""

    context = bootstrap_context(repository=repository, cli_args=cli_args, debug=debug)
    if pick:
        revset = _pick_tracked_stack_head(context)
    with acquire_operation_lock(context.state_store.require_writable(), command="checkout"):
        result = asyncio.run(
            _run_checkout_async(
                context=context,
                fetch=fetch,
                pull_request_reference=pull_request,
                revset=revset,
            )
        )
    if result.fetched_tip_commit is not None:
        console.output(ui.prefixed_line("Fetched tip commit: ", result.fetched_tip_commit))
    if result.adopted_count:
        noun = "review" if result.adopted_count == 1 else "reviews"
        console.output(f"Updated local tracking for {result.adopted_count} {noun}.")
    elif result.stack.revisions:
        console.output("Local tracking is already up to date for this stack.")
    else:
        console.output("The selected stack has no changes to review.")
    return 0


async def _run_checkout_async(
    *,
    context: CommandContext,
    fetch: bool,
    pull_request_reference: str | None,
    revset: str | None,
) -> CheckoutResult:
    if pull_request_reference is not None and revset is not None:
        raise UsageError(
            t"{ui.cmd('checkout')} accepts at most one selector: "
            t"{ui.cmd('--pull-request')} or {ui.cmd('--revset')}."
        )
    if pull_request_reference is None:
        return _checkout_saved_stack(context=context, fetch=fetch, revset=revset)
    return await _checkout_pull_request_stack(
        context=context,
        fetch=fetch,
        pull_request_reference=pull_request_reference,
    )


def _checkout_saved_stack(
    *,
    context: CommandContext,
    fetch: bool,
    revset: str | None,
) -> CheckoutResult:
    client = context.jj_client
    state = context.state_store.load()
    remote = select_submit_remote(client.list_git_remotes())
    if fetch:
        client.fetch_remote(
            remote=remote.name,
        )
    stack = select_review_path(jj_client=client, revset=revset, state=state).stack
    incomplete = tuple(
        revision
        for revision in stack.revisions
        if state.review_identities.get(revision.change_id) is None
        or state.submitted_baselines.get(revision.change_id) is None
        or state.issues_for(revision.change_id)
    )
    if incomplete:
        raise CliError(
            t"jj-stack has no saved pull request for some changes in this stack: "
            t"{ui.join(ui.change_id, (revision.change_id for revision in incomplete))}.",
            hint=t"Attach it with {ui.cmd('checkout --pull-request PR --fetch')}.",
        )
    return CheckoutResult(adopted_count=0, fetched_tip_commit=None, stack=stack)


async def _checkout_pull_request_stack(
    *,
    context: CommandContext,
    fetch: bool,
    pull_request_reference: str,
) -> CheckoutResult:
    client = context.jj_client
    remote = select_submit_remote(client.list_git_remotes())
    repository = require_github_repo(remote)
    pull_number = parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=repository,
    )
    async with build_github_client(repository=repository) as github_client:
        top_pull_request = await _load_pull_request(
            github_client=github_client,
            pull_number=pull_number,
        )
        _validate_same_repository_managed_pull_request(
            pull_request=top_pull_request,
            repository=repository,
        )
        await _require_unique_pull_request_head(
            github_client=github_client,
            pull_request=top_pull_request,
        )
        top_head_sha = _require_pull_request_head_sha(top_pull_request)
        observed_top = client.list_remote_branches(
            remote=remote.name,
            patterns=(f"refs/heads/{top_pull_request.head.ref}",),
        ).get(top_pull_request.head.ref)
        if observed_top != top_head_sha:
            raise CliError(
                t"PR #{pull_number} and remote branch "
                t"{ui.bookmark(top_pull_request.head.ref)} no longer identify the same commit."
            )

        if fetch:
            client.fetch_remote(
                remote=remote.name,
            )
            _reject_locally_rewritten_change(
                client=client,
                head_sha=top_head_sha,
                pull_number=pull_number,
                remote_name=remote.name,
            )
            with client.import_remote_review_ref(
                remote=remote.name,
                branch=top_pull_request.head.ref,
                expected_target=top_head_sha,
            ) as imported:
                _require_branch_matches_revision(
                    branch=top_pull_request.head.ref,
                    revision=imported,
                )
                stack = _discover_checkout_stack(
                    client=client,
                    revision=imported.commit_id,
                    state=context.state_store.load(),
                )
        else:
            matches = client.query_revisions_by_commit_ids((top_head_sha,))
            if len(matches) != 1:
                raise CliError(
                    t"PR #{pull_number}'s head commit is not present locally.",
                    hint=t"Re-run with {ui.cmd('--fetch')} to attach it.",
                )
            _require_branch_matches_revision(
                branch=top_pull_request.head.ref,
                revision=matches[0],
            )
            stack = _discover_checkout_stack(
                client=client,
                revision=top_head_sha,
                state=context.state_store.load(),
            )

        # Only the chain read after this re-read is used for the tracking write below.
        fresh_top = await _load_pull_request(
            github_client=github_client,
            pull_number=pull_number,
        )
        _validate_same_repository_managed_pull_request(
            pull_request=fresh_top,
            repository=repository,
        )
        await _require_unique_pull_request_head(
            github_client=github_client,
            pull_request=fresh_top,
        )
        pull_requests = await _load_pull_request_chain(
            github_client=github_client,
            repository=repository,
            top=fresh_top,
        )
        adopted_count = _save_checkout_tracking(
            context=context,
            pull_requests=pull_requests,
            remote_name=remote.name,
            repository=repository,
            stack=stack,
        )
    return CheckoutResult(
        adopted_count=adopted_count,
        fetched_tip_commit=(top_head_sha if fetch else None),
        stack=stack,
    )


def _reject_locally_rewritten_change(
    *,
    client: JjClient,
    head_sha: str,
    pull_number: int,
    remote_name: str,
) -> None:
    """Reject a fetch that would import a rewritten copy of a visible local change.

    The remote commit's change ID is read without creating a ref, because importing it first
    would leave a divergent second copy behind that no rerun can remove. On a fresh checkout
    that costs one extra object fetch; reading the same header inside the import primitive
    would instead give that shared primitive a second policy path.
    """

    change_id = client.read_remote_git_change_id(
        remote=remote_name,
        commit_id=head_sha,
    )
    if change_id is None:
        return
    # `change_id()` rather than an exact symbol: it tolerates a change that is already
    # divergent, which is the state this check exists to keep the tool out of.
    local = client.query_revisions(f"change_id({change_id})")
    if not any(revision.commit_id != head_sha for revision in local):
        return
    if len(local) > 1:
        raise CliError(
            t"Change {ui.change_id(change_id)} already has more than one visible revision "
            t"here, so PR #{pull_number} cannot be attached to one of them.",
            hint=t"Inspect them with {ui.cmd('jj log -r')} "
            t"{ui.revset(f'change_id({change_id})')}, abandon the copies you do not want, "
            t"then retry.",
        )
    raise CliError(
        t"Change {ui.change_id(change_id)} is already here at a different commit than "
        t"PR #{pull_number}'s head, so fetching it would leave two copies.",
        hint=t"Attach the pull request to the local change with "
        t"{ui.cmd(f'jj-stack relink {pull_number} {change_id}')}.",
    )


def _discover_checkout_stack(
    *,
    client: JjClient,
    revision: str,
    state: ReviewState,
) -> LocalStack:
    """Resolve the reviewed stack, translating shape failures into repair guidance."""

    try:
        return select_review_path(jj_client=client, revset=revision, state=state).stack
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error


async def _load_pull_request(
    *,
    github_client: GithubClient,
    pull_number: int,
) -> GithubPullRequest:
    try:
        return await github_client.get_pull_request(pull_number=pull_number)
    except GithubClientError as error:
        raise CliError(f"Could not load pull request #{pull_number}") from error


async def _load_pull_request_chain(
    *,
    github_client: GithubClient,
    repository: GithubRepoAddress,
    top: GithubPullRequest,
) -> tuple[GithubPullRequest, ...]:
    """Walk exact managed base refs from the selected PR to trunk."""

    top_down = [top]
    seen = {top.head.ref}
    base = top.base.ref
    while is_review_branch(base):
        if base in seen:
            raise CliError(
                t"Pull request base branches point at each other in a loop, starting at "
                t"{ui.bookmark(base)}.",
                hint=t"Retarget one of those pull requests on GitHub so the stack has a "
                t"bottom, then rerun {ui.cmd('jj-stack checkout')}.",
            )
        seen.add(base)
        try:
            matches = (await github_client.get_pull_requests_by_head_refs(head_refs=(base,))).get(
                base,
                (),
            )
        except GithubClientError as error:
            raise CliError(f"Could not inspect pull request branch {base}") from error
        if len(matches) != 1:
            raise CliError(
                t"Expected one pull request for managed base branch {ui.bookmark(base)}, "
                t"but GitHub reports {len(matches)}.",
                hint=t"Select the intended review explicitly and repair it with "
                t"{ui.cmd('relink')}.",
            )
        parent = matches[0]
        _validate_same_repository_managed_pull_request(
            pull_request=parent,
            repository=repository,
        )
        top_down.append(parent)
        base = parent.base.ref
    return tuple(reversed(top_down))


async def _require_unique_pull_request_head(
    *,
    github_client: GithubClient,
    pull_request: GithubPullRequest,
) -> None:
    try:
        matches = (
            await github_client.get_pull_requests_by_head_refs(
                head_refs=(pull_request.head.ref,),
            )
        ).get(pull_request.head.ref, ())
    except GithubClientError as error:
        raise CliError(t"Could not verify PR #{pull_request.number}'s head branch.") from error
    if len(matches) != 1 or matches[0].number != pull_request.number:
        raise CliError(
            t"Head branch {ui.bookmark(pull_request.head.ref)} does not uniquely identify "
            t"PR #{pull_request.number}.",
            hint=t"Inspect them with {ui.cmd('jj-stack view')}, then attach the "
            t"intended review with {ui.cmd('jj-stack relink')}.",
        )


def _save_checkout_tracking(
    *,
    context: CommandContext,
    pull_requests: tuple[GithubPullRequest, ...],
    remote_name: str,
    repository: GithubRepoAddress,
    stack: LocalStack,
) -> int:
    pull_request_heads = tuple(
        _require_pull_request_head_sha(pull_request) for pull_request in pull_requests
    )
    if len(pull_requests) != len(stack.revisions) or pull_request_heads != tuple(
        revision.commit_id for revision in stack.revisions
    ):
        raise CliError(
            "The selected pull requests do not describe the stack that was just fetched.",
            hint=t"Run {ui.cmd('jj-stack view')} to compare them, then submit or "
            t"relink the reviews that should match this history.",
        )
    remote_targets = context.jj_client.list_remote_branches(
        remote=remote_name,
        patterns=tuple(f"refs/heads/{pull_request.head.ref}" for pull_request in pull_requests),
    )
    replacements: dict[str, tuple[ReviewIdentity, SubmittedBaseline]] = {}
    for pull_request, head_sha, revision in zip(
        pull_requests,
        pull_request_heads,
        stack.revisions,
        strict=True,
    ):
        _require_branch_matches_revision(branch=pull_request.head.ref, revision=revision)
        if remote_targets.get(pull_request.head.ref) != head_sha:
            raise CliError(
                t"PR #{pull_request.number} and branch "
                t"{ui.bookmark(pull_request.head.ref)} no longer identify the same commit."
            )
        replacements[revision.change_id] = (
            ReviewIdentity(
                repository_owner=repository.owner,
                repository_name=repository.repo,
                pr_number=pull_request.number,
                head_owner=repository.owner,
                head_ref=pull_request.head.ref,
            ),
            SubmittedBaseline(commit_id=head_sha),
        )
    state = context.state_store.load()
    _reject_duplicate_checkout_claims(
        current=state.review_identities,
        replacements={change_id: pair[0] for change_id, pair in replacements.items()},
    )
    changed_count = sum(
        (
            state.review_identities.get(change_id),
            state.submitted_baselines.get(change_id),
        )
        != replacement
        for change_id, replacement in replacements.items()
    )
    if not changed_count:
        return 0
    context.state_store.relink_reviews(
        expected={
            change_id: (
                state.review_identities.get(change_id),
                state.submitted_baselines.get(change_id),
            )
            for change_id in replacements
        },
        expected_issues={change_id: state.issues_for(change_id) for change_id in replacements},
        replacements=replacements,
    )
    return changed_count


def _reject_duplicate_checkout_claims(
    *,
    current: dict[str, ReviewIdentity],
    replacements: dict[str, ReviewIdentity],
) -> None:
    combined = dict(current)
    combined.update(replacements)
    if duplicate_review_claim_change_ids(combined).intersection(replacements):
        raise CliError(
            "Another saved change already claims one of those pull request numbers or branches.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the claiming change, then drop its "
            t"tracking with {ui.cmd('jj-stack unstack --local')} or clean it up with "
            t"{ui.cmd('jj-stack cleanup')}.",
        )


def _validate_same_repository_managed_pull_request(
    *,
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
) -> None:
    expected_label = f"{repository.owner}:{pull_request.head.ref}"
    if pull_request.head.label != expected_label:
        raise CliError(
            t"Pull request #{pull_request.number} head "
            t"{ui.bookmark(pull_request.head.label or pull_request.head.ref)} does not "
            t"belong to {repository.full_name}."
        )
    if not is_review_branch(pull_request.head.ref):
        raise CliError(
            t"Pull request #{pull_request.number} head "
            t"{ui.bookmark(pull_request.head.ref)} is not in the reserved "
            t"{ui.bookmark(review_namespace())} namespace."
        )


def _require_pull_request_head_sha(pull_request: GithubPullRequest) -> str:
    head_sha = pull_request.head.sha
    if head_sha is None:
        raise CliError(
            t"GitHub did not report a head commit for PR #{pull_request.number}.",
            hint="Refresh the pull request on GitHub, then retry.",
        )
    return head_sha


def _require_branch_matches_revision(*, branch: str, revision: LocalRevision) -> None:
    if not review_branch_matches_change(branch, revision.change_id):
        raise CliError(
            t"Review branch {ui.bookmark(branch)} does not match change "
            t"{ui.change_id(revision.change_id)}."
        )


def _pick_tracked_stack_head(context: CommandContext) -> str:
    """Prompt for one locally tracked stack without holding the operation lock."""

    state = context.state_store.load()
    if not state.review_identities:
        stacks: list[LocalStack] = []
    else:
        repository_paths = observe_repository_paths(
            jj_client=context.jj_client,
            state=state,
        )
        stacks = sorted(
            (path.stack for path in repository_paths.paths if path.tracked_change_ids),
            key=lambda stack: stack.head.change_id,
        )
    if not stacks:
        raise CliError(
            "No locally tracked stacks to pick from.",
            hint=t"Use {ui.cmd('checkout --pull-request PR --fetch')} to attach one.",
        )
    console.output("Locally tracked stacks:")
    for index, stack in enumerate(stacks, start=1):
        console.output(
            t"  [{index}] {ui.change_id(stack.head.change_id)} "
            t"{stack.head.subject} ({len(stack.revisions)} changes)"
        )
    console.output(t"Pick a stack [1-{len(stacks)}]: ")
    selection = sys.stdin.readline().strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(stacks):
        raise UsageError(
            t"{ui.cmd(selection or '(empty)')} is not a valid stack number; "
            t"expected 1-{len(stacks)}."
        )
    return stacks[int(selection) - 1].head.change_id
