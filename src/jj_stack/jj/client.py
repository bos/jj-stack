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
from itertools import batched
from pathlib import Path
from textwrap import dedent
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

import jj_stack.ui as ui
from jj_stack.errors import (
    AmbiguousSelectionError,
    CliError,
    DriftError,
    ErrorHint,
    ErrorMessage,
    UsageError,
)
from jj_stack.identifiers import ChangeId, CommitId, short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.colors import JjColorWhen
from jj_stack.jj.settings import JjSettings
from jj_stack.models.git import GitRemote
from jj_stack.models.stack import LocalCommit
from jj_stack.pr_branch_namespace import current_pr_branch_namespace
from jj_stack.timing import timed

QUERY_BATCH_SIZE = 200

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
    ",\"signed\":" ++ json(if(self.signature(), true, false)) ++
    ",\"conflict\":" ++ json(self.conflict())
    """
).strip()
_COMMIT_TEMPLATE = rf'"{{" ++ {_CHANGE_JSON_FIELDS} ++ "}}\n"'
_CHANGE_VERSION_TEMPLATE = dedent(
    r"""
    "{\"change_id\":" ++ json(commit.change_id()) ++
    ",\"commit_id\":" ++ json(commit.commit_id()) ++
    ",\"description\":" ++ json(commit.description()) ++ "}\n"
    """
).strip()
_BOOKMARK_TEMPLATE = dedent(
    r"""
    "{\"name\":" ++ json(self.name()) ++
    ",\"target\":" ++ json(self.added_targets().map(|commit| commit.commit_id())) ++
    ",\"remote\":" ++ json(self.remote()) ++
    ",\"tracked\":" ++ json(self.tracked()) ++ "}\n"
    """
).strip()
_PR_BRANCH_TEMP_BOOKMARK = "jj-stack-tmp/checkout"
_PR_BRANCH_TEMP_REF = f"refs/heads/{_PR_BRANCH_TEMP_BOOKMARK}"
_CONFIG_ORIGIN_TEMPLATE = r'json(self) ++ "\n"'
_SHORT_CHANGE_ID_TEMPLATE = dedent(
    r"""
    json(change_id) ++ "\t" ++
    change_id.shortest(8).prefix() ++
    change_id.shortest(8).rest() ++ "\n"
    """
).strip()
_WORKSPACE_TEMPLATE = dedent(
    r"""
    "{\"name\":" ++ json(name) ++
    ",\"root\":" ++ if(root, json(root.absolute()), "null") ++ "}\n"
    """
).strip()


class JjCommandError(CliError):
    """Raised when a `jj` invocation fails; `stderr` holds jj's own diagnostics."""

    def __init__(
        self, message: ErrorMessage, *, hint: ErrorHint | None = None, stderr: str = ""
    ) -> None:
        super().__init__(message, hint=hint)
        self.stderr = stderr


PRBranchFetchIsolationStatus = Literal["ready", "applied", "required"]
PRBranchFetchIsolationProblem = Literal["missing", "duplicate"]


@dataclass(frozen=True, slots=True)
class PRBranchFetchIsolation:
    """Result of checking the ordinary-fetch exclusion for PR branches."""

    status: PRBranchFetchIsolationStatus
    problem: PRBranchFetchIsolationProblem | None


@dataclass(frozen=True, slots=True)
class PRRefUpdate:
    """A PR branch update with the expected old commit and desired new commit."""

    branch: str
    expected_target: CommitId | None
    desired_target: CommitId | None


@dataclass(frozen=True, slots=True)
class GitCommitMetadata:
    """Raw headers and subject of one backing-Git commit."""

    change_id: ChangeId | None
    parents: tuple[CommitId, ...]
    author: str
    subject: str


@dataclass(frozen=True, slots=True)
class PRTempArtifacts:
    """Targets of the temporary ref and bookmark used to import PR branches."""

    bookmark_targets: tuple[CommitId, ...]
    ref_target: CommitId | None


class JjWorkspace(BaseModel):
    """A named jj workspace and its recorded working-copy location."""

    model_config = ConfigDict(frozen=True, strict=True)

    name: str
    root: Path | None


class _ConfigOrigin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    source: str
    path: str


