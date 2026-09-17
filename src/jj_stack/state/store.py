"""Persistence helpers for jj-stack tracking data."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

import jj_stack.ui as ui
from jj_stack.errors import TrackingStateError
from jj_stack.identifiers import ChangeId
from jj_stack.models.tracking import (
    PRIdentity,
    SubmittedBaseline,
    TrackedPR,
    TrackingState,
)
from jj_stack.pr_branch_namespace import pr_branch_matches_change
from jj_stack.state.migrations import migrate_tracking_state

STATE_DIRNAME = "jj-stack"
STATE_FILENAME = "state.json"


class TrackingStore:
    """Load and atomically write pull request tracking state."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_repo(cls, repo_root: Path) -> TrackingStore:
        """Build a jj-stack data store for the supplied repo root."""

        return cls(resolve_state_path(repo_root))

    def is_in_use(self) -> bool:
        """Return whether a valid tracking file exists without creating one."""

        try:
            self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise TrackingStateError(
                f"Could not inspect jj-stack data path {self.path}: {error}"
            ) from error
        self.load()
        return True

    def relink_pr(
        self,
        change_id: ChangeId,
        *,
        identity: PRIdentity,
        baseline: SubmittedBaseline,
    ) -> TrackingState:
        """Atomically replace one complete pull request record."""

        return self.relink_prs(
            replacements={change_id: TrackedPR(pr_identity=identity, submitted_baseline=baseline)}
        )

    def relink_prs(
        self,
        *,
        replacements: Mapping[ChangeId, TrackedPR],
    ) -> TrackingState:
        """Atomically replace complete pull request records."""

        for change_id, tracked in replacements.items():
            _require_identity_matches_change(tracked.pr_identity, change_id)
        return self._persist(TrackingState(prs={**self.load().prs, **replacements}))

    def remove_pr(self, change_id: ChangeId) -> None:
        """Atomically remove one complete pull request record."""

        state = self.load()
        prs = dict(state.prs)
        del prs[change_id]
        self._persist(TrackingState(prs=prs))

    def load(self) -> TrackingState:
        """Load and validate the complete tracking file."""

        if not self.path.exists():
            return TrackingState()
        if not self.path.is_file():
            raise self._invalid_state_error(f"jj-stack data path is not a file: {self.path}")
        try:
            rendered = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise self._invalid_state_error(
                f"Could not read jj-stack data file {self.path}: {error}"
            ) from error
        try:
            raw = json.loads(rendered)
        except json.JSONDecodeError as error:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self.path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self.path}: top level must be an object"
            )
        try:
            raw = migrate_tracking_state(raw)
            state = TrackingState.model_validate(raw)
            for change_id, tracked in state.prs.items():
                _require_identity_matches_change(tracked.pr_identity, change_id)
        except (ValidationError, ValueError) as error:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self.path}: {error}"
            ) from error
        return state

    def _persist(self, state: TrackingState) -> TrackingState:
        rendered = state.model_dump_json(exclude_none=True, indent=2) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=self.path.name + ".",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(rendered)
                Path(tmp_name).replace(self.path)
            except OSError:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as error:
            raise TrackingStateError(
                f"Could not write jj-stack data file {self.path}: {error}"
            ) from error
        return state

    def _invalid_state_error(self, message: str) -> TrackingStateError:
        backup_path = self.path.with_name(f"{self.path.name}.bak")
        move_command = f"mv -i {shlex.quote(str(self.path))} {shlex.quote(str(backup_path))}"
        return TrackingStateError(
            message,
            hint=(
                t"Move the file aside with {ui.cmd(move_command)}, then relink pull requests "
                t"with {ui.cmd('jj-stack checkout --pull-request <pr>')} or "
                t"{ui.cmd('jj-stack relink <pr> <change-id>')}."
            ),
        )


def _require_identity_matches_change(identity: PRIdentity, change_id: ChangeId) -> None:
    if not pr_branch_matches_change(identity.head_ref, change_id):
        raise ValueError(f"PR branch {identity.head_ref!r} does not match change {change_id!r}.")


def resolve_state_path(repo_root: Path) -> Path:
    """Return the machine-written jj-stack data path for the repo."""

    repo_storage_root = _resolve_repo_storage_root(repo_root)
    repo_id = hashlib.sha256(str(repo_storage_root).encode("utf-8")).hexdigest()
    return default_state_root() / STATE_DIRNAME / "repos" / repo_id / STATE_FILENAME


def _resolve_repo_storage_root(repo_root: Path) -> Path:
    """Resolve the storage directory shared by every workspace for a jj repo."""

    repo_path = repo_root / ".jj" / "repo"
    if repo_path.is_file():
        try:
            target = repo_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise TrackingStateError(
                f"Could not read jj repo path file {repo_path}: {error}"
            ) from error
        if not target:
            raise TrackingStateError(f"jj repo path file is empty: {repo_path}")
        repo_path = repo_path.parent / target
    return repo_path.resolve()


def default_state_root() -> Path:
    """Return the base directory used for machine-written jj-stack data."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~", ".local", "state").expanduser().resolve()
