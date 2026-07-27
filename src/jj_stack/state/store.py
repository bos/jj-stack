"""Persistence helpers for jj-stack tracking data."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Never

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, ValidationError

from jj_stack.errors import CliError
from jj_stack.models.review_state import (
    ReviewIdentity,
    ReviewState,
    ReviewStateRecordIssue,
    ReviewStateRecordType,
    SubmittedBaseline,
)
from jj_stack.review.branches import review_branch_matches_change

STATE_DIRNAME = "jj-stack"
STATE_FILENAME = "state.json"

type ReviewStateIssueReporter = Callable[[tuple[ReviewStateRecordIssue, ...]], None]

_IDENTITY: ReviewStateRecordType = "review_identity"
_BASELINE: ReviewStateRecordType = "submitted_baseline"


class ReviewStateError(CliError):
    """Raised when the tracking data is unreadable or invalid."""


class ReviewStateConflictError(ReviewStateError):
    """Raised when a record changed after a caller observed it."""


class _StoredReviewState(BaseModel):
    """Strict envelope that keeps each record opaque until isolated validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    review_identities: dict[str, JsonValue] = Field(default_factory=dict)
    stacked_pull_requests: dict[str, StrictBool] = Field(default_factory=dict)
    submitted_baselines: dict[str, JsonValue] = Field(default_factory=dict)


class ReviewStateStore:
    """Load tracking state and atomically compare-and-write review records."""

    def __init__(
        self,
        path: Path,
        *,
        issue_reporter: ReviewStateIssueReporter | None = None,
    ) -> None:
        self._path = path
        self._issue_reporter = issue_reporter
        self._reported_issues: set[tuple[ReviewStateRecordType, str, str]] = set()

    @classmethod
    def for_repo(
        cls,
        repo_root: Path,
        *,
        issue_reporter: ReviewStateIssueReporter | None = None,
    ) -> ReviewStateStore:
        """Build a jj-stack data store for the supplied repository root."""

        return cls(resolve_state_path(repo_root), issue_reporter=issue_reporter)

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
        """Load valid records and isolate malformed entries without interpreting them."""

        return self._observe(self._load_envelope())

    def get_stacked_pull_requests(self, repository_key: str) -> bool | None:
        return self._load_envelope().stacked_pull_requests.get(repository_key)

    def set_stacked_pull_requests(self, repository_key: str, supported: bool) -> None:
        envelope = self._load_envelope()
        envelope.stacked_pull_requests[repository_key] = supported
        self._persist(envelope)

    def create_review(
        self,
        change_id: str,
        *,
        identity: ReviewIdentity,
        baseline: SubmittedBaseline,
    ) -> ReviewState:
        """Atomically create an identity and baseline when both records are absent."""

        _require_identity_matches_change(identity, change_id)
        envelope = self._load_envelope()
        if change_id in envelope.review_identities or change_id in envelope.submitted_baselines:
            self._raise_conflict(change_id, "create review")
        envelope.review_identities[change_id] = identity.model_dump(mode="json")
        envelope.submitted_baselines[change_id] = baseline.model_dump(mode="json")
        return self._persist(envelope)

    def relink_review(
        self,
        change_id: str,
        *,
        expected_identity: ReviewIdentity | None,
        expected_baseline: SubmittedBaseline | None,
        expected_issues: tuple[ReviewStateRecordIssue, ...] = (),
        identity: ReviewIdentity,
        baseline: SubmittedBaseline,
    ) -> ReviewState:
        """Atomically replace the two records after comparing their prior values."""

        return self.relink_reviews(
            expected={change_id: (expected_identity, expected_baseline)},
            expected_issues={change_id: expected_issues},
            replacements={change_id: (identity, baseline)},
        )

    def relink_reviews(
        self,
        *,
        expected: Mapping[
            str,
            tuple[ReviewIdentity | None, SubmittedBaseline | None],
        ],
        expected_issues: Mapping[str, tuple[ReviewStateRecordIssue, ...]] | None = None,
        replacements: Mapping[str, tuple[ReviewIdentity, SubmittedBaseline]],
    ) -> ReviewState:
        """Atomically replace complete review pairs after comparing every prior value."""

        if expected.keys() != replacements.keys():
            raise ValueError("Expected and replacement review sets must have identical keys.")
        envelope = self._load_envelope()
        for change_id, (identity, _baseline) in replacements.items():
            _require_identity_matches_change(identity, change_id)
            issues = () if expected_issues is None else expected_issues.get(change_id, ())
            fingerprints = {
                issue.record_type: issue.fingerprint
                for issue in issues
                if issue.change_id == change_id
            }
            for expected_record, record_type in zip(
                expected[change_id], (_IDENTITY, _BASELINE), strict=True
            ):
                self._compare_record(
                    envelope,
                    change_id,
                    expected_record,
                    "relink reviews",
                    record_type,
                    fingerprints.get(record_type),
                )
        for change_id, (identity, baseline) in replacements.items():
            envelope.review_identities[change_id] = identity.model_dump(mode="json")
            envelope.submitted_baselines[change_id] = baseline.model_dump(mode="json")
        return self._persist(envelope)

    def retire_review(
        self,
        change_id: str,
        *,
        expected_identity: ReviewIdentity,
        expected_baseline: SubmittedBaseline,
    ) -> ReviewState:
        """Atomically remove one exact identity and baseline pair."""

        envelope = self._load_envelope()
        self._compare_record(envelope, change_id, expected_identity, "retire review", _IDENTITY)
        self._compare_record(envelope, change_id, expected_baseline, "retire review", _BASELINE)
        del envelope.review_identities[change_id]
        del envelope.submitted_baselines[change_id]
        return self._persist(envelope)

    def _load_envelope(self) -> _StoredReviewState:
        if not self._path.exists():
            return _StoredReviewState(version=3)
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
        if version != 3:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: unsupported version {version!r}"
            )
        try:
            envelope = _StoredReviewState.model_validate(raw)
        except ValidationError as error:
            raise self._invalid_state_error(
                f"Invalid jj-stack data in {self._path}: {error}"
            ) from error
        return envelope

    def _persist(self, envelope: _StoredReviewState) -> ReviewState:
        rendered = envelope.model_dump_json(exclude_none=True, indent=2) + "\n"
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
        return self._observe(envelope)

    def _observe(self, envelope: _StoredReviewState) -> ReviewState:
        state = _isolate_records(envelope)
        if self._issue_reporter is None:
            return state
        unreported = tuple(
            issue
            for issue in state.record_issues
            if (issue.record_type, issue.change_id, issue.fingerprint)
            not in self._reported_issues
        )
        if unreported:
            self._issue_reporter(unreported)
            self._reported_issues.update(
                (issue.record_type, issue.change_id, issue.fingerprint) for issue in unreported
            )
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

    def _compare_record(
        self,
        envelope: _StoredReviewState,
        change_id: str,
        expected: ReviewIdentity | SubmittedBaseline | None,
        operation: str,
        record_type: ReviewStateRecordType,
        invalid_fingerprint: str | None = None,
    ) -> None:
        records = (
            envelope.review_identities
            if record_type == _IDENTITY
            else envelope.submitted_baselines
        )
        if change_id not in records:
            if expected is not None:
                self._raise_conflict(change_id, operation)
            if invalid_fingerprint is not None and invalid_fingerprint != _record_fingerprint(
                None,
                present=False,
            ):
                self._raise_conflict(change_id, operation)
            return
        record = records.get(change_id)
        try:
            current = (
                _validate_identity(record, change_id)
                if record_type == _IDENTITY
                else _validate_baseline(record)
            )
        except ValidationError, ValueError:
            if expected is None and invalid_fingerprint == _record_fingerprint(record):
                return
            self._raise_conflict(change_id, operation)
        if expected is None or current != expected:
            self._raise_conflict(change_id, operation)

    def _raise_conflict(self, change_id: str, operation: str) -> Never:
        raise ReviewStateConflictError(
            f"Tracking data for {change_id} changed before {operation}; reload and retry."
        )


