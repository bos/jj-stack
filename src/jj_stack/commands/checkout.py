"""Connect jj-stack to an existing stack of pull requests.

By default, `checkout` tries to match the current stack headed by `@-` to the
existing pull requests for that stack.

Use `--pull-request` to select a specific stack by PR number or URL, or
`--revset` to point at a different local stack. Use `--pick` to choose from a
numbered list of the stacks jj-stack already tracks. Use `--fetch` when the
review branches are not available locally yet; this fetches them first and
then sets up tracking.

`checkout` does not rewrite commits, rebase changes, or modify GitHub.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import AmbiguousSelectionError, CliError, ErrorMessage, UsageError
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.pull_request_refs import parse_repository_pull_request_reference
from jj_stack.github.resolution import (
    GithubRepoAddress,
    require_github_repo,
    select_submit_remote,
)
from jj_stack.jj.client import JjCliArgs, JjClient
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import CachedChange
from jj_stack.models.stack import LocalStack
from jj_stack.review.bookmarks import (
    bookmark_matches_generated_change_id,
    bookmark_ownership_for_source,
    discover_bookmarks_for_revisions,
)
from jj_stack.review.change_status import classify_review_change_without_pull_request
from jj_stack.review.discovery import discover_tracked_stacks
from jj_stack.review.status import (
    PreparedRevision,
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    stream_status_async,
)
from jj_stack.state.operation_lock import acquire_operation_lock
from jj_stack.ui import Message, plain_text

HELP = "Connect jj-stack to an existing stack of pull requests"

_DISPLAY_CHANGE_ID_LENGTH = 8
ImportActionStatus = Literal["applied"]
type ImportActionBody = Message


@dataclass(frozen=True, slots=True)
class ImportAction:
    """One applied checkout action."""

    kind: str
    body: ImportActionBody
    status: ImportActionStatus

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Rendered checkout result for the selected repository."""

    actions: tuple[ImportAction, ...]
    fetched_tip_commit: str | None
    github_error: ErrorMessage | None
    github_repository: GithubRepoAddress | None
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    reviewable_revision_count: int
    selected_revset: str
    selector: str


@dataclass(frozen=True, slots=True)
class _Selection:
    default_current_stack: bool
    fetched_tip_commit: str | None
    selector: str
    head_bookmark: str | None
    selected_revset: str | None


@dataclass(frozen=True, slots=True)
class _PlannedCheckout:
    bookmark: str
    track_remote: bool
    update_local_bookmark: bool
    update_local_target: str


@dataclass(frozen=True, slots=True)
class _PreparedCheckout:
    """Resolved checkout inputs before local tracking state is updated."""

    bookmark_by_change_id: dict[str, str]
    bookmark_states: dict[str, BookmarkState]
    prepared_status: PreparedStatus
    selection: _Selection
    status_result: StatusResult


class _RevisionWithChangeId(Protocol):
    @property
    def change_id(self) -> str: ...


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

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    if pick:
        # The prompt happens before the operation lock so an idle picker never
        # blocks other commands.
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
    _print_checkout_result(result)
    return 0


def _print_checkout_result(result: CheckoutResult) -> None:
    if result.fetched_tip_commit is not None:
        console.output(ui.prefixed_line("Fetched tip commit: ", result.fetched_tip_commit))
    for message in remote_and_github_unavailable_messages(
        github_error=result.github_error,
        github_repository=result.github_repository,
        remote=result.remote,
        remote_error=result.remote_error,
    ):
        console.warning(message)
    if result.actions:
        console.output("Updated local tracking:")
        for action in result.actions:
            console.output(
                ui.prefixed_line(
                    "  - applied: ",
                    (ui.semantic_text(action.kind, "prefix"), ": ", action.body),
                )
            )
    else:
        if result.reviewable_revision_count:
            console.output("Local tracking is already up to date for this stack.")
        else:
            console.output("The selected stack has no changes to review.")


