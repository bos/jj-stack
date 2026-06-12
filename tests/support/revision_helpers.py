from __future__ import annotations

from jj_stack.models.stack import LocalRevision


def make_revision(*, commit_id: str, change_id: str, description: str) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=commit_id,
        conflict=False,
        current_working_copy=False,
        description=description,
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("trunk",),
    )
