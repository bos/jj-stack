"""Persistence helpers for jj-stack tracking data."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue, ValidationError

from jj_stack.errors import CliError
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    SubmittedBaseline,
)
from jj_stack.review.branches import review_branch_matches_change

STATE_DIRNAME = "jj-stack"
STATE_FILENAME = "state.json"


class ReviewStateError(CliError):
    """Raised when the tracking data is unreadable or invalid."""


class ReviewStateStore:
    """Load and atomically write review tracking state."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_repo(cls, repo_root: Path) -> ReviewStateStore:
        """Build a jj-stack data store for the supplied repository root."""

        return cls(resolve_state_path(repo_root))

    def require_writable(self) -> Path:
        """Ensure the data directory can be created and written, then return it."""

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ReviewStateError(
                f"Could not create jj-stack data directory {self._path.parent}: {error}"
            ) from error
        return self._path.parent

    def load(self) -> ReviewState:
        """Load and validate the complete tracking file."""

        return self._load_state()

    def is_in_use(self) -> bool:
        """Return whether a valid tracking file exists without creating one."""

        try:
            self._path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ReviewStateError(
                f"Could not inspect jj-stack data path {self._path}: {error}"
            ) from error
        self._load_state()
        return True

    def create_review(
        self,
        change_id: str,
        *,
        identity: ReviewIdentity,
        baseline: SubmittedBaseline,
    ) -> ReviewState:
        """Atomically create an identity and baseline when both records are absent."""

        _require_identity_matches_change(identity, change_id)
        state = self._load_state()
        if change_id in state.review_identities:
            raise ReviewStateError(f"Tracking data already exists for {change_id}.")
        return self._persist(_replace_reviews(state, {change_id: (identity, baseline)}))

    def relink_review(
        self,
        change_id: str,
        *,
        identity: ReviewIdentity,
        baseline: SubmittedBaseline,
    ) -> ReviewState:
        """Atomically replace one complete review pair."""

        return self.relink_reviews(
            replacements={change_id: (identity, baseline)},
        )

    def relink_reviews(
        self,
        *,
        replacements: Mapping[str, tuple[ReviewIdentity, SubmittedBaseline]],
    ) -> ReviewState:
        """Atomically replace complete review pairs."""

        for change_id, (identity, _baseline) in replacements.items():
            _require_identity_matches_change(identity, change_id)
        return self._persist(_replace_reviews(self._load_state(), replacements))

    def retire_review(
        self,
        change_id: str,
    ) -> ReviewState:
        """Atomically remove one complete review pair."""

        state = self._load_state()
        identities = dict(state.review_identities)
        baselines = dict(state.submitted_baselines)
        del identities[change_id]
        del baselines[change_id]
        return self._persist(
            ReviewState(review_identities=identities, submitted_baselines=baselines)
        )

    def _load_state(self) -> ReviewState:
        if not self._path.exists():
            return ReviewState()
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
        version = raw.get("version")
        if version != 4:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: unsupported version {version!r}"
            )
        try:
            _require_nested_versions(raw)
            state = ReviewState.model_validate(raw)
            for change_id, identity in state.review_identities.items():
                _require_identity_matches_change(identity, change_id)
        except (ValidationError, ValueError) as error:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: {error}"
            ) from error
        return state

    def _persist(self, state: ReviewState) -> ReviewState:
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
            raise ReviewStateError(
                f"Could not write jj-stack data file {self._path}: {error}"
            ) from error
        return state

    def _invalid_state_error(self, message: str) -> ReviewStateError:
        backup_path = self._path.with_name(f"{self._path.name}.bak")
        move_command = f"mv -i {shlex.quote(str(self._path))} {shlex.quote(str(backup_path))}"
        return ReviewStateError(
            message,
            hint=(
                f"Move the file aside with `{move_command}`, then explicitly re-adopt reviews "
                "with `jj-stack checkout --pull-request PR` or "
                "`jj-stack relink PR CHANGE`."
            ),
        )


def _replace_reviews(
    state: ReviewState,
    replacements: Mapping[str, tuple[ReviewIdentity, SubmittedBaseline]],
) -> ReviewState:
    identities = dict(state.review_identities)
    baselines = dict(state.submitted_baselines)
    for change_id, (identity, baseline) in replacements.items():
        identities[change_id] = identity
        baselines[change_id] = baseline
    return ReviewState(review_identities=identities, submitted_baselines=baselines)


def _require_nested_versions(raw: dict[str, JsonValue]) -> None:
    for field_name, label in (
        ("review_identities", "review identity"),
        ("submitted_baselines", "submitted baseline"),
    ):
        records = raw.get(field_name)
        if not isinstance(records, dict):
            continue
        for record in records.values():
            if not isinstance(record, dict) or "version" not in record:
                raise ValueError(f"Persisted {label} is missing its version.")


def _require_identity_matches_change(identity: ReviewIdentity, change_id: str) -> None:
    if not review_branch_matches_change(identity.head_ref, change_id):
        raise ValueError(
            f"Review branch {identity.head_ref!r} does not match change {change_id!r}."
        )


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
            raise ReviewStateError(
                f"Could not read jj repository path file {repo_path}: {error}"
            ) from error
        if not target:
            raise ReviewStateError(f"jj repository path file is empty: {repo_path}")
        repo_path = repo_path.parent / target
    return repo_path.resolve()


def default_state_root() -> Path:
    """Return the base directory used for machine-written jj-stack data."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~", ".local", "state").expanduser().resolve()
