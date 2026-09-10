"""Update a local stack after GitHub merges or rebases its pull requests.

`jj-stack sync` fetches trunk, removes obsolete local copies of merged changes, rebases the
remaining changes, updates their existing pull requests, and cleans up unused PR branches,
stack overview comments, and saved links. It never creates pull requests.

Run it after a merge queue finishes, after someone merges the PRs through another client, or
after GitHub's Rebase stack action rewrites the PR branches. When GitHub merges immediately,
`jj-stack merge` performs this update itself. While a selected PR is still queued, sync leaves
the stack unchanged.

After a Rebase stack action, sync checks that the PR order and contents match, rebases your
original changes, and updates the PR branches with commits that retain their jj change IDs.

Sync stops if it would discard local edits or cannot determine which local changes and PRs to
update. The error explains what needs attention. If a rebase produces conflicts, the local rebase
stays in place but the affected PRs are not updated. Resolve the conflicts with `jj`, then run
`jj-stack submit <head-change-id>`.

`jj-stack sync --all` updates every local stack affected by a completed merge and cleans up merged
PRs whose local changes are gone. A blocked stack does not prevent it from syncing independent
stacks. It does not handle Rebase stack actions; use `jj-stack sync <head-change-id>` for those.

Use plain `jj rebase` when trunk merely advanced and GitHub did not rewrite the commits.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.cleanup.command import cleanup_tracked_prs
from jj_stack.commands.submit.render import print_selected_line
from jj_stack.commands.sync_apply import apply_pr_finishes, apply_selected_convergence
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.errors import (
    CliError,
    UnsupportedStackError,
    UsageError,
    error_hint,
    error_message,
    resolve_exit_code,
)
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import (
    GithubTarget,
    UnresolvedGithubTarget,
    resolve_github_target,
    resolve_trunk_branch,
)
from jj_stack.identifiers import CommitId
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import quote_revset_symbol
from jj_stack.models.github import GithubStack
from jj_stack.pr_branch_namespace import current_pr_branch_namespace
from jj_stack.stack.convergence import (
    CheckedOutMergedChangeError,
    build_selected_convergence_plan,
)
from jj_stack.stack.convergence_models import GithubStackRebasePlan, SelectedConvergencePlan
from jj_stack.stack.convergence_observation import (
    complete_sync_observation,
    queued_pr_numbers,
)
from jj_stack.stack.global_convergence import (
    build_global_convergence_plan,
    observe_global_sync,
)
from jj_stack.stack.pr_facts import (
    classify_observed_commit_ancestries,
    observe_github_stacks,
    observe_prs,
)
from jj_stack.stack.preparation import (
    PreparedLocalStack,
    prepare_local_stack,
    stack_preparation_cli_error,
)
from jj_stack.stack.selection import resolve_linked_change_for_pr
from jj_stack.state.operation_lock import operation_lock_if_mutating
from jj_stack.ui import Message

HELP = "Update a local stack after GitHub merges or rebases it"


def sync(
    *,
    all_: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    pr: str | None,
    repo: Path | None,
    revset: str | None,
) -> int:
    if sum((all_, pr is not None, revset is not None)) > 1:
        raise UsageError("Use only one of sync --all, --pull-request, or a revset.")
    context = bootstrap_context(repo=repo, cli_args=cli_args, debug=debug)
    with operation_lock_if_mutating(
        context.state_store,
        command="sync --all" if all_ else "sync",
        mutating=not dry_run,
    ):
        if not dry_run:
            context.jj_client.clear_pr_branch_temp_artifacts()
        if all_:
            return _run_all_convergence(context=context, dry_run=dry_run)
        containing_change_id = None
        if pr is not None:
            _, containing_change_id, _ = resolve_linked_change_for_pr(
                jj_client=context.jj_client, pr_reference=pr, revset=None
            )
        return run_stack_convergence(
            context=context,
            containing_change_id=containing_change_id,
            dry_run=dry_run,
            print_selected=revset is None,
            revset=revset,
        )


def _run_all_convergence(*, context: CommandContext, dry_run: bool) -> int:
    target = _require_github_target(resolve_github_target(context.jj_client.list_git_remotes()))
    with console.spinner(description="Fetching trunk") as progress:
        previous_trunk = context.jj_client.resolve_commit("trunk()")
        branches = tuple(
            branch
            for branch in context.jj_client.remote_bookmarks_at_commit(
                remote=target.remote.name,
                commit_id=previous_trunk.commit_id,
            )
            if not current_pr_branch_namespace().contains(branch)
        )
        context.jj_client.fetch_remote(branches=branches, remote=target.remote.name)
        progress.update("Comparing pull requests with trunk")
        trunk = context.jj_client.resolve_commit("trunk()")
    exit_code, change_ids, trunk_branch = asyncio.run(
        _run_global_plan(
            context=context,
            dry_run=dry_run,
            target=target,
            trunk_commit_id=trunk.commit_id,
        )
    )
    for change_id in change_ids:
        console.output(t"Syncing local stack {ui.change_id(change_id)}:")
        try:
            stack_exit_code = run_stack_convergence(
                context=context,
                dry_run=dry_run,
                fetch_remote_state=False,
                revset=change_id,
                trunk_branch=trunk_branch,
            )
        except CliError as error:
            console.error(
                t"Could not sync local stack {ui.change_id(change_id)}: {error_message(error)}"
            )
            if hint := error_hint(error):
                console.stderr_output(
                    (ui.semantic_text("Hint: ", "hint", "heading"), hint),
                    soft_wrap=True,
                )
            if exit_code == 0:
                exit_code = resolve_exit_code(error)
        else:
            if exit_code == 0:
                exit_code = stack_exit_code
    return exit_code


async def _run_global_plan(
    *,
    context: CommandContext,
    dry_run: bool,
    target: GithubTarget,
    trunk_commit_id: CommitId,
) -> tuple[int, tuple[str, ...], str | None]:
    async with build_github_client(repo=target.repo) as github:
        with console.spinner(description="Inspecting tracked pull requests"):
            try:
                facts = await observe_global_sync(
                    context=context,
                    github=github,
                    remote_name=target.remote.name,
                    trunk_commit_id=trunk_commit_id,
                )
            except GithubClientError as error:
                raise CliError("Could not inspect pull requests") from error
            state = context.state_store.load()
            plan = build_global_convergence_plan(
                facts=facts,
                state=state,
            )
        for change_id, candidate, reason in plan.blocked:
            pr_label = format_pr_label(candidate.pr_identity.pr_number, repo=facts.pr_facts.repo)
            console.warning(t"Skipped {pr_label} for {ui.change_id(change_id)}: {reason}.")
        trunk_branch = None
        if plan.sync_change_ids:
            repo_state = facts.pr_facts.github_repo
            trunk_branch, _targets = resolve_trunk_branch(
                branches_at_trunk=context.jj_client.remote_bookmarks_at_commit(
                    remote=target.remote.name,
                    commit_id=trunk_commit_id,
                ),
                github_repo_state=repo_state,
                remote=target.remote,
                trunk_commit_id=trunk_commit_id,
            )
        results = await apply_pr_finishes(
            plans=plan.finishes,
            dry_run=dry_run,
            github=github,
        )
        cleanup = await cleanup_tracked_prs(
            change_ids=tuple(
                result.change_id for result in results if result.outcome != "skipped"
            ),
            context=context,
            dry_run=dry_run,
            github_client=github,
            github_target=target,
            planned_detached_dependents=frozenset(
                result.candidate.pr_identity.pr_number for result in results
            ),
        )
    blocked = (
        bool(plan.blocked)
        or any(result.outcome == "skipped" for result in results)
        or any(action.status == "blocked" for action in cleanup.actions)
    )
    return 1 if blocked else 0, plan.sync_change_ids, trunk_branch


def run_stack_convergence(
    *,
    context: CommandContext,
    containing_change_id: str | None = None,
    dry_run: bool,
    fetch_remote_state: bool = True,
    print_selected: bool = False,
    revset: str | None,
    trunk_branch: str | None = None,
) -> int:
    with console.spinner(description="Inspecting local stack"):
        try:
            prepared = prepare_local_stack(
                containing_change_id=containing_change_id,
                context=context,
                fetch_remote_state=fetch_remote_state,
                revset=revset,
            )
        except UnsupportedStackError as error:
            raise stack_preparation_cli_error(error) from error
    if print_selected and prepared.stack.changes:
        head = prepared.stack.head
        print_selected_line(head.change_id, head.subject)
    try:
        return asyncio.run(
            _run_selected_convergence(
                context=context,
                dry_run=dry_run,
                prepared=prepared,
                trunk_branch=trunk_branch,
            )
        )
    except CheckedOutMergedChangeError as error:
        raise CliError(
            error.message,
            hint=_checked_out_workspace_hint(
                workspaces=error.workspaces,
                context=context,
            ),
        ) from error


async def _run_selected_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    prepared: PreparedLocalStack,
    trunk_branch: str | None,
) -> int:
    target = _require_github_target(prepared.github_target)
    selected = prepared.stack.changes
    if not selected:
        console.output("Nothing to sync: the selected change is already on trunk.")
        return 0
    complete = False
    github_stacks: tuple[GithubStack, ...] = ()
    async with build_github_client(repo=target.repo) as github:
        with console.spinner(description="Inspecting pull requests") as progress:
            prs_task = asyncio.create_task(
                observe_prs(
                    change_ids=tuple(change.change_id for change in selected),
                    context=context,
                    github_client=github,
                    include_remote_targets=False,
                    remote_name=target.remote.name,
                )
            )
            stacks_task = asyncio.create_task(observe_github_stacks(github=github))
            await wait_for_read_tasks(prs_task, stacks_task)
            observation = prs_task.result()
            observed_stacks = stacks_task.result()
            queued = queued_pr_numbers(observation, selected)
            if not queued:
                progress.update("Checking PR branches")
                observation, github_stacks, complete = await complete_sync_observation(
                    context=context,
                    github=github,
                    initial=observation,
                    remote_name=target.remote.name,
                    selected=selected,
                    stacks=observed_stacks,
                )
                queued = queued_pr_numbers(observation, selected)
        if queued:
            labels = ui.join(
                lambda number: format_pr_label(number, repo=observation.repo),
                queued,
            )
            console.output(
                t"Stack unchanged because the merge queue still contains {labels}. Run "
                t"{ui.cmd('jj-stack sync')} again after GitHub finishes."
            )
            return 0
        if not complete:
            console.output("No completed merges or GitHub stack rebases to sync.")
            return 0
        with console.spinner(description="Planning local sync"):
            repo_state = observation.github_repo
            if trunk_branch is None:
                trunk_branch, _trunk_targets = resolve_trunk_branch(
                    branches_at_trunk=context.jj_client.remote_bookmarks_at_commit(
                        remote=target.remote.name,
                        commit_id=prepared.stack.trunk.commit_id,
                    ),
                    github_repo_state=repo_state,
                    remote=target.remote,
                    trunk_commit_id=prepared.stack.trunk.commit_id,
                )
            ancestries = classify_observed_commit_ancestries(
                context=context,
                observation=observation,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
            plan = build_selected_convergence_plan(
                ancestries=ancestries,
                github_stacks=github_stacks,
                head_children=context.jj_client.query_commits(
                    f"children({quote_revset_symbol(selected[-1].commit_id)})"
                ),
                observation=observation,
                prepared=prepared,
                trunk_branch=trunk_branch,
            )
        _render_selected_plan(dry_run=dry_run, plan=plan)
        return await apply_selected_convergence(
            context=context,
            dry_run=dry_run,
            github=github,
            plan=plan,
            github_stacks=observed_stacks,
            trunk_branch=trunk_branch,
            target=target,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )


def _require_github_target(
    target: GithubTarget | UnresolvedGithubTarget,
) -> GithubTarget:
    if not isinstance(target, GithubTarget):
        raise CliError(
            target.github_repo_error or "Could not resolve GitHub target.",
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
    return target


def _render_selected_plan(*, dry_run: bool, plan: SelectedConvergencePlan) -> None:
    if isinstance(plan, GithubStackRebasePlan):
        action = "Would apply" if dry_run else "Applying"
        console.output(f"{action} GitHub's stack rebase using the original jj change IDs.")
        return
    if not plan.actions.on_trunk:
        console.output("No completed merges to apply to this stack.")
        return
    status = "Would remove" if dry_run else "Removing"
    console.output(
        t"{status} merged changes from the bottom of the stack: "
        t"{ui.join(lambda item: ui.change_id(item.change_id), plan.actions.on_trunk)}"
    )


def _checked_out_workspace_hint(
    *, workspaces: tuple[str, ...], context: CommandContext
) -> Message:
    known = {workspace.name: workspace for workspace in context.jj_client.list_workspaces()}
    if not workspaces:
        workspaces = tuple(workspace.name for workspace in known.values() if workspace.current)
    hint: list[Message] = ["Move each workspace off the merged change:\n"]
    disposable: list[tuple[str, str]] = []
    for name in workspaces:
        workspace = known.get(name)
        if workspace is None or (workspace.root is None and not workspace.current):
            forget_command = _workspace_forget_command(name=name, platform=sys.platform)
            hint.append(
                t"jj no longer reports a directory for {ui.code(name)}. If the workspace was "
                t"deleted, forget it:\n  {ui.cmd(forget_command)}\n"
                t"If it still exists elsewhere, run {ui.cmd("jj new 'trunk()'")} from its "
                t"directory.\n"
            )
            continue
        root = str(workspace.root or context.repo_root)
        shell = " (PowerShell)" if sys.platform == "win32" else ""
        hint.append(
            t"For {ui.code(name)} at {ui.code(root)}{shell}:\n  "
            t"{ui.cmd(_workspace_move_command(root=root, platform=sys.platform))}\n"
        )
        if not workspace.current:
            disposable.append((name, root))
    if disposable:
        hint.append(
            "Alternatively, forget and move to the trash any workspace that is no longer "
            "needed:\n"
        )
        for name, root in disposable:
            shell = " (PowerShell)" if sys.platform == "win32" else ""
            command = _workspace_disposal_command(name=name, root=root, platform=sys.platform)
            hint.append(t"For {ui.code(name)}{shell}:\n  {ui.cmd(command)}\n")
    hint.append("Then rerun the same jj-stack sync command.")
    return tuple(hint)


def _workspace_move_command(*, root: str, platform: str) -> str:
    if platform == "win32":
        return (
            f"Push-Location -LiteralPath {_powershell_quote(root)}; try {{ "
            "jj new 'trunk()' } finally { Pop-Location }"
        )
    return f"(cd {shlex.quote(root)} && jj new {shlex.quote('trunk()')})"


def _workspace_disposal_command(*, name: str, root: str, platform: str) -> str:
    forget_command = _workspace_forget_command(name=name, platform=platform)
    if platform == "win32":
        quoted = _powershell_quote(root)
        return (
            f"{forget_command}; if ($LASTEXITCODE -eq 0) {{ "
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
            f"{quoted}, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
            "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin) }"
        )
    trash_command = "trash" if platform == "darwin" else "gio trash"
    return f"({forget_command} && {trash_command} {shlex.quote(root)})"


def _workspace_forget_command(*, name: str, platform: str) -> str:
    quoted_name = _powershell_quote(name) if platform == "win32" else shlex.quote(name)
    return f"jj workspace forget -- {quoted_name}"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
