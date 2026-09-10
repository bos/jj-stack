"""Check out an existing stack of pull requests submitted with jj-stack.

Use this command to continue work you submitted from another machine or checkout. It fetches any
missing commits, saves their pull request links, and runs `jj edit` on the selected change. If a
PR's version differs from your local version, checkout keeps both and explains how to resolve the
difference.

Use `--pull-request` to bring in a PR and the PRs below it. Select the top PR to check out the
whole stack. Use `--pick` to choose from local and GitHub stacks in an interactive list. Use
`--revset` to edit the head of a stack this checkout already tracks; it confirms that every
change has a saved pull request link and does not contact GitHub.

The PRs and their head branches must belong to the repo selected by your Git remote. PR branches
must use jj-stack's branch naming scheme with this checkout's configured prefix, normally
`jj-stack/`. If the original checkout used a custom prefix, set the same `jj-stack.branch_prefix`
here first. PRs with head branches in another repository, such as a contributor's fork, are
not supported.

Checkout does not rebase changes or modify GitHub. To start a new change on top, run `jj new`
afterward.

In terminals with hyperlink support, the PR beside "Top" in each GitHub stack's `--pick` entry
is a clickable link. Open it to inspect that stack on GitHub before choosing an entry.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.errors import CliError, UnsupportedStackError, UsageError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import observe_github_repo
from jj_stack.github.pr_refs import load_pr, parse_repo_pr_reference, require_managed_pr_head
from jj_stack.github.resolution import (
    GithubRepoAddress,
    require_github_repo,
    select_submit_remote,
)
from jj_stack.identifiers import CommitId, short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR, GithubStack
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.pr_branch_namespace import current_pr_branch_namespace, pr_branch_matches_change
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.observation import observe_pr_bookmarks
from jj_stack.stack.pr_facts import duplicate_pr_claim_change_ids, observe_github_stacks
from jj_stack.stack.preparation import stack_preparation_cli_error
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.selected import select_stack_path
from jj_stack.state.operation_lock import operation_lock

HELP = "Check out an existing stack of pull requests"


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """Result of checking out a stack and saving its PR links."""

    adopted_count: int
    fetched_tip_commit: str | None
    stack: LocalStack
    warnings: tuple[ui.Message, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckoutPickerChoice:
    """One local or GitHub stack offered by the interactive picker."""

    details: tuple[ui.Message, ...]
    heading: ui.Message
    pr: str | None = None
    revset: str | None = None


def checkout(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    pick: bool,
    pr: str | None,
    repo: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `checkout`."""

    context = bootstrap_context(repo=repo, cli_args=cli_args, debug=debug)
    if pr is not None and revset is not None:
        raise UsageError(
            t"{ui.cmd('jj-stack checkout')} accepts at most one selector: "
            t"{ui.cmd('--pull-request')} or {ui.cmd('--revset')}."
        )
    result = asyncio.run(_checkout_async(context=context, pick=pick, pr=pr, revset=revset))
    if result.fetched_tip_commit is not None:
        console.output(ui.prefixed_line("Fetched PR head commit: ", result.fetched_tip_commit))
    if result.adopted_count:
        noun = "PR" if result.adopted_count == 1 else "PRs"
        console.output(f"Saved pull request links for {result.adopted_count} {noun}.")
    elif result.stack.changes:
        console.output("Saved pull request links are already up to date for this stack.")
    else:
        console.output("The selected stack has no changes to check out.")
    if result.stack.changes:
        console.output(
            t"Working copy now edits {ui.change_id(result.stack.head.change_id)} "
            t"({result.stack.head.subject})."
        )
    for warning in result.warnings:
        console.warning(warning, soft_wrap=True)
    return 0


async def _checkout_async(
    *,
    context: CommandContext,
    pick: bool,
    pr: str | None,
    revset: str | None,
) -> CheckoutResult:
    if not pick and pr is None:
        return await _adopt_under_lock(context, lambda: _checkout_saved_stack(context, revset))
    remote = select_submit_remote(context.jj_client.list_git_remotes())
    repo = require_github_repo(remote)
    async with build_github_client(repo=repo) as github_client:
        if pick:
            choice = await _pick_stack(context, github_client=github_client, repo=repo)
            pr, revset = choice.pr, choice.revset
        if pr is None:
            return await _adopt_under_lock(
                context, lambda: _checkout_saved_stack(context, revset)
            )
        pr_reference = pr
        return await _adopt_under_lock(
            context,
            lambda: _checkout_pr_stack(
                context=context,
                github_client=github_client,
                remote=remote,
                repo=repo,
                pr_reference=pr_reference,
            ),
        )


