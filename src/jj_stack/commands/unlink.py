"""Stop tracking one local change with jj-stack while leaving the rest of the
stack alone.

Later jj-stack commands will ignore that change unless you link it again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError
from jj_stack.jj.client import JjCliArgs
from jj_stack.review.selection import resolve_selected_revset
from jj_stack.review.status import (
    prepare_status,
)
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Stop managing one local change as part of review"


@dataclass(frozen=True, slots=True)
class UnlinkResult:
    """Rendered unlink result for one selected local revision."""

    already_unlinked: bool
    bookmark: str | None
    change_id: str
    subject: str


def unlink(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `unlink`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    with acquire_operation_lock(context.state_store.require_writable(), command="unlink"):
        result = asyncio.run(
            _run_unlink_async(
                context=context,
                revset=revset,
            )
        )
    _print_unlink_result(result)
    return 0


def _print_unlink_result(result: UnlinkResult) -> None:
    revision_label = t"{result.subject} ({ui.change_id(result.change_id)})"
    if result.already_unlinked:
        console.output(t"{revision_label} is already unlinked from review tracking.")
        return
    if result.bookmark is None:
        console.output(t"Stopped review tracking for {revision_label}.")
    else:
        console.output(
            t"Stopped review tracking for {revision_label}, preserving "
            t"{ui.bookmark(result.bookmark)}."
        )


async def _run_unlink_async(
    *,
    context: CommandContext,
    revset: str | None,
) -> UnlinkResult:
    revset = resolve_selected_revset(
        command_label="unlink",
        require_explicit=True,
        revset=revset,
    )
    # Unlink is a local-only repair command: it must not refresh remote
    # observations, because a fetch imports whatever the remote now holds —
    # moved review branches, resurrected predecessors — into the local view
    # mid-repair. Saved tracking and remembered observations decide link state.
    with console.spinner(description="Inspecting jj stack"):
        prepared_status = prepare_status(
            context=context,
            fetch_remote_state=False,
            revset=revset,
        )
    prepared = prepared_status.prepared
    if not prepared.status_revisions:
        raise CliError("The selected stack has no changes to review.")

    prepared_revision = prepared.status_revisions[-1]
    state = context.state_store.load()
    change_id = prepared_revision.revision.change_id
    identity = state.review_identities.get(change_id)
    if identity is None:
        raise CliError(
            t"The selected change has no active review tracking link to unlink.",
            hint=(
                t"Use {ui.cmd('relink')} only when you need to attach an existing PR "
                t"intentionally."
            ),
        )
    if identity.is_unlinked:
        return UnlinkResult(
            already_unlinked=True,
            bookmark=identity.head_ref,
            change_id=change_id,
            subject=prepared_revision.revision.subject,
        )
    context.state_store.set_link_state(
        change_id,
        expected_identity=identity,
        link_state="unlinked",
    )
    return UnlinkResult(
        already_unlinked=False,
        bookmark=identity.head_ref,
        change_id=change_id,
        subject=prepared_revision.revision.subject,
    )