def _pick_tracked_stack_head(context: CommandContext) -> str:
    """List the tracked stacks, read a pick from stdin, and return its head."""

    discovered = discover_tracked_stacks(
        jj_client=context.jj_client,
        state=context.state_store.load(),
    )
    stacks = sorted(
        discovered.stacks,
        key=lambda stack: (
            not _stack_contains_commit_id(stack, discovered.current_commit_id),
            stack.head.change_id,
        ),
    )
    if not stacks:
        raise CliError(
            "No locally tracked stacks to pick from.",
            hint=t"Use {ui.cmd('checkout --pull-request PR')} to attach a stack "
            t"that has no local tracking yet.",
        )
    console.output("Locally tracked stacks:")
    for index, stack in enumerate(stacks, start=1):
        count = len(stack.revisions)
        noun = "change" if count == 1 else "changes"
        console.output(
            t"  [{index}] {ui.change_id(stack.head.change_id)} "
            t"{stack.head.subject} ({count} {noun})"
        )
    console.output(t"Pick a stack [1-{len(stacks)}]: ")
    line = sys.stdin.readline()
    if not line:
        raise UsageError(t"{ui.cmd('--pick')} needs a stack number on standard input.")
    selection = line.strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(stacks):
        raise UsageError(
            t"{ui.cmd(selection or '(empty)')} is not a valid stack number; "
            t"expected 1-{len(stacks)}."
        )
    picked = stacks[int(selection) - 1]
    console.output(
        t"Picked stack {ui.change_id(picked.head.change_id)} ({picked.head.subject})"
    )
    return picked.head.change_id


def _stack_contains_commit_id(stack: LocalStack, commit_id: str | None) -> bool:
    if commit_id is None:
        return False
    return any(revision.commit_id == commit_id for revision in stack.revisions)


async def _run_checkout_async(
    *,
    context: CommandContext,
    fetch: bool,
    pull_request_reference: str | None,
    revset: str | None,
) -> CheckoutResult:
    prepared_checkout = await _prepare_checkout(
        context=context,
        fetch=fetch,
        pull_request_reference=pull_request_reference,
        revset=revset,
    )
    actions = _checkout_local_state(
        client=prepared_checkout.prepared_status.prepared.client,
        prepared_status=prepared_checkout.prepared_status,
        status_result=prepared_checkout.status_result,
        bookmark_by_change_id=prepared_checkout.bookmark_by_change_id,
        bookmark_states=prepared_checkout.bookmark_states,
    )
    return _checkout_result(
        actions=actions,
        prepared_checkout=prepared_checkout,
    )


