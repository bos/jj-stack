"""Check out an existing stack of pull requests.

Use `--pull-request` to select a GitHub pull request, `--revset` to select a locally tracked head,
or `--pick` to choose from local and GitHub stacks in an interactive numbered list. When a pull
request's reviewed commits are not present locally, the command fetches them automatically. It
records which pull request belongs to each local change, then runs `jj edit` on the selected
change.

`jj-stack` changes the working copy only after validating the entire stack and saving any new
pull request links. `checkout` does not rebase changes or modify GitHub. To create a new change
on top of the checked-out change, run `jj new` afterward.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
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
from jj_stack.github.stack_availability import github_stacks_unavailable_error
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.github import GithubPullRequest, GithubStack
from jj_stack.models.review_state import ReviewIdentity, ReviewState, SubmittedBaseline
from jj_stack.models.stack import LocalRevision, LocalStack
from jj_stack.review.branches import (
    is_review_branch,
    prepare_visible_review_snapshots,
    review_branch_matches_change,
    review_namespace,
)
from jj_stack.review.observation import duplicate_review_claim_change_ids
from jj_stack.review.repository import observe_repository_paths
from jj_stack.review.selected import select_review_path
from jj_stack.review.status import status_preparation_cli_error
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Check out an existing stack of pull requests"


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Outcome of attaching one exact pull-request stack."""

    adopted_count: int
    fetched_tip_commit: str | None
    stack: LocalStack


@dataclass(frozen=True, slots=True)
class CheckoutPickerChoice:
    """One local or GitHub stack offered by the interactive picker."""

    details: tuple[str, ...]
    heading: str
    pull_request: str | None = None
    revset: str | None = None


