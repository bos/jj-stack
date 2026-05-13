"""Typed access to local `jj` stack state."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import jj_review.ui as ui
from jj_review.errors import CliError, ErrorHint, ErrorMessage
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.stack import LocalRevision, LocalStack

_COMMIT_TEMPLATE = (
    r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
    r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
    r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
    r'json(current_working_copy) ++ "\t" ++ json(self.hidden()) ++ "\t" ++ '
    r'json(immutable) ++ "\t" ++ json(self.conflict()) ++ "\n"'
)
_SCAN_TEMPLATE_PREFIX = _COMMIT_TEMPLATE.removesuffix(r'"\n"') + r'"\t" ++ '
_TRUNK_SCAN_TEMPLATE = _SCAN_TEMPLATE_PREFIX + r'json(self.contained_in("trunk()")) ++ "\n"'
_BOOKMARK_TEMPLATE = r'json(self) ++ "\n"'


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails."""


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


@dataclass(slots=True)
class _RawBookmarkState:
    local_targets: tuple[str, ...] = ()
    remote_targets: list[RemoteBookmarkState] = field(default_factory=list)


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
            (
                trunk,
                head,
                selected_revset,
                merged_trunk_side_branch_commit_ids,
            ) = self._resolve_default_head_and_trunk()
        else:
            trunk, head, merged_trunk_side_branch_commit_ids = (
                self._resolve_selected_head_and_trunk(revset)
            )
            selected_revset = revset
            if head.current_working_copy and head.empty:
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
            merged_trunk_side_branch_commit_ids=merged_trunk_side_branch_commit_ids,
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
    ) -> tuple[LocalRevision, LocalRevision, str, set[str]]:
        """Resolve the default head, `trunk()`, and merged side-branch parents in one call."""

        revisions_with_trunk_membership = self._query_revisions_with_trunk_membership(
            "trunk() | @ | @- | (merges() & ::trunk())"
        )
        trunk: LocalRevision | None = None
        working_copy: LocalRevision | None = None
        merged_side_branch_commit_ids: set[str] = set()
        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for revision, is_trunk in revisions_with_trunk_membership:
            revisions_by_commit_id[revision.commit_id] = revision
            if is_trunk and trunk is None:
                trunk = revision
            if revision.current_working_copy:
                working_copy = revision
            if len(revision.parents) > 1:
                merged_side_branch_commit_ids.update(revision.parents[1:])
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
                return trunk, parent, "@-", merged_side_branch_commit_ids
            return (
                trunk,
                self.resolve_revision("@-"),
                "@-",
                merged_side_branch_commit_ids,
            )
        return trunk, working_copy, "@", merged_side_branch_commit_ids

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
            raise CliError(t"Revset {ui.revset(revset)} resolved to more than one revision.")
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
            revisions = self._query_revisions(
                _union_revset_symbols(
                    tuple(f"present({_quote_revset_symbol(change_id)})" for change_id in chunk),
                    quote=False,
                )
            )
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
            change_ids_revset = _union_revset_symbols(
                tuple(f"present({_quote_revset_symbol(change_id)})" for change_id in chunk),
                quote=False,
            )
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

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return set()

        trunk_ancestor_commit_ids: set[str] = set()
        for chunk in _chunked(ordered_commit_ids):
            revisions = self._query_revisions(f"({_union_revset_symbols(chunk)}) & ::trunk()")
            for revision in revisions:
                trunk_ancestor_commit_ids.add(revision.commit_id)
        return trunk_ancestor_commit_ids

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

    def supported_review_stack_change_ids(
        self,
        candidate_revisions: Sequence[LocalRevision],
        *,
        allow_divergent: bool = False,
        allow_immutable: bool = False,
    ) -> set[str]:
        """Return change IDs whose selected-parent path remains a supported review stack."""

        supported_change_ids: set[str] = set()
        for revision in candidate_revisions:
            try:
                self.discover_review_stack(
                    revision.commit_id,
                    allow_divergent=allow_divergent,
                    allow_immutable=allow_immutable,
                )
            except UnsupportedStackError:
                continue
            supported_change_ids.add(revision.change_id)
        return supported_change_ids

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
        merged_trunk_side_branch_commit_ids: set[str],
    ) -> tuple[LocalRevision, bool]:
        """Resolve the nearest stack boundary on the selected-parent path to `head`."""

        candidate_revset = f"::{_quote_revset_symbol(trunk.commit_id)}"
        if merged_trunk_side_branch_commit_ids:
            candidate_revset = (
                f"({candidate_revset} | "
                f"{_union_revset_symbols(sorted(merged_trunk_side_branch_commit_ids))})"
            )
        boundary_candidates = self._query_revisions(
            "heads("
            f"first_ancestors({_quote_revset_symbol(head_commit_id)}) & "
            f"{candidate_revset}"
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
        return boundary, boundary.commit_id in merged_trunk_side_branch_commit_ids

    def _resolve_selected_head_and_trunk(
        self,
        revset: str,
    ) -> tuple[LocalRevision, LocalRevision, set[str]]:
        """Resolve `revset`, `trunk()`, and merged side-branch parents in one call."""

        try:
            revisions = self._query_revisions_with_trunk_and_selection_membership(
                f"trunk() | ({revset}) | (merges() & ::trunk())",
                selection_revset=revset,
            )
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise

        trunk: LocalRevision | None = None
        selected: list[LocalRevision] = []
        merged_side_branch_commit_ids: set[str] = set()
        for revision, is_trunk, is_selected in revisions:
            if is_trunk and trunk is None:
                trunk = revision
            if is_selected:
                selected.append(revision)
            if len(revision.parents) > 1:
                merged_side_branch_commit_ids.update(revision.parents[1:])

        if not selected:
            raise CliError(t"Revset {ui.revset(revset)} did not resolve to a visible revision.")
        if len(selected) > 1:
            raise CliError(t"Revset {ui.revset(revset)} resolved to more than one revision.")

        return self._validate_trunk(trunk), selected[0], merged_side_branch_commit_ids

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

    def _merged_trunk_side_branch_commit_ids(self, trunk_commit_id: str) -> set[str]:
        """Return side-branch tips merged into trunk via non-first-parent merges.

        `status` only needs the merge-side parents that make a selected-parent
        walk safely stop before reaching the root. Querying every trunk ancestor
        and all of their children scales with total repo history, so in large
        repos we derive the same stop-set from trunk merge commits directly.
        """

        merge_revisions = self._query_revisions(
            f"(merges() & ::{_quote_revset_symbol(trunk_commit_id)})"
        )
        return {
            parent_commit_id
            for revision in merge_revisions
            for parent_commit_id in revision.parents[1:]
        }

    def get_config_string(self, key: str) -> str | None:
        """Return the string value of a jj config key, or None if unset."""

        try:
            value = self._run_jj(("config", "get", key))
        except JjCommandError:
            return None
        stripped = value.strip()
        return stripped if stripped else None

    def read_jj_review_config_list_output(self) -> str:
        """Return raw stdout from ``jj config list 'jj-review'``.

        Delegates scope merging and override handling to jj itself, so the
        same ``--config`` / ``--config-file`` overrides that flow to every jj
        invocation also shape jj-review's own configuration. The caller is
        responsible for parsing the TOML-dotted-key output.
        """

        return self._run_jj(("config", "list", "jj-review"))

    def show_with_stat(self, revset: str) -> str:
        """Return raw stdout from ``jj show --stat -r <revset>``.

        Raises `JjCommandError` if jj fails. The caller is responsible for
        parsing the diffstat out of the output and framing any user-facing
        error message.
        """

        return self._run_jj(("show", "--stat", "-r", revset))

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
            revset = _union_revset_symbols(
                tuple(f"present({_quote_revset_symbol(change_id)})" for change_id in chunk),
                quote=False,
            )
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

    def get_commit_diff(self, revision: str) -> str:
        """Return the `jj diff` output for one revision against its parent."""

        return self._run_jj(
            (
                "diff",
                "--git",
                "--no-pager",
                "-r",
                _quote_revset_symbol(revision),
            )
        )

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        """List configured Git remotes for the repository."""

        stdout = self._run_jj(("git", "remote", "list"))
        remotes: list[GitRemote] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name, url = stripped.split(maxsplit=1)
            remotes.append(GitRemote(name=name, url=url))
        return tuple(remotes)

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        """Return local and remote state for the named bookmark."""

        return self.list_bookmark_states((bookmark,)).get(bookmark, BookmarkState(name=bookmark))

    def list_bookmark_states(
        self,
        bookmarks: Sequence[str] | None = None,
    ) -> dict[str, BookmarkState]:
        """Return local and remote state for the requested bookmark names."""

        command = ["bookmark", "list", "--all-remotes", "-T", _BOOKMARK_TEMPLATE]
        if bookmarks:
            command.extend(bookmarks)

        stdout = self._run_jj(command)
        grouped: dict[str, _RawBookmarkState] = {}
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            raw_bookmark = json.loads(stripped)
            if not isinstance(raw_bookmark, dict):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload: expected a JSON object."
                )
            name = raw_bookmark["name"]
            if not isinstance(name, str):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload: missing bookmark name."
                )
            bookmark_state = grouped.setdefault(name, _RawBookmarkState())
            targets = tuple(_require_sequence(raw_bookmark.get("target", ())))
            remote_name = raw_bookmark.get("remote")
            if remote_name is None:
                bookmark_state.local_targets = targets
                continue
            if not isinstance(remote_name, str):
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload: invalid remote bookmark "
                    t"entry."
                )
            tracking_target = raw_bookmark.get("tracking_target", ())
            bookmark_state.remote_targets.append(
                RemoteBookmarkState(
                    remote=remote_name,
                    targets=targets,
                    tracking_targets=tuple(_require_sequence(tracking_target)),
                )
            )

        states = {
            name: BookmarkState(
                name=name,
                local_targets=raw_state.local_targets,
                remote_targets=tuple(raw_state.remote_targets),
            )
            for name, raw_state in grouped.items()
        }
        if bookmarks:
            for bookmark in bookmarks:
                states.setdefault(bookmark, BookmarkState(name=bookmark))
        return states

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        """Create or move a local bookmark to the supplied revision."""

        command = ["bookmark", "set"]
        if allow_backwards:
            command.append("--allow-backwards")
        command.extend((bookmark, "-r", revision))
        self._run_jj(command)

    def forget_bookmarks(self, bookmarks: Sequence[str]) -> None:
        """Forget one or more local bookmarks without scheduling remote deletions."""

        ordered_bookmarks = tuple(bookmarks)
        if not ordered_bookmarks:
            return
        self._run_jj(("bookmark", "forget", *ordered_bookmarks))

    def push_bookmarks(
        self,
        *,
        remote: str,
        bookmarks: Sequence[str],
    ) -> None:
        """Push one or more bookmarks to the selected remote."""

        ordered_bookmarks = tuple(bookmarks)
        if not ordered_bookmarks:
            return
        command = ["git", "push", "--remote", remote]
        for bookmark in ordered_bookmarks:
            command.extend(["--bookmark", bookmark])
        self._run_jj(command)

    def fetch_remote(
        self,
        *,
        remote: str,
        branches: Sequence[str] | None = None,
    ) -> None:
        """Refresh remembered remote bookmark state for the selected remote."""

        command = ["git", "fetch", "--remote", remote]
        if branches:
            for branch in branches:
                command.extend(["--branch", branch])
        self._run_jj(command)

    def list_remote_branches(
        self,
        *,
        remote: str,
        patterns: Sequence[str],
    ) -> dict[str, str]:
        """List matching remote branch heads without importing unrelated bookmark state."""

        if not patterns:
            return {}
        stdout = self._run_git(("ls-remote", "--refs", remote, *patterns))
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

    def track_bookmark(self, *, remote: str, bookmark: str) -> None:
        """Track an existing remote bookmark locally."""

        self._run_jj(("bookmark", "track", bookmark, "--remote", remote))

    def update_untracked_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        """Update an existing untracked remote bookmark without importing it first."""

        self._run_git(
            (
                "push",
                f"--force-with-lease=refs/heads/{bookmark}:{expected_remote_target}",
                remote,
                f"{desired_target}:refs/heads/{bookmark}",
            )
        )
        self.fetch_remote(remote=remote)
        self.track_bookmark(remote=remote, bookmark=bookmark)

    def delete_remote_bookmarks(
        self,
        *,
        remote: str,
        deletions: Sequence[tuple[str, str]],
        fetch: bool = True,
    ) -> None:
        """Delete one or more remote bookmarks by name."""

        ordered_deletions = tuple(deletions)
        if not ordered_deletions:
            return
        command = ["push"]
        for bookmark, expected_remote_target in ordered_deletions:
            command.append(f"--force-with-lease=refs/heads/{bookmark}:{expected_remote_target}")
        command.append(remote)
        for bookmark, _expected_remote_target in ordered_deletions:
            command.append(f":refs/heads/{bookmark}")
        self._run_git(command)
        if fetch:
            self.fetch_remote(remote=remote)

    def rebase_revision(self, *, source: str, destination: str) -> None:
        """Rebase one revision and its descendants onto a new destination."""

        self._run_jj(("rebase", "-s", source, "-d", destination))

    def _query_revisions(self, revset: str, *, limit: int | None = None) -> list[LocalRevision]:
        command = ["log", "--no-graph", "-r", revset, "-T", _COMMIT_TEMPLATE]
        if limit is not None:
            command.extend(["--limit", str(limit)])

        stdout = self._run_jj(command)
        revisions: list[LocalRevision] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            revisions.append(_parse_revision_line(stripped))
        return revisions

    def _query_revisions_with_trunk_membership(
        self,
        revset: str,
    ) -> list[tuple[LocalRevision, bool]]:
        stdout = self._run_jj(("log", "--no-graph", "-r", revset, "-T", _TRUNK_SCAN_TEMPLATE))
        revisions: list[tuple[LocalRevision, bool]] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            revisions.append(_parse_revision_with_flag_line(stripped))
        return revisions

    def _query_revisions_with_trunk_and_selection_membership(
        self,
        revset: str,
        *,
        selection_revset: str,
    ) -> list[tuple[LocalRevision, bool, bool]]:
        stdout = self._run_jj(
            (
                "log",
                "--no-graph",
                "-r",
                revset,
                "-T",
                _selection_scan_template(selection_revset),
            )
        )
        revisions: list[tuple[LocalRevision, bool, bool]] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            revisions.append(_parse_revision_with_two_flag_line(stripped))
        return revisions

    def _run_jj(self, args: Sequence[str]) -> str:
        return self._run_command(
            ["jj", *self._cli_args.to_argv(), *args],
            missing_tool_message=t"{ui.cmd('jj')} is not installed or is not on PATH.",
            detect_stale_workspace=True,
        )

    def _run_git(self, args: Sequence[str]) -> str:
        return self._run_command(
            ["git", *args],
            missing_tool_message=t"{ui.cmd('git')} is not installed or is not on PATH.",
            detect_stale_workspace=False,
        )

    def _run_command(
        self,
        command: Sequence[str],
        *,
        missing_tool_message: ErrorMessage,
        detect_stale_workspace: bool,
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

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            if detect_stale_workspace and "The working copy is stale" in message:
                raise StaleWorkspaceError(
                    "The current workspace is stale.",
                    hint=t"Run {ui.cmd('jj workspace update-stale')} and retry.",
                )
            raise JjCommandError(t"{ui.cmd(shlex.join(command))} failed: {message}")
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


_EXPECTED_FIELD_COUNT = 10
_EXPECTED_FIELD_COUNT_WITH_FLAG = 11
_EXPECTED_FIELD_COUNT_WITH_TWO_FLAGS = 12


def _is_missing_revision_error(message: str) -> bool:
    return "Revision `" in message and "doesn't exist" in message


def _unwrap_command_error_message(message: str) -> str:
    _prefix, separator, suffix = message.partition(" failed: ")
    return suffix if separator else message


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
        return CliError(t"Invalid revset {ui.revset(revset)}: {detail}.")

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
        )
    except json.JSONDecodeError as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error