async def _prepare_checkout(
    *,
    context: CommandContext,
    fetch: bool,
    pull_request_reference: str | None,
    revset: str | None,
) -> _PreparedCheckout:
    client = context.jj_client
    with console.spinner(description="Resolving checkout selection"):
        selection = await _resolve_selection(
            client=client,
            fetch=fetch,
            pull_request_reference=pull_request_reference,
            revset=revset,
        )
    if (
        not fetch
        and selection.head_bookmark is not None
        and selection.selected_revset is not None
        and not client.query_revisions(selection.selected_revset, limit=1)
    ):
        bookmark_token = ui.bookmark(selection.head_bookmark)
        checkout_fetch_cmd = ui.cmd("checkout --fetch")
        raise CliError(
            t"Branch {bookmark_token} is not present locally.",
            hint=t"Re-run {checkout_fetch_cmd} to fetch that stack before checking out.",
        )
    with console.spinner(description="Inspecting jj stack"):
        prepared_status = prepare_status(
            context=context,
            fetch_remote_state=fetch and selection.head_bookmark is None,
            persist_bookmarks=False,
            revset=selection.selected_revset,
        )
    if (
        selection.default_current_stack
        and selection.head_bookmark is None
        and not _prepared_status_has_discoverable_remote_link(prepared_status)
    ):
        checkout_cmd = ui.cmd("checkout")
        raise CliError(
            t"{checkout_cmd} cannot proceed because the current stack has no matching "
            t"remote pull request."
        )
    progress_total = prepared_status.github_inspection_count(discover_remote_review=True)
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = await stream_status_async(
            discover_remote_review=True,
            persist_cache_updates=False,
            prepared_status=prepared_status,
            on_revision=lambda _revision, _github_available: progress.advance(),
        )
    _ensure_selected_head_has_pull_request(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    prepared = prepared_status.prepared
    with console.spinner(description="Loading bookmark state"):
        bookmark_states = prepared.client.list_bookmark_states()
    authoritative_remote_targets: dict[str, str] = {}
    if fetch and selection.head_bookmark is not None and prepared.remote is not None:
        with console.spinner(description="Fetching jj remote"):
            authoritative_remote_targets = _fetch_selected_stack_bookmarks(
                prefix=context.config.bookmark_prefix,
                client=prepared.client,
                explicit_head_bookmark=selection.head_bookmark,
                remote=prepared.remote,
                revisions=prepared.stack.revisions,
            )
            bookmark_states = _apply_authoritative_remote_targets(
                bookmark_states=prepared.client.list_bookmark_states(),
                authoritative_remote_targets=authoritative_remote_targets,
                remote_name=prepared.remote.name,
                relevant_bookmarks={
                    prepared_revision.bookmark for prepared_revision in prepared.status_revisions
                },
            )
    bookmark_by_change_id: dict[str, str] = {}
    if prepared.remote is not None:
        bookmark_by_change_id.update(
            discover_bookmarks_for_revisions(
                bookmark_states=bookmark_states,
                prefix=context.config.bookmark_prefix,
                remote_name=prepared.remote.name,
                revisions=prepared.stack.revisions,
            )
        )
    if selection.head_bookmark is not None and prepared_status.prepared.status_revisions:
        head_revision = prepared_status.prepared.status_revisions[-1]
        bookmark_by_change_id[head_revision.revision.change_id] = selection.head_bookmark

    return _PreparedCheckout(
        bookmark_by_change_id=bookmark_by_change_id,
        bookmark_states=bookmark_states,
        prepared_status=prepared_status,
        status_result=status_result,
        selection=selection,
    )


def _checkout_result(
    *,
    actions: tuple[ImportAction, ...],
    prepared_checkout: _PreparedCheckout,
) -> CheckoutResult:
    prepared_status = prepared_checkout.prepared_status
    selection = prepared_checkout.selection
    status_result = prepared_checkout.status_result
    return CheckoutResult(
        actions=actions,
        fetched_tip_commit=selection.fetched_tip_commit,
        github_error=status_result.github_error,
        github_repository=prepared_status.github_repository,
        remote=prepared_status.prepared.remote,
        remote_error=prepared_status.prepared.remote_error,
        reviewable_revision_count=len(prepared_status.prepared.status_revisions),
        selected_revset=prepared_status.selected_revset,
        selector=selection.selector,
    )


async def _resolve_selection(
    *,
    client: JjClient,
    fetch: bool,
    pull_request_reference: str | None,
    revset: str | None,
) -> _Selection:
    selector_count = sum(
        1
        for present in (
            pull_request_reference is not None,
            revset is not None,
        )
        if present
    )
    if selector_count > 1:
        checkout_cmd = ui.cmd("checkout")
        pull_request_flag = ui.cmd("--pull-request")
        revset_flag = ui.cmd("--revset")
        raise CliError(
            t"{checkout_cmd} accepts at most one selector: {pull_request_flag} or {revset_flag}."
        )

    if selector_count == 0:
        return _Selection(
            default_current_stack=True,
            fetched_tip_commit=None,
            selector="default current stack (@-)",
            head_bookmark=None,
            selected_revset="@-",
        )
    if revset is not None:
        return _Selection(
            default_current_stack=False,
            fetched_tip_commit=None,
            selector=f"--revset {revset}",
            head_bookmark=None,
            selected_revset=revset,
        )
    if pull_request_reference is not None:
        return await _resolve_pull_request_selection(
            client=client,
            fetch=fetch,
            pull_request_reference=pull_request_reference,
        )
    raise AssertionError("One selector is always required.")


async def _resolve_pull_request_selection(
    *,
    client: JjClient,
    fetch: bool,
    pull_request_reference: str,
) -> _Selection:
    remotes = client.list_git_remotes()
    remote = select_submit_remote(remotes)
    github_repository = require_github_repo(remote)
    pull_request = await _load_pull_request(
        github_repository=github_repository,
        pull_request_reference=pull_request_reference,
    )
    head = pull_request.head.ref
    if fetch:
        client.fetch_remote(remote=remote.name, branches=(head,))

    pull_requests = await _list_pull_requests_by_head(
        github_repository=github_repository,
        head=head,
    )
    if len(pull_requests) != 1:
        view_fetch_cmd = ui.cmd("view --fetch")
        relink_cmd = ui.cmd("relink")
        head_branch = ui.bookmark(f"{github_repository.owner}:{head}")
        if not pull_requests:
            raise CliError(
                t"GitHub no longer reports a pull request for head branch {head_branch}.",
                hint=(
                    t"Inspect the PR link with {view_fetch_cmd} and repair it with "
                    t"{relink_cmd} before checking out again."
                ),
            )
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        raise CliError(
            t"GitHub reports multiple pull requests for head branch {head_branch}: {numbers}.",
            hint=(
                t"Inspect the PR link with {view_fetch_cmd} and repair it with "
                t"{relink_cmd} before checking out again."
            ),
        )
    pull_request = pull_requests[0]
    if pull_request.head.label != f"{github_repository.owner}:{head}":
        pull_request_head = ui.bookmark(pull_request.head.label or pull_request.head.ref)
        checkout_cmd = ui.cmd("checkout")
        raise CliError(
            t"Pull request #{pull_request.number} head {pull_request_head} does not belong to "
            t"{github_repository.full_name}. {checkout_cmd} only supports same-repository pull "
            t"request branches."
        )

    remote_state = client.get_bookmark_state(head).remote_target(remote.name)
    selected_revset = _remote_bookmark_commit_id(
        fetch=fetch,
        remote=remote,
        remote_state=remote_state,
        head=head,
    )
    return _Selection(
        default_current_stack=False,
        fetched_tip_commit=selected_revset if fetch else None,
        selector=f"--pull-request {pull_request_reference}",
        head_bookmark=head,
        selected_revset=selected_revset,
    )


def _fetch_selected_stack_bookmarks(
    *,
    prefix: str,
    client: JjClient,
    explicit_head_bookmark: str,
    remote: GitRemote,
    revisions: Sequence[_RevisionWithChangeId],
) -> dict[str, str]:
    head_change_id = revisions[-1].change_id if revisions else None
    patterns = tuple(
        sorted(
            {
                f"refs/heads/{explicit_head_bookmark}",
                *(
                    f"refs/heads/{prefix}/*-{revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]}"
                    for revision in revisions
                ),
            }
        )
    )
    remote_branches = client.list_remote_branches(remote=remote.name, patterns=patterns)
    if explicit_head_bookmark not in remote_branches:
        remote_bookmark = ui.bookmark(f"{explicit_head_bookmark}@{remote.name}")
        raise CliError(
            t"Remote bookmark {remote_bookmark} does not exist.",
            hint="Fetch and retry once that branch is visible on the selected remote.",
        )
    selected_branch_targets = {
        explicit_head_bookmark: remote_branches[explicit_head_bookmark],
    }
    for revision in revisions:
        change_id = revision.change_id
        if change_id == head_change_id:
            continue
        candidates = sorted(
            name
            for name in remote_branches
            if bookmark_matches_generated_change_id(
                name,
                change_id,
                prefix=prefix,
            )
        )
        if len(candidates) > 1:
            raise CliError(
                t"Could not safely check out the selected stack because "
                t"{ui.change_id(change_id)} matches multiple review branches on "
                t"{ui.bookmark(remote.name)}: "
                t"{ui.join(ui.bookmark, candidates)}."
            )
        if len(candidates) == 1:
            selected_branch_targets[candidates[0]] = remote_branches[candidates[0]]

    bookmark_states = client.list_bookmark_states(tuple(sorted(selected_branch_targets)))
    branches_to_fetch: list[str] = []
    for bookmark, target in sorted(selected_branch_targets.items()):
        remote_status = classify_review_change_without_pull_request(
            commit_id=target,
            remote_state=bookmark_states.get(
                bookmark,
                BookmarkState(name=bookmark),
            ).remote_target(remote.name),
        )
        if remote_status.remote_branch_matches_commit is not True:
            branches_to_fetch.append(bookmark)
    if branches_to_fetch:
        client.fetch_remote(remote=remote.name, branches=tuple(branches_to_fetch))
    return selected_branch_targets


def _apply_authoritative_remote_targets(
    *,
    bookmark_states: dict[str, BookmarkState],
    authoritative_remote_targets: dict[str, str],
    remote_name: str,
    relevant_bookmarks: set[str],
) -> dict[str, BookmarkState]:
    if not authoritative_remote_targets:
        return bookmark_states

    updated_states = dict(bookmark_states)
    for bookmark in sorted(relevant_bookmarks | set(authoritative_remote_targets)):
        bookmark_state = updated_states.get(bookmark, BookmarkState(name=bookmark))
        existing_remote_state = bookmark_state.remote_target(remote_name)
        other_remote_targets = tuple(
            remote_state
            for remote_state in bookmark_state.remote_targets
            if remote_state.remote != remote_name
        )
        authoritative_target = authoritative_remote_targets.get(bookmark)
        if authoritative_target is None:
            updated_states[bookmark] = bookmark_state.model_copy(
                update={"remote_targets": other_remote_targets}
            )
            continue
        if (
            existing_remote_state is not None
            and existing_remote_state.target == authoritative_target
        ):
            updated_states[bookmark] = bookmark_state
            continue
        updated_states[bookmark] = bookmark_state.model_copy(
            update={
                "remote_targets": other_remote_targets
                + (
                    RemoteBookmarkState(
                        remote=remote_name,
                        targets=(authoritative_target,),
                        tracking_targets=(
                            ()
                            if existing_remote_state is None
                            else existing_remote_state.tracking_targets
                        ),
                    ),
                )
            }
        )
    return updated_states


async def _load_pull_request(
    *,
    github_repository: GithubRepoAddress,
    pull_request_reference: str,
) -> GithubPullRequest:
    pull_request_number = parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
    )
    async with build_github_client(repository=github_repository) as github_client:
        try:
            pull_request = await github_client.get_pull_request(
                pull_number=pull_request_number,
            )
        except GithubClientError as error:
            raise CliError(f"Could not load pull request #{pull_request_number}") from error

    if pull_request.head.label != f"{github_repository.owner}:{pull_request.head.ref}":
        pull_request_head = ui.bookmark(pull_request.head.label or pull_request.head.ref)
        raise CliError(
            t"Pull request #{pull_request.number} head {pull_request_head} does not belong to "
            t"{github_repository.full_name}. Import only supports same-repository "
            t"pull request branches."
        )
    return pull_request