async def _adopt_under_lock(
    context: CommandContext,
    adopt: Callable[[], Awaitable[CheckoutResult]],
) -> CheckoutResult:
    with operation_lock(context.state_store, command="checkout"):
        context.jj_client.clear_pr_branch_temp_artifacts()
        result = await adopt()
        if result.stack.changes:
            edit_args, _snapshots = observe_pr_bookmarks(
                jj_client=context.jj_client, state=context.state_store.load()
            )
            context.jj_client.edit_commit(result.stack.head.commit_id, cli_args=edit_args)
    return result


async def _checkout_saved_stack(
    context: CommandContext,
    revset: str | None,
) -> CheckoutResult:
    client = context.jj_client
    state = context.state_store.load()
    stack = select_stack_path(
        jj_client=client,
        revset=revset,
        state=state,
    ).stack
    incomplete = tuple(
        change for change in stack.changes if state.prs.get(change.change_id) is None
    )
    if incomplete:
        raise CliError(
            t"jj-stack has no saved pull request link for these changes: "
            t"{ui.join(ui.change_id, (change.change_id for change in incomplete))}.",
            hint=t"To check out their existing PRs, run "
            t"{ui.cmd('jj-stack checkout --pull-request <top-pr>')}. "
            t"For changes that have not been submitted, use {ui.cmd('jj edit <change-id>')}.",
        )
    return CheckoutResult(adopted_count=0, fetched_tip_commit=None, stack=stack)


async def _checkout_pr_stack(
    *,
    context: CommandContext,
    github_client: GithubClient,
    remote: GitRemote,
    repo: GithubRepoAddress,
    pr_reference: str,
) -> CheckoutResult:
    client = context.jj_client
    state = context.state_store.load()
    pr_number = parse_repo_pr_reference(
        reference=pr_reference,
        github_repo=repo,
    )
    top_pr = await load_pr(
        github_client=github_client,
        pr_number=pr_number,
    )
    top_head_sha = require_managed_pr_head(
        pr=top_pr,
        repo=repo,
    )
    targets_task = asyncio.create_task(
        github_client.get_branch_targets(branches=(top_pr.head.ref,))
    )
    chain_task = asyncio.create_task(
        _load_pr_chain(
            github_client=github_client,
            repo=repo,
            top=top_pr,
        ),
    )
    await wait_for_read_tasks(targets_task, chain_task)
    observed_top_targets = targets_task.result()
    prs = chain_task.result()
    observed_top = observed_top_targets.get(top_pr.head.ref)
    if observed_top != top_head_sha:
        pr_label = format_pr_label(top_pr.number, url=top_pr.html_url)
        raise CliError(
            t"{pr_label} and remote branch "
            t"{ui.bookmark(top_pr.head.ref)} no longer identify the same commit."
        )

    # A hidden local copy of the head still needs the import so it becomes visible again.
    fetched = not any(
        not commit.hidden for commit in client.query_commits_by_ids((top_head_sha,))
    )
    if fetched:
        client.fetch_remote(remote=remote.name)
        with client.import_remote_pr_branch_ref(
            remote=remote.name,
            branch=top_pr.head.ref,
            expected_target=top_head_sha,
        ):
            stack = _discover_checkout_stack(
                client=client,
                commit_id=top_head_sha,
                state=state,
            )
    else:
        stack = _discover_checkout_stack(
            client=client,
            commit_id=top_head_sha,
            state=state,
        )

    remote_targets = await github_client.get_branch_targets(
        branches=tuple(pr.head.ref for pr in prs),
    )
    adopted_count = _save_checkout_tracking(
        context=context,
        prs=prs,
        remote_targets=remote_targets,
        repo=repo,
        stack=stack,
        state=state,
    )
    tracked = stack.changes[: len(prs)]
    return CheckoutResult(
        adopted_count=adopted_count,
        fetched_tip_commit=(top_head_sha if fetched else None),
        stack=stack,
        warnings=(
            *_divergent_copy_warnings(client=client, prs=prs, changes=tracked),
            *_added_commit_warnings(
                client=client,
                remote=remote.name,
                pr=prs[-1],
                change=tracked[-1],
                added=stack.changes[len(prs) :],
            ),
        ),
    )


