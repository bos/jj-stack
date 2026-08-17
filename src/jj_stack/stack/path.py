"""Pure projection of one ordinary selected stack path."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.errors import AmbiguousSelectionError, CliError
from jj_stack.models.stack import LocalCommit, LocalStack


@dataclass(frozen=True, slots=True)
class SelectedPathObservation:
    """Immutable facts needed to derive one selected parent path."""

    candidate_commit_ids: frozenset[str]
    current_working_copy_commit_id: str | None
    fetched_trunk_commit_ids: frozenset[str]
    commits: tuple[LocalCommit, ...]
    selected_revset: str
    selector_commits: tuple[LocalCommit, ...]
    select_mutable_copy: bool
    trunk: LocalCommit


@dataclass(frozen=True, slots=True)
class SelectedStackPath:
    """One ordinary selected parent path."""

    is_maximal: bool
    stack: LocalStack


@dataclass(frozen=True, slots=True)
class RepoStackPath:
    """One repo path annotated by existing tracking."""

    is_maximal: bool
    stack: LocalStack
    tracked_change_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class RepoPathObservation:
    """Immutable facts needed to derive ordinary repo paths."""

    candidate_commit_ids: frozenset[str]
    current_tracked_commit_id: str | None
    fetched_trunk_commit_ids: frozenset[str]
    commits: tuple[LocalCommit, ...]
    tracked_change_ids: frozenset[str]
    trunk: LocalCommit


@dataclass(frozen=True, slots=True)
class RepoStackPaths:
    """Ordinary maximal paths observed from one bounded repo scope."""

    current_tracked_commit_id: str | None
    paths: tuple[RepoStackPath, ...]


def project_selected_path(observation: SelectedPathObservation) -> SelectedStackPath:
    """Derive a parent-connected path without consulting external state."""

    selected = _select_commit(observation)
    if selected.commit_id in observation.fetched_trunk_commit_ids:
        stack = LocalStack(
            base_parent=selected,
            head=selected,
            changes=(),
            selected_revset=observation.selected_revset,
            trunk=observation.trunk,
        )
        return SelectedStackPath(
            is_maximal=False,
            stack=stack,
        )

    commits_by_id = {
        commit.commit_id: commit
        for commit in sorted(observation.commits, key=lambda item: item.commit_id)
    }
    commits_by_id[selected.commit_id] = selected

    head_first: list[LocalCommit] = []
    current = selected
    while current.commit_id not in observation.fetched_trunk_commit_ids:
        head_first.append(current)
        parent_commit_id = current.parents[0]
        parent = commits_by_id.get(parent_commit_id)
        if parent is None:
            raise CliError(
                "Could not resolve the complete selected parent path: "
                f"commit {current.commit_id} has unobserved parent {parent_commit_id}."
            )
        current = parent

    changes = tuple(reversed(head_first))
    candidates = _ordinary_candidates(
        candidate_commit_ids=observation.candidate_commit_ids,
        commits_by_id=commits_by_id,
    )
    stack = LocalStack(
        base_parent=current,
        head=selected,
        changes=changes,
        selected_revset=observation.selected_revset,
        trunk=observation.trunk,
    )
    return SelectedStackPath(
        is_maximal=selected.commit_id in _maximal_candidate_commit_ids(candidates),
        stack=stack,
    )


def project_repo_paths(
    observation: RepoPathObservation,
) -> RepoStackPaths:
    """Derive maximal parent-connected paths from ordinary visible candidates."""

    commits_by_id = {
        commit.commit_id: commit
        for commit in sorted(observation.commits, key=lambda item: item.commit_id)
    }
    candidates = _ordinary_candidates(
        candidate_commit_ids=observation.candidate_commit_ids,
        commits_by_id=commits_by_id,
    )
    maximal_commit_ids = _maximal_candidate_commit_ids(candidates)
    heads = tuple(candidates[commit_id] for commit_id in sorted(maximal_commit_ids))

    paths: list[RepoStackPath] = []
    for head in heads:
        head_first: list[LocalCommit] = []
        current = head
        while current.commit_id in candidates:
            head_first.append(current)
            current = commits_by_id[current.parents[0]]
        changes = tuple(reversed(head_first))
        path_change_ids = frozenset(change.change_id for change in changes)
        paths.append(
            RepoStackPath(
                is_maximal=True,
                stack=LocalStack(
                    base_parent=current,
                    head=head,
                    changes=changes,
                    selected_revset=head.change_id,
                    trunk=observation.trunk,
                ),
                tracked_change_ids=observation.tracked_change_ids & path_change_ids,
            )
        )
    return RepoStackPaths(
        current_tracked_commit_id=observation.current_tracked_commit_id,
        paths=tuple(paths),
    )


def _maximal_candidate_commit_ids(
    candidates: dict[str, LocalCommit],
) -> frozenset[str]:
    parent_commit_ids = {commit.parents[0] for commit in candidates.values() if commit.parents}
    return frozenset(candidates.keys() - parent_commit_ids)


def _ordinary_candidates(
    *,
    candidate_commit_ids: frozenset[str],
    commits_by_id: dict[str, LocalCommit],
) -> dict[str, LocalCommit]:
    return {
        commit_id: commits_by_id[commit_id]
        for commit_id in candidate_commit_ids & commits_by_id.keys()
        if (
            not commits_by_id[commit_id].is_working_copy
            or bool(commits_by_id[commit_id].description.strip())
        )
        and not commits_by_id[commit_id].hidden
        and len(commits_by_id[commit_id].parents) == 1
        and not (commits_by_id[commit_id].is_working_copy and commits_by_id[commit_id].empty)
    }


def _select_commit(observation: SelectedPathObservation) -> LocalCommit:
    candidates = tuple(sorted(observation.selector_commits, key=lambda commit: commit.commit_id))
    if observation.current_working_copy_commit_id is not None:
        current = next(
            (
                commit
                for commit in candidates
                if commit.commit_id == observation.current_working_copy_commit_id
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
                    commit for commit in candidates if commit.commit_id == parent_commit_id
                )
            except StopIteration as error:
                raise ValueError(
                    "Default parent is absent from the selected observation."
                ) from error
        return current

    if observation.select_mutable_copy:
        off_trunk = tuple(
            commit
            for commit in candidates
            if commit.commit_id not in observation.fetched_trunk_commit_ids
        )
        mutable = tuple(commit for commit in off_trunk if not commit.immutable)
        if len(mutable) > 1:
            raise AmbiguousSelectionError(
                "The selected change has more than one mutable local copy.",
            )
        if mutable:
            return mutable[0]
        if len(off_trunk) > 1:
            raise AmbiguousSelectionError("The selector resolved to more than one commit.")
        if off_trunk:
            # A stack merge side parent is immutable to jj but remains outside
            # the fetched trunk's first-parent path until sync retires it.
            return off_trunk[0]
        raise CliError("This change is already on trunk, so it is not part of a local stack.")

    if len(candidates) != 1:
        raise AmbiguousSelectionError("The selector resolved to more than one commit.")
    return candidates[0]