def _isolate_records(envelope: _StoredReviewState) -> ReviewState:
    identities: dict[str, ReviewIdentity] = {}
    baselines: dict[str, SubmittedBaseline] = {}
    issues: list[ReviewStateRecordIssue] = []
    for change_id, record in envelope.review_identities.items():
        try:
            identities[change_id] = _validate_identity(record, change_id)
        except (ValidationError, ValueError) as error:
            issues.append(
                _record_issue("review_identity", change_id, record, validation_error=str(error))
            )
    for change_id, record in envelope.submitted_baselines.items():
        try:
            baselines[change_id] = _validate_baseline(record)
        except (ValidationError, ValueError) as error:
            issues.append(
                _record_issue(
                    "submitted_baseline",
                    change_id,
                    record,
                    validation_error=str(error),
                )
            )
    for change_id in identities.keys() - envelope.submitted_baselines.keys():
        issues.append(
            _record_issue(
                "submitted_baseline",
                change_id,
                None,
                missing=True,
                validation_error="Submitted baseline is missing.",
            )
        )
    for change_id in baselines.keys() - envelope.review_identities.keys():
        issues.append(
            _record_issue(
                "review_identity",
                change_id,
                None,
                missing=True,
                validation_error="Review identity is missing.",
            )
        )
    return ReviewState(
        review_identities=identities,
        submitted_baselines=baselines,
        record_issues=tuple(issues),
    )


def _record_issue(
    record_type: ReviewStateRecordType,
    change_id: str,
    record: JsonValue | None,
    *,
    missing: bool = False,
    validation_error: str,
) -> ReviewStateRecordIssue:
    return ReviewStateRecordIssue(
        record_type=record_type,
        change_id=change_id,
        fingerprint=_record_fingerprint(record, present=not missing),
        validation_error=validation_error,
    )


def _record_fingerprint(record: JsonValue | None, *, present: bool = True) -> str:
    rendered = json.dumps(
        [present, record],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _validate_identity(record: JsonValue | None, change_id: str) -> ReviewIdentity:
    if not isinstance(record, dict) or "version" not in record:
        raise ValueError("Persisted review identity is missing its version.")
    identity = ReviewIdentity.model_validate(record)
    _require_identity_matches_change(identity, change_id)
    return identity


def _require_identity_matches_change(identity: ReviewIdentity, change_id: str) -> None:
    if not review_branch_matches_change(identity.head_ref, change_id):
        raise ValueError(
            f"Review branch {identity.head_ref!r} does not match change {change_id!r}."
        )


def _validate_baseline(record: JsonValue | None) -> SubmittedBaseline:
    if not isinstance(record, dict) or "version" not in record:
        raise ValueError("Persisted submitted baseline is missing its version.")
    return SubmittedBaseline.model_validate(record)


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
