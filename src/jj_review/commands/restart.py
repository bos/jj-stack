"""Start a fresh review for the selected local stack.

Use this when the local changes should be reviewed again as new pull requests
instead of continuing, reopening, or relinking the old PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import CommandContext, bootstrap_context
from jj_review.commands._operation_lock import mutating_command_lock
from jj_review.errors import CliError
from jj_review.jj import JjCliArgs
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.review_state import ReviewState
from jj_review.models.stack import LocalStack
from jj_review.review.restart import RestartedChange, restart_state_for_stack
from jj_review.review.selection import resolve_selected_revset

HELP = "Start a fresh review for local changes that should get new pull requests"


@dataclass(frozen=True, slots=True)
class RestartOptions:
    """Parsed command options for `restart`."""

    dry_run: bool
    revset: str | None


@dataclass(frozen=True, slots=True)
class RestartResult:
    """Rendered restart result for the selected stack."""

    changed: tuple[RestartedChange, ...]
    dry_run: bool
    selected_revset: str


@dataclass(frozen=True, slots=True)
class _PreparedRestart:
    """Resolved restart target before saved review state mutation."""

    bookmark_states: dict[str, BookmarkState]
    context: CommandContext
    options: RestartOptions
    stack: LocalStack
    state: ReviewState


def restart(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `restart`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    options = _restart_options_from_cli(
        dry_run=dry_run,
        revset=revset,
    )
    with mutating_command_lock(command="restart", context=context):
        result = _run_restart(
            context=context,
            options=options,
        )
    _render_restart_result(result)
    return 0


def _restart_options_from_cli(
    *,
    dry_run: bool,
    revset: str | None,
) -> RestartOptions:
    return RestartOptions(
        dry_run=dry_run,
        revset=revset,
    )


def _run_restart(
    *,
    context: CommandContext,
    options: RestartOptions,
) -> RestartResult:
    prepared = _prepare_restart(context=context, options=options)
    return _apply_restart(prepared=prepared)


def _prepare_restart(
    *,
    context: CommandContext,
    options: RestartOptions,
) -> _PreparedRestart:
    revset = resolve_selected_revset(
        command_label="restart",
        require_explicit=True,
        revset=options.revset,
    )
    with console.spinner(description="Inspecting jj stack"):
        stack = context.jj_client.discover_review_stack(revset)
    if not stack.revisions:
        raise CliError("The selected stack has no changes to review.")

    state_store = context.state_store
    if not options.dry_run:
        state_store.require_writable()
    state = state_store.load()
    return _PreparedRestart(
        bookmark_states=context.jj_client.list_bookmark_states(),
        context=context,
        options=options,
        stack=stack,
        state=state,
    )


def _apply_restart(
    *,
    prepared: _PreparedRestart,
) -> RestartResult:
    context = prepared.context
    options = prepared.options
    state_store = context.state_store
    restart_result = restart_state_for_stack(
        bookmark_states=prepared.bookmark_states,
        config=context.config,
        stack=prepared.stack,
        state=prepared.state,
    )
    if restart_result.changed and not options.dry_run:
        state_store.save(restart_result.state)
    return RestartResult(
        changed=restart_result.changed,
        dry_run=options.dry_run,
        selected_revset=prepared.stack.selected_revset,
    )


def _render_restart_result(result: RestartResult) -> None:
    action = "Would prepare" if result.dry_run else "Prepared"
    if not result.changed:
        console.output("No previous PR tracking found for the selected stack.")
        return

    change_count = len(result.changed)
    noun = "change" if change_count == 1 else "changes"
    console.output(f"{action} fresh review tracking for {change_count} {noun}:")
    for item in result.changed:
        old = (
            f"PR #{item.old_pr_number}"
            if item.old_pr_number is not None
            else "previous tracking"
        )
        console.output(
            t"  {ui.change_id(item.change_id)} {item.subject}: {old} -> "
            t"{ui.bookmark(item.new_bookmark)}"
        )
    console.output(
        t"Run {ui.cmd(f'jj-review submit {result.selected_revset}')} to create fresh PRs."
    )
