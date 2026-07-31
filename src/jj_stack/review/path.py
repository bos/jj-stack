"""Pure projection of one ordinary selected review path."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.errors import AmbiguousSelectionError, CliError
from jj_stack.models.stack import LocalRevision, LocalStack


@dataclass(frozen=True, slots=True)
class SelectedPathObservation:
    """Immutable facts needed to derive one selected parent path."""

    current_working_copy_commit_id: str | None
    fetched_trunk_commit_ids: frozenset[str]
    revisions: tuple[LocalRevision, ...]
    selected_revset: str
    selector_revisions: tuple[LocalRevision, ...]
    select_mutable_copy: bool
    tracked_change_ids: frozenset[str]
    trunk: LocalRevision


@dataclass(frozen=True, slots=True)
class SelectedReviewPath:
    """One ordinary selected path annotated by existing tracking."""

    stack: LocalStack
    tracked_change_ids: frozenset[str]


def project_selected_path(observation: SelectedPathObservation) -> SelectedReviewPath:
    """Derive a parent-connected path without consulting external state."""

    selected = _select_revision(observation)
    if selected.commit_id in observation.fetched_trunk_commit_ids:
        stack = LocalStack(
            base_parent=selected,
            base_parent_is_trunk_ancestor=True,
            head=selected,
            revisions=(),
            selected_revset=observation.selected_revset,
            trunk=observation.trunk,
        )
        return SelectedReviewPath(stack=stack, tracked_change_ids=frozenset())

    revisions_by_commit_id = {
        revision.commit_id: revision
        for revision in sorted(observation.revisions, key=lambda item: item.commit_id)
    }
    revisions_by_commit_id[selected.commit_id] = selected

    head_first: list[LocalRevision] = []
    current = selected
    while current.commit_id not in observation.fetched_trunk_commit_ids:
        head_first.append(current)
        parent_commit_id = current.parents[0]
        current = revisions_by_commit_id[parent_commit_id]

    revisions = tuple(reversed(head_first))
    path_change_ids = frozenset(revision.change_id for revision in revisions)
    stack = LocalStack(
        base_parent=current,
        base_parent_is_trunk_ancestor=True,
        head=selected,
        revisions=revisions,
        selected_revset=observation.selected_revset,
        trunk=observation.trunk,
    )
    return SelectedReviewPath(
        stack=stack,
        tracked_change_ids=observation.tracked_change_ids & path_change_ids,
    )


def _select_revision(observation: SelectedPathObservation) -> LocalRevision:
    candidates = tuple(
        sorted(observation.selector_revisions, key=lambda revision: revision.commit_id)
    )
    if observation.current_working_copy_commit_id is not None:
        current = next(
            (
                revision
                for revision in candidates
                if revision.commit_id == observation.current_working_copy_commit_id
            ),
            None,
        )
        if current is None:
            raise ValueError("Current working copy is absent from the selected observation.")
        if current.empty or not current.description.strip():
            if len(current.parents) != 1:
                raise ValueError("Default selection has no ordinary parent.")
            parent_commit_id = current.parents[0]
            try:
                return next(
                    revision for revision in candidates if revision.commit_id == parent_commit_id
                )
            except StopIteration as error:
                raise ValueError(
                    "Default parent is absent from the selected observation."
                ) from error
        return current

    if observation.select_mutable_copy:
        off_trunk = tuple(
            revision
            for revision in candidates
            if revision.commit_id not in observation.fetched_trunk_commit_ids
        )
        mutable = tuple(revision for revision in off_trunk if not revision.immutable)
        if len(mutable) > 1:
            raise AmbiguousSelectionError(
                "The selected change has more than one mutable local copy.",
            )
        if mutable:
            return mutable[0]
        if len(off_trunk) > 1:
            raise AmbiguousSelectionError("The selector resolved to more than one revision.")
        if off_trunk:
            # A native merge side parent is immutable to jj but remains outside
            # the fetched trunk's first-parent path until selected sync retires it.
            return off_trunk[0]
        raise CliError("The selected change has no mutable local copy.")

    if len(candidates) != 1:
        raise AmbiguousSelectionError("The selector resolved to more than one revision.")
    return candidates[0]
