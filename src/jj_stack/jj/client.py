"""Typed access to local `jj` stack state."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import jj_stack.ui as ui
from jj_stack.errors import (
    EXIT_NO_STACK,
    AmbiguousSelectionError,
    CliError,
    DriftError,
    ErrorHint,
    ErrorMessage,
    UsageError,
)
from jj_stack.models.git import GitRemote
from jj_stack.models.stack import LocalRevision, LocalStack

_COMMIT_TEMPLATE = (
    r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
    r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
    r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
    r'json(current_working_copy) ++ "\t" ++ '
    r'json(working_copies.map(|wc| wc.name())) ++ "\t" ++ '
    r'json(self.hidden()) ++ "\t" ++ '
    r'json(immutable) ++ "\t" ++ json(self.conflict()) ++ "\n"'
)
_SCAN_TEMPLATE_PREFIX = _COMMIT_TEMPLATE.removesuffix(r'"\n"') + r'"\t" ++ '
_BOOKMARK_TEMPLATE = r'json(self) ++ "\n"'
_REVIEW_TEMP_BOOKMARK = "jj-stack-tmp/checkout"
_REVIEW_TEMP_REF = f"refs/heads/{_REVIEW_TEMP_BOOKMARK}"
_CONFIG_ORIGIN_TEMPLATE = r'json(source) ++ "\t" ++ json(path) ++ "\n"'


def _review_fetch_refspec() -> str:
    """Derive the fetch exclusion lazily from the review-branch policy authority."""

    from jj_stack.review.branches import review_branch_glob

    return f"^refs/heads/{review_branch_glob()}"


def _review_namespace() -> str:
    """Return the reserved namespace lazily to avoid a formatting/client import cycle."""

    from jj_stack.review.branches import REVIEW_BRANCH_PREFIX

    return f"{REVIEW_BRANCH_PREFIX}/"


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails."""


ReviewFetchIsolationStatus = Literal["ready", "applied", "required"]


@dataclass(frozen=True, slots=True)
class ReviewFetchIsolation:
    """Result of checking the remote-only review fetch boundary."""

    status: ReviewFetchIsolationStatus
    remote: str
    existing_count: int = 0
    refspec: str = field(default_factory=_review_fetch_refspec)


class ReviewFetchIsolationRequired(CliError):
    """Raised when a dry run needs a local fetch-isolation configuration change."""

    def __init__(self, isolation: ReviewFetchIsolation) -> None:
        self.isolation = isolation
        change = "adding" if isolation.existing_count == 0 else "normalizing"
        super().__init__(
            t"Dry run would reserve {ui.bookmark(_review_namespace())} for jj-stack by "
            t"{change} {ui.code(isolation.refspec)} in remote "
            t"{ui.bookmark(isolation.remote)}'s Git fetch refspecs.",
            hint="Run the command without --dry-run once to apply this local configuration.",
        )


@dataclass(frozen=True, slots=True)
class ReviewRefUpdate:
    """One exact leased review-ref update in a complete remote mutation set."""

    branch: str
    expected_target: str | None
    desired_target: str | None


@dataclass(frozen=True, slots=True)
class ReviewTempArtifacts:
    """Observed fixed review-import artifacts without applying recovery."""

    bookmark_targets: tuple[str, ...]
    ref_target: str | None


@dataclass(frozen=True, slots=True)
class _ConfigOrigin:
    source: str
    path: str


UnsupportedStackReason = Literal[
    "divergent_change",
    "empty_working_copy",
    "hidden_commit",
    "immutable_commit",
    "merge_commit",
    "reached_root_before_trunk",
    "trunk_resolved_to_root",
]