def _divergent_copy_warnings(
    *,
    client: JjClient,
    prs: tuple[GithubPR, ...],
    changes: tuple[LocalCommit, ...],
) -> tuple[ui.Message, ...]:
    """Report each adopted change that is now visible at more than one commit."""

    copies = client.query_commits_by_change_ids(tuple(change.change_id for change in changes))
    warnings: list[ui.Message] = []
    for pr, change in zip(prs, changes, strict=True):
        others = tuple(
            copy.commit_id
            for copy in copies.get(change.change_id, ())
            if copy.commit_id != change.commit_id
        )
        if not others:
            continue
        warnings.append(
            t"Change {ui.change_id(change.change_id)} now has {len(others) + 1} visible commits: "
            t"{ui.commit_id(change.commit_id)} (from "
            t"{format_pr_label(pr.number, url=pr.html_url)}) and "
            t"{ui.join(ui.commit_id, others)}. "
            t"{divergence_recovery_hint(change.change_id)}"
        )
    return tuple(warnings)


def _added_commit_warnings(
    *,
    client: JjClient,
    remote: str,
    pr: GithubPR,
    change: LocalCommit,
    added: tuple[LocalCommit, ...],
) -> tuple[ui.Message, ...]:
    """Report commits on the PR branch above the pull request's own change."""

    if not added:
        return ()
    target = short_change_id(change.change_id)
    source = (
        short_change_id(added[0].change_id)
        if len(added) == 1
        else shlex.quote(
            f"{short_change_id(added[0].change_id)}::{short_change_id(added[-1].change_id)}"
        )
    )
    noun, pronoun = ("change", "it") if len(added) == 1 else ("changes", "them")
    return (
        t"PR branch {ui.bookmark(pr.head.ref)} adds {noun} "
        t"{ui.join(lambda commit: _describe_added_commit(client, remote, commit), added)} on "
        t"top of change {ui.change_id(change.change_id)}. Fold {pronoun} into "
        t"{ui.change_id(change.change_id)} with "
        t"{ui.cmd(f'jj squash --from {source} --into {target}')}.",
    )


def _describe_added_commit(client: JjClient, remote: str, commit: LocalCommit) -> ui.Message:
    author = client.read_remote_git_commit(remote=remote, commit_id=commit.commit_id).author
    return t"{ui.change_id(commit.change_id)} ({author}: {commit.subject})"


def _discover_checkout_stack(
    *,
    client: JjClient,
    commit_id: str,
    state: TrackingState,
) -> LocalStack:
    """Resolve the PR stack, translating shape failures into repair guidance."""

    try:
        return select_stack_path(
            jj_client=client,
            revset=commit_id,
            state=state,
        ).stack
    except UnsupportedStackError as error:
        raise stack_preparation_cli_error(error) from error


async def _load_pr_chain(
    *,
    github_client: GithubClient,
    repo: GithubRepoAddress,
    top: GithubPR,
) -> tuple[GithubPR, ...]:
    """Follow PR base branches from the selected PR to trunk."""

    namespace = current_pr_branch_namespace()
    top_down = [top]
    seen = {top.head.ref}
    base = top.base.ref
    while namespace.contains(base):
        if base in seen:
            raise CliError(
                t"Pull request base branches point at each other in a loop, starting at "
                t"{ui.bookmark(base)}.",
                hint=t"Retarget one of those pull requests on GitHub so the stack has a "
                t"bottom, then rerun {ui.cmd('jj-stack checkout')}.",
            )
        seen.add(base)
        try:
            matches = (await github_client.get_open_prs_by_head_refs(head_refs=(base,))).get(
                base,
                (),
            )
        except GithubClientError as error:
            raise CliError(f"Could not inspect pull request branch {base}") from error
        if len(matches) != 1:
            raise CliError(
                t"PR base branch {ui.bookmark(base)} must belong to one open pull request, "
                t"but GitHub reports {len(matches)}.",
                hint=t"Check the PR's base branch on GitHub. To link a known open PR to a "
                t"local change, run {ui.cmd('jj-stack relink <pr> <change-id>')}.",
            )
        parent = matches[0]
        require_managed_pr_head(
            pr=parent,
            repo=repo,
        )
        top_down.append(parent)
        base = parent.base.ref
    return tuple(reversed(top_down))