async def _list_pull_requests_by_head(
    *,
    github_repository: GithubRepoAddress,
    head: str,
) -> tuple[GithubPullRequest, ...]:
    async with build_github_client(repository=github_repository) as github_client:
        try:
            pull_requests = await github_client.list_pull_requests(
                head=f"{github_repository.owner}:{head}",
                state="all",
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not list pull requests for head {ui.bookmark(head)}"
            ) from error
    return tuple(pull_requests)


def _remote_bookmark_commit_id(
    *,
    fetch: bool,
    remote: GitRemote,
    remote_state: RemoteBookmarkState | None,
    head: str,
) -> str:
    bookmark_token = ui.bookmark(head)
    remote_token = ui.bookmark(remote.name)
    remote_status = classify_review_change_without_pull_request(
        commit_id=None,
        remote_state=remote_state,
    )
    if remote_status.remote_branch == "absent":
        if not fetch:
            raise CliError(
                t"Remote bookmark {bookmark_token}@{remote_token} is not available in "
                t"remembered local remote state.",
                hint=t"Re-run {ui.cmd('checkout --fetch')} to fetch that branch before "
                t"checking out.",
            )
        raise CliError(
            t"Remote bookmark {bookmark_token}@{remote_token} does not exist.",
            hint="Fetch and retry once that branch is visible on the selected remote.",
        )
    if remote_status.remote_branch == "conflicted":
        raise CliError(
            t"Remote bookmark {bookmark_token}@{remote_token} is conflicted.",
            hint="Resolve it before checking out.",
        )
    if remote_state is None:
        raise AssertionError("Classified remote bookmark must have an observed state.")
    commit_id = remote_state.target
    if commit_id is None:
        raise AmbiguousSelectionError(
            t"Remote bookmark {bookmark_token}@{remote_token} is ambiguous. "
            t"{ui.cmd('checkout')} requires one exact branch."
        )
    return commit_id


