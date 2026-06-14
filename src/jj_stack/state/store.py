"""Persistence helpers for jj-stack tracking data."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from jj_stack.errors import CliError
from jj_stack.models.review_state import ReviewState

STATE_DIRNAME = "jj-stack"
STATE_FILENAME = "state.json"


class ReviewStateError(CliError):
    """Raised when the tracking data is unreadable or invalid."""


class ReviewStateStore:
    """Load and save jj-stack data in a user state directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_repo(cls, repo_root: Path) -> ReviewStateStore:
        """Build a jj-stack data store for the supplied repository root."""

        return cls(resolve_state_path(repo_root))

    @property
    def state_dir(self) -> Path:
        return self._path.parent

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
        """Load tracking data, or defaults when the file is missing."""

        try:
            return self._load_state()
        except ValidationError as error:
            raise ReviewStateError(f"Invalid jj-stack data in {self._path}: {error}") from error

    def save(self, state: ReviewState) -> None:
        """Persist the supplied jj-stack data."""

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

    def _load_state(self) -> ReviewState:
        if not self._path.exists():
            return ReviewState()
        if not self._path.is_file():
            raise ReviewStateError(f"jj-stack data path is not a file: {self._path}")
        try:
            return ReviewState.model_validate_json(self._path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ReviewStateError(
                f"Could not read jj-stack data file {self._path}: {error}"
            ) from error


def resolve_state_path(repo_root: Path) -> Path:
    """Return the machine-written jj-stack data path for the repo."""

    repo_storage_root = (repo_root / ".jj" / "repo").resolve()
    repo_id = hashlib.sha256(str(repo_storage_root).encode("utf-8")).hexdigest()
    return default_state_root() / STATE_DIRNAME / "repos" / repo_id / STATE_FILENAME


def default_state_root() -> Path:
    """Return the base directory used for machine-written jj-stack data."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~", ".local", "state").expanduser().resolve()

