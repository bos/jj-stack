"""Prepare local stack inputs shared by inspection and lifecycle commands."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import UnsupportedStackError
from jj_stack.github.resolution import GithubTarget, UnresolvedGithubTarget, resolve_github_target
from jj_stack.jj.client import JjClient
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.selected import select_stack_path, select_stack_path_containing_change

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreparedLocalStack:
    """Selected history, saved links, and one resolved or unavailable GitHub target."""

    client: JjClient
    github_target: GithubTarget | UnresolvedGithubTarget
    stack: LocalStack
    state: TrackingState


def prepare_local_stack(
    *,
    context: CommandContext,
    fetch_remote_state: bool = False,
    revset: str | None,
    containing_change_id: str | None = None,
    inspection_mode: bool = False,
) -> PreparedLocalStack:
    """Resolve a local stack, tracking, and target before GitHub inspection."""

    jj_client = context.jj_client
    state_store = context.state_store
    state = state_store.load()
    github_target = resolve_github_target(jj_client.list_git_remotes())
    if fetch_remote_state and github_target.remote is not None:
        jj_client.fetch_remote(remote=github_target.remote.name)

    if containing_change_id is not None:
        selected_path = select_stack_path_containing_change(
            change_id=containing_change_id,
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            state=state,
        )
    else:
        selected_path = select_stack_path(
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            revset=revset,
            state=state,
        )
    if selected_path.stack.head.hidden:
        # A commit ID resolves a hidden predecessor, while `change_id()` does not. Only
        # visible changes are stack members, so refuse both selector forms alike. `checkout`
        # selects its own path because it makes an imported hidden commit visible again.
        selected_revset = ui.revset(selected_path.stack.selected_revset)
        restore = ui.cmd(f"jj new {selected_path.stack.head.commit_id}")
        raise UnsupportedStackError(
            t"Revset {selected_revset} did not resolve to a visible commit.",
            hint=t"Restore it with {restore}, or select a visible change.",
            reason="hidden_commit",
        )
    stack = selected_path.stack
    logger.debug(
        "stack prepared: selected_revset=%s changes=%d remote=%s",
        stack.selected_revset,
        len(stack.changes),
        github_target.remote.name if github_target.remote is not None else "unavailable",
    )
    return PreparedLocalStack(
        client=jj_client,
        github_target=github_target,
        stack=stack,
        state=state,
    )
