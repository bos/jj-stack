"""Apply completed GitHub merges to a local stack and refresh the pull requests that remain.

`sync` fetches trunk and determines which submitted changes have reached it. It then rebases the
remaining changes, updates only their existing pull requests, and removes PR branches,
comments, and saved links for merged pull requests. If GitHub used rebase merging, `sync`
verifies the new commits, applies them locally, and restores the original `jj` change IDs. A
completed direct `merge` performs the same update. Neither command creates a pull request.

`sync` stops before rebasing in any of these cases:

- A remaining change has multiple visible commits. `sync` cannot choose one.

- A merged change contains edits made after it was submitted. Removing it would discard work.

- A local change that has not merged is a parent of submitted work that has merged. Moving the
  local change could put it before or after the merged work. `sync` will not choose for you.

- An unsubmitted change sits between submitted changes. `sync` updates existing pull requests but
  never creates the missing pull request.

Before rebasing, `sync` also checks saved pull request links and GitHub stack membership. A
missing or closed pull request, a changed stack relationship, or ambiguous tracking stops the
command before it changes local history. The error identifies what needs attention.

Conflicts do not prevent the local rebase. If a rebased change remains conflicted, `sync` leaves
the conflict in local history and stops before updating that pull request. Resolve the conflict
with `jj`, then run `jj-stack submit`.

Rebasing a `jj` change also rebases its descendants. This may move local work above the selected
stack, but `sync` updates pull requests only for the selected stack.

Another local stack may share a merged change with the stack being synced. If that stack still
uses the old local change, `sync` leaves the change in place and prints the other stack to sync
next. Rerunning `sync` skips completed work and continues.

`sync --all` checks every pull request known to `jj-stack`. It updates each affected local stack
in turn and also cleans up pull requests whose submitted commits are on trunk even when their
local changes are gone.

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
from jj_stack.errors import (
    CliError,
    UsageError,
    error_hint,
    error_message,
    resolve_exit_code,
)
from jj_stack.github.client import GithubClientError, build_github_client
from jj_stack.github.resolution import GithubTarget, resolve_github_target, resolve_trunk_branch
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import UnsupportedStackError
from jj_stack.models.stack import LocalCommit
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
    RepoFacts,
    observe_github_stacks,
    observe_prs,
)
from jj_stack.stack.status import PreparedStatus, prepare_status, status_preparation_cli_error
from jj_stack.stack.trunk_evidence import classify_commit_ancestries
from jj_stack.state.operation_lock import acquire_operation_lock
from jj_stack.ui import Message

HELP = "Apply completed GitHub merges locally and refresh the pull requests that remain"


def sync(
    *,
    all_: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repo: Path | None,
    revset: str | None,
) -> int:
    if all_ and revset is not None:
        raise UsageError(t"Use either {ui.cmd('jj-stack sync --all')} or a revset, not both.")
    context = bootstrap_context(repo=repo, cli_args=cli_args, debug=debug)
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="sync --all" if all_ else "sync",
    ):
        if all_:
            return _run_all_convergence(context=context, dry_run=dry_run)
        return run_stack_convergence(
            context=context,
            dry_run=dry_run,
            print_selected=revset is None,
            revset=revset,
        )


def _run_all_convergence(*, context: CommandContext, dry_run: bool) -> int:
    target = resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, GithubTarget):
        raise CliError(
            target.github_repo_error or "Could not resolve GitHub target.",
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
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
        console.output(t"Syncing local stack {ui.change_id(change_id[:8])}:")
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
                t"Could not sync local stack {ui.change_id(change_id[:8])}: "
                t"{error_message(error)}"
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
    trunk_commit_id: str,
) -> tuple[int, tuple[str, ...], str | None]:
    async with build_github_client(repo=target.repo) as github:
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
            repo=target.repo,
            state=state,
        )
        for candidate, reason in plan.blocked:
            console.warning(
                t"Skipped PR #{candidate.pr_identity.pr_number} for "
                t"{ui.change_id(candidate.change_id)}: {reason}."
            )
        required = bool(plan.finishes or plan.sync_change_ids)
        trunk_branch = None
        if required:
            repo_state = facts.pr_facts.github_repo
            if repo_state is None:
                raise AssertionError("Global sync requires GitHub repo state.")
            trunk_branch, _targets = resolve_trunk_branch(
                client=context.jj_client,
                github_repo_state=repo_state,
                remote=target.remote,
                trunk_commit_id=trunk_commit_id,
            )
        results = (
            await apply_pr_finishes(
                plans=plan.finishes,
                dry_run=dry_run,
                github=github,
                trunk_branch=trunk_branch,
            )
            if trunk_branch is not None
            else ()
        )
        cleanup = await cleanup_tracked_prs(
            change_ids=tuple(
                result.candidate.change_id for result in results if result.outcome != "skipped"
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
    dry_run: bool,
    fetch_remote_state: bool = True,
    print_selected: bool = False,
    revset: str | None,
    trunk_branch: str | None = None,
) -> int:
    try:
        prepared_status = prepare_status(
            context=context,
            fetch_remote_state=fetch_remote_state,
            observe_remote_targets=False,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    if print_selected and prepared_status.prepared.stack.changes:
        head = prepared_status.prepared.stack.head
        print_selected_line(head.change_id, head.subject)
    try:
        return asyncio.run(
            _run_selected_convergence(
                context=context,
                dry_run=dry_run,
                prepared_status=prepared_status,
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
    prepared_status: PreparedStatus,
    trunk_branch: str | None,
) -> int:
    prepared = prepared_status.prepared
    target, selected = _selected_target(prepared_status)
    if not selected:
        console.output("Nothing to sync: the selected change is already on trunk.")
        return 0
    async with build_github_client(repo=target.repo) as github:
        observation, observed_stacks = await asyncio.gather(
            observe_prs(
                change_ids=tuple(change.change_id for change in selected),
                context=context,
                github_client=github,
                include_remote_targets=False,
                remote_name=target.remote.name,
            ),
            observe_github_stacks(github=github),
        )
        if queued := queued_pr_numbers(observation, selected):
            _render_queued_sync(queued)
            return 0
        observation, github_stacks, complete = await complete_sync_observation(
            context=context,
            github=github,
            initial=observation,
            remote_name=target.remote.name,
            repo=target.repo,
            selected=selected,
            stacks=observed_stacks,
        )
        if queued := queued_pr_numbers(observation, selected):
            _render_queued_sync(queued)
            return 0
        if not complete:
            console.output("No merged changes in this stack need rebasing.")
            return 0
        repo_state = observation.github_repo
        if repo_state is None:
            raise AssertionError("Sync observation requires GitHub repo state.")
        if trunk_branch is None:
            trunk_branch, _trunk_targets = resolve_trunk_branch(
                client=prepared.client,
                github_repo_state=repo_state,
                remote=target.remote,
                trunk_commit_id=prepared.stack.trunk.commit_id,
            )
        ancestries = _classify_observed_ancestries(
            context=context,
            observation=observation,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        plan = build_selected_convergence_plan(
            ancestries=ancestries,
            context=context,
            github_stacks=github_stacks,
            observation=observation,
            prepared_status=prepared_status,
            repo=target.repo,
            trunk_branch=trunk_branch,
        )
        _render_selected_plan(dry_run=dry_run, plan=plan)
        return await apply_selected_convergence(
            context=context,
            dry_run=dry_run,
            github=github,
            plan=plan,
            target=target,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )


def _render_queued_sync(pr_numbers: tuple[int, ...]) -> None:
    console.output(
        t"Nothing to sync while the selected pull request is in the merge queue "
        t"({ui.join(lambda number: f'PR #{number}', pr_numbers)})."
    )


def _selected_target(
    prepared_status: PreparedStatus,
) -> tuple[GithubTarget, tuple[LocalCommit, ...]]:
    target = prepared_status.github_target
    if not isinstance(target, GithubTarget):
        raise CliError(
            target.github_repo_error or "Could not resolve GitHub target.",
            hint=t"Point jj-stack at a GitHub remote, then rerun. "
            t"{ui.cmd('jj-stack doctor')} reports what it found.",
        )
    return target, prepared_status.prepared.stack.changes


def _render_selected_plan(*, dry_run: bool, plan: SelectedConvergencePlan) -> None:
    if isinstance(plan, GithubStackRebasePlan):
        action = "Would restore" if dry_run else "Restoring"
        console.output(f"{action} the stack's jj change IDs after GitHub rebased it.")
        return
    if not plan.actions.on_trunk:
        console.output("No merged changes in this stack need rebasing.")
        return
    status = "Would remove" if dry_run else "Removing"
    console.output(
        t"{status} merged changes from the bottom of the stack: "
        t"{ui.join(lambda item: ui.change_id(item.candidate.change_id), plan.actions.on_trunk)}"
    )


def _classify_observed_ancestries(
    *,
    context: CommandContext,
    observation: RepoFacts,
    trunk_commit_id: str,
) -> dict:
    return classify_commit_ancestries(
        commit_ids=tuple(
            commit_id
            for item in observation.prs.values()
            for commit_id in (
                item.baseline.commit_id if item.baseline is not None else None,
                item.pr.merge_commit_sha if item.pr is not None else None,
            )
        ),
        context=context,
        trunk_commit_id=trunk_commit_id,
    )


def _checked_out_workspace_hint(
    *, workspaces: tuple[str, ...], context: CommandContext
) -> Message:
    known = {workspace.name: workspace for workspace in context.jj_client.list_workspaces()}
    if not workspaces:
        workspaces = tuple(workspace.name for workspace in known.values() if workspace.current)
    hint: list[Message] = ["Move off the merged change in each workspace:\n"]
    disposable: list[tuple[str, str]] = []
    for name in workspaces:
        workspace = known.get(name)
        if workspace is None or (workspace.root is None and not workspace.current):
            hint.append(
                t"For {ui.code(name)}, run {ui.cmd("jj new 'trunk()'")} in that workspace.\n"
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
    hint.append("Then rerun the same sync command.")
    return tuple(hint)


def _workspace_move_command(*, root: str, platform: str) -> str:
    if platform == "win32":
        return (
            f"Push-Location -LiteralPath {_powershell_quote(root)}; try {{ "
            "jj new 'trunk()' } finally { Pop-Location }"
        )
    return f"(cd {shlex.quote(root)} && jj new {shlex.quote('trunk()')})"


def _workspace_disposal_command(*, name: str, root: str, platform: str) -> str:
    if platform == "win32":
        quoted_name = _powershell_quote(name)
        quoted = _powershell_quote(root)
        return (
            f"jj workspace forget -- {quoted_name}; if ($LASTEXITCODE -eq 0) {{ "
            "Add-Type -AssemblyName Microsoft.VisualBasic; "
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
            f"{quoted}, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
            "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin) }"
        )
    trash_command = "trash" if platform == "darwin" else "gio trash"
    return f"(jj workspace forget -- {shlex.quote(name)} && {trash_command} {shlex.quote(root)})"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