def checkout(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    pick: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `checkout`."""

    context = bootstrap_context(repository=repository, cli_args=cli_args, debug=debug)
    if pick:
        choice = asyncio.run(_pick_stack(context))
        pull_request = choice.pull_request
        revset = choice.revset
    with acquire_operation_lock(context.state_store.require_writable(), command="checkout"):
        result = asyncio.run(
            _run_checkout_async(
                context=context,
                pull_request_reference=pull_request,
                revset=revset,
            )
        )
        if result.stack.revisions:
            if result.adopted_count:
                prepare_visible_review_snapshots(
                    jj_client=context.jj_client,
                    state=context.state_store.load(),
                )
            context.jj_client.edit_revision(result.stack.head.commit_id)
    if result.fetched_tip_commit is not None:
        console.output(ui.prefixed_line("Fetched tip commit: ", result.fetched_tip_commit))
    if result.adopted_count:
        noun = "review" if result.adopted_count == 1 else "reviews"
        console.output(f"Updated local tracking for {result.adopted_count} {noun}.")
    elif result.stack.revisions:
        console.output("Local tracking is already up to date for this stack.")
    else:
        console.output("The selected stack has no changes to review.")
    if result.stack.revisions:
        console.output(
            t"Working copy now edits {ui.change_id(result.stack.head.change_id)} "
            t"({result.stack.head.subject})."
        )
    return 0


async def _run_checkout_async(
    *,
    context: CommandContext,
    pull_request_reference: str | None,
    revset: str | None,
) -> CheckoutResult:
    if pull_request_reference is not None and revset is not None:
        raise UsageError(
            t"{ui.cmd('checkout')} accepts at most one selector: "
            t"{ui.cmd('--pull-request')} or {ui.cmd('--revset')}."
        )
    if pull_request_reference is None:
        return _checkout_saved_stack(context=context, revset=revset)
    return await _checkout_pull_request_stack(
        context=context,
        pull_request_reference=pull_request_reference,
    )


def _checkout_saved_stack(
    *,
    context: CommandContext,
    revset: str | None,
) -> CheckoutResult:
    client = context.jj_client
    state = context.state_store.load()
    stack = select_review_path(jj_client=client, revset=revset, state=state).stack
    incomplete = tuple(
        revision
        for revision in stack.revisions
        if state.review_identities.get(revision.change_id) is None
    )
    if incomplete:
        raise CliError(
            t"jj-stack has no saved pull request for some changes in this stack: "
            t"{ui.join(ui.change_id, (revision.change_id for revision in incomplete))}.",
            hint=t"Attach it with {ui.cmd('checkout --pull-request PR')}.",
        )
    return CheckoutResult(adopted_count=0, fetched_tip_commit=None, stack=stack)


async def _checkout_pull_request_stack(
    *,
    context: CommandContext,
    pull_request_reference: str,
) -> CheckoutResult:
    client = context.jj_client
    state = context.state_store.load()
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
        pull_requests = await _load_pull_request_chain(
            github_client=github_client,
            repository=repository,
            top=top_pull_request,
        )

        for pull_request in reversed(pull_requests):
            _reject_locally_rewritten_change(
                client=client,
                head_sha=_require_pull_request_head_sha(pull_request),
                pull_number=pull_request.number,
                remote_name=remote.name,
            )
        matches = client.query_revisions_by_commit_ids((top_head_sha,))
        fetched = not matches
        if fetched:
            client.fetch_remote(
                remote=remote.name,
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
                    state=state,
                )
        else:
            _require_branch_matches_revision(
                branch=top_pull_request.head.ref,
                revision=matches[0],
            )
            stack = _discover_checkout_stack(
                client=client,
                revision=top_head_sha,
                state=state,
            )

        adopted_count = _save_checkout_tracking(
            context=context,
            pull_requests=pull_requests,
            remote_name=remote.name,
            repository=repository,
            stack=stack,
            state=state,
        )
    return CheckoutResult(
        adopted_count=adopted_count,
        fetched_tip_commit=(top_head_sha if fetched else None),
        stack=stack,
    )


def _reject_locally_rewritten_change(
    *,
    client: JjClient,
    head_sha: str,
    pull_number: int,
    remote_name: str,
) -> None:
    """Reject a reviewed snapshot that disagrees with a visible local change.

    The remote commit's change ID is read without creating a ref. On a fresh checkout that costs
    one extra object fetch; reading the same header inside the import primitive would instead
    give that shared primitive a second policy path. The check also covers an already visible
    reviewed snapshot, where editing it would silently choose against the rewritten local change.
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
        t"PR #{pull_number}'s head, so checkout cannot choose between them.",
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
    state: ReviewState,
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


async def _pick_stack(context: CommandContext) -> CheckoutPickerChoice:
    """Prompt for one local or GitHub stack without holding the operation lock."""

    state = context.state_store.load()
    if not state.review_identities:
        local_stacks: list[LocalStack] = []
    else:
        repository_paths = observe_repository_paths(
            jj_client=context.jj_client,
            state=state,
        )
        local_stacks = sorted(
            (path.stack for path in repository_paths.paths if path.tracked_change_ids),
            key=lambda stack: stack.head.change_id,
        )
    remote = select_submit_remote(context.jj_client.list_git_remotes())
    repository = require_github_repo(remote)
    async with build_github_client(repository=repository) as github_client:
        repository_result, stacks_result = await asyncio.gather(
            github_client.get_repository(),
            github_client.list_stacks(),
            return_exceptions=True,
        )
        if isinstance(repository_result, GithubClientError):
            raise CliError(
                f"Could not inspect GitHub repository {repository.full_name}"
            ) from repository_result
        if isinstance(repository_result, BaseException):
            raise repository_result
        if isinstance(stacks_result, GithubClientError):
            unavailable = github_stacks_unavailable_error(
                error=stacks_result,
                repository=repository.full_name,
            )
            if unavailable is not None:
                raise unavailable from None
            raise CliError("Could not list GitHub stacks for checkout.") from stacks_result
        if isinstance(stacks_result, BaseException):
            raise stacks_result
        github_stacks = stacks_result
        try:
            pull_requests = await github_client.get_pull_requests_by_numbers(
                pull_numbers=tuple(
                    member.number for stack in github_stacks for member in stack.pull_requests
                ),
            )
        except GithubClientError as error:
            raise CliError("Could not list GitHub stacks for checkout.") from error
    choices = _picker_choices(
        github_stacks=github_stacks,
        local_stacks=local_stacks,
        pull_requests=pull_requests,
        repository=repository,
        state=state,
        visible_commit_ids={
            revision.commit_id
            for revision in context.jj_client.query_revisions_by_commit_ids(
                tuple(
                    member.head.sha for stack in github_stacks for member in stack.pull_requests
                )
            )
            if not revision.hidden
        },
    )
    return _prompt_picker_choice(choices)


def _prompt_picker_choice(
    choices: tuple[CheckoutPickerChoice, ...],
) -> CheckoutPickerChoice:
    """Read one validated numbered selection from the interactive picker."""

    if not choices:
        raise CliError(
            "No active local or GitHub stacks to pick from.",
            hint=t"Use {ui.cmd('checkout --pull-request PR')} to attach a pull request directly.",
        )
    console.output("Available stacks:")
    for index, choice in enumerate(choices, start=1):
        console.output(f"  [{index}] {choice.heading}")
        for detail in choice.details:
            console.output(f"      {detail}")
    console.output(t"Pick a stack [1-{len(choices)}]: ")
    selection = sys.stdin.readline().strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(choices):
        raise UsageError(
            t"{ui.cmd(selection or '(empty)')} is not a valid stack number; "
            t"expected 1-{len(choices)}."
        )
    return choices[int(selection) - 1]


def _picker_choices(
    *,
    github_stacks: tuple[GithubStack, ...],
    local_stacks: list[LocalStack],
    pull_requests: dict[int, GithubPullRequest | None],
    repository: GithubRepoAddress,
    state: ReviewState,
    visible_commit_ids: set[str],
) -> tuple[CheckoutPickerChoice, ...]:
    saved_by_pull = {
        identity.pr_number: (change_id, identity)
        for change_id, identity in state.review_identities.items()
        if identity.repository_key == repository.repository_key
    }
    choices: list[CheckoutPickerChoice] = []
    listed_pull_numbers: set[int] = set()
    for stack in sorted(github_stacks, key=lambda candidate: candidate.number):
        active_numbers = stack.active_pull_request_numbers
        if not active_numbers:
            continue
        numbers = stack.pull_request_numbers
        members = tuple(pull_requests.get(number) for number in numbers)
        if any(member is None for member in members):
            missing = next(
                number for number, member in zip(numbers, members, strict=True) if member is None
            )
            raise CliError(f"GitHub stack #{stack.number} refers to missing PR #{missing}.")
        resolved = tuple(member for member in members if member is not None)
        if not all(_picker_pull_request_is_adoptable(member, repository) for member in resolved):
            continue
        bottom = resolved[0]
        top = next(member for member in reversed(resolved) if member.number in active_numbers)
        statuses = Counter(_picker_pull_request_status(member) for member in resolved)
        status = ", ".join(
            f"{count} {name}"
            for name in ("open", "draft", "closed", "merged")
            if (count := statuses[name])
        )
        active_members = tuple(member for member in resolved if member.number in active_numbers)
        change_id_by_pull = {
            member.number: saved[0]
            for member in active_members
            if (saved := saved_by_pull.get(member.number)) is not None
            and saved[1].matches_pull_request(member)
        }
        local = len(change_id_by_pull) == len(active_members) and all(
            member.head.sha in visible_commit_ids for member in active_members
        )
        visible_count = sum(member.head.sha in visible_commit_ids for member in active_members)
        locality = "local" if local else "partly local" if visible_count else "GitHub only"
        noun = "PR" if len(numbers) == 1 else "PRs"
        choices.append(
            CheckoutPickerChoice(
                heading=f"GitHub stack #{stack.number} ({locality})",
                details=(
                    f"Top: PR #{top.number} {top.title}",
                    f"Base: {bottom.base.ref}",
                    f"Size: {len(numbers)} {noun}",
                    f"Status: {status}",
                ),
                revset=change_id_by_pull[top.number] if local else None,
                pull_request=None if local else str(top.number),
            )
        )
        listed_pull_numbers.update(numbers)
    for stack in local_stacks:
        identity = state.review_identities.get(stack.head.change_id)
        if identity is not None and identity.pr_number in listed_pull_numbers:
            continue
        count = len(stack.revisions)
        noun = "change" if count == 1 else "changes"
        choices.append(
            CheckoutPickerChoice(
                heading=f"Local stack {stack.head.change_id}",
                details=(f"Head: {stack.head.subject}", f"Size: {count} {noun}"),
                revset=stack.head.change_id,
            )
        )
    return tuple(choices)


def _picker_pull_request_status(pull_request: GithubPullRequest) -> str:
    normalized = pull_request.normalize_state()
    if normalized.state == "open" and normalized.is_draft:
        return "draft"
    return normalized.state


def _picker_pull_request_is_adoptable(
    pull_request: GithubPullRequest,
    repository: GithubRepoAddress,
) -> bool:
    return is_review_branch(pull_request.head.ref) and (
        pull_request.head.label == f"{repository.owner}:{pull_request.head.ref}"
    )