class UnsupportedStackError(CliError):
    """Raised when local history cannot be treated as a linear review stack."""

    exit_code = EXIT_NO_STACK

    def __init__(
        self,
        message: ErrorMessage,
        *,
        change_id: str | None = None,
        hint: ErrorHint | None = None,
        reason: UnsupportedStackReason | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.change_id = change_id
        self.reason = reason

    @classmethod
    def stack_shape(
        cls,
        change_id: str,
        detail: ErrorMessage,
        *,
        reason: UnsupportedStackReason,
    ) -> UnsupportedStackError:
        return cls(
            t"Unsupported stack shape at {ui.change_id(change_id)}: {detail}",
            change_id=change_id,
            reason=reason,
        )


class StaleWorkspaceError(CliError):
    """Raised when `jj` refuses to run because the current workspace is stale."""


class _NativeRevision(Protocol):
    @property
    def commit_id(self) -> str: ...


CliColorMode = Literal["always", "auto", "debug", "never"]
JjColorWhen = Literal["always", "debug", "never"]


@dataclass(slots=True, frozen=True)
class JjCliArgs:
    """Global `jj` CLI overrides that flow to every jj invocation.

    Mirrors jj's own `--config NAME=VALUE` and `--config-file PATH` options so
    that a single value object carries the user's intent from the CLI down to
    every subprocess call. The argv is stored as one ordered tuple so the
    interleaving between `--config` and `--config-file` is preserved — jj
    applies later overrides on top of earlier ones, and a file listed after
    an inline value wins over it.
    """

    argv: tuple[str, ...] = ()

    def to_argv(self) -> tuple[str, ...]:
        return self.argv


_NO_CLI_ARGS = JjCliArgs()


class JjClient:
    """Thin wrapper around `jj` commands used by the review tool."""

    def __init__(
        self,
        repo_root: Path,
        *,
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> None:
        self._repo_root = repo_root
        self._cli_args = cli_args
        self._config_strings: dict[str, str | None] = {}
        self._git_root: Path | None = None

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def cli_args(self) -> JjCliArgs:
        return self._cli_args

    def discover_review_stack(
        self,
        revset: str | None = None,
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
    ) -> LocalStack:
        """Resolve the selected review stack plus its trunk and base-parent context."""

        if revset is None:
            trunk, head, selected_revset = self._resolve_default_head_and_trunk()
        else:
            trunk, head = self._resolve_selected_head_and_trunk(revset)
            selected_revset = revset
            if head.is_working_copy and head.empty:
                raise UnsupportedStackError(
                    "Selected revision resolves to the empty working-copy commit. "
                    "Select a concrete change instead.",
                    reason="empty_working_copy",
                )

        if head.commit_id == trunk.commit_id:
            return LocalStack(
                base_parent=trunk,
                base_parent_is_trunk_ancestor=True,
                head=head,
                revisions=(),
                selected_revset=selected_revset,
                trunk=trunk,
            )

        self._validate_reviewable_revision(
            head,
            allow_divergent=allow_divergent,
            allow_immutable=allow_immutable,
        )
        boundary, include_boundary_in_stack = self._resolve_review_stack_boundary(
            head_commit_id=head.commit_id,
            trunk=trunk,
        )
        ancestor_revisions = self._query_revisions(
            f"{_quote_revset_symbol(boundary.commit_id)}::{_quote_revset_symbol(head.commit_id)}"
        )
        revisions_by_commit_id = {revision.commit_id: revision for revision in ancestor_revisions}
        revisions_by_commit_id[head.commit_id] = head
        revisions_by_commit_id[boundary.commit_id] = boundary
        revisions_by_commit_id[trunk.commit_id] = trunk

        stack_head_first: list[LocalRevision] = []
        current = head
        while True:
            if current.commit_id == boundary.commit_id:
                if include_boundary_in_stack:
                    if current.commit_id != head.commit_id:
                        self._validate_reviewable_revision(
                            current,
                            allow_divergent=allow_divergent,
                            allow_immutable=allow_immutable,
                        )
                    stack_head_first.append(current)
                break
            if current.commit_id != head.commit_id:
                self._validate_reviewable_revision(
                    current,
                    allow_divergent=allow_divergent,
                    allow_immutable=allow_immutable,
                )
            stack_head_first.append(current)
            parent_commit_id = current.only_parent_commit_id()
            current = revisions_by_commit_id.get(parent_commit_id) or self.resolve_revision(
                parent_commit_id
            )

        stack_revisions = tuple(reversed(stack_head_first))
        stack_base_parent = trunk
        base_parent_is_trunk_ancestor = True
        if stack_revisions:
            stack_base_parent_commit_id = stack_revisions[0].only_parent_commit_id()
            stack_base_parent = revisions_by_commit_id.get(
                stack_base_parent_commit_id
            ) or self.resolve_revision(stack_base_parent_commit_id)
            base_parent_is_trunk_ancestor = (
                stack_base_parent.commit_id == boundary.commit_id
                and not include_boundary_in_stack
            )

        return LocalStack(
            base_parent=stack_base_parent,
            base_parent_is_trunk_ancestor=base_parent_is_trunk_ancestor,
            head=head,
            revisions=stack_revisions,
            selected_revset=selected_revset,
            trunk=trunk,
        )

    def _resolve_default_head_and_trunk(
        self,
    ) -> tuple[LocalRevision, LocalRevision, str]:
        """Resolve the default head and `trunk()` in one call."""

        revisions_with_trunk_membership = self._query_revisions_with_membership(
            "trunk() | @ | @-", membership_revsets=("trunk()",)
        )
        trunk: LocalRevision | None = None
        working_copy: LocalRevision | None = None
        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for revision, (is_trunk,) in revisions_with_trunk_membership:
            revisions_by_commit_id[revision.commit_id] = revision
            if is_trunk and trunk is None:
                trunk = revision
            if revision.current_working_copy:
                working_copy = revision
        trunk = self._validate_trunk(trunk)
        if working_copy is None:
            raise CliError("Could not resolve the current working-copy revision.")
        if working_copy.empty:
            parent_commit_id = working_copy.parents[0] if working_copy.parents else None
            parent = (
                revisions_by_commit_id.get(parent_commit_id)
                if parent_commit_id is not None
                else None
            )
            if parent is not None:
                return trunk, parent, "@-"
            return trunk, self.resolve_revision("@-"), "@-"
        return trunk, working_copy, "@"

    def resolve_revision(self, revset: str) -> LocalRevision:
        """Resolve a revset to exactly one revision."""

        try:
            revisions = self._query_revisions(revset, limit=2)
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise
        if not revisions:
            raise CliError(t"Revset {ui.revset(revset)} did not resolve to a visible revision.")
        if len(revisions) > 1:
            raise AmbiguousSelectionError(
                t"Revset {ui.revset(revset)} resolved to more than one revision."
            )
        return revisions[0]

    def query_revisions(
        self,
        revset: str,
        *,
        limit: int | None = None,
    ) -> tuple[LocalRevision, ...]:
        """Return revisions matching the supplied revset."""

        try:
            return tuple(self._query_revisions(revset, limit=limit))
        except JjCommandError as error:
            if _is_missing_revision_error(_unwrap_command_error_message(str(error))):
                return ()
            raise

    def query_revisions_by_change_ids(
        self,
        change_ids: Sequence[str],
    ) -> dict[str, tuple[LocalRevision, ...]]:
        """Return visible revisions grouped by logical change ID."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        grouped: dict[str, list[LocalRevision]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for chunk in _chunked(ordered_change_ids):
            revisions = self._query_revisions(_present_symbols_revset(chunk))
            for revision in revisions:
                grouped.setdefault(revision.change_id, []).append(revision)
        return {change_id: tuple(grouped.get(change_id, ())) for change_id in ordered_change_ids}

    def query_revisions_by_change_ids_descending_from(
        self,
        change_ids: Sequence[str],
        ancestor_commit_ids: Sequence[str],
    ) -> tuple[LocalRevision, ...]:
        """Return visible change-id matches that descend from any supplied ancestor."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        ordered_ancestor_commit_ids = tuple(dict.fromkeys(ancestor_commit_ids))
        if not ordered_change_ids or not ordered_ancestor_commit_ids:
            return ()

        ancestor_revset = f"({_union_revset_symbols(ordered_ancestor_commit_ids)})::"
        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for chunk in _chunked(ordered_change_ids):
            change_ids_revset = _present_symbols_revset(chunk)
            for revision in self._query_revisions(f"({change_ids_revset}) & {ancestor_revset}"):
                revisions_by_commit_id.setdefault(revision.commit_id, revision)
        return tuple(revisions_by_commit_id.values())

    def query_revisions_by_commit_ids(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalRevision, ...]:
        """Return visible revisions for the supplied commit IDs in evaluation order."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for chunk in _chunked(ordered_commit_ids):
            for revision in self._query_revisions(_union_revset_symbols(chunk)):
                revisions_by_commit_id.setdefault(revision.commit_id, revision)
        return tuple(revisions_by_commit_id.values())

    def query_trunk_ancestor_commit_ids(
        self,
        commit_ids: Sequence[str],
    ) -> set[str]:
        """Return supplied commit IDs that are ancestors of `trunk()`."""

        return self._query_commit_ids_in_ancestor_revset(
            commit_ids,
            ancestor_revset="::trunk()",
        )

    def query_present_commit_ancestor_membership(
        self,
        commit_ids: Sequence[str],
        *,
        descendant_commit_id: str,
    ) -> dict[str, bool]:
        """Return presence and ancestry together, omitting unavailable commit IDs."""

        memberships: dict[str, bool] = {}
        for chunk in _chunked(tuple(dict.fromkeys(commit_ids))):
            try:
                revisions = self._query_revisions_with_membership(
                    _present_symbols_revset(chunk),
                    membership_revsets=(f"::{_quote_revset_symbol(descendant_commit_id)}",),
                )
            except JjCommandError:
                continue
            for revision, (is_ancestor,) in revisions:
                memberships[revision.commit_id] = is_ancestor
        return memberships

    def _query_commit_ids_in_ancestor_revset(
        self,
        commit_ids: Sequence[str],
        *,
        ancestor_revset: str,
    ) -> set[str]:
        """Return supplied commit IDs selected by an ancestor revset."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return set()

        matching_commit_ids: set[str] = set()
        for chunk in _chunked(ordered_commit_ids):
            revisions = self._query_revisions(
                f"({_union_revset_symbols(chunk)}) & {ancestor_revset}"
            )
            for revision in revisions:
                matching_commit_ids.add(revision.commit_id)
        return matching_commit_ids

    def query_ancestor_revisions(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalRevision, ...]:
        """Return ancestors for the supplied commits, including the commits themselves."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for chunk in _chunked(ordered_commit_ids):
            revisions = self._query_revisions(f"::{_union_revset_symbols(chunk)}")
            for revision in revisions:
                revisions_by_commit_id.setdefault(revision.commit_id, revision)
        return tuple(revisions_by_commit_id.values())

    def query_descendant_revisions(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalRevision, ...]:
        """Return descendants for the supplied commits, including the commits themselves."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for chunk in _chunked(ordered_commit_ids):
            revisions = self._query_revisions(f"{_union_revset_symbols(chunk)}::")
            for revision in revisions:
                revisions_by_commit_id.setdefault(revision.commit_id, revision)
        return tuple(revisions_by_commit_id.values())

    def query_paired_ancestor_membership(
        self,
        pairs: Sequence[tuple[str, str]],
    ) -> set[str]:
        """Return subject commit IDs from `pairs` that are ancestors of any paired target.

        Each `(subject, target)` pair becomes one term in a unioned revset of the
        form `(subject_i & ::target_i)`, so the whole check runs as one `jj log`
        invocation regardless of pair count. A subject's commit_id appears in the
        result iff at least one of its paired targets contains it. Equal commit
        IDs count as ancestors. Repeated pairs are deduped.
        """

        deduped_pairs = tuple(dict.fromkeys(pairs))
        if not deduped_pairs:
            return set()

        terms = " | ".join(
            f"({_quote_revset_symbol(subject)} & ::{_quote_revset_symbol(target)})"
            for subject, target in deduped_pairs
        )
        revisions = self._query_revisions(terms)
        return {revision.commit_id for revision in revisions}

    def query_children_by_parent_for_commit_ids(
        self,
        commit_ids: Sequence[str],
    ) -> dict[str, tuple[LocalRevision, ...]]:
        """Return visible children grouped by parent for the ancestors of the supplied commits."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return {}

        grouped: dict[str, dict[str, LocalRevision]] = {}
        for chunk in _chunked(ordered_commit_ids):
            children_by_parent = self._query_children_by_parent(
                f"children(::{_union_revset_symbols(chunk)})"
            )
            for parent_commit_id, children in children_by_parent.items():
                parent_group = grouped.setdefault(parent_commit_id, {})
                for child in children:
                    parent_group.setdefault(child.commit_id, child)
        return {
            parent_commit_id: tuple(children.values())
            for parent_commit_id, children in grouped.items()
        }

    def _resolve_trunk(self) -> LocalRevision:
        """Resolve `trunk()` and reject the implicit root fallback."""

        return self._validate_trunk(self.resolve_revision("trunk()"))

    def _validate_trunk(self, trunk: LocalRevision | None) -> LocalRevision:
        """Reject missing-trunk and implicit-root-fallback resolutions."""

        if trunk is None:
            raise JjCommandError(t"{ui.cmd('jj log')} did not resolve {ui.revset('trunk()')}.")
        if len(trunk.parents) == 0:
            raise UnsupportedStackError(
                t"No trunk bookmark is configured for this repo.",
                hint=t"Create a trunk bookmark such as {ui.bookmark('main')}, then retry.",
                reason="trunk_resolved_to_root",
            )
        return trunk

    def _resolve_review_stack_boundary(
        self,
        *,
        head_commit_id: str,
        trunk: LocalRevision,
    ) -> tuple[LocalRevision, bool]:
        """Resolve the nearest stack boundary on the selected-parent path to `head`."""

        boundary_candidates = self._query_revisions(
            "heads("
            f"first_ancestors({_quote_revset_symbol(head_commit_id)}) & "
            f"::{_quote_revset_symbol(trunk.commit_id)}"
            ")",
            limit=2,
        )
        if not boundary_candidates:
            raise UnsupportedStackError.stack_shape(
                head_commit_id,
                t"selected-parent path reached the root commit before {ui.revset('trunk()')}",
                reason="reached_root_before_trunk",
            )
        boundary = boundary_candidates[0]
        if len(boundary.parents) == 0:
            raise UnsupportedStackError.stack_shape(
                head_commit_id,
                t"selected-parent path reached the root commit before {ui.revset('trunk()')}",
                reason="reached_root_before_trunk",
            )
        return boundary, self._is_trunk_side_parent(
            boundary_commit_id=boundary.commit_id,
            trunk_commit_id=trunk.commit_id,
        )

    def _resolve_selected_head_and_trunk(
        self,
        revset: str,
    ) -> tuple[LocalRevision, LocalRevision]:
        """Resolve `revset` and `trunk()` in one call."""

        try:
            revisions = self._query_revisions_with_membership(
                f"trunk() | ({revset})",
                membership_revsets=("trunk()", revset),
            )
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise

        trunk: LocalRevision | None = None
        selected: list[LocalRevision] = []
        for revision, (is_trunk, is_selected) in revisions:
            if is_trunk and trunk is None:
                trunk = revision
            if is_selected:
                selected.append(revision)

        if not selected:
            raise CliError(t"Revset {ui.revset(revset)} did not resolve to a visible revision.")
        if len(selected) > 1:
            raise AmbiguousSelectionError(
                t"Revset {ui.revset(revset)} resolved to more than one revision."
            )

        return self._validate_trunk(trunk), selected[0]

    def _query_children_by_parent(
        self,
        revset: str,
    ) -> dict[str, tuple[LocalRevision, ...]]:
        revisions = self._query_revisions(revset)
        grouped: dict[str, list[LocalRevision]] = {}
        for revision in revisions:
            for parent_commit_id in revision.parents:
                grouped.setdefault(parent_commit_id, []).append(revision)
        return {
            parent_commit_id: tuple(children) for parent_commit_id, children in grouped.items()
        }

    def _is_trunk_side_parent(
        self,
        *,
        boundary_commit_id: str,
        trunk_commit_id: str,
    ) -> bool:
        """Return whether the boundary was merged into trunk as a non-first parent.

        Boundary discovery already found the nearest selected-parent commit that
        reaches the current trunk. To decide whether that boundary itself still
        belongs in the review stack, only its immediate trunk-merge children are
        relevant; scanning every merge under `trunk()` would make routine stack
        discovery scale with repository history.
        """

        merge_revisions = self._query_revisions(
            f"children({_quote_revset_symbol(boundary_commit_id)}) & "
            f"merges() & ::{_quote_revset_symbol(trunk_commit_id)}"
        )
        return any(boundary_commit_id in revision.parents[1:] for revision in merge_revisions)

    def get_config_string(self, key: str) -> str | None:
        """Return the string value of a jj config key, or None if unset.

        Reads are cached for the client's lifetime: nothing rewrites jj
        config during a command run, and callers such as per-revision
        rendering re-read the same key many times.
        """

        if key in self._config_strings:
            return self._config_strings[key]
        try:
            value = self._run_jj(("config", "get", key), ignore_working_copy=True)
        except JjCommandError:
            value = ""
        stripped = value.strip()
        result = stripped if stripped else None
        self._config_strings[key] = result
        return result

    def read_jj_stack_config_list_output(self) -> str:
        """Return raw stdout from ``jj config list 'jj-stack'``.

        Delegates scope merging and override handling to jj itself, so the
        same ``--config`` / ``--config-file`` overrides that flow to every jj
        invocation also shape jj-stack's own configuration. The caller is
        responsible for parsing the TOML-dotted-key output.
        """

        # Keep this as the initial repo-scoped jj command during bootstrap so jj
        # snapshots the working copy once before later read-only calls ignore it.
        return self._run_jj(("config", "list", "jj-stack"))

    def show_with_stat(self, revset: str) -> str:
        """Return raw stdout from ``jj show --stat -r <revset>``.

        Raises `JjCommandError` if jj fails. The caller is responsible for
        parsing the diffstat out of the output and framing any user-facing
        error message.
        """

        return self._run_jj(("show", "--stat", "-r", revset), ignore_working_copy=True)

    def resolve_color_when(
        self,
        *,
        cli_color: CliColorMode | None = None,
        stdout_is_tty: bool,
    ) -> JjColorWhen:
        """Resolve the effective `jj --color` mode for embedded log rendering."""

        configured = cli_color or self.get_config_string("ui.color")
        if configured == "always":
            return "always"
        if configured == "debug":
            return "debug"
        if configured == "never":
            return "never"
        return "always" if stdout_is_tty else "never"

    def render_revision_log_lines(
        self,
        revision: _NativeRevision,
        *,
        color_when: JjColorWhen,
    ) -> tuple[str, ...]:
        """Render one revision with the user's native `jj log` formatting."""

        stdout = self._run_jj(
            (
                "--ignore-working-copy",
                "--no-pager",
                "--color",
                color_when,
                "log",
                "-r",
                _quote_revset_symbol(revision.commit_id),
                "--limit",
                "1",
            )
        )
        return tuple(line for line in stdout.rstrip("\n").splitlines() if line.strip() != "~")

    def render_revision_log_blocks(
        self,
        revisions: Sequence[_NativeRevision],
        *,
        color_when: JjColorWhen,
    ) -> dict[str, tuple[str, ...]]:
        """Render several revisions in parallel, keyed by commit_id.

        Each `jj log` invocation pays a substantial startup cost, so rendering
        a stack sequentially dominates the wall-clock time of commands like
        `status`. Fan the per-revision calls out onto a thread pool so their
        subprocess spawns overlap.
        """

        if not revisions:
            return {}
        if len(revisions) == 1:
            revision = revisions[0]
            return {
                revision.commit_id: self.render_revision_log_lines(
                    revision, color_when=color_when
                )
            }
        with ThreadPoolExecutor(max_workers=min(len(revisions), 10)) as pool:
            rendered = list(
                pool.map(
                    lambda revision: (
                        revision.commit_id,
                        self.render_revision_log_lines(revision, color_when=color_when),
                    ),
                    revisions,
                )
            )
        return dict(rendered)

    def render_short_change_ids(
        self,
        change_ids: Sequence[str],
        *,
        color_when: JjColorWhen,
        min_len: int = 8,
    ) -> dict[str, str]:
        """Render shortest visible change IDs for the supplied logical change IDs."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        rendered: dict[str, str] = {}
        template = _short_change_id_render_template(min_len=min_len)
        for chunk in _chunked(ordered_change_ids):
            revset = _present_symbols_revset(chunk)
            stdout = self._run_jj(
                (
                    "--ignore-working-copy",
                    "--no-pager",
                    "--color",
                    color_when,
                    "log",
                    "--no-graph",
                    "-r",
                    revset,
                    "-T",
                    template,
                )
            )
            for line in stdout.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                raw_change_id, rendered_change_id = stripped.split("\t", maxsplit=1)
                change_id = json.loads(raw_change_id)
                rendered.setdefault(change_id, rendered_change_id)
        return rendered

    def find_private_commits(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> tuple[LocalRevision, ...]:
        """Return revisions blocked by the repo's git.private-commits policy."""

        private_commits_revset = self.get_config_string("git.private-commits")
        if not private_commits_revset or not revisions:
            return ()
        if private_commits_revset == "none()":
            return ()
        commit_ids_revset = " | ".join(_quote_revset_symbol(r.commit_id) for r in revisions)
        combined_revset = f"({private_commits_revset}) & ({commit_ids_revset})"
        return tuple(self.query_revisions(combined_revset))

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        """List configured Git remotes for the repository."""

        stdout = self._run_jj(("git", "remote", "list"), ignore_working_copy=True)
        remotes: list[GitRemote] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name, rendered_urls = stripped.split(maxsplit=1)
            fetch_url, separator, push_url = rendered_urls.rpartition(" (push: ")
            if separator and push_url.endswith(")"):
                push_url = push_url.removesuffix(")")
            else:
                fetch_url = push_url = rendered_urls
            remotes.append(GitRemote(name=name, fetch_url=fetch_url, push_url=push_url))
        return tuple(remotes)

    def ensure_review_fetch_isolation(
        self,
        *,
        remote: str,
        dry_run: bool = False,
        on_change: Callable[[ReviewFetchIsolation], None] | None = None,
    ) -> ReviewFetchIsolation:
        """Ensure ordinary fetches cannot import jj-stack review branches."""

        override_key = f"remotes.{json.dumps(remote)}.fetch-bookmarks"
        override_origin = self._effective_config_origin(override_key)
        if override_origin is not None:
            origin = override_origin.source
            if override_origin.path:
                origin = f"{origin} config at {override_origin.path}"
            if override_origin.source in {"user", "repo", "workspace"}:
                unset = ui.cmd(
                    f"jj config unset --{override_origin.source} {shlex.quote(override_key)}"
                )
                hint = t"Remove the override with {unset}, then retry."
            else:
                hint = (
                    t"Remove that {override_origin.source} override from the jj invocation "
                    t"or environment, then retry."
                )
            raise CliError(
                t"Effective jj setting {ui.code(override_key)} from {origin} overrides "
                t"Git fetch refspecs, so jj-stack cannot keep "
                t"{ui.bookmark(_review_namespace())} remote-only.",
                hint=hint,
            )

        self._require_no_imported_review_bookmarks()

        config_key = f"remote.{remote}.fetch"
        configured = self._git_fetch_refspecs(remote)
        review_fetch_refspec = _review_fetch_refspec()
        count = configured.count(review_fetch_refspec)
        if count == 1:
            return ReviewFetchIsolation(
                status="ready",
                remote=remote,
                existing_count=count,
            )

        status: ReviewFetchIsolationStatus = "required" if dry_run else "applied"
        result = ReviewFetchIsolation(
            status=status,
            remote=remote,
            existing_count=count,
        )
        if dry_run:
            if on_change is not None:
                on_change(result)
            raise ReviewFetchIsolationRequired(result)

        default_fetch_refspec = f"+refs/heads/*:refs/remotes/{remote}/*"
        if not configured:
            self._run_git(
                (
                    "config",
                    "--fixed-value",
                    "--replace-all",
                    config_key,
                    default_fetch_refspec,
                    default_fetch_refspec,
                )
            )
        self._run_git(
            (
                "config",
                "--fixed-value",
                "--replace-all",
                config_key,
                review_fetch_refspec,
                review_fetch_refspec,
            )
        )
        updated = self._git_fetch_refspecs(remote)
        retained_default = bool(configured) or updated.count(default_fetch_refspec) == 1
        if updated.count(review_fetch_refspec) != 1 or not retained_default:
            raise JjCommandError(
                t"Git fetch configuration for remote {ui.bookmark(remote)} did not retain "
                t"the required positive and negative refspecs."
            )
        if on_change is not None:
            on_change(result)
        return result

    def _require_no_imported_review_bookmarks(self) -> None:
        """Reject managed review bookmarks that have entered the local jj view."""

        imported = self.list_imported_review_bookmarks()
        if not imported:
            return
        forget = ui.cmd(
            "jj bookmark forget --include-remotes "
            + " ".join(shlex.quote(name) for name in imported)
        )
        export = ui.cmd("jj git export")
        raise CliError(
            t"Managed review bookmarks are already imported locally: "
            t"{ui.join(ui.bookmark, imported)}.",
            hint=(
                t"Move any work you need to keep to names outside "
                t"{ui.bookmark(_review_namespace())}, run {forget}, then run {export}. "
                t"Then retry the command."
            ),
        )

    def list_imported_review_bookmarks(self) -> tuple[str, ...]:
        """Return managed-namespace bookmarks already imported into jj."""

        from jj_stack.review.branches import is_managed_review_branch, review_branch_glob

        stdout = self._run_jj(
            (
                "bookmark",
                "list",
                "--all-remotes",
                "-T",
                _BOOKMARK_TEMPLATE,
                review_branch_glob(),
            ),
            ignore_working_copy=True,
        )
        names: set[str] = set()
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            raw = json.loads(stripped)
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload while checking "
                    t"the reserved review namespace."
                )
            name = raw["name"]
            if is_managed_review_branch(name):
                names.add(name)
        return tuple(sorted(names))

    def review_temp_ref_target(self) -> str | None:
        """Return the exact temporary review-import ref target, if it exists."""

        target = self._run_git(
            ("rev-parse", "--verify", "--quiet", _REVIEW_TEMP_REF),
            allowed_returncodes=frozenset({0, 1}),
        ).strip()
        return target or None

    def review_temp_artifacts(self) -> ReviewTempArtifacts:
        """Observe the fixed temporary import ref and its transient jj bookmark."""

        return ReviewTempArtifacts(
            bookmark_targets=self._local_bookmark_targets(_REVIEW_TEMP_BOOKMARK),
            ref_target=self.review_temp_ref_target(),
        )

    @contextmanager
    def import_remote_review_ref(
        self,
        *,
        remote: str,
        branch: str,
        expected_target: str,
        expected_change_id: str | None = None,
        expected_chain: Sequence[tuple[str, str, str]] = (),
        expected_parent_commit_id: str | None = None,
        on_isolation_change: Callable[[ReviewFetchIsolation], None] | None = None,
    ) -> Iterator[LocalRevision]:
        """Import one exact remote review ref, then remove all temporary artifacts.

        An expected chain guards every member's raw Git change ID and first-parent ancestry.
        Remote targets are rechecked immediately before jj import and after a successful yield.
        """

        ref = _review_ref(branch)
        chain = tuple(expected_chain)
        if chain and (
            expected_parent_commit_id is None
            or chain[-1] != (branch, expected_target, expected_change_id)
            or len({item[0] for item in chain}) != len(chain)
        ):
            raise ValueError("invalid expected remote review chain")
        expected_targets = (
            {chain_branch: target for chain_branch, target, _change_id in chain}
            if chain
            else {branch: expected_target}
        )
        self.ensure_review_fetch_isolation(
            remote=remote,
            on_change=on_isolation_change,
        )
        self._clear_review_temp_ref()
        try:
            configured_remote = self._git_remote(remote)
            self._run_git(
                (
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    configured_remote.fetch_url,
                    f"+{ref}:{_REVIEW_TEMP_REF}",
                )
            )
            if self.review_temp_ref_target() != expected_target:
                raise DriftError(
                    t"Remote branch {ui.bookmark(branch)} changed while it was being imported.",
                    condition="remote_branch_moved",
                )
            if chain:
                expected_parent = expected_parent_commit_id
                for _chain_branch, target, change_id in chain:
                    actual_change_id, parents = self._read_git_commit_metadata(target)
                    if actual_change_id != change_id or parents != (expected_parent,):
                        raise CliError("Imported review heads no longer form the expected stack.")
                    expected_parent = target
            self._require_remote_branch_targets_at_url(
                fetch_url=configured_remote.fetch_url,
                expected_targets=expected_targets,
            )
            self._run_jj(("git", "import"), ignore_working_copy=True)
            self._require_no_imported_review_bookmarks()
            revision = self.resolve_revision(_quote_revset_symbol(_REVIEW_TEMP_BOOKMARK))
            if revision.commit_id != expected_target:
                raise JjCommandError(
                    t"{ui.cmd('jj git import')} did not import the exact temporary review ref."
                )
            if expected_change_id is not None and revision.change_id != expected_change_id:
                raise CliError(
                    t"Remote branch {ui.bookmark(branch)} resolves to change "
                    t"{ui.change_id(revision.change_id)}, not the expected change "
                    t"{ui.change_id(expected_change_id)}."
                )
            yield revision
            self._require_remote_branch_targets_at_url(
                fetch_url=configured_remote.fetch_url,
                expected_targets=expected_targets,
            )
        finally:
            self._clear_review_temp_ref()

    def read_remote_git_change_id(self, *, remote: str, commit_id: str) -> str | None:
        """Fetch and inspect one exact remote Git object without creating a ref."""

        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit_id) is None:
            raise ValueError("remote commit ID must be a full SHA-1 or SHA-256 object ID")
        self.ensure_review_fetch_isolation(remote=remote)
        configured_remote = self._git_remote(remote)
        try:
            change_id, _parents = self._read_git_commit_metadata(commit_id)
        except JjCommandError:
            self._run_git(
                (
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    configured_remote.fetch_url,
                    commit_id,
                )
            )
            change_id, _parents = self._read_git_commit_metadata(commit_id)
        return change_id

    def _read_git_commit_metadata(
        self,
        commit_id: str,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Read one backing-Git commit's full change ID and ordered parents."""

        raw_commit = self._run_git(("cat-file", "commit", commit_id))
        headers, _, _message = raw_commit.partition("\n\n")
        entries = tuple(line.partition(" ") for line in headers.splitlines())
        change_ids = [
            value
            for key, separator, value in entries
            if separator and key == "change-id" and value
        ]
        parents = tuple(
            value for key, separator, value in entries if separator and key == "parent" and value
        )
        return (change_ids[0] if len(change_ids) == 1 else None), parents

    def fetch_remote(
        self,
        *,
        remote: str,
        dry_run: bool = False,
        on_isolation_change: Callable[[ReviewFetchIsolation], None] | None = None,
    ) -> None:
        """Fetch ordinary repository state while excluding managed review branches."""

        self.ensure_review_fetch_isolation(
            remote=remote,
            dry_run=dry_run,
            on_change=on_isolation_change,
        )
        self._run_jj(("git", "fetch", "--remote", remote))
        self._require_no_imported_review_bookmarks()

    def list_remote_branches(
        self,
        *,
        remote: str,
        patterns: Sequence[str],
    ) -> dict[str, str]:
        """List matching remote branch heads without importing them into jj."""

        if not patterns:
            return {}
        return self._list_remote_branches_at_url(
            fetch_url=self._git_remote(remote).fetch_url,
            patterns=patterns,
        )

    def _list_remote_branches_at_url(
        self,
        *,
        fetch_url: str,
        patterns: Sequence[str],
    ) -> dict[str, str]:
        """List matching remote heads from one already-resolved fetch URL."""

        stdout = self._run_git(("ls-remote", "--refs", fetch_url, *patterns))
        branches: dict[str, str] = {}
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            commit_id, separator, ref = stripped.partition("\t")
            if not separator or not commit_id or not ref.startswith("refs/heads/"):
                raise JjCommandError(
                    t"{ui.cmd('git ls-remote')} output has unexpected format: {line!r}"
                )
            branches[ref.removeprefix("refs/heads/")] = commit_id
        return branches

    def _require_remote_branch_targets_at_url(
        self,
        *,
        fetch_url: str,
        expected_targets: dict[str, str],
    ) -> None:
        observed = self._list_remote_branches_at_url(
            fetch_url=fetch_url,
            patterns=tuple(_review_ref(branch) for branch in expected_targets),
        )
        for branch, expected_target in expected_targets.items():
            actual_target = observed.get(branch)
            if actual_target != expected_target:
                raise DriftError(
                    t"Remote branch {ui.bookmark(branch)} no longer points to the expected "
                    t"commit.",
                    condition=(
                        "remote_branch_moved"
                        if actual_target is not None
                        else "remote_branch_missing"
                    ),
                )

    def mutate_remote_review_refs(
        self,
        *,
        remote: str,
        updates: Sequence[ReviewRefUpdate],
    ) -> None:
        """Freshly verify and atomically apply a complete review-ref update set."""

        ordered_updates = tuple(updates)
        if not ordered_updates:
            return
        branches = tuple(update.branch for update in ordered_updates)
        if len(set(branches)) != len(branches):
            raise ValueError("remote review-ref update set contains duplicate branches")
        refs = tuple(_review_ref(branch) for branch in branches)
        if any(
            update.expected_target is None and update.desired_target is None
            for update in ordered_updates
        ):
            raise ValueError("cannot delete a review ref that is expected to be absent")

        self.ensure_review_fetch_isolation(remote=remote)
        configured_remote = self._git_remote(remote)
        actual_targets = self._list_remote_branches_at_url(
            fetch_url=configured_remote.fetch_url,
            patterns=refs,
        )
        for update in ordered_updates:
            actual_target = actual_targets.get(update.branch)
            if actual_target == update.expected_target:
                continue
            condition = (
                "remote_branch_missing" if actual_target is None else "remote_branch_moved"
            )
            raise DriftError(
                t"Remote branch {ui.bookmark(update.branch)} changed before the atomic push.",
                condition=condition,
            )

        if all(update.desired_target == update.expected_target for update in ordered_updates):
            return

        command = ["push", "--atomic"]
        for ref, update in zip(refs, ordered_updates, strict=True):
            expected = update.expected_target or ""
            command.append(f"--force-with-lease={ref}:{expected}")
        command.append(configured_remote.push_url)
        for ref, update in zip(refs, ordered_updates, strict=True):
            desired = update.desired_target or ""
            command.append(f"{desired}:{ref}")
        self._run_git(command)

    def rebase_revisions_only(
        self,
        *,
        revisions: Sequence[str],
        destination: str,
    ) -> None:
        """Rebase named revisions and any empty working-copy children together."""

        ordered_revisions = list(dict.fromkeys(revisions))
        if not ordered_revisions:
            return
        selected_head = ordered_revisions[-1]
        ordered_revisions.extend(
            revision.commit_id
            for revision in self.query_descendant_revisions((selected_head,))
            if revision.is_working_copy
            and revision.empty
            and revision.parents == (selected_head,)
        )
        self._run_jj(("rebase", "-r", "|".join(ordered_revisions), "-d", destination))

    def abandon_revisions(self, revsets: Sequence[str]) -> None:
        """Abandon revisions; jj rebases descendants and drops pointing bookmarks."""

        ordered_revsets = tuple(revsets)
        if not ordered_revsets:
            return
        self._run_jj(("abandon", *ordered_revsets))

    def _query_revisions(self, revset: str, *, limit: int | None = None) -> list[LocalRevision]:
        lines = self._query_template_lines(revset, _COMMIT_TEMPLATE, limit=limit)
        return [_parse_revision_line(line) for line in lines]

    def _query_revisions_with_membership(
        self,
        revset: str,
        *,
        membership_revsets: Sequence[str],
    ) -> list[tuple[LocalRevision, tuple[bool, ...]]]:
        """Query revisions plus one containment flag per membership revset."""

        lines = self._query_template_lines(revset, _membership_scan_template(membership_revsets))
        return [_parse_revision_with_flags_line(line, len(membership_revsets)) for line in lines]

    def _query_template_lines(
        self,
        revset: str,
        template: str,
        *,
        limit: int | None = None,
    ) -> list[str]:
        command = ["log", "--no-graph", "-r", revset, "-T", template]
        if limit is not None:
            command.extend(["--limit", str(limit)])
        stdout = self._run_jj(command, ignore_working_copy=True)
        return [stripped for line in stdout.splitlines() if (stripped := line.strip())]

    def _run_jj(self, args: Sequence[str], *, ignore_working_copy: bool = False) -> str:
        extra_args = ("--ignore-working-copy",) if ignore_working_copy else ()
        return self._run_command(
            ["jj", *self._cli_args.to_argv(), *extra_args, *args],
            missing_tool_message=t"{ui.cmd('jj')} is not installed or is not on PATH.",
            detect_stale_workspace=True,
        )

    def _run_git(
        self,
        args: Sequence[str],
        *,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> str:
        return self._run_command(
            ["git", "--git-dir", str(self._backing_git_root()), *args],
            missing_tool_message=t"{ui.cmd('git')} is not installed or is not on PATH.",
            detect_stale_workspace=False,
            allowed_returncodes=allowed_returncodes,
        )

    def _backing_git_root(self) -> Path:
        """Resolve the exact Git object store used by this jj repository."""

        if self._git_root is None:
            rendered = self._run_jj(("git", "root"), ignore_working_copy=True).strip()
            if not rendered:
                raise JjCommandError(f"{ui.cmd('jj git root')} returned an empty path.")
            self._git_root = Path(rendered)
        return self._git_root

    def _git_remote(self, remote: str) -> GitRemote:
        """Resolve one jj remote name to its fetch and push URLs."""

        for configured_remote in self.list_git_remotes():
            if configured_remote.name == remote:
                return configured_remote
        raise JjCommandError(t"Git remote {ui.bookmark(remote)} is not configured.")

    def _git_fetch_refspecs(self, remote: str) -> tuple[str, ...]:
        """Read the backing Git fetch refspecs for one remote."""

        return tuple(
            line
            for line in self._run_git(
                ("config", "--get-all", f"remote.{remote}.fetch"),
                allowed_returncodes=frozenset({0, 1}),
            ).splitlines()
            if line
        )

    def _effective_config_origin(self, key: str) -> _ConfigOrigin | None:
        """Return the effective origin for one jj config key, if it is set."""

        stdout = self._run_jj(
            ("config", "list", key, "-T", _CONFIG_ORIGIN_TEMPLATE),
            ignore_working_copy=True,
        )
        lines = tuple(line for line in stdout.splitlines() if line.strip())
        if not lines:
            return None
        if len(lines) != 1:
            raise JjCommandError(
                t"{ui.cmd('jj config list')} returned multiple effective values for "
                t"{ui.code(key)}."
            )
        source_json, separator, path_json = lines[0].partition("\t")
        if not separator:
            raise JjCommandError(
                t"{ui.cmd('jj config list')} returned an unexpected config-origin payload."
            )
        try:
            source = json.loads(source_json)
            path = json.loads(path_json)
        except json.JSONDecodeError as error:
            raise JjCommandError(
                t"{ui.cmd('jj config list')} returned invalid config-origin JSON."
            ) from error
        if not isinstance(source, str) or not isinstance(path, str):
            raise JjCommandError(
                t"{ui.cmd('jj config list')} returned invalid config-origin fields."
            )
        return _ConfigOrigin(source=source, path=path)

    def _local_bookmark_targets(self, bookmark: str) -> tuple[str, ...]:
        """Return targets of one exact local bookmark, excluding remote entries."""

        stdout = self._run_jj(
            ("bookmark", "list", "-T", _BOOKMARK_TEMPLATE, bookmark),
            ignore_working_copy=True,
        )
        targets: list[str] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            raw = json.loads(stripped)
            if (
                not isinstance(raw, dict)
                or raw.get("name") != bookmark
                or ("remote" in raw and raw["remote"] is not None)
            ):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload while checking "
                    t"{ui.bookmark(bookmark)}."
                )
            targets.extend(_require_sequence(raw.get("target", ())))
        return tuple(dict.fromkeys(targets))

    def _clear_review_temp_ref(self) -> None:
        """Remove the fixed transient jj bookmark and backing Git import ref."""

        try:
            if self._local_bookmark_targets(_REVIEW_TEMP_BOOKMARK):
                self._run_jj(
                    ("bookmark", "forget", _REVIEW_TEMP_BOOKMARK),
                    ignore_working_copy=True,
                )
                self._run_jj(("git", "export"), ignore_working_copy=True)
        finally:
            raw_target = self.review_temp_ref_target()
            try:
                if raw_target is not None:
                    self._run_git(("update-ref", "-d", _REVIEW_TEMP_REF, raw_target))
            finally:
                if self.review_temp_ref_target() is not None:
                    raise JjCommandError(
                        t"Could not remove temporary Git ref {ui.code(_REVIEW_TEMP_REF)}."
                    )

        if self._local_bookmark_targets(_REVIEW_TEMP_BOOKMARK):
            raise JjCommandError(
                t"Could not forget temporary bookmark {ui.bookmark(_REVIEW_TEMP_BOOKMARK)}."
            )

    def _run_command(
        self,
        command: Sequence[str],
        *,
        missing_tool_message: ErrorMessage,
        detect_stale_workspace: bool,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> str:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                cwd=self._repo_root,
                text=True,
            )
        except FileNotFoundError as error:
            raise JjCommandError(missing_tool_message) from error

        if completed.returncode not in allowed_returncodes:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            if detect_stale_workspace and "The working copy is stale" in message:
                raise StaleWorkspaceError(
                    "The current workspace is stale.",
                    hint=t"Run {ui.cmd('jj workspace update-stale')} and retry.",
                )
            displayed_command = _redact_http_url_userinfo(shlex.join(command))
            displayed_message = _redact_http_url_userinfo(message)
            raise JjCommandError(t"{ui.cmd(displayed_command)} failed: {displayed_message}")
        return completed.stdout

    def _validate_reviewable_revision(
        self,
        revision: LocalRevision,
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
    ) -> None:
        # Check the root-commit condition before immutable, because the root
        # is always immutable in jj and "reached root before trunk()" is more
        # actionable than "immutable commit".
        if len(revision.parents) == 0:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                t"stack reached the root commit before {ui.revset('trunk()')}.",
                reason="reached_root_before_trunk",
            )
        if revision.is_working_copy and revision.empty:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "empty working-copy commits are not reviewable.",
                reason="empty_working_copy",
            )
        if revision.hidden:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "hidden commits are not reviewable.",
                reason="hidden_commit",
            )
        if revision.immutable and not allow_immutable:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "immutable commits are not reviewable.",
                reason="immutable_commit",
            )
        if revision.divergent and not allow_divergent:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "divergent changes are not supported.",
                reason="divergent_change",
            )
        if len(revision.parents) > 1:
            raise UnsupportedStackError.stack_shape(
                revision.change_id,
                "merge commits are not supported.",
                reason="merge_commit",
            )