def _checkout_local_state(
    *,
    client: JjClient,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
    bookmark_by_change_id: dict[str, str],
    bookmark_states: dict[str, BookmarkState],
) -> tuple[ImportAction, ...]:
    prepared = prepared_status.prepared
    state_store = prepared.state_store
    current_state = state_store.load()
    next_changes = dict(current_state.changes)
    actions: list[ImportAction] = []
    selected_remote_name = prepared.remote.name if prepared.remote is not None else None
    planned_checkouts: list[_PlannedCheckout] = []

    seen_bookmarks: set[str] = set()
    for prepared_revision in prepared.status_revisions:
        bookmark = _resolve_checkout_bookmark(
            bookmark_by_change_id=bookmark_by_change_id,
            bookmark_states=bookmark_states,
            prepared_revision=prepared_revision,
            selected_remote_name=selected_remote_name,
        )
        if bookmark in seen_bookmarks:
            bookmark_token = ui.bookmark(bookmark)
            raise CliError(
                t"Selected stack resolves multiple changes to the same bookmark {bookmark_token}."
            )
        seen_bookmarks.add(bookmark)

        bookmark_state = bookmark_states.get(bookmark, BookmarkState(name=bookmark))
        _validate_bookmark_state(
            bookmark=bookmark,
            bookmark_state=bookmark_state,
            desired_commit_id=prepared_revision.revision.commit_id,
            selected_remote_name=selected_remote_name,
        )
        remote_state = (
            bookmark_state.remote_target(prepared.remote.name)
            if prepared.remote is not None
            else None
        )
        remote_status = classify_review_change_without_pull_request(
            commit_id=prepared_revision.revision.commit_id,
            remote_state=remote_state,
        )
        track_remote = (
            prepared.remote is not None
            and remote_status.remote_branch == "untracked"
            and remote_status.remote_branch_matches_commit is True
        )

        existing_change = next_changes.get(
            prepared_revision.revision.change_id
        ) or current_state.changes.get(prepared_revision.revision.change_id)
        cached_change = existing_change or CachedChange(bookmark=bookmark)
        updated_change = _update_cached_change_from_status(
            cached_change=cached_change,
            bookmark=bookmark,
            status_revision=_find_status_revision(
                status_result.revisions, prepared_revision.revision.change_id
            ),
        )
        if existing_change is None or updated_change != cached_change:
            next_changes[prepared_revision.revision.change_id] = updated_change
        planned_checkouts.append(
            _PlannedCheckout(
                bookmark=bookmark,
                track_remote=track_remote,
                update_local_bookmark=(
                    bookmark_state.local_target != prepared_revision.revision.commit_id
                ),
                update_local_target=prepared_revision.revision.commit_id,
            )
        )

    for planned in planned_checkouts:
        if planned.update_local_bookmark:
            bookmark_token = ui.bookmark(planned.bookmark)
            short_target = planned.update_local_target[:_DISPLAY_CHANGE_ID_LENGTH]
            client.set_bookmark(planned.bookmark, planned.update_local_target)
            actions.append(
                ImportAction(
                    kind="bookmark",
                    body=t"set local bookmark {bookmark_token} -> {short_target}",
                    status="applied",
                )
            )
        if planned.track_remote:
            if prepared.remote is None:
                raise AssertionError("Tracking requires a selected remote.")
            remote_bookmark = ui.bookmark(f"{planned.bookmark}@{prepared.remote.name}")
            client.track_bookmark(remote=prepared.remote.name, bookmark=planned.bookmark)
            actions.append(
                ImportAction(
                    kind="bookmark tracking",
                    body=t"track remote branch {remote_bookmark}",
                    status="applied",
                )
            )

    next_state = current_state.model_copy(update={"changes": next_changes})
    if next_state != current_state:
        state_store.save(next_state)
        actions.append(
            ImportAction(
                kind="tracking",
                body="update local tracking for this stack",
                status="applied",
            )
        )
    return tuple(actions)


