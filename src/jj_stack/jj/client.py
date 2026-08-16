"""Typed access to local `jj` stack state."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

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
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.git import GitRemote
from jj_stack.models.stack import LocalRevision
from jj_stack.review_namespace import ReviewNamespace

_REVISION_JSON_FIELDS = (
    r'"\"change_id\":" ++ json(change_id) ++ '
    r'",\"commit_id\":" ++ json(commit_id) ++ '
    r'",\"description\":" ++ json(description) ++ '
    r'",\"parents\":" ++ json(parents.map(|p| p.commit_id())) ++ '
    r'",\"empty\":" ++ json(empty) ++ '
    r'",\"divergent\":" ++ json(divergent) ++ '
    r'",\"current_working_copy\":" ++ json(current_working_copy) ++ '
    r'",\"working_copy_workspaces\":" ++ json(working_copies.map(|wc| wc.name())) ++ '
    r'",\"hidden\":" ++ json(self.hidden()) ++ '
    r'",\"immutable\":" ++ json(immutable) ++ '
    r'",\"conflict\":" ++ json(self.conflict())'
)
_COMMIT_TEMPLATE = r'"{" ++ ' + _REVISION_JSON_FIELDS + r' ++ "}\n"'
_BOOKMARK_TEMPLATE = r'json(self) ++ "\n"'
_REVIEW_TEMP_BOOKMARK = "jj-stack-tmp/checkout"
_REVIEW_TEMP_REF = f"refs/heads/{_REVIEW_TEMP_BOOKMARK}"
_CONFIG_ORIGIN_TEMPLATE = r'json(self) ++ "\n"'
_WORKSPACE_TEMPLATE = (
    r'"{\"name\":" ++ json(name) ++ '
    r'",\"root\":" ++ if(root, json(root.absolute()), "null") ++ '
    r'",\"current\":" ++ json(target.current_working_copy()) ++ "}\n"'
)


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails."""


ReviewFetchIsolationStatus = Literal["ready", "applied", "required"]
ReviewFetchIsolationProblem = Literal["missing", "duplicate"]


@dataclass(frozen=True, slots=True)
class ReviewFetchIsolation:
    """Result of checking the ordinary-fetch exclusion for review branches."""

    status: ReviewFetchIsolationStatus
    problem: ReviewFetchIsolationProblem | None


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


class JjWorkspace(BaseModel):
    """A named jj workspace and its recorded working-copy location."""

    model_config = ConfigDict(frozen=True, strict=True)

    name: str
    root: Path | None
    current: bool


ExpectedGitChangeId = str | None | tuple[str | None, ...]


class _ConfigOrigin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    source: str
    path: str


class _BookmarkRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    name: str
    target: tuple[str, ...]
    remote: str | None = None
    tracking_target: tuple[str, ...] | None = None


class _RevisionScan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    revision: LocalRevision
    membership: tuple[bool, ...]