def _parse_revision_with_flag_line(line: str) -> tuple[LocalRevision, bool]:
    parts = line.split("\t")
    if len(parts) != _EXPECTED_FIELD_COUNT_WITH_FLAG:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected format: expected "
            t"{_EXPECTED_FIELD_COUNT_WITH_FLAG} tab-separated fields, got {len(parts)}. "
            t"Raw line: {line!r}"
        )
    revision = _parse_revision_line("\t".join(parts[:_EXPECTED_FIELD_COUNT]))
    try:
        return revision, bool(json.loads(parts[_EXPECTED_FIELD_COUNT]))
    except json.JSONDecodeError as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error
    except (TypeError, ValueError) as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected field types: {error}. Raw line: {line!r}"
        ) from error


def _parse_revision_with_two_flag_line(line: str) -> tuple[LocalRevision, bool, bool]:
    parts = line.split("\t")
    if len(parts) != _EXPECTED_FIELD_COUNT_WITH_TWO_FLAGS:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected format: expected "
            t"{_EXPECTED_FIELD_COUNT_WITH_TWO_FLAGS} tab-separated fields, got {len(parts)}. "
            t"Raw line: {line!r}"
        )
    revision = _parse_revision_line("\t".join(parts[:_EXPECTED_FIELD_COUNT]))
    try:
        return (
            revision,
            bool(json.loads(parts[_EXPECTED_FIELD_COUNT])),
            bool(json.loads(parts[_EXPECTED_FIELD_COUNT + 1])),
        )
    except json.JSONDecodeError as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output contains invalid JSON: {error}. Raw line: {line!r}"
        ) from error
    except (TypeError, ValueError) as error:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has unexpected field types: {error}. Raw line: {line!r}"
        ) from error


def _require_sequence(value: object) -> Sequence[str]:
    if not isinstance(value, list | tuple):
        raise JjCommandError(
            t"Unexpected {ui.cmd('jj bookmark list')} payload: expected a sequence."
        )
    return tuple(str(item) for item in value if item is not None)


def _selection_scan_template(selection_revset: str) -> str:
    return (
        _SCAN_TEMPLATE_PREFIX
        + r'json(self.contained_in("trunk()")) ++ "\t" ++ '
        + r"json(self.contained_in("
        + json.dumps(selection_revset)
        + r')) ++ "\n"'
    )


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
    return f"'{symbol}'"


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