def _validate_bookmark_state(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    desired_commit_id: str,
    selected_remote_name: str | None,
) -> None:
    if len(bookmark_state.local_targets) > 1:
        bookmark_token = ui.bookmark(bookmark)
        raise CliError(
            t"Local bookmark {bookmark_token} is conflicted.",
            hint="Resolve it before checking out.",
        )
    if (
        bookmark_state.local_target is not None
        and bookmark_state.local_target != desired_commit_id
    ):
        bookmark_token = ui.bookmark(bookmark)
        raise CliError(
            t"Local bookmark {bookmark_token} already points to a different revision.",
            hint="Move or forget it explicitly before checking out.",
        )
    if selected_remote_name is None:
        return
    remote_state = bookmark_state.remote_target(selected_remote_name)
    remote_status = classify_review_change_without_pull_request(
        commit_id=desired_commit_id,
        remote_state=remote_state,
    )
    if remote_status.remote_branch == "absent":
        return
    if remote_status.remote_branch == "conflicted":
        remote_bookmark = ui.bookmark(bookmark)
        remote_bookmark_location = f"'{remote_bookmark}'@{selected_remote_name}"
        raise CliError(
            f"Remote bookmark {remote_bookmark_location} is conflicted.",
            hint="Resolve it before checking out.",
        )
    if remote_status.remote_branch_matches_commit is not True:
        remote_bookmark = ui.bookmark(bookmark)
        remote_bookmark_location = f"'{remote_bookmark}'@{selected_remote_name}"
        raise CliError(
            f"Remote bookmark {remote_bookmark_location} already points to a different revision.",
            hint="Import will not overwrite a stale remote identity.",
        )