class Bookmark(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    name: str
    target: tuple[CommitId, ...]
    remote: str | None = None
    tracked: bool


class _CommitScan(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    commit: LocalCommit
    membership: tuple[bool, ...]


class ChangeVersion(BaseModel):
    """One commit from a change's evolution log."""

    model_config = ConfigDict(frozen=True, strict=True)

    change_id: ChangeId
    commit_id: CommitId
    description: str


class _CommitDiffStat(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    commit_id: CommitId
    diffstat: str


class RenderableCommit(Protocol):
    """Change-like value that can be rendered by commit ID."""

    @property
    def commit_id(self) -> CommitId: ...


_NO_CLI_ARGS = JjCliArgs()
# User settings that would otherwise change the text jj-stack parses.
_PINNED_OUTPUT_CONFIG = ("--config", "ui.quiet=false", "--config", "ui.log-word-wrap=false")


class JjClient:
    """Thin wrapper around `jj` commands used by jj-stack."""

    def __init__(
        self,
        repo_root: Path,
        *,
        cli_args: JjCliArgs = _NO_CLI_ARGS,
        settings: JjSettings | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._cli_args = cli_args
        self._settings = settings
        self._config_strings: dict[str, str | None] = {}
        self._git_remotes: tuple[GitRemote, ...] | None = None
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
            if _is_missing_commit_error(error.stderr):
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
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> tuple[tuple[LocalCommit, tuple[bool, ...]], ...]:
        """Return commits with one containment flag per supplied revset."""

        try:
            return self._query_commits_with_membership(
                revset,
                membership_revsets=membership_revsets,
                cli_args=cli_args,
            )
        except JjCommandError as error:
            friendly_error = _revset_resolution_error(selected_revset or revset, error)
            if friendly_error is not None:
                raise friendly_error from error
            raise

    def query_commits_by_change_ids(
        self,
        change_ids: Sequence[ChangeId],
        *,
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> dict[ChangeId, tuple[LocalCommit, ...]]:
        """Return visible commits grouped by logical change ID."""

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        grouped: dict[ChangeId, list[LocalCommit]] = {
            change_id: [] for change_id in ordered_change_ids
        }
        for chunk in batched(ordered_change_ids, QUERY_BATCH_SIZE, strict=False):
            for commit in self._query_commits(change_ids_revset(chunk), cli_args=cli_args):
                grouped[commit.change_id].append(commit)
        return {change_id: tuple(commits) for change_id, commits in grouped.items()}

    def query_commits_by_ids(
        self,
        commit_ids: Sequence[CommitId],
    ) -> tuple[LocalCommit, ...]:
        """Return locally available commits for the supplied commit IDs in evaluation order."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return ()

        commits_by_id: dict[CommitId, LocalCommit] = {}
        for chunk in batched(ordered_commit_ids, QUERY_BATCH_SIZE, strict=False):
            commits = self._query_commits(_present_symbols_revset(chunk))
            for commit in commits:
                commits_by_id.setdefault(commit.commit_id, commit)
        return tuple(commits_by_id.values())

    def change_versions(self, commit_ids: Sequence[CommitId]) -> tuple[ChangeVersion, ...]:
        """Return the commits in these commits' evolution logs, skipping unavailable IDs.

        jj also lists the commits of other changes that were squashed into one of them.
        """

        versions: dict[CommitId, ChangeVersion] = {}
        for chunk in batched(tuple(dict.fromkeys(commit_ids)), QUERY_BATCH_SIZE, strict=False):
            stdout = self._run_jj(
                (
                    "evolog",
                    "--no-graph",
                    "-r",
                    _present_symbols_revset(chunk),
                    "-T",
                    _CHANGE_VERSION_TEMPLATE,
                )
            )
            for version in _parse_json_lines(stdout, command="jj evolog", model=ChangeVersion):
                versions.setdefault(version.commit_id, version)
        return tuple(versions.values())

    def query_present_commit_ancestor_membership(
        self,
        commit_ids: Sequence[CommitId],
        *,
        descendant_commit_id: CommitId,
    ) -> dict[CommitId, bool]:
        """Return presence and ancestry together, omitting unavailable commit IDs."""

        memberships: dict[CommitId, bool] = {}
        for chunk in batched(tuple(dict.fromkeys(commit_ids)), QUERY_BATCH_SIZE, strict=False):
            commits = self._query_commits_with_membership(
                _present_symbols_revset(chunk),
                membership_revsets=(f"::{quote_revset_symbol(descendant_commit_id)}",),
            )
            for commit, (is_ancestor,) in commits:
                memberships[commit.commit_id] = is_ancestor
        return memberships

    def query_paired_ancestor_membership(
        self,
        pairs: Sequence[tuple[CommitId, CommitId]],
    ) -> set[CommitId]:
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
            f"({quote_revset_symbol(subject)} & ::present({quote_revset_symbol(target)}))"
            for subject, target in deduped_pairs
        )
        commits = self._query_commits(terms)
        return {commit.commit_id for commit in commits}

    def get_config_string(self, key: str) -> str | None:
        """Return the string value of a jj config key, or None if unset.

        The config listing read at startup answers when it has the key; otherwise one
        `jj config get` runs and is cached, since nothing rewrites jj config during a run.
        """

        if key in self._config_strings:
            return self._config_strings[key]
        if self._settings is not None:
            listed = self._settings.string(*key.split("."))
            if listed is not None:
                self._config_strings[key] = listed
                return listed
        try:
            value = self._run_jj(("config", "get", key))
        except JjCommandError:
            value = ""
        stripped = value.strip()
        result = stripped if stripped else None
        self._config_strings[key] = result
        return result

    def enable_initial_working_copy_snapshot(self) -> None:
        """Let the first post-bootstrap jj command use jj's normal working-copy lifecycle."""

        self._initial_working_copy_snapshot_pending = True

    def diffstats(self, commit_ids: Sequence[CommitId]) -> dict[CommitId, str]:
        """Return diffstats for the given commits without rendering their descriptions."""

        template = (
            r'"{\"commit_id\":" ++ json(commit_id) ++ '
            r'",\"diffstat\":" ++ json(stringify(self.diff().stat())) ++ "}\n"'
        )
        result: dict[CommitId, str] = {}
        for chunk in batched(commit_ids, QUERY_BATCH_SIZE, strict=False):
            revset = " | ".join(quote_revset_symbol(commit_id) for commit_id in chunk)
            for line in self._query_template_lines(revset, template):
                row = _parse_json_line(line, command="jj log", model=_CommitDiffStat)
                result[row.commit_id] = row.diffstat.rstrip()
        return result

    def render_commit_log_lines(
        self,
        change: RenderableCommit,
        *,
        color_when: JjColorWhen,
    ) -> tuple[str, ...]:
        """Render one change with the user's `jj log` formatting."""

        stdout = self._run_jj(
            ("--no-pager", "log", "-r", quote_revset_symbol(change.commit_id), "--limit", "1"),
            color=color_when,
        )
        return tuple(line for line in stdout.rstrip("\n").splitlines() if line.strip() != "~")

    def render_commit_log_blocks(
        self,
        changes: Sequence[RenderableCommit],
        *,
        color_when: JjColorWhen,
    ) -> dict[CommitId, tuple[str, ...]]:
        """Render several changes in parallel, keyed by commit_id.

        Each `jj log` invocation pays a substantial startup cost, so rendering
        a stack sequentially dominates the wall-clock time of commands like
        `status`. Fan the per-change calls out onto a thread pool so their
        subprocess spawns overlap.
        """

        if not changes:
            return {}
        with ThreadPoolExecutor(max_workers=min(len(changes), 10)) as pool:
            return dict(
                pool.map(
                    lambda change: (
                        change.commit_id,
                        self.render_commit_log_lines(change, color_when=color_when),
                    ),
                    changes,
                )
            )

    def render_short_change_ids(
        self,
        change_ids: Sequence[ChangeId],
        *,
        color_when: JjColorWhen,
    ) -> dict[ChangeId, str]:
        """Render the shortest unique prefix of each change ID, in jj's own colors.

        A change with no visible commit renders as its ordinary short prefix.
        """

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return {}

        rendered: dict[ChangeId, str] = {}
        for chunk in batched(ordered_change_ids, QUERY_BATCH_SIZE, strict=False):
            revset = change_ids_revset(chunk)
            stdout = self._run_jj(
                (
                    "--no-pager",
                    "log",
                    "--no-graph",
                    "-r",
                    revset,
                    "-T",
                    _SHORT_CHANGE_ID_TEMPLATE,
                ),
                color=color_when,
            )
            for line in stdout.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                raw_change_id, rendered_change_id = stripped.split("\t", maxsplit=1)
                change_id = json.loads(raw_change_id)
                rendered.setdefault(change_id, rendered_change_id)
        return {
            change_id: rendered.get(change_id, short_change_id(change_id))
            for change_id in ordered_change_ids
        }

    def find_private_commits(
        self,
        changes: tuple[LocalCommit, ...],
    ) -> tuple[LocalCommit, ...]:
        """Return changes blocked by the repo's git.private-commits policy."""

        private_commits_revset = self.get_config_string("git.private-commits")
        if private_commits_revset in (None, "", "none()") or not changes:
            return ()
        commit_ids_revset = " | ".join(quote_revset_symbol(r.commit_id) for r in changes)
        combined_revset = f"({private_commits_revset}) & ({commit_ids_revset})"
        return tuple(self.query_commits(combined_revset))

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        """List configured Git remotes for the repo, cached for the client's lifetime."""

        if self._git_remotes is None:
            self._git_remotes = self._read_git_remotes()
        return self._git_remotes

    def _read_git_remotes(self) -> tuple[GitRemote, ...]:
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
        commit_id: CommitId,
    ) -> tuple[str, ...]:
        """Return locally observed remote bookmarks pointing at one commit.

        `jj bookmark list --revision` filters on local targets, which hides untracked remote
        bookmarks, so the remote rows are filtered by target here instead.
        """

        stdout = self._run_jj(("bookmark", "list", "--remote", remote, "-T", _BOOKMARK_TEMPLATE))
        return tuple(
            row.name
            for row in _parse_bookmark_rows(stdout)
            if row.remote == remote and commit_id in row.target
        )

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
                hint = (
                    t"Remove the override with {unset}, then run "
                    t"{ui.cmd('jj-stack doctor --fix')}."
                )
            else:
                hint = (
                    t"Remove that {override_origin.source} override from the jj invocation "
                    t"or environment, then run {ui.cmd('jj-stack doctor --fix')}."
                )
            raise CliError(
                t"The jj setting {ui.code(override_key)} from {origin} overrides the fetch "
                t"rule that excludes PR branches, so {ui.cmd('jj git fetch')} may import "
                t"{ui.bookmark(namespace.branch_glob)} bookmarks.",
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
        return result

    def visible_pr_bookmark_targets(
        self,
    ) -> dict[str, frozenset[CommitId]]:
        """Return visible reserved-namespace bookmark targets grouped by name."""

        namespace = current_pr_branch_namespace()
        targets_by_name: dict[str, set[CommitId]] = {}
        for row in self.query_bookmarks(namespace.branch_glob):
            targets_by_name.setdefault(row.name, set()).update(row.target)
        return {name: frozenset(targets) for name, targets in sorted(targets_by_name.items())}

    def untracked_pr_bookmarks(self) -> tuple[str, ...]:
        """Return untracked remote bookmark names in the reserved namespace."""

        rows = self.query_bookmarks(current_pr_branch_namespace().branch_glob)
        return tuple(
            sorted({row.name for row in rows if row.remote is not None and not row.tracked})
        )

    def forget_bookmarks(self, names: Sequence[str]) -> None:
        """Forget bookmarks and their remote counterparts without touching the remote."""

        self._run_jj(("bookmark", "forget", "--include-remotes", *names))

    def query_bookmarks(self, *patterns: str) -> tuple[Bookmark, ...]:
        stdout = self._run_jj(
            ("bookmark", "list", "--all-remotes", "-T", _BOOKMARK_TEMPLATE, *patterns)
        )
        return _parse_bookmark_rows(stdout)

    def pr_branch_temp_ref_target(self) -> CommitId | None:
        """Return the temporary PR branch import ref target, if it exists."""

        target = self._run_git(
            ("rev-parse", "--verify", "--quiet", _PR_BRANCH_TEMP_REF),
            allowed_returncodes=frozenset({0, 1}),
        ).strip()
        return CommitId(target) if target else None

    def pr_branch_temp_artifacts(self) -> PRTempArtifacts:
        """Observe the fixed temporary import ref and its transient jj bookmark."""

        return PRTempArtifacts(
            bookmark_targets=self._local_bookmark_targets(),
            ref_target=self.pr_branch_temp_ref_target(),
        )

    @contextmanager
    def import_remote_pr_branch_ref(
        self,
        *,
        remote: str,
        branch: str,
        expected_target: CommitId,
    ) -> Iterator[LocalCommit]:
        """Import a PR branch at its expected commit, then remove the temporary ref and bookmark.

        Checkout uses this to make an existing PR head a local commit.
        """

        ref = f"refs/heads/{branch}"
        self.clear_pr_branch_temp_artifacts()
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
            self._run_jj(("git", "import"))
            change = self.resolve_commit(quote_revset_symbol(_PR_BRANCH_TEMP_BOOKMARK))
            if change.commit_id != expected_target:
                raise JjCommandError(
                    t"{ui.cmd('jj git import')} did not import the expected PR branch commit."
                )
            yield change
        finally:
            self.clear_pr_branch_temp_artifacts()

    def fetch_commits(self, *, remote: str, commit_ids: Sequence[CommitId]) -> None:
        """Fetch commits by ID into the backing Git store without creating any ref."""

        url = self._git_remote(remote).fetch_url
        self._run_git(("fetch", "--no-tags", "--no-write-fetch-head", url, *commit_ids))

    def rebased_tree(
        self, commit_id: CommitId, *, parent: CommitId, onto: CommitId
    ) -> str | None:
        """Return the tree of the commit's changes from `parent` applied to `onto`.

        None means the changes conflict there. Renames are not detected, as in a jj rebase.
        """

        output = self._run_git(
            (
                "merge-tree",
                "--write-tree",
                "-X",
                "no-renames",
                f"--merge-base={parent}",
                onto,
                commit_id,
            ),
            allowed_returncodes=frozenset({0, 1}),
        )
        tree, _, rest = output.partition("\n")
        return None if rest.strip() else tree

    def on_first_parent_chain(self, commit_id: CommitId, *, tip: CommitId) -> bool:
        """Whether the commit is the tip or reached from it through first parents only."""

        if commit_id == tip:
            return True
        # The walk stops at the first commit that the candidate already reaches.
        walked = self._run_git(("rev-list", "--first-parent", "--parents", tip, f"^{commit_id}"))
        lines = walked.splitlines()
        return bool(lines) and lines[-1].split()[1:2] == [commit_id]

    def read_remote_git_commit(
        self,
        *,
        remote: str,
        commit_id: CommitId,
    ) -> GitCommitMetadata:
        """Read a Git commit by ID, fetching it without a ref when it is absent."""

        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit_id) is None:
            raise ValueError("remote commit ID must be a full SHA-1 or SHA-256 object ID")
        try:
            return self._read_git_commit_metadata(commit_id)
        except JjCommandError:
            self._run_git(
                (
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    self._git_remote(remote).fetch_url,
                    commit_id,
                )
            )
            return self._read_git_commit_metadata(commit_id)

    def _read_git_commit_metadata(self, commit_id: CommitId) -> GitCommitMetadata:
        """Read one backing-Git commit's change ID, ordered parents, author name, and subject.

        A Git commit object is a byte string, so a legacy encoding in its author, committer,
        or message is legal; undecodable bytes are replaced instead of aborting the command
        that needed them.
        """

        raw_commit = self._run_git(("cat-file", "commit", commit_id), lossy_text=True)
        headers, _, message = raw_commit.partition("\n\n")
        values: dict[str, list[str]] = {}
        for line in headers.splitlines():
            key, separator, value = line.partition(" ")
            if separator and value:
                values.setdefault(key, []).append(value)
        change_ids = values.get("change-id", ())
        author = values.get("author", ("",))[0]
        return GitCommitMetadata(
            change_id=ChangeId(change_ids[0]) if len(change_ids) == 1 else None,
            parents=tuple(CommitId(parent) for parent in values.get("parent", ())),
            author=author.rsplit(" <", 1)[0],
            subject=message.partition("\n")[0],
        )

    def fetch_remote(self, *, remote: str) -> None:
        """Fetch ordinary repo state using its configured selection."""

        # Normal fetch also imports backing-Git ref changes in a colocated repo.
        self._run_jj(("git", "fetch", "--remote", remote), manage_working_copy=True)

    def mutate_remote_pr_branch_refs(
        self,
        *,
        remote: str,
        updates: Sequence[PRRefUpdate],
    ) -> None:
        """Update PR branches atomically, checking each branch against its expected commit."""

        ordered_updates = tuple(updates)
        if not ordered_updates:
            return
        refs = tuple(f"refs/heads/{update.branch}" for update in ordered_updates)

        if all(update.desired_target == update.expected_target for update in ordered_updates):
            return

        configured_remote = self._git_remote(remote)
        # Git 2.56 updates a matching remote's tracking refs after a URL push, so push through a
        # remote defined only for this command, with no fetch mapping. The name is random so a
        # user's remote of the same name cannot add its URLs or mappings.
        push_remote = f"jj-stack-push-{uuid4().hex}"
        # Carry only the leased PR branch refs: tag auto-follow would publish unrelated local
        # tags, and a pre-push hook was never invoked when this went through `jj git push`.
        command = [
            "-c",
            f"remote.{push_remote}.url={configured_remote.push_url}",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--no-verify",
        ]
        for ref, update in zip(refs, ordered_updates, strict=True):
            expected = update.expected_target or ""
            command.append(f"--force-with-lease={ref}:{expected}")
        command.append(push_remote)
        for ref, update in zip(refs, ordered_updates, strict=True):
            desired = update.desired_target or ""
            command.append(f"{desired}:{ref}")
        try:
            self._run_git(command)
        except JjCommandError as error:
            if not error.stderr:
                raise
            # Git's own report names every rejected ref and carries GitHub's reason; the
            # command line, with one lease per branch, only buries it.
            push_url = _redact_http_url_userinfo(configured_remote.push_url)
            raise JjCommandError(
                t"Pushing PR branches to {ui.code(push_url)} failed:\n"
                t"{_git_push_diagnostics(error.stderr)}",
                hint="The push is all-or-nothing, so no PR branch changed. Fix the cause above "
                "and rerun the same jj-stack command.",
                stderr=error.stderr,
            ) from error

    def edit_commit(self, commit_id: CommitId, *, cli_args: JjCliArgs) -> None:
        """Edit the given commit in the current workspace.

        The caller's immutability revset stays in force: an edit rewrites nothing, so jj's check
        is the safety boundary for a PR commit that another bookmark also protects.
        """

        self._run_jj(("edit", commit_id), manage_working_copy=True, cli_args=cli_args)

    def new_empty_change(self, parent: CommitId) -> None:
        """Check out a new empty change on the given commit in the current workspace."""

        self._run_jj(("new", parent), manage_working_copy=True)

    def rebase_changes(
        self,
        *,
        change_ids: Sequence[ChangeId],
        destination: CommitId,
    ) -> None:
        """Rebase the current visible commits of the named changes onto one destination.

        jj rebases a hidden commit ID as readily as a visible one, resurrecting it beside any
        later rewrite, so the changes are selected by ID after jj's own working-copy snapshot.
        """

        ordered_change_ids = tuple(dict.fromkeys(change_ids))
        if not ordered_change_ids:
            return
        self._run_jj(
            ("rebase", "-r", change_ids_revset(ordered_change_ids), "-d", destination),
            manage_working_copy=True,
            ignore_immutable=True,
        )

    def git_tree_ids(self, commit_ids: Sequence[CommitId]) -> dict[CommitId, str]:
        """Return Git tree IDs for the given commits."""

        ordered_commit_ids = tuple(dict.fromkeys(commit_ids))
        if not ordered_commit_ids:
            return {}
        stdout = self._run_git(
            ("rev-parse", *(f"{commit_id}^{{tree}}" for commit_id in ordered_commit_ids))
        )
        return dict(zip(ordered_commit_ids, stdout.split(), strict=True))

    def abandon_commits(self, commit_ids: Sequence[CommitId]) -> None:
        """Abandon commits, rebasing descendants and removing bookmarks that point to them."""

        ordered_commit_ids = tuple(commit_ids)
        if not ordered_commit_ids:
            return
        self._run_jj(
            ("abandon", *ordered_commit_ids), manage_working_copy=True, ignore_immutable=True
        )

    def _query_commits(
        self, revset: str, *, limit: int | None = None, cli_args: JjCliArgs = _NO_CLI_ARGS
    ) -> list[LocalCommit]:
        lines = self._query_template_lines(
            revset, _COMMIT_TEMPLATE, limit=limit, cli_args=cli_args
        )
        return [_parse_commit_line(line) for line in lines]

    def _query_commits_with_membership(
        self,
        revset: str,
        *,
        membership_revsets: Sequence[str],
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> tuple[tuple[LocalCommit, tuple[bool, ...]], ...]:
        """Query commits plus one containment flag per membership revset."""

        lines = self._query_template_lines(
            revset, _membership_scan_template(membership_revsets), cli_args=cli_args
        )
        scans = (_parse_json_line(line, command="jj log", model=_CommitScan) for line in lines)
        return tuple((scan.commit, scan.membership) for scan in scans)

    def _query_template_lines(
        self,
        revset: str,
        template: str,
        *,
        limit: int | None = None,
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> list[str]:
        command = ["log", "--no-graph", "-r", revset, "-T", template]
        if limit is not None:
            command.extend(["--limit", str(limit)])
        stdout = self._run_jj(command, cli_args=cli_args)
        return [stripped for line in stdout.splitlines() if (stripped := line.strip())]

    def _run_jj(
        self,
        args: Sequence[str],
        *,
        manage_working_copy: bool = False,
        ignore_immutable: bool = False,
        color: JjColorWhen = "never",
        cli_args: JjCliArgs = _NO_CLI_ARGS,
    ) -> str:
        """Run jj without touching the working copy unless the caller explicitly requires it.

        Output settings are pinned after the user's own overrides; only output shown to the
        user asks for color.

        Planned rewrites pass ignore_immutable instead of an immutability revset: the caller has
        already checked that every commit it rewrites is mutable, and a revset that names a PR
        bookmark makes jj rebase the hidden commit it points at back into view.
        """

        use_working_copy = manage_working_copy or self._initial_working_copy_snapshot_pending
        self._initial_working_copy_snapshot_pending = False
        extra_args = ["--color", color, *_PINNED_OUTPUT_CONFIG]
        if not use_working_copy:
            extra_args.append("--ignore-working-copy")
        if ignore_immutable:
            extra_args.append("--ignore-immutable")
        return self._run_command(
            ["jj", *self._cli_args.argv, *cli_args.argv, *extra_args, *args],
            missing_tool_message=t"{ui.cmd('jj')} is not installed or is not on PATH.",
            detect_stale_workspace=True,
        )

    def _run_git(
        self,
        args: Sequence[str],
        *,
        allowed_returncodes: frozenset[int] = frozenset({0}),
        lossy_text: bool = False,
    ) -> str:
        return self._run_command(
            ["git", "--git-dir", str(self._backing_git_root()), *args],
            missing_tool_message=t"{ui.cmd('git')} is not installed or is not on PATH.",
            detect_stale_workspace=False,
            allowed_returncodes=allowed_returncodes,
            lossy_text=lossy_text,
        )

    def _backing_git_root(self) -> Path:
        """Resolve the Git object store used by this jj repo."""

        if self._git_root is None:
            self._git_root = Path(self._run_jj(("git", "root")).strip())
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
        if not stdout.strip():
            return None
        return _parse_json_line(stdout, command="jj config list", model=_ConfigOrigin)

    def _local_bookmark_targets(self) -> tuple[CommitId, ...]:
        """Return local targets of the transient checkout bookmark."""

        bookmark = _PR_BRANCH_TEMP_BOOKMARK
        stdout = self._run_jj(("bookmark", "list", "-T", _BOOKMARK_TEMPLATE, bookmark))
        targets: list[CommitId] = []
        for row in _parse_bookmark_rows(stdout):
            if row.name != bookmark or row.remote is not None:
                raise JjCommandError(
                    t"Unexpected {ui.cmd('jj bookmark list')} payload while checking "
                    t"{ui.bookmark(bookmark)}."
                )
            targets.extend(row.target)
        return tuple(dict.fromkeys(targets))

    def clear_pr_branch_temp_artifacts(self) -> None:
        """Remove the fixed transient jj bookmark and backing Git import ref."""

        try:
            if self._local_bookmark_targets():
                self._run_jj(("bookmark", "forget", _PR_BRANCH_TEMP_BOOKMARK))
                self._run_jj(("git", "export"))
        finally:
            raw_target = self.pr_branch_temp_ref_target()
            if raw_target is not None:
                self._run_git(("update-ref", "-d", _PR_BRANCH_TEMP_REF, raw_target))

    def _run_command(
        self,
        command: Sequence[str],
        *,
        missing_tool_message: ErrorMessage,
        detect_stale_workspace: bool,
        allowed_returncodes: frozenset[int] = frozenset({0}),
        lossy_text: bool = False,
    ) -> str:
        try:
            with timed(command[0], _redact_http_url_userinfo(shlex.join(command))):
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    cwd=self._repo_root,
                    encoding="utf-8",
                    errors="replace" if lossy_text else "strict",
                    text=True,
                )
        except FileNotFoundError as error:
            raise JjCommandError(missing_tool_message) from error

        if completed.returncode not in allowed_returncodes:
            message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            if detect_stale_workspace and "The working copy is stale" in message:
                raise CliError(
                    "The current workspace is stale.",
                    hint=t"Run {ui.cmd('jj workspace update-stale')} and retry.",
                )
            immutable_commit = _immutable_commit_id(message)
            if immutable_commit is not None:
                raise JjCommandError(
                    t"jj will not rewrite commit {ui.commit_id(immutable_commit)} because it "
                    t"is immutable here.",
                    hint=t"Run {ui.cmd('jj bookmark list --all-remotes')} to check for an "
                    t"untracked remote bookmark. If it is a branch you intend to edit, track "
                    t"it with {ui.cmd('jj bookmark track <bookmark>@<remote>')}. Otherwise, "
                    t"check your {ui.code('immutable_heads()')} configuration before retrying.",
                    stderr=message,
                )
            displayed_command = _redact_http_url_userinfo(shlex.join(command))
            displayed_message = _redact_http_url_userinfo(message)
            raise JjCommandError(
                t"{ui.cmd(displayed_command)} failed: {displayed_message}", stderr=message
            )
        return completed.stdout


def _git_push_diagnostics(stderr: str) -> str:
    """Return git's push report without trailing padding or empty `remote:` lines."""

    lines = (line.rstrip() for line in _redact_http_url_userinfo(stderr).splitlines())
    return "\n".join(line for line in lines if line != "remote:")


_HTTP_URL_AUTHORITY_PATTERN = re.compile(
    r"(?P<scheme>https?://)(?P<authority>[^/\s'\"<>]+)",
    re.IGNORECASE,
)


def _immutable_commit_id(message: str) -> str | None:
    """Return the commit jj named as immutable, matching jj's own diagnostic vocabulary."""

    match = re.search(r"^Error: Commit ([0-9a-f]+) is immutable$", message, re.MULTILINE)
    return match.group(1) if match is not None else None


def _is_missing_commit_error(message: str) -> bool:
    # Match jj's own diagnostic vocabulary at the subprocess boundary.
    return "Revision `" in message and "doesn't exist" in message


def _redact_http_url_userinfo(text: str) -> str:
    """Remove HTTP URL credentials from command and subprocess-error displays."""

    def redact(match: re.Match[str]) -> str:
        authority = match.group("authority")
        if "@" not in authority:
            return match.group(0)
        return f"{match.group('scheme')}{authority.rsplit('@', maxsplit=1)[1]}"

    return _HTTP_URL_AUTHORITY_PATTERN.sub(redact, text)


def _revset_resolution_error(revset: str, error: JjCommandError) -> CliError | None:
    if _is_missing_commit_error(error.stderr):
        return CliError(t"Revset {ui.revset(revset)} did not resolve to a visible commit.")

    first_line = error.stderr.partition("\n")[0].strip()
    if first_line.startswith("Error: Failed to parse revset:"):
        detail = first_line.removeprefix("Error: ").strip()
        return UsageError(t"Invalid revset {ui.revset(revset)}: {detail}.")

    return None


def divergent_change_id_from_error(error: JjCommandError) -> str | None:
    """Return the short change ID that made a bare revset symbol divergent."""

    first_line = error.stderr.partition("\n")[0].strip()
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


def _parse_bookmark_rows(stdout: str) -> tuple[Bookmark, ...]:
    return _parse_json_lines(
        stdout,
        command="jj bookmark list",
        model=Bookmark,
    )


def _parse_commit_line(line: str) -> LocalCommit:
    return _parse_json_line(line, command="jj log", model=LocalCommit)


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


def quote_revset_symbol(symbol: str) -> str:
    """Quote one symbol as a jj revset string literal, escaping when needed."""

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
        tuple(f"present({quote_revset_symbol(symbol)})" for symbol in symbols)
    )


def change_ids_revset(change_ids: Sequence[ChangeId]) -> str:
    """Union change IDs as `change_id(...)` terms.

    Every caller wants each change's visible copies, and a bare change-ID symbol fails outright
    once a change is divergent. Selecting through `change_id()` returns all of them, and like
    `present(...)` an unmatched change ID contributes nothing instead of failing the query.
    """

    return _union_revset_symbols(
        tuple(f"change_id({quote_revset_symbol(change_id)})" for change_id in change_ids)
    )


def _union_revset_symbols(symbols: Sequence[str]) -> str:
    if not symbols:
        raise ValueError("Expected at least one revset symbol.")
    if len(symbols) == 1:
        return symbols[0]
    return f"({' | '.join(symbols)})"