def _save_checkout_tracking(
    *,
    context: CommandContext,
    prs: tuple[GithubPR, ...],
    remote_targets: dict[str, CommitId],
    repo: GithubRepoAddress,
    stack: LocalStack,
    state: TrackingState,
) -> int:
    pr_heads = tuple(require_managed_pr_head(pr=pr, repo=repo) for pr in prs)
    changes = stack.changes[: len(prs)]
    if len(changes) < len(prs):
        raise CliError(
            "The selected pull requests do not describe the stack that was just fetched.",
            hint=t"Compare {ui.cmd('jj log')} with the PRs on GitHub. Use "
            t"{ui.cmd('jj-stack submit')} to update the PRs from local history, or "
            t"{ui.cmd('jj-stack relink <pr> <change-id>')} to repair a saved link.",
        )
    # The stack was discovered from the top PR's head, so any changes above the top PR's own
    # change are additions to its branch. Each lower PR's head must be exactly its change's
    # commit, and every branch must name the change it is paired with.
    replacements: dict[str, TrackedPR] = {}
    for pr, head_sha, change in zip(prs, pr_heads, changes, strict=True):
        _require_branch_matches_change(branch=pr.head.ref, change=change)
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        if pr is not prs[-1] and head_sha != change.commit_id:
            raise CliError(
                t"{pr_label} is at a commit that "
                t"{format_pr_label(prs[-1].number, url=prs[-1].html_url)} does not build on.",
                hint=t"Check out that pull request first with "
                t"{ui.cmd(f'jj-stack checkout --pull-request {pr.number}')}.",
            )
        if remote_targets.get(pr.head.ref) != head_sha:
            raise CliError(
                t"{pr_label} and branch "
                t"{ui.bookmark(pr.head.ref)} no longer identify the same commit."
            )
        replacements[change.change_id] = TrackedPR(
            pr_identity=PRIdentity(
                pr_number=pr.number,
                head_ref=pr.head.ref,
            ),
            submitted_baseline=SubmittedBaseline(commit_id=head_sha),
        )
    _reject_duplicate_checkout_claims(
        current={change_id: tracked.pr_identity for change_id, tracked in state.prs.items()},
        replacements={
            change_id: tracked.pr_identity for change_id, tracked in replacements.items()
        },
    )
    changed_count = sum(
        state.prs.get(change_id) != replacement for change_id, replacement in replacements.items()
    )
    if not changed_count:
        return 0
    context.state_store.relink_prs(
        replacements=replacements,
    )
    return changed_count


def _reject_duplicate_checkout_claims(
    *,
    current: dict[str, PRIdentity],
    replacements: dict[str, PRIdentity],
) -> None:
    combined = dict(current)
    combined.update(replacements)
    if duplicate_pr_claim_change_ids(combined).intersection(replacements):
        raise CliError(
            "Another local change is already linked to one of those pull request numbers or "
            "branches.",
            hint=t"Run {ui.cmd('jj-stack list')} to find the linked change. To forget its "
            t"stack's saved links, run {ui.cmd('jj-stack unstack --local <change-id>')}. "
            t"For a closed or merged PR, use {ui.cmd('jj-stack cleanup --pull-request <pr>')}.",
        )


def _require_branch_matches_change(*, branch: str, change: LocalCommit) -> None:
    if not pr_branch_matches_change(branch, change.change_id):
        raise CliError(
            t"PR branch {ui.bookmark(branch)} does not match change "
            t"{ui.change_id(change.change_id)}."
        )