_EXPECTED_FIELD_COUNT = 11

_DIVERGENT_CHANGE_ID_ERROR_PATTERN = re.compile(
    r"Change ID `(?P<change_id>[0-9a-z]+)` is divergent"
)
_HTTP_URL_AUTHORITY_PATTERN = re.compile(
    r"(?P<scheme>https?://)(?P<authority>[^/\s'\"<>]+)",
    re.IGNORECASE,
)


def _is_missing_revision_error(message: str) -> bool:
    return "Revision `" in message and "doesn't exist" in message


def _review_ref(branch: str) -> str:
    """Return the full Git ref for one managed jj-stack review branch."""

    from jj_stack.review.branches import is_managed_review_branch

    if not is_managed_review_branch(branch):
        raise ValueError(f"not a managed jj-stack review branch: {branch!r}")
    return f"refs/heads/{branch}"


def _unwrap_command_error_message(message: str) -> str:
    _prefix, separator, suffix = message.partition(" failed: ")
    return suffix if separator else message


def _redact_http_url_userinfo(text: str) -> str:
    """Remove HTTP URL credentials from command and subprocess-error displays."""

    def redact(match: re.Match[str]) -> str:
        authority = match.group("authority")
        if "@" not in authority:
            return match.group(0)
        return f"{match.group('scheme')}{authority.rsplit('@', maxsplit=1)[1]}"

    return _HTTP_URL_AUTHORITY_PATTERN.sub(redact, text)