def _find_status_revision(
    revisions: Sequence[ReviewStatusRevision],
    change_id: str,
) -> ReviewStatusRevision:
    for revision in revisions:
        if revision.change_id == change_id:
            return revision
    raise AssertionError("Status revision for checked-out change was not found.")


def _update_cached_change_from_status(
    *,
    cached_change: CachedChange,
    bookmark: str,
    status_revision: ReviewStatusRevision,
) -> CachedChange:
    updated_change = cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": bookmark_ownership_for_source(status_revision.bookmark_source),
        }
    )
    if cached_change.is_unlinked:
        return updated_change
    pull_request_lookup = status_revision.pull_request_lookup
    if pull_request_lookup is not None:
        if pull_request_lookup.state == "missing":
            updated_change = updated_change.with_cleared_pr_identity().with_cleared_comments()
        elif pull_request_lookup.pull_request is not None:
            pull_request = pull_request_lookup.pull_request
            updated_change = updated_change.model_copy(
                update={
                    "pr_number": pull_request.number,
                    "pr_state": pull_request.state,
                    "pr_url": pull_request.html_url,
                }
            )
            if pull_request_lookup.review_decision_error is None:
                updated_change = updated_change.model_copy(
                    update={"pr_review_decision": pull_request_lookup.review_decision}
                )
            if pull_request_lookup.state != "open":
                updated_change = updated_change.with_cleared_comments()

    managed_comments_lookup = status_revision.managed_comments_lookup
    if managed_comments_lookup is not None and managed_comments_lookup.state == "resolved":
        updated_change = updated_change.model_copy(
            update={
                "navigation_comment_id": (
                    None
                    if managed_comments_lookup.navigation_comment is None
                    else managed_comments_lookup.navigation_comment.id
                ),
                "overview_comment_id": (
                    None
                    if managed_comments_lookup.overview_comment is None
                    else managed_comments_lookup.overview_comment.id
                ),
            }
        )
    return updated_change