async def _pick_stack(
    context: CommandContext,
    *,
    github_client: GithubClient,
    repo: GithubRepoAddress,
) -> CheckoutPickerChoice:
    """Prompt for one local or GitHub stack without holding the operation lock."""

    state = context.state_store.load()
    if not state.prs:
        local_stacks: list[LocalStack] = []
    else:
        repo_paths = observe_repo_paths(
            jj_client=context.jj_client,
            state=state,
        )
        local_stacks = sorted(
            (path.stack for path in repo_paths.paths if path.tracked_change_ids),
            key=lambda stack: stack.head.change_id,
        )
    repo_task = asyncio.create_task(observe_github_repo(github_client))
    stacks_task = asyncio.create_task(observe_github_stacks(github=github_client))
    await wait_for_read_tasks(repo_task, stacks_task)
    github_stacks = stacks_task.result()
    try:
        prs = await github_client.get_prs_by_numbers(
            pr_numbers=tuple(member.number for stack in github_stacks for member in stack.prs),
        )
    except GithubClientError as error:
        raise CliError("Could not list GitHub stacks for checkout.") from error
    choices = _picker_choices(
        github_stacks=github_stacks,
        local_stacks=local_stacks,
        prs=prs,
        repo=repo,
        state=state,
        visible_commit_ids={
            commit.commit_id
            for commit in context.jj_client.query_commits_by_ids(
                tuple(member.head.sha for stack in github_stacks for member in stack.prs)
            )
            if not commit.hidden
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
            hint=t"Use {ui.cmd('jj-stack checkout --pull-request PR')} to link a pull request "
            t"directly.",
        )
    console.output("Available stacks:")
    for index, choice in enumerate(choices, start=1):
        console.output((f"  [{index}] ", choice.heading))
        for detail in choice.details:
            console.output(("      ", detail))
    console.output(t"Pick a stack [1-{len(choices)}]: ")
    selection = sys.stdin.readline().strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(choices):
        raise UsageError(
            t"{ui.cmd(selection or '(empty)')} is not a valid choice; "
            t"enter a number from 1 to {len(choices)}."
        )
    return choices[int(selection) - 1]


def _picker_choices(
    *,
    github_stacks: tuple[GithubStack, ...],
    local_stacks: list[LocalStack],
    prs: dict[int, GithubPR | None],
    repo: GithubRepoAddress,
    state: TrackingState,
    visible_commit_ids: set[str],
) -> tuple[CheckoutPickerChoice, ...]:
    saved_by_pr = {
        tracked.pr_identity.pr_number: (change_id, tracked.pr_identity)
        for change_id, tracked in state.prs.items()
    }
    choices: list[CheckoutPickerChoice] = []
    listed_pr_numbers: set[int] = set()
    for stack in sorted(github_stacks, key=lambda candidate: candidate.number):
        active_numbers = stack.active_pr_numbers
        if not active_numbers:
            continue
        numbers = stack.pr_numbers
        members = tuple(prs.get(number) for number in numbers)
        if any(member is None for member in members):
            missing = next(
                number for number, member in zip(numbers, members, strict=True) if member is None
            )
            pr_label = format_pr_label(missing, repo=repo)
            raise CliError(t"GitHub stack #{stack.number} refers to missing {pr_label}.")
        resolved = tuple(member for member in members if member is not None)
        if not all(_picker_pr_is_adoptable(member, repo) for member in resolved):
            continue
        bottom = resolved[0]
        top = next(member for member in reversed(resolved) if member.number in active_numbers)
        statuses = Counter(_picker_pr_status(member) for member in resolved)
        status = ", ".join(
            f"{count} {name}"
            for name in ("open", "draft", "closed", "merged")
            if (count := statuses[name])
        )
        active_members = tuple(member for member in resolved if member.number in active_numbers)
        change_id_by_pr = {
            member.number: saved[0]
            for member in active_members
            if (saved := saved_by_pr.get(member.number)) is not None
            and saved[1].matches_pr(member)
        }
        local = len(change_id_by_pr) == len(active_members) and all(
            member.head.sha in visible_commit_ids for member in active_members
        )
        visible_count = sum(member.head.sha in visible_commit_ids for member in active_members)
        locality = "local" if local else "partly local" if visible_count else "GitHub only"
        noun = "PR" if len(numbers) == 1 else "PRs"
        choices.append(
            CheckoutPickerChoice(
                heading=f"GitHub stack #{stack.number} ({locality})",
                details=(
                    t"Top: {format_pr_label(top.number, url=top.html_url)} {top.title}",
                    f"Base: {bottom.base.ref}",
                    f"Size: {len(numbers)} {noun}",
                    f"Status: {status}",
                ),
                revset=change_id_by_pr[top.number] if local else None,
                pr=None if local else str(top.number),
            )
        )
        listed_pr_numbers.update(numbers)
    for path in local_stacks:
        tracked = state.prs.get(path.head.change_id)
        if tracked is not None and tracked.pr_identity.pr_number in listed_pr_numbers:
            continue
        count = len(path.changes)
        noun = "change" if count == 1 else "changes"
        choices.append(
            CheckoutPickerChoice(
                heading=f"Local stack {path.head.change_id}",
                details=(f"Head: {path.head.subject}", f"Size: {count} {noun}"),
                revset=path.head.change_id,
            )
        )
    return tuple(choices)


def _picker_pr_status(pr: GithubPR) -> str:
    if pr.state == "open" and pr.is_draft:
        return "draft"
    return pr.state


def _picker_pr_is_adoptable(
    pr: GithubPR,
    repo: GithubRepoAddress,
) -> bool:
    return current_pr_branch_namespace().contains(pr.head.ref) and (
        pr.head.label == f"{repo.owner}:{pr.head.ref}"
    )