def _revset_resolution_error(revset: str, error: JjCommandError) -> CliError | None:
    raw_message = _unwrap_command_error_message(str(error))
    if _is_missing_revision_error(raw_message):
        first_line = raw_message.splitlines()[0].strip()
        if first_line.startswith("Error: "):
            first_line = first_line.removeprefix("Error: ").strip()
        return CliError(first_line.rstrip("."))

    first_line = raw_message.splitlines()[0].strip()
    if first_line.startswith("Error: Failed to parse revset:"):
        detail = first_line.removeprefix("Error: ").strip()
        return UsageError(t"Invalid revset {ui.revset(revset)}: {detail}.")

    divergent_match = _DIVERGENT_CHANGE_ID_ERROR_PATTERN.search(first_line)
    if divergent_match is not None:
        return UnsupportedStackError.stack_shape(
            divergent_match.group("change_id"),
            "divergent changes are not supported.",
            reason="divergent_change",
        )

    return None


def _parse_revision_line(line: str) -> LocalRevision:
    parts = line.split("\t")
    if len(parts) != _EXPECTED_FIELD_COUNT:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected format: expected {_EXPECTED_FIELD_COUNT} "
            t"tab-separated fields, got {len(parts)}. Raw line: {line!r}"
        )
    (
        change_id_json,
        commit_id_json,
        description_json,
        parents_json,
        empty_json,
        divergent_json,
        working_copy_json,
        working_copy_workspaces_json,
        hidden_json,
        immutable_json,
        conflict_json,
    ) = parts
    try:
        parents_raw = json.loads(parents_json)
        if not isinstance(parents_raw, list):
            raise JjCommandError(
                t"{ui.cmd('jj log')} output has unexpected field types: "
                t"parents field is not a JSON array. Raw line: {line!r}"
            )
        working_copy_workspaces_raw = json.loads(working_copy_workspaces_json)
        if not isinstance(working_copy_workspaces_raw, list) or not all(
            isinstance(workspace, str) for workspace in working_copy_workspaces_raw
        ):
            raise JjCommandError(
                t"{ui.cmd('jj log')} output has unexpected field types: "
                t"working-copy workspaces field is not a JSON string array. Raw line: {line!r}"
            )
        return LocalRevision(
            change_id=json.loads(change_id_json),
            commit_id=json.loads(commit_id_json),
            conflict=json.loads(conflict_json),
            current_working_copy=json.loads(working_copy_json),
            description=json.loads(description_json),
            divergent=json.loads(divergent_json),
            empty=json.loads(empty_json),
            hidden=json.loads(hidden_json),
            immutable=json.loads(immutable_json),
            parents=tuple(parents_raw),
            working_copy_workspaces=tuple(working_copy_workspaces_raw),
        )
    except json.JSONDecodeError as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error


