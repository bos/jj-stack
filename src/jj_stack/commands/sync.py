"""Fetch remote state, drop merged changes, and resubmit the selected stack.

This chains the routine catch-up flow into one command: refresh what jj knows about
the remote, rebase the selected stack off any changes whose pull requests have merged
(the same repair `cleanup --rebase` performs), then run `submit` to refresh the
stack's pull requests. If the rebase step is blocked, `sync` stops with its
diagnostics; if everything selected has already merged, it reports that there is
nothing left to submit.

`sync` only rewrites history to remove merged changes. It does not rebase your stack
onto newer trunk commits when nothing in the stack has merged; use `jj rebase` for
that. It also takes no submit flags: runs that need draft handling, descriptions,
reviewers, or restart behavior use `submit` directly.

With `--dry-run`, `sync` previews the rebase plan and makes no changes. The submit
preview follows only when no rebase work is planned, because a submit preview taken
before the rebase would describe the wrong stack.
"""

from __future__ import annotations

from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import bootstrap_context
from jj_stack.commands.cleanup.rebase import run_cleanup_rebase_command
from jj_stack.commands.submit.command import print_selected_line, run_submit
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_submit_result
from jj_stack.jj.client import JjCliArgs
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Fetch, drop merged changes, and resubmit the current stack"


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
        rebase_result = run_cleanup_rebase_command(
            context=context,
            dry_run=dry_run,
            rebase_revset=selected_revset,
        )
        if rebase_result.blocked:
            return 1
        if dry_run and any(
            action.status == "planned" for action in rebase_result.actions
        ):
            console.output(
                t"Submit preview skipped: run {ui.cmd('sync')} without "
                t"{ui.cmd('--dry-run')} to apply the rebase first."
            )
            return 0
        if rebase_result.fully_merged:
            console.output("Nothing to submit: everything on the selected stack has merged.")
            return 0
        result = run_submit(
            context=context,
            # The selected line is only rendered when sync picked the default
            # head for the user.
            on_prepared=print_selected_line if revset is None else None,
            options=_sync_submit_options(dry_run=dry_run, revset=selected_revset),
        )
    print_submit_result(result)
    return 0


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