UnsupportedStackReason = Literal[
    "divergent_change",
    "empty_working_copy",
    "hidden_commit",
    "immutable_commit",
    "merge_commit",
    "reached_root_before_trunk",
    "trunk_resolved_to_root",
    "undescribed_working_copy",
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


class _RenderableRevision(Protocol):
    @property
    def commit_id(self) -> str: ...


CliColorMode = Literal["always", "auto", "debug", "never"]
JjColorWhen = Literal["always", "debug", "never"]


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
        self._base_cli_args = cli_args
        self._cli_args = cli_args
        self._published_review_snapshots: dict[str, str] = {}
        self._config_strings: dict[str, str | None] = {}
        self._git_root: Path | None = None
        self._initial_working_copy_snapshot_pending = False

    @property
    def repo_root(self) -> Path:
        return self._repo_root

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

    def list_workspaces(self) -> tuple[JjWorkspace, ...]:
        """Return named workspaces with any working-copy roots recorded by jj."""

        stdout = self._run_jj(("workspace", "list", "-T", _WORKSPACE_TEMPLATE))
        return _parse_json_lines(
            stdout,
            command="jj workspace list",
            model=JjWorkspace,
        )

    def query_revisions_with_membership(
        self,
        revset: str,
        *,
        membership_revsets: Sequence[str],
        selected_revset: str | None = None,
    ) -> tuple[tuple[LocalRevision, tuple[bool, ...]], ...]:
        """Return revisions with one containment flag per supplied revset."""

        try:
            rows = self._query_revisions_with_membership(
                revset,
                membership_revsets=membership_revsets,
            )
            return tuple(
                (projected, flags)
                for revision, flags in rows
                if (projected := self._project(revision))
            )
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(selected_revset or revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise

    def query_revisions_by_change_ids(
        self,
        change_ids: Sequence[str],
        *,
        off_trunk: bool = False,
    ) -> dict[str, tuple[LocalRevision, ...]]:
        """Return visible revisions grouped by logical change ID."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        grouped: dict[str, list[LocalRevision]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for chunk in _chunked(ordered_change_ids):
            revset = _change_ids_revset(chunk)
            if off_trunk:
                revset = f"({revset}) ~ first_ancestors(trunk())"
            revisions = self._query_revisions(revset)
            for revision in revisions:
                if projected := self._project(revision):
                    grouped.setdefault(revision.change_id, []).append(projected)
        return {change_id: tuple(grouped.get(change_id, ())) for change_id in ordered_change_ids}

    def query_revisions_by_commit_ids(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalRevision, ...]:
        """Return locally available revisions for the supplied commit IDs in evaluation order."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        revisions_by_commit_id: dict[str, LocalRevision] = {}
        for chunk in _chunked(ordered_commit_ids):
            for revision in self._query_revisions(_present_symbols_revset(chunk)):
                revisions_by_commit_id.setdefault(revision.commit_id, revision)
        return tuple(revisions_by_commit_id.values())

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

        Each `(subject, target)` pair becomes one term in a unioned revset of the form
        `(subject_i & ::present(target_i))`, so the whole check runs as one `jj log` invocation
        regardless of pair count. Targets may be observed remotely without being available
        locally; those pairs yield no match. Subjects are required local commits. A subject's
        commit_id appears in the result iff at least one of its paired targets contains it. Equal
        commit IDs count as ancestors. Repeated pairs are deduped.
        """

        deduped_pairs = tuple(dict.fromkeys(pairs))
        if not deduped_pairs:
            return set()

        terms = " | ".join(
            f"({_quote_revset_symbol(subject)} & ::present({_quote_revset_symbol(target)}))"
            for subject, target in deduped_pairs
        )
        revisions = self._query_revisions(terms)
        return {revision.commit_id for revision in revisions}

    def get_config_string(self, key: str) -> str | None:
        """Return the string value of a jj config key, or None if unset.

        Reads are cached for the client's lifetime: nothing rewrites jj
        config during a command run, and callers such as per-revision
        rendering re-read the same key many times.
        """

        if key in self._config_strings:
            return self._config_strings[key]
        try:
            value = self._run_jj(("config", "get", key))
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

        return self._run_jj(("config", "list", "jj-stack"))

    def enable_initial_working_copy_snapshot(self) -> None:
        """Let the first post-bootstrap jj command use jj's normal working-copy lifecycle."""

        self._initial_working_copy_snapshot_pending = True

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
        revision: _RenderableRevision,
        *,
        color_when: JjColorWhen,
    ) -> tuple[str, ...]:
        """Render one revision with the user's `jj log` formatting."""

        stdout = self._run_jj(
            (
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
        revisions: Sequence[_RenderableRevision],
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
            revset = _change_ids_revset(chunk)
            stdout = self._run_jj(
                (
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

        stdout = self._run_jj(("git", "remote", "list"))
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

    def remote_bookmarks_at_revision(
        self,
        *,
        remote: str,
        revision: str,
    ) -> tuple[str, ...]:
        """Return locally observed remote bookmarks pointing at one revision."""

        stdout = self._run_jj(
            (
                "bookmark",
                "list",
                "--remote",
                remote,
                "--revision",
                revision,
                "-T",
                _BOOKMARK_TEMPLATE,
            )
        )
        return tuple(row.name for row in _parse_bookmark_rows(stdout) if row.remote == remote)

    def ensure_review_fetch_isolation(
        self,
        *,
        namespace: ReviewNamespace,
        remote: str,
        dry_run: bool = False,
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
                hint = t"Remove the override with {unset} to restore the normal exclusion."
            else:
                hint = (
                    t"Remove that {override_origin.source} override from the jj invocation "
                    t"or environment to restore the normal exclusion."
                )
            raise CliError(
                t"Effective jj setting {ui.code(override_key)} from {origin} overrides "
                t"Git fetch refspecs and can import {ui.bookmark(namespace.branch_prefix)} "
                t"bookmarks.",
                hint=hint,
            )

        config_key = f"remote.{remote}.fetch"
        configured = self._git_fetch_refspecs(remote)
        refspec = namespace.fetch_refspec
        count = configured.count(refspec)
        if count == 1:
            return ReviewFetchIsolation(status="ready", problem=None)

        status: ReviewFetchIsolationStatus = "required" if dry_run else "applied"
        result = ReviewFetchIsolation(
            status=status,
            problem="missing" if count == 0 else "duplicate",
        )
        if dry_run:
            return result

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
                refspec,
                refspec,
            )
        )
        updated = self._git_fetch_refspecs(remote)
        retained_default = bool(configured) or updated.count(default_fetch_refspec) == 1
        if updated.count(refspec) != 1 or not retained_default:
            raise JjCommandError(
                t"Git fetch configuration for remote {ui.bookmark(remote)} did not retain "
                t"the required positive and negative refspecs."
            )
        return result

    def visible_review_bookmark_targets(
        self,
        *,
        namespace: ReviewNamespace,
    ) -> dict[str, frozenset[str]]:
        """Return visible reserved-namespace bookmark targets grouped by name."""

        targets_by_name: dict[str, set[str]] = {}
        for row in self._bookmark_rows(namespace.branch_glob):
            targets_by_name.setdefault(row.name, set()).update(row.target)
        return {name: frozenset(targets) for name, targets in sorted(targets_by_name.items())}

    def accept_expected_review_bookmarks(
        self,
        bookmarks: Sequence[tuple[str, str, str]],
    ) -> None:
        """Keep exact expected remote bookmarks from making their snapshots immutable.

        The override narrows only jj's built-in untracked-remote rule. Trunk, tags, another
        untracked bookmark, and additions in the user's `immutable_heads()` still apply.
        """

        self._published_review_snapshots = {}
        untracked: list[tuple[str, str]] = []
        target_counts: dict[str, int] = {}
        for row in self._bookmark_rows():
            if row.remote is not None and row.tracking_target is None:
                for target in row.target:
                    untracked.append((row.name, target))
                    target_counts[target] = target_counts.get(target, 0) + 1
        selectors = " | ".join(
            f"(remote_bookmarks(exact:{json.dumps(name)}) & {_quote_revset_symbol(commit_id)})"
            for name, _change_id, commit_id in sorted(bookmarks)
            if (name, commit_id) in untracked and target_counts[commit_id] == 1
        )
        self._cli_args = self._base_cli_args
        if selectors:
            immutable_heads = f"trunk() | tags() | (untracked_remote_bookmarks() ~ ({selectors}))"
            self._cli_args = JjCliArgs(
                argv=(
                    *self._base_cli_args.to_argv(),
                    "--config",
                    f'revset-aliases."builtin_immutable_heads()"={immutable_heads}',
                )
            )
        revisions = self.query_revisions_by_change_ids(
            tuple(change_id for _name, change_id, _commit_id in bookmarks)
        )
        self._published_review_snapshots = {
            change_id: commit_id
            for _name, change_id, commit_id in bookmarks
            for matches in (revisions[change_id],)
            for published in (
                tuple(revision for revision in matches if revision.commit_id == commit_id),
            )
            for local in (
                tuple(revision for revision in matches if revision.commit_id != commit_id),
            )
            if len(published) == 1 and not published[0].immutable
            if len(local) == 1 and not local[0].immutable
        }

    def _bookmark_rows(self, *patterns: str) -> tuple[_BookmarkRow, ...]:
        stdout = self._run_jj(
            ("bookmark", "list", "--all-remotes", "-T", _BOOKMARK_TEMPLATE, *patterns)
        )
        return _parse_bookmark_rows(stdout)

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
        namespace: ReviewNamespace,
        expected_target: str,
        expected_change_id: str | None = None,
        expected_chain: Sequence[tuple[str, str, ExpectedGitChangeId]] = (),
        expected_parent_commit_id: str | None = None,
    ) -> Iterator[LocalRevision]:
        """Import one exact remote review ref, then remove all temporary artifacts.

        An expected chain guards every member's raw Git change ID and first-parent ancestry. A
        tuple accepts any listed ID, including a missing change-ID header represented by `None`.
        """

        ref = namespace.branch_ref(branch)
        chain = tuple(expected_chain)
        if chain and (
            expected_parent_commit_id is None
            or chain[-1][:2] != (branch, expected_target)
            or len({item[0] for item in chain}) != len(chain)
            or (
                expected_change_id is not None
                and not _expected_git_change_id_matches(chain[-1][2], expected_change_id)
            )
        ):
            raise ValueError("invalid expected remote review chain")
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
                for _chain_branch, target, expected_git_change_id in chain:
                    actual_change_id, parents = self._read_git_commit_metadata(target)
                    if not _expected_git_change_id_matches(
                        expected_git_change_id, actual_change_id
                    ) or parents != (expected_parent,):
                        raise CliError("Imported review heads no longer form the expected stack.")
                    expected_parent = target
            self._run_jj(("git", "import"))
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
        finally:
            self._clear_review_temp_ref()

    def read_remote_git_change_id(
        self,
        *,
        remote: str,
        commit_id: str,
    ) -> str | None:
        """Fetch and inspect one exact remote Git object without creating a ref."""

        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit_id) is None:
            raise ValueError("remote commit ID must be a full SHA-1 or SHA-256 object ID")
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
        branches: Sequence[str] = (),
        remote: str,
    ) -> None:
        """Fetch ordinary repository state using its configured selection."""

        # Normal fetch also imports backing-Git ref changes in a colocated repository.
        args = ["git", "fetch", "--remote", remote]
        for branch in dict.fromkeys(branches):
            args.extend(("--branch", branch))
        self._run_jj(tuple(args), manage_working_copy=True)

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

    def mutate_remote_review_refs(
        self,
        *,
        namespace: ReviewNamespace,
        remote: str,
        updates: Sequence[ReviewRefUpdate],
    ) -> None:
        """Atomically apply a complete review-ref update set with exact leases."""

        ordered_updates = tuple(updates)
        if not ordered_updates:
            return
        branches = tuple(update.branch for update in ordered_updates)
        if len(set(branches)) != len(branches):
            raise ValueError("remote review-ref update set contains duplicate branches")
        refs = tuple(namespace.branch_ref(branch) for branch in branches)
        if any(
            update.expected_target is None and update.desired_target is None
            for update in ordered_updates
        ):
            raise ValueError("cannot delete a review ref that is expected to be absent")

        if all(update.desired_target == update.expected_target for update in ordered_updates):
            return

        configured_remote = self._git_remote(remote)
        # Carry only the leased review refs: tag auto-follow would publish unrelated local
        # tags, and a pre-push hook was never invoked when this went through `jj git push`.
        command = ["push", "--atomic", "--no-follow-tags", "--no-verify"]
        for ref, update in zip(refs, ordered_updates, strict=True):
            expected = update.expected_target or ""
            command.append(f"--force-with-lease={ref}:{expected}")
        command.append(configured_remote.push_url)
        for ref, update in zip(refs, ordered_updates, strict=True):
            desired = update.desired_target or ""
            command.append(f"{desired}:{ref}")
        self._run_git(command)

    def edit_revision(self, commit_id: str) -> None:
        """Set the current workspace's working-copy revision to one exact commit."""

        self._run_jj(("edit", commit_id), manage_working_copy=True)

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
        self._run_jj(
            ("rebase", "-r", "|".join(ordered_revisions), "-d", destination),
            manage_working_copy=True,
        )

    def prepare_rebase_revisions_only(
        self,
        *,
        revisions: Sequence[str],
        destination: str,
    ) -> str:
        """Compute a rebase in an unintegrated operation and return its operation ID."""

        ordered_revisions = list(dict.fromkeys(revisions))
        if not ordered_revisions:
            raise ValueError("speculative rebase requires at least one revision")
        selected_head = ordered_revisions[-1]
        ordered_revisions.extend(
            revision.commit_id
            for revision in self.query_descendant_revisions((selected_head,))
            if revision.is_working_copy
            and revision.empty
            and revision.parents == (selected_head,)
        )
        output = self._run_jj(
            (
                "--no-integrate-operation",
                "rebase",
                "-r",
                "|".join(ordered_revisions),
                "-d",
                destination,
            ),
            return_stderr=True,
        )
        match = re.search(
            r"Operation left uncommitted because --no-integrate-operation was requested: "
            r"([0-9a-f]+)",
            output,
        )
        if match is None:
            raise JjCommandError(
                t"{ui.cmd('jj --no-integrate-operation rebase')} did not report its operation ID."
            )
        return match.group(1)

    def query_revisions_at_operation(
        self,
        *,
        change_ids: Sequence[str],
        operation_id: str,
    ) -> dict[str, tuple[LocalRevision, ...]]:
        """Return visible revisions for logical changes in one unintegrated operation."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}
        stdout = self._run_jj(
            (
                f"--at-op={operation_id}",
                "log",
                "--no-graph",
                "-r",
                _change_ids_revset(ordered_change_ids),
                "-T",
                _COMMIT_TEMPLATE,
            )
        )
        grouped: dict[str, list[LocalRevision]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for line in stdout.splitlines():
            if line.strip():
                revision = _parse_revision_line(line)
                grouped.setdefault(revision.change_id, []).append(revision)
        return {change_id: tuple(grouped.get(change_id, ())) for change_id in ordered_change_ids}

    def integrate_operation(self, operation_id: str) -> None:
        """Integrate one previously prepared jj operation."""

        self._run_jj(("op", "integrate", operation_id), manage_working_copy=True)
        self._run_jj(("workspace", "update-stale"), manage_working_copy=True)

    def git_tree_ids(self, commit_ids: Sequence[str]) -> dict[str, str]:
        """Return backing-Git tree IDs for exact commits."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return {}
        stdout = self._run_git(
            ("rev-parse", *(f"{commit_id}^{{tree}}" for commit_id in ordered_commit_ids))
        )
        tree_ids = tuple(line.strip() for line in stdout.splitlines() if line.strip())
        if len(tree_ids) != len(ordered_commit_ids):
            raise JjCommandError(t"{ui.cmd('git rev-parse')} returned incomplete tree data.")
        return dict(zip(ordered_commit_ids, tree_ids, strict=True))

    def abandon_revisions(self, revsets: Sequence[str]) -> None:
        """Abandon revisions; jj rebases descendants and drops pointing bookmarks."""

        ordered_revsets = tuple(revsets)
        if not ordered_revsets:
            return
        self._run_jj(("abandon", *ordered_revsets), manage_working_copy=True)

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

    def _project(self, revision: LocalRevision) -> LocalRevision | None:
        published = self._published_review_snapshots.get(revision.change_id)
        if revision.commit_id == published:
            return None
        if published is not None:
            return revision.model_copy(update={"divergent": False})
        return revision

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
        stdout = self._run_jj(command)
        return [stripped for line in stdout.splitlines() if (stripped := line.strip())]

    def _run_jj(
        self,
        args: Sequence[str],
        *,
        manage_working_copy: bool = False,
        return_stderr: bool = False,
    ) -> str:
        """Run jj without touching the working copy unless the caller explicitly requires it."""

        use_working_copy = manage_working_copy or self._initial_working_copy_snapshot_pending
        self._initial_working_copy_snapshot_pending = False
        extra_args = () if use_working_copy else ("--ignore-working-copy",)
        return self._run_command(
            ["jj", *self._cli_args.to_argv(), *extra_args, *args],
            missing_tool_message=t"{ui.cmd('jj')} is not installed or is not on PATH.",
            detect_stale_workspace=True,
            return_stderr=return_stderr,
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
            rendered = self._run_jj(("git", "root")).strip()
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

        stdout = self._run_jj(("config", "list", key, "-T", _CONFIG_ORIGIN_TEMPLATE))
        lines = tuple(line for line in stdout.splitlines() if line.strip())
        if not lines:
            return None
        if len(lines) != 1:
            raise JjCommandError(
                t"{ui.cmd('jj config list')} returned multiple effective values for "
                t"{ui.code(key)}."
            )
        return _parse_json_line(
            lines[0],
            command="jj config list",
            model=_ConfigOrigin,
        )

    def _local_bookmark_targets(self, bookmark: str) -> tuple[str, ...]:
        """Return targets of one exact local bookmark, excluding remote entries."""

        stdout = self._run_jj(("bookmark", "list", "-T", _BOOKMARK_TEMPLATE, bookmark))
        targets: list[str] = []
        for row in _parse_bookmark_rows(stdout):
            if row.name != bookmark or row.remote is not None:
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload while checking "
                    t"{ui.bookmark(bookmark)}."
                )
            targets.extend(row.target)
        return tuple(dict.fromkeys(targets))

    def _clear_review_temp_ref(self) -> None:
        """Remove the fixed transient jj bookmark and backing Git import ref."""

        try:
            if self._local_bookmark_targets(_REVIEW_TEMP_BOOKMARK):
                self._run_jj(("bookmark", "forget", _REVIEW_TEMP_BOOKMARK))
                self._run_jj(("git", "export"))
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
        return_stderr: bool = False,
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
        return completed.stderr if return_stderr else completed.stdout


_HTTP_URL_AUTHORITY_PATTERN = re.compile(
    r"(?P<scheme>https?://)(?P<authority>[^/\s'\"<>]+)",
    re.IGNORECASE,
)


def _is_missing_revision_error(message: str) -> bool:
    return "Revision `" in message and "doesn't exist" in message


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

    return None


def divergent_change_id_from_error(error: JjCommandError) -> str | None:
    """Return the short change ID that made a bare revset symbol divergent."""

    first_line = _unwrap_command_error_message(str(error)).splitlines()[0].strip()
    match = re.fullmatch(r"Error: Change ID `([k-z]+)` is divergent", first_line)
    return match.group(1) if match is not None else None


def _parse_json_line[Row: BaseModel](
    line: str,
    *,
    command: str,
    model: type[Row],
) -> Row:
    try:
        return model.model_validate_json(line)
    except ValidationError as error:
        raise JjCommandError(t"{ui.cmd(command)} returned invalid structured output.") from error


def _parse_json_lines[Row: BaseModel](
    stdout: str,
    *,
    command: str,
    model: type[Row],
) -> tuple[Row, ...]:
    return tuple(
        _parse_json_line(line, command=command, model=model)
        for line in stdout.splitlines()
        if line.strip()
    )


def _parse_bookmark_rows(stdout: str) -> tuple[_BookmarkRow, ...]:
    return _parse_json_lines(
        stdout,
        command="jj bookmark list",
        model=_BookmarkRow,
    )


def _parse_revision_line(line: str) -> LocalRevision:
    return _parse_json_line(line, command="jj log", model=LocalRevision)


def _parse_revision_with_flags_line(
    line: str,
    flag_count: int,
) -> tuple[LocalRevision, tuple[bool, ...]]:
    scan = _parse_json_line(line, command="jj log", model=_RevisionScan)
    if len(scan.membership) != flag_count:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has {len(scan.membership)} membership flags; "
            t"expected {flag_count}."
        )
    return scan.revision, scan.membership


def _membership_scan_template(membership_revsets: Sequence[str]) -> str:
    flags = r' ++ "," ++ '.join(
        f"json(self.contained_in({json.dumps(revset)}))" for revset in membership_revsets
    )
    return (
        r'"{\"revision\":{" ++ '
        + _REVISION_JSON_FIELDS
        + r' ++ "},\"membership\":[" ++ '
        + flags
        + r' ++ "]}\n"'
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


def _change_ids_revset(change_ids: Sequence[str]) -> str:
    """Union change IDs as `change_id(...)` terms.

    Every caller wants each change's visible copies, and a bare change-ID symbol fails outright
    once a change is divergent. Selecting through `change_id()` returns all of them, and like
    `present(...)` an unmatched change ID contributes nothing instead of failing the query.
    """

    return _union_revset_symbols(
        tuple(f"change_id({_quote_revset_symbol(change_id)})" for change_id in change_ids),
        quote=False,
    )


def _expected_git_change_id_matches(
    expected: ExpectedGitChangeId,
    actual: str | None,
) -> bool:
    accepted = expected if isinstance(expected, tuple) else (expected,)
    return actual in accepted


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
