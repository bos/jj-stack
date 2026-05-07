"""Start a fresh review for the selected local stack.

Use this when the local changes should be reviewed again as new pull requests
instead of continuing, reopening, or relinking the old PRs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.jj import JjCliArgs, JjClient
from jj_review.review.restart import RestartedChange, restart_state_for_stack
from jj_review.review.selection import resolve_selected_revset
from jj_review.state.store import ReviewStateStore

HELP = "Start a fresh review for local changes that should get new pull requests"


@dataclass(frozen=True, slots=True)
class RestartResult:
    """Rendered restart result for the selected stack."""

    changed: tuple[RestartedChange, ...]
    dry_run: bool
    selected_revset: str


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
    result = _run_restart(
        config=context.config,
        dry_run=dry_run,
        jj_client=context.jj_client,
        revset=resolve_selected_revset(
            command_label="restart",
            require_explicit=True,
            revset=revset,
        ),
    )
    _render_restart_result(result)
    return 0


def _run_restart(
    *,
    config: RepoConfig,
    dry_run: bool,
    jj_client: JjClient,
    revset: str | None,
) -> RestartResult:
    with console.spinner(description="Inspecting jj stack"):
        stack = jj_client.discover_review_stack(revset)
    if not stack.revisions:
        raise CliError("The selected stack has no changes to review.")

    state_store = ReviewStateStore.for_repo(jj_client.repo_root)
    if not dry_run:
        state_store.require_writable()
    state = state_store.load()
    bookmark_states = jj_client.list_bookmark_states()
    restart_result = restart_state_for_stack(
        bookmark_states=bookmark_states,
        config=config,
        stack=stack,
        state=state,
    )
    if restart_result.changed and not dry_run:
        state_store.save(restart_result.state)
    return RestartResult(
        changed=restart_result.changed,
        dry_run=dry_run,
        selected_revset=stack.selected_revset,
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
