"""Shared stale-stack advisory rendering for command output."""

from __future__ import annotations

from collections.abc import Sequence

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.models.review_state import ReviewState
from jj_stack.models.stack import LocalStack
from jj_stack.review.change_status import submitted_state_disagreement


def emit_stale_stacks_advisory(
    *,
    stacks: Sequence[LocalStack],
    state: ReviewState,
    single_subject: str,
    plural_subject: str,
) -> None:
    """Hint that tracked stacks have changed since their last successful submit."""

    stale_heads = tuple(
        stack.head.change_id for stack in stacks if submitted_state_disagreement(state, (stack,))
    )
    if not stale_heads:
        return
    if len(stale_heads) == 1:
        head = stale_heads[0][:8]
        console.warning(
            (
                f"{single_subject} has changed since its last submit; ",
                t"inspect with {ui.cmd(f'jj-stack view {head}')} or refresh with "
                t"{ui.cmd(f'jj-stack submit {head}')}.",
            )
        )
        return
    heads_fragments = ui.join(ui.change_id, stale_heads)
    console.warning(
        (
            f"{plural_subject} have changed since their last submit; ",
            t"inspect with {ui.cmd('jj-stack view <head>')} or refresh with "
            t"{ui.cmd('jj-stack submit <head>')}: ",
            *heads_fragments,
        )
    )
