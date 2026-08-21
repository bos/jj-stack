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

from jj_stack.errors import TrackingStateError
from jj_stack.models.tracking import (
    PRIdentity,
    SubmittedBaseline,
    TrackingState,
)
from jj_stack.pr_branch_namespace import pr_branch_matches_change
from jj_stack.state.migrations import migrate_tracking_state

STATE_DIRNAME = "jj-stack"
STATE_FILENAME = "state.json"


class TrackingStore:
    """Load and atomically write pull request tracking state."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_repo(cls, repo_root: Path) -> TrackingStore:
        """Build a jj-stack data store for the supplied repo root."""

        return cls(resolve_state_path(repo_root))

    def require_writable(self) -> Path:
        """Ensure the data directory can be created and written, then return it."""

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise TrackingStateError(
                f"Could not create jj-stack data directory {self._path.parent}: {error}"
            ) from error
        return self._path.parent

    def load(self) -> TrackingState:
        """Load and validate the complete tracking file."""

        return self._load_state()

    def is_in_use(self) -> bool:
        """Return whether a valid tracking file exists without creating one."""

        try:
            self._path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise TrackingStateError(
                f"Could not inspect jj-stack data path {self._path}: {error}"
            ) from error
        self._load_state()
        return True

    def create_pr(
        self,
        change_id: str,
        *,
        identity: PRIdentity,
        baseline: SubmittedBaseline,
    ) -> TrackingState:
        """Atomically create an identity and baseline when both records are absent."""

        _require_identity_matches_change(identity, change_id)
        state = self._load_state()
        if change_id in state.pr_identities:
            raise TrackingStateError(f"Tracking data already exists for {change_id}.")
        return self._persist(_replace_prs(state, {change_id: (identity, baseline)}))

    def relink_pr(
        self,
        change_id: str,
        *,
        identity: PRIdentity,
        baseline: SubmittedBaseline,
    ) -> TrackingState:
        """Atomically replace one complete pull request pair."""

        return self.relink_prs(replacements={change_id: (identity, baseline)})

    def relink_prs(
        self,
        *,
        replacements: Mapping[str, tuple[PRIdentity, SubmittedBaseline]],
    ) -> TrackingState:
        """Atomically replace complete pull request pairs."""

        for change_id, (identity, _baseline) in replacements.items():
            _require_identity_matches_change(identity, change_id)
        return self._persist(_replace_prs(self._load_state(), replacements))

    def retire_pr(self, change_id: str) -> TrackingState:
        """Atomically remove one complete pull request pair."""

        state = self._load_state()
        identities = dict(state.pr_identities)
        baselines = dict(state.submitted_baselines)
        del identities[change_id]
        del baselines[change_id]
        return self._persist(
            TrackingState(pr_identities=identities, submitted_baselines=baselines)
        )

    def _load_state(self) -> TrackingState:
        if not self._path.exists():
            return TrackingState()
        if not self._path.is_file():
            raise self._invalid_state_error(f"jj-stack data path is not a file: {self._path}")
        try:
            rendered = self._path.read_text(encoding="utf-8")
        except OSError as error:
            raise self._invalid_state_error(
                f"Could not read jj-stack data file {self._path}: {error}"
            ) from error
        try:
            raw = json.loads(rendered)
        except json.JSONDecodeError as error:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: top level must be an object"
            )
        try:
            raw = migrate_tracking_state(raw)
            state = TrackingState.model_validate(raw)
            for change_id, identity in state.pr_identities.items():
                _require_identity_matches_change(identity, change_id)
        except (ValidationError, ValueError) as error:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: {error}"
            ) from error
        return state

    def _persist(self, state: TrackingState) -> TrackingState:
        rendered = state.model_dump_json(exclude_none=True, indent=2) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=self._path.name + ".",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(rendered)
                Path(tmp_name).replace(self._path)
            except OSError:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as error:
            raise TrackingStateError(
                f"Could not write jj-stack data file {self._path}: {error}"
            ) from error
        return state

    def _invalid_state_error(self, message: str) -> TrackingStateError:
        backup_path = self._path.with_name(f"{self._path.name}.bak")
        move_command = f"mv -i {shlex.quote(str(self._path))} {shlex.quote(str(backup_path))}"
        return TrackingStateError(
            message,
            hint=(
                f"Move the file aside with `{move_command}`, then explicitly re-adopt pull "
                "requests with `jj-stack checkout --pull-request PR` or "
                "`jj-stack relink PR CHANGE`."
            ),
        )


def _replace_prs(
    state: TrackingState,
    replacements: Mapping[str, tuple[PRIdentity, SubmittedBaseline]],
) -> TrackingState:
    identities = dict(state.pr_identities)
    baselines = dict(state.submitted_baselines)
    for change_id, (identity, baseline) in replacements.items():
        identities[change_id] = identity
        baselines[change_id] = baseline
    return TrackingState(pr_identities=identities, submitted_baselines=baselines)


def _require_identity_matches_change(identity: PRIdentity, change_id: str) -> None:
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
