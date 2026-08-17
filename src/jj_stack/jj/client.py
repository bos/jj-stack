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
from textwrap import dedent
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
from jj_stack.models.stack import LocalCommit
from jj_stack.pr_branch_namespace import current_pr_branch_namespace

_CHANGE_JSON_FIELDS = dedent(
    r"""
    "\"change_id\":" ++ json(change_id) ++
    ",\"commit_id\":" ++ json(commit_id) ++
    ",\"description\":" ++ json(description) ++
    ",\"parents\":" ++ json(parents.map(|p| p.commit_id())) ++
    ",\"empty\":" ++ json(empty) ++
    ",\"divergent\":" ++ json(divergent) ++
    ",\"current_working_copy\":" ++ json(current_working_copy) ++
    ",\"working_copy_workspaces\":" ++ json(working_copies.map(|wc| wc.name())) ++
    ",\"hidden\":" ++ json(self.hidden()) ++
    ",\"immutable\":" ++ json(immutable) ++
    ",\"conflict\":" ++ json(self.conflict())
    """
).strip()
_COMMIT_TEMPLATE = rf'"{{" ++ {_CHANGE_JSON_FIELDS} ++ "}}\n"'
_BOOKMARK_TEMPLATE = r'json(self) ++ "\n"'
_PR_BRANCH_TEMP_BOOKMARK = "jj-stack-tmp/checkout"
_PR_BRANCH_TEMP_REF = f"refs/heads/{_PR_BRANCH_TEMP_BOOKMARK}"
_CONFIG_ORIGIN_TEMPLATE = r'json(self) ++ "\n"'
_WORKSPACE_TEMPLATE = dedent(
    r"""
    "{\"name\":" ++ json(name) ++
    ",\"root\":" ++ if(root, json(root.absolute()), "null") ++
    ",\"current\":" ++ json(target.current_working_copy()) ++ "}\n"
    """
).strip()


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails."""


PRBranchFetchIsolationStatus = Literal["ready", "applied", "required"]
PRBranchFetchIsolationProblem = Literal["missing", "duplicate"]


@dataclass(frozen=True, slots=True)
class PRBranchFetchIsolation:
    """Result of checking the ordinary-fetch exclusion for PR branches."""

    status: PRBranchFetchIsolationStatus
    problem: PRBranchFetchIsolationProblem | None


@dataclass(frozen=True, slots=True)
class PRRefUpdate:
    """One exact leased PR branch update in a complete remote mutation set."""

    branch: str
    expected_target: str | None
    desired_target: str | None


@dataclass(frozen=True, slots=True)
class PRTempArtifacts:
    """Observed fixed PR branch import artifacts without applying recovery."""

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


class _CommitScan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    commit: LocalCommit
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
    """Raised when local history cannot be treated as a linear stack."""

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


class _RenderableCommit(Protocol):
    @property
    def commit_id(self) -> str: ...


CliColorMode = Literal["always", "auto", "debug", "never"]
JjColorWhen = Literal["always", "debug", "never"]


_NO_CLI_ARGS = JjCliArgs()


class JjClient:
    """Thin wrapper around `jj` commands used by jj-stack."""

    def __init__(
        self,
        repo_root: Path,
        *,
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> None:
        self._repo_root = repo_root
        self._base_cli_args = cli_args
        self._cli_args = cli_args
        self._published_pr_snapshots: dict[str, str] = {}
        self._config_strings: dict[str, str | None] = {}
        self._git_root: Path | None = None
        self._initial_working_copy_snapshot_pending = False

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    def resolve_commit(self, revset: str) -> LocalCommit:
        """Resolve a revset to exactly one commit."""

        try:
            commits = self._query_commits(revset, limit=2)
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise
        if not commits:
            raise CliError(t"Revset {ui.revset(revset)} did not resolve to a visible commit.")
        if len(commits) > 1:
            raise AmbiguousSelectionError(
                t"Revset {ui.revset(revset)} resolved to more than one commit."
            )
        return commits[0]

    def query_commits(
        self,
        revset: str,
    ) -> tuple[LocalCommit, ...]:
        """Return commits matching the supplied revset."""

        try:
            return tuple(self._query_commits(revset))
        except JjCommandError as error:
            if _is_missing_commit_error(_unwrap_command_error_message(str(error))):
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

    def query_commits_with_membership(
        self,
        revset: str,
        *,
        membership_revsets: Sequence[str],
        selected_revset: str | None = None,
    ) -> tuple[tuple[LocalCommit, tuple[bool, ...]], ...]:
        """Return commits with one containment flag per supplied revset."""

        try:
            rows = self._query_commits_with_membership(
                revset,
                membership_revsets=membership_revsets,
            )
            return tuple(
                (projected, flags)
                for commit, flags in rows
                if (projected := self._project(commit))
            )
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(selected_revset or revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise

    def query_commits_by_change_ids(
        self,
        change_ids: Sequence[str],
    ) -> dict[str, tuple[LocalCommit, ...]]:
        """Return visible commits grouped by logical change ID."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        grouped: dict[str, list[LocalCommit]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for chunk in _chunked(ordered_change_ids):
            revset = _change_ids_revset(chunk)
            commits = self._query_commits(revset)
            for commit in commits:
                if projected := self._project(commit):
                    grouped.setdefault(commit.change_id, []).append(projected)
        return {change_id: tuple(grouped.get(change_id, ())) for change_id in ordered_change_ids}

    def query_commits_by_change_ids_with_off_trunk(
        self,
        change_ids: Sequence[str],
    ) -> tuple[
        dict[str, tuple[LocalCommit, ...]],
        dict[str, tuple[LocalCommit, ...]],
    ]:
        """Return all visible copies and the subset outside trunk in one scan."""

        ordered = tuple(dict.fromkeys(change_ids))
        all_copies: dict[str, list[LocalCommit]] = {change_id: [] for change_id in ordered}
        off_trunk: dict[str, list[LocalCommit]] = {change_id: [] for change_id in ordered}
        for chunk in _chunked(ordered):
            rows = self._query_commits_with_membership(
                _change_ids_revset(chunk),
                membership_revsets=("~first_ancestors(trunk())",),
            )
            for commit, (is_off_trunk,) in rows:
                if projected := self._project(commit):
                    all_copies.setdefault(commit.change_id, []).append(projected)
                    if is_off_trunk:
                        off_trunk.setdefault(commit.change_id, []).append(projected)
        return (
            {change_id: tuple(all_copies[change_id]) for change_id in ordered},
            {change_id: tuple(off_trunk[change_id]) for change_id in ordered},
        )

    def query_commits_by_ids(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalCommit, ...]:
        """Return locally available commits for the supplied commit IDs in evaluation order."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        commits_by_id: dict[str, LocalCommit] = {}
        for chunk in _chunked(ordered_commit_ids):
            for commit in self._query_commits(_present_symbols_revset(chunk)):
                commits_by_id.setdefault(commit.commit_id, commit)
        return tuple(commits_by_id.values())

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
                commits = self._query_commits_with_membership(
                    _present_symbols_revset(chunk),
                    membership_revsets=(f"::{_quote_revset_symbol(descendant_commit_id)}",),
                )
            except JjCommandError:
                continue
            for commit, (is_ancestor,) in commits:
                memberships[commit.commit_id] = is_ancestor
        return memberships

    def query_descendant_commits(
        self,
        commit_ids: Sequence[str],
    ) -> tuple[LocalCommit, ...]:
        """Return descendants for the supplied commits, including the commits themselves."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        commits_by_id: dict[str, LocalCommit] = {}
        for chunk in _chunked(ordered_commit_ids):
            commits = self._query_commits(f"{_union_revset_symbols(chunk)}::")
            for commit in commits:
                commits_by_id.setdefault(commit.commit_id, commit)
        return tuple(commits_by_id.values())

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
        commits = self._query_commits(terms)
        return {commit.commit_id for commit in commits}

    def get_config_string(self, key: str) -> str | None:
        """Return the string value of a jj config key, or None if unset.

        Reads are cached for the client's lifetime: nothing rewrites jj
        config during a command run, and callers such as per-change
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

    def render_commit_log_lines(
        self,
        change: _RenderableCommit,
        *,
        color_when: JjColorWhen,
    ) -> tuple[str, ...]:
        """Render one change with the user's `jj log` formatting."""

        stdout = self._run_jj(
            (
                "--no-pager",
                "--color",
                color_when,
                "log",
                "-r",
                _quote_revset_symbol(change.commit_id),
                "--limit",
                "1",
            )
        )
        return tuple(line for line in stdout.rstrip("\n").splitlines() if line.strip() != "~")

    def render_commit_log_blocks(
        self,
        changes: Sequence[_RenderableCommit],
        *,
        color_when: JjColorWhen,
    ) -> dict[str, tuple[str, ...]]:
        """Render several changes in parallel, keyed by commit_id.

        Each `jj log` invocation pays a substantial startup cost, so rendering
        a stack sequentially dominates the wall-clock time of commands like
        `status`. Fan the per-change calls out onto a thread pool so their
        subprocess spawns overlap.
        """

        if not changes:
            return {}
        if len(changes) == 1:
            change = changes[0]
            return {change.commit_id: self.render_commit_log_lines(change, color_when=color_when)}
        with ThreadPoolExecutor(max_workers=min(len(changes), 10)) as pool:
            rendered = list(
                pool.map(
                    lambda change: (
                        change.commit_id,
                        self.render_commit_log_lines(change, color_when=color_when),
                    ),
                    changes,
                )
            )
        return dict(rendered)

    def render_short_change_ids(
        self,
        change_ids: Sequence[str],
        *,
        color_when: JjColorWhen,
    ) -> dict[str, str]:
        """Render shortest visible change IDs for the supplied logical change IDs."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        rendered: dict[str, str] = {}
        template = _short_change_id_render_template()
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
        changes: tuple[LocalCommit, ...],
    ) -> tuple[LocalCommit, ...]:
        """Return changes blocked by the repo's git.private-commits policy."""

        private_commits_revset = self.get_config_string("git.private-commits")
        if not private_commits_revset or not changes:
            return ()
        if private_commits_revset == "none()":
            return ()
        commit_ids_revset = " | ".join(_quote_revset_symbol(r.commit_id) for r in changes)
        combined_revset = f"({private_commits_revset}) & ({commit_ids_revset})"
        return tuple(self.query_commits(combined_revset))

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        """List configured Git remotes for the repo."""

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

    def remote_bookmarks_at_commit(
        self,
        *,
        remote: str,
        commit_id: str,
    ) -> tuple[str, ...]:
        """Return locally observed remote bookmarks pointing at one commit."""

        stdout = self._run_jj(
            (
                "bookmark",
                "list",
                "--remote",
                remote,
                "--revision",
                commit_id,
                "-T",
                _BOOKMARK_TEMPLATE,
            )
        )
        return tuple(row.name for row in _parse_bookmark_rows(stdout) if row.remote == remote)

    def ensure_pr_branch_fetch_isolation(
        self,
        *,
        remote: str,
        dry_run: bool = False,
    ) -> PRBranchFetchIsolation:
        """Ensure ordinary fetches cannot import jj-stack PR branches."""

        namespace = current_pr_branch_namespace()
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
            return PRBranchFetchIsolation(status="ready", problem=None)

        status: PRBranchFetchIsolationStatus = "required" if dry_run else "applied"
        result = PRBranchFetchIsolation(
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

    def visible_pr_bookmark_targets(
        self,
    ) -> dict[str, frozenset[str]]:
        """Return visible reserved-namespace bookmark targets grouped by name."""

        namespace = current_pr_branch_namespace()
        targets_by_name: dict[str, set[str]] = {}
        for row in self._bookmark_rows(namespace.branch_glob):
            targets_by_name.setdefault(row.name, set()).update(row.target)
        return {name: frozenset(targets) for name, targets in sorted(targets_by_name.items())}

    def accept_expected_pr_bookmarks(
        self,
        bookmarks: Sequence[tuple[str, str, str]],
    ) -> None:
        """Keep exact expected remote bookmarks from making their snapshots immutable.

        The override narrows only jj's built-in untracked-remote rule. Trunk, tags, another
        untracked bookmark, and additions in the user's `immutable_heads()` still apply.
        """

        self._published_pr_snapshots = {}
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
        commits = self.query_commits_by_change_ids(
            tuple(change_id for _name, change_id, _commit_id in bookmarks)
        )
        self._published_pr_snapshots = {
            change_id: commit_id
            for _name, change_id, commit_id in bookmarks
            for matches in (commits[change_id],)
            for published in (
                tuple(commit for commit in matches if commit.commit_id == commit_id),
            )
            for local in (tuple(commit for commit in matches if commit.commit_id != commit_id),)
            if len(published) == 1 and not published[0].immutable
            if len(local) == 1 and not local[0].immutable
        }

    def _bookmark_rows(self, *patterns: str) -> tuple[_BookmarkRow, ...]:
        stdout = self._run_jj(
            ("bookmark", "list", "--all-remotes", "-T", _BOOKMARK_TEMPLATE, *patterns)
        )
        return _parse_bookmark_rows(stdout)

    def pr_branch_temp_ref_target(self) -> str | None:
        """Return the exact temporary PR branch import ref target, if it exists."""

        target = self._run_git(
            ("rev-parse", "--verify", "--quiet", _PR_BRANCH_TEMP_REF),
            allowed_returncodes=frozenset({0, 1}),
        ).strip()
        return target or None

    def pr_branch_temp_artifacts(self) -> PRTempArtifacts:
        """Observe the fixed temporary import ref and its transient jj bookmark."""

        return PRTempArtifacts(
            bookmark_targets=self._local_bookmark_targets(_PR_BRANCH_TEMP_BOOKMARK),
            ref_target=self.pr_branch_temp_ref_target(),
        )

    @contextmanager
    def import_remote_pr_branch_ref(
        self,
        *,
        remote: str,
        branch: str,
        expected_target: str,
        expected_change_id: str | None = None,
        expected_chain: Sequence[tuple[str, str, ExpectedGitChangeId]] = (),
        expected_parent_commit_id: str | None = None,
    ) -> Iterator[LocalCommit]:
        """Import one exact remote PR branch ref, then remove all temporary artifacts.

        An expected chain guards every member's raw Git change ID and first-parent ancestry. A
        tuple accepts any listed ID, including a missing change-ID header represented by `None`.
        """

        ref = current_pr_branch_namespace().branch_ref(branch)
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
            raise ValueError("invalid expected remote PR branch chain")
        self._clear_pr_branch_temp_ref()
        try:
            configured_remote = self._git_remote(remote)
            self._run_git(
                (
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    configured_remote.fetch_url,
                    f"+{ref}:{_PR_BRANCH_TEMP_REF}",
                )
            )
            if self.pr_branch_temp_ref_target() != expected_target:
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
                        raise CliError(
                            "Imported pull request heads no longer form the expected stack."
                        )
                    expected_parent = target
            self._run_jj(("git", "import"))
            change = self.resolve_commit(_quote_revset_symbol(_PR_BRANCH_TEMP_BOOKMARK))
            if change.commit_id != expected_target:
                raise JjCommandError(
                    t"{ui.cmd('jj git import')} did not import the exact temporary PR branch ref."
                )
            if expected_change_id is not None and change.change_id != expected_change_id:
                raise CliError(
                    t"Remote branch {ui.bookmark(branch)} resolves to change "
                    t"{ui.change_id(change.change_id)}, not the expected change "
                    t"{ui.change_id(expected_change_id)}."
                )
            yield change
        finally:
            self._clear_pr_branch_temp_ref()

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
        """Fetch ordinary repo state using its configured selection."""

        # Normal fetch also imports backing-Git ref changes in a colocated repo.
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

    def mutate_remote_pr_branch_refs(
        self,
        *,
        remote: str,
        updates: Sequence[PRRefUpdate],
    ) -> None:
        """Atomically apply a complete PR branch update set with exact leases."""

        namespace = current_pr_branch_namespace()
        ordered_updates = tuple(updates)
        if not ordered_updates:
            return
        branches = tuple(update.branch for update in ordered_updates)
        if len(set(branches)) != len(branches):
            raise ValueError("remote PR branch update set contains duplicate branches")
        refs = tuple(namespace.branch_ref(branch) for branch in branches)
        if any(
            update.expected_target is None and update.desired_target is None
            for update in ordered_updates
        ):
            raise ValueError("cannot delete a PR branch ref that is expected to be absent")

        if all(update.desired_target == update.expected_target for update in ordered_updates):
            return

        configured_remote = self._git_remote(remote)
        # Carry only the leased PR branch refs: tag auto-follow would publish unrelated local
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

    def edit_commit(self, commit_id: str) -> None:
        """Set the current workspace's working-copy change to one exact commit."""

        self._run_jj(("edit", commit_id), manage_working_copy=True)

    def rebase_exact_commits(
        self,
        *,
        commit_ids: Sequence[str],
        destination: str,
    ) -> None:
        """Rebase exactly the named commits onto one destination."""

        ordered_commit_ids = list(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return
        self._run_jj(
            ("rebase", "-r", "|".join(ordered_commit_ids), "-d", destination),
            manage_working_copy=True,
        )

    def prepare_rebase_exact_commits(
        self,
        *,
        commit_ids: Sequence[str],
        destination: str,
    ) -> str:
        """Compute a rebase in an unintegrated operation and return its operation ID."""

        ordered_commit_ids = list(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            raise ValueError("speculative rebase requires at least one commit")
        output = self._run_jj(
            (
                "--no-integrate-operation",
                "rebase",
                "-r",
                "|".join(ordered_commit_ids),
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

    def query_commits_at_operation(
        self,
        *,
        change_ids: Sequence[str],
        operation_id: str,
    ) -> dict[str, tuple[LocalCommit, ...]]:
        """Return visible commits for logical changes in one unintegrated operation."""

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
        grouped: dict[str, list[LocalCommit]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for line in stdout.splitlines():
            if line.strip():
                commit = _parse_commit_line(line)
                grouped.setdefault(commit.change_id, []).append(commit)
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

    def abandon_changes(self, revsets: Sequence[str]) -> None:
        """Abandon changes; jj rebases descendants and drops pointing bookmarks."""

        ordered_revsets = tuple(revsets)
        if not ordered_revsets:
            return
        self._run_jj(("abandon", *ordered_revsets), manage_working_copy=True)

    def _query_commits(self, revset: str, *, limit: int | None = None) -> list[LocalCommit]:
        lines = self._query_template_lines(revset, _COMMIT_TEMPLATE, limit=limit)
        return [_parse_commit_line(line) for line in lines]

    def _query_commits_with_membership(
        self,
        revset: str,
        *,
        membership_revsets: Sequence[str],
    ) -> list[tuple[LocalCommit, tuple[bool, ...]]]:
        """Query commits plus one containment flag per membership revset."""

        lines = self._query_template_lines(revset, _membership_scan_template(membership_revsets))
        return [_parse_commit_with_flags_line(line, len(membership_revsets)) for line in lines]

    def _project(self, commit: LocalCommit) -> LocalCommit | None:
        published = self._published_pr_snapshots.get(commit.change_id)
        if commit.commit_id == published:
            return None
        if published is not None:
            return commit.model_copy(update={"divergent": False})
        return commit

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
        """Resolve the exact Git object store used by this jj repo."""

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

    def _clear_pr_branch_temp_ref(self) -> None:
        """Remove the fixed transient jj bookmark and backing Git import ref."""

        try:
            if self._local_bookmark_targets(_PR_BRANCH_TEMP_BOOKMARK):
                self._run_jj(("bookmark", "forget", _PR_BRANCH_TEMP_BOOKMARK))
                self._run_jj(("git", "export"))
        finally:
            raw_target = self.pr_branch_temp_ref_target()
            try:
                if raw_target is not None:
                    self._run_git(("update-ref", "-d", _PR_BRANCH_TEMP_REF, raw_target))
            finally:
                if self.pr_branch_temp_ref_target() is not None:
                    raise JjCommandError(
                        t"Could not remove temporary Git ref {ui.code(_PR_BRANCH_TEMP_REF)}."
                    )

        if self._local_bookmark_targets(_PR_BRANCH_TEMP_BOOKMARK):
            raise JjCommandError(
                t"Could not forget temporary bookmark {ui.bookmark(_PR_BRANCH_TEMP_BOOKMARK)}."
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


def _is_missing_commit_error(message: str) -> bool:
    # Match jj's own diagnostic vocabulary at the subprocess boundary.
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
    if _is_missing_commit_error(raw_message):
        return CliError(t"Revset {ui.revset(revset)} did not resolve to a visible commit.")

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


def _parse_commit_line(line: str) -> LocalCommit:
    return _parse_json_line(line, command="jj log", model=LocalCommit)


def _parse_commit_with_flags_line(
    line: str,
    flag_count: int,
) -> tuple[LocalCommit, tuple[bool, ...]]:
    scan = _parse_json_line(line, command="jj log", model=_CommitScan)
    if len(scan.membership) != flag_count:
        raise JjCommandError(
            t"{ui.cmd('jj log')} output has {len(scan.membership)} membership flags; "
            t"expected {flag_count}."
        )
    return scan.commit, scan.membership


def _membership_scan_template(membership_revsets: Sequence[str]) -> str:
    flags = r' ++ "," ++ '.join(
        f"json(self.contained_in({json.dumps(revset)}))" for revset in membership_revsets
    )
    return dedent(
        rf"""
        "{{\"commit\":{{" ++ {_CHANGE_JSON_FIELDS} ++
        "}},\"membership\":[" ++ {flags} ++ "]}}\n"
        """
    ).strip()


def _short_change_id_render_template() -> str:
    shortest = "change_id.shortest(8)"
    return dedent(
        rf"""
        json(change_id) ++ "\t" ++
        {shortest}.prefix() ++
        {shortest}.rest() ++ "\n"
        """
    ).strip()


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