def _parse_revision_with_flags_line(
    line: str,
    flag_count: int,
) -> tuple[LocalRevision, tuple[bool, ...]]:
    expected_field_count = _EXPECTED_FIELD_COUNT + flag_count
    parts = line.split("\t")
    if len(parts) != expected_field_count:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected format: expected "
            t"{expected_field_count} tab-separated fields, got {len(parts)}. "
            t"Raw line: {line!r}"
        )
    revision = _parse_revision_line("\t".join(parts[:_EXPECTED_FIELD_COUNT]))
    try:
        flags = tuple(bool(json.loads(part)) for part in parts[_EXPECTED_FIELD_COUNT:])
    except json.JSONDecodeError as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error
    return revision, flags


def _require_sequence(value: object) -> Sequence[str]:
    if not isinstance(value, list | tuple):
        raise JjCommandError(
            t"Unexpected {ui.cmd('jj bookmark list')} payload: expected a sequence."
        )
    return tuple(str(item) for item in value if item is not None)


def _membership_scan_template(membership_revsets: Sequence[str]) -> str:
    flags = r' ++ "\t" ++ '.join(
        f"json(self.contained_in({json.dumps(revset)}))" for revset in membership_revsets
    )
    return _SCAN_TEMPLATE_PREFIX + flags + r' ++ "\n"'


