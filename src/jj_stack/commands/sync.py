"""Fetch remote state and repair the selected stack after merges.

The intended behavior is to fetch trunk, recognize merged changes from current GitHub
and commit data, rebase the remaining selected changes, and update only PRs that already
exist for them. It does not open a PR for trailing work or change another local stack.
A separate `sync --all` mode will finish cleanup for reviews whose submitted commits are
already on trunk, without rebasing or submitting any stack.

Development status: those boundaries are not implemented yet. The current build still
checks every tracked review and runs ordinary `submit`, so it may retarget or close PRs
for other tracked stacks and may open PRs. Preview an explicit selection with
`sync --dry-run <change-id>` before live recovery until that work lands.

`sync` is also the recovery command. After an interrupted `land` or `sync`, rerun it so
the command can continue from the current jj and GitHub state.

`sync` only rewrites history to remove merged changes. It does not rebase your stack
onto newer trunk commits when nothing in the stack has merged; use `jj rebase` for
that. It also takes no submit flags: runs that need draft handling, descriptions,
reviewers, or restart behavior use `submit` directly.

With `--dry-run`, `sync` fetches remote state and previews the current build's rebase
plan. Fetching can update jj's remote-bookmark observations, but the command does not
apply the planned rebase, push, PR, cleanup, or tracking changes. The submit preview
follows only when no rebase work is planned, because a submit preview taken before the
rebase would describe the wrong stack.
"""

from __future__ import annotations

from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands.cleanup.rebase import run_cleanup_rebase_command
from jj_stack.commands.submit.command import print_selected_line, run_submit
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_submit_result
from jj_stack.github.resolution import GithubTarget, resolve_github_target
from jj_stack.jj.client import JjCliArgs
from jj_stack.review.landed import LandedReviewResult, run_landed_review_sweep
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Fetch remote state and repair reviewed stacks after merges"


def sync(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `sync`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    selected_revset = revset if revset is not None else "@-"
    with acquire_operation_lock(
        context.state_store.require_writable(),
        command="sync",
    ):
        return run_stack_convergence(
            context=context,
            dry_run=dry_run,
            print_selected=revset is None,
            revset=selected_revset,
        )


def run_stack_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    print_selected: bool = False,
    revset: str,
) -> int:
    """Converge the selected stack with what GitHub and the remote report.

    This is the current convergence routine: `sync` is a thin wrapper around it,
    and `land` invokes it after GitHub accepts merges. It composes the legacy
    cleanup rebase, landed-review sweep, and ordinary submit until selected-only
    convergence replaces it.
    """

    rebase_result = run_cleanup_rebase_command(
        context=context,
        dry_run=dry_run,
        rebase_revset=revset,
    )
    # Landed reviews retire even when the surviving stack is blocked: the
    # sweep's work is independent of the selected path's rebase plan.
    sweep_landed_reviews(context=context, dry_run=dry_run)
    if rebase_result.blocked:
        return 1
    if dry_run and any(action.status == "planned" for action in rebase_result.actions):
        console.output(
            t"Submit preview skipped: run {ui.cmd('jj-stack sync')} {ui.revset(revset)} "
            t"without {ui.cmd('--dry-run')} to apply the rebase first."
        )
        return 0
    if rebase_result.fully_merged:
        console.output("Nothing to submit: everything on the selected stack has merged.")
        return 0
    result = run_submit(
        context=context,
        # The selected line is only rendered when sync picked the default
        # head for the user.
        on_prepared=print_selected_line if print_selected else None,
        options=_sync_submit_options(dry_run=dry_run, revset=revset),
    )
    print_submit_result(result)
    return 0


def sweep_landed_reviews(*, context: CommandContext, dry_run: bool) -> None:
    """Finalize and retire tracked reviews whose commits already reached trunk.

    This covers transports that preserve commit IDs: an interrupted direct push
    leaves open PRs whose exact commits are ancestors of trunk, and a
    merge-commit merge lands the exact local commit. Neither is visible on the
    selected stack (the commits are inside trunk), so convergence checks saved
    tracking directly. Reviews it cannot prove safe are reported and skipped.
    """

    state = context.state_store.load()
    if not state.review_identities or not state.submitted_baselines:
        return
    target = resolve_github_target(context.jj_client.list_git_remotes())
    if not isinstance(target, GithubTarget):
        return
    trunk_commit_id = context.jj_client.resolve_revision("trunk()").commit_id
    results = run_landed_review_sweep(
        cleanup_bookmarks=True,
        context=context,
        dry_run=dry_run,
        remote_name=target.remote.name,
        repository=target.repository,
        trunk_commit_id=trunk_commit_id,
    )
    render_sweep_results(dry_run=dry_run, results=results)


def render_sweep_results(
    *,
    dry_run: bool,
    results: tuple[LandedReviewResult, ...],
) -> None:
    if not results:
        return
    console.output("Planned post-land cleanup:" if dry_run else "Applied post-land cleanup:")
    marker = "•" if dry_run else "✓"
    for result in results:
        candidate = result.candidate
        if result.outcome == "skipped":
            console.output(
                t"  ! skip landed {ui.change_id(candidate.change_id)}: {result.skip_reason}"
            )
            continue
        if result.outcome == "finalized":
            console.output(
                t"  {marker} finalize PR #{candidate.review_identity.pr_number} for "
                t"{ui.change_id(candidate.change_id)}"
            )
        if result.forgot_bookmark:
            console.output(
                t"  {marker} forget {ui.bookmark(candidate.review_identity.head_ref)} for "
                t"{ui.change_id(candidate.change_id)}"
            )
        console.output(
            t"  {marker} remove tracking for landed {ui.change_id(candidate.change_id)}"
        )


def _sync_submit_options(*, dry_run: bool, revset: str) -> SubmitOptions:
    return SubmitOptions(
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=dry_run,
        edit=False,
        labels=None,
        re_request=False,
        restart=False,
        reviewers=None,
        revset=revset,
        team_reviewers=None,
        use_bookmarks=None,
    )