def _prepared_status_has_discoverable_remote_link(
    prepared_status: PreparedStatus,
) -> bool:
    prepared = prepared_status.prepared
    remote = prepared.remote
    if remote is None:
        return False
    bookmark_states = prepared.client.list_bookmark_states(
        [revision.bookmark for revision in prepared.status_revisions]
    )
    for revision in prepared.status_revisions:
        remote_state = bookmark_states.get(
            revision.bookmark,
            BookmarkState(name=revision.bookmark),
        ).remote_target(remote.name)
        remote_status = classify_review_change_without_pull_request(
            commit_id=revision.revision.commit_id,
            remote_state=remote_state,
        )
        if remote_status.remote_branch != "absent":
            return True
    return False


def _ensure_selected_head_has_pull_request(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> None:
    if not status_result.revisions:
        return

    selected_head_change_id = prepared_status.prepared.stack.head.change_id
    selected_head = next(
        (
            revision
            for revision in status_result.revisions
            if revision.change_id == selected_head_change_id
        ),
        None,
    )
    if selected_head is None:
        raise AssertionError("Selected checkout head is missing from the status result.")
    lookup = selected_head.pull_request_lookup
    if lookup is not None and lookup.pull_request is not None:
        return

    checkout_cmd = ui.cmd("checkout")
    selected_head_change_id = ui.change_id(selected_head.change_id)
    raise CliError(
        t"{checkout_cmd} only supports stacks whose selected head already has a pull request. "
        t"Missing pull request for: {selected_head.subject} ({selected_head_change_id})."
    )


def _resolve_checkout_bookmark(
    *,
    bookmark_by_change_id: dict[str, str],
    bookmark_states: dict[str, BookmarkState],
    prepared_revision: PreparedRevision,
    selected_remote_name: str | None,
) -> str:
    exact_bookmark = bookmark_by_change_id.get(prepared_revision.revision.change_id)
    if exact_bookmark is not None:
        if selected_remote_name is None:
            return exact_bookmark
        bookmark = exact_bookmark
    else:
        bookmark = prepared_revision.bookmark
        if prepared_revision.bookmark_source == "generated":
            view_fetch_cmd = ui.cmd("view --fetch")
            raise CliError(
                t"Could not safely check out the selected stack because "
                t"{ui.change_id(prepared_revision.revision.change_id)} has no matching "
                t"pull request on the selected remote. Refresh with {view_fetch_cmd} "
                t"or select an exact pull request."
            )
    if selected_remote_name is None:
        return bookmark
    bookmark_state = bookmark_states.get(bookmark, BookmarkState(name=bookmark))
    remote_state = bookmark_state.remote_target(selected_remote_name)
    remote_status = classify_review_change_without_pull_request(
        commit_id=prepared_revision.revision.commit_id,
        remote_state=remote_state,
    )
    if remote_status.remote_branch in {"absent", "conflicted"}:
        bookmark_token = ui.bookmark(bookmark)
        view_fetch_cmd = ui.cmd("view --fetch")
        raise CliError(
            t"Could not safely check out the selected stack because saved branch "
            t"{bookmark_token} for {ui.change_id(prepared_revision.revision.change_id)} "
            t"is not present on the selected remote.",
            hint=t"Refresh with {view_fetch_cmd} or select an exact pull request.",
        )
    if remote_status.remote_branch_matches_commit is not True:
        bookmark_token = ui.bookmark(bookmark)
        view_fetch_cmd = ui.cmd("view --fetch")
        raise CliError(
            t"Could not safely check out the selected stack because saved branch "
            t"{bookmark_token} for {ui.change_id(prepared_revision.revision.change_id)} "
            t"points to a different revision on the selected remote.",
            hint=(
                t"Refresh with {view_fetch_cmd} or repair the stale remote match "
                t"before checking out again."
            ),
        )
    return bookmark