def _short_change_id_render_template(*, min_len: int) -> str:
    shortest = f"change_id.shortest({min_len})"
    return (
        r'json(change_id) ++ "\t" ++ '
        + shortest
        + r".prefix() ++ "
        + shortest
        + r'.rest() ++ "\n"'
    )


def _quote_revset_symbol(symbol: str) -> str:
    if "'" not in symbol and all(ord(character) >= 32 for character in symbol):
        return f"'{symbol}'"
    escaped: list[str] = []
    for character in symbol:
        if character in {'"', "\\"}:
            escaped.append(f"\\{character}")
        elif ord(character) < 32:
            escaped.append(f"\\x{ord(character):02x}")
        else:
            escaped.append(character)
    return f'"{"".join(escaped)}"'


def _present_symbols_revset(symbols: Sequence[str]) -> str:
    """Union symbols as `present(...)` terms so unavailable ones do not fail the query."""

    return _union_revset_symbols(
        tuple(f"present({_quote_revset_symbol(symbol)})" for symbol in symbols),
        quote=False,
    )


def _union_revset_symbols(symbols: Sequence[str], *, quote: bool = True) -> str:
    parts = [_quote_revset_symbol(symbol) if quote else symbol for symbol in symbols]
    if not parts:
        raise ValueError("Expected at least one revset symbol.")
    if len(parts) == 1:
        return parts[0]
    return f"({' | '.join(parts)})"


def _chunked(values: Sequence[str], *, size: int = 200) -> tuple[tuple[str, ...], ...]:
    if size <= 0:
        raise ValueError("Chunk size must be positive.")
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))
