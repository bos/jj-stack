"""JSON projections for user-facing review status."""

from __future__ import annotations

from jj_stack.models.review_state import CachedChange
from jj_stack.review.change_status import (
    ReviewChangeStatus,
    classify_review_status_revision,
)
from jj_stack.review.status import ReviewStatusRevision


def review_change_json(
    revision: ReviewStatusRevision,
    *,
    current: bool = False,
) -> dict[str, object]:
    """Return the public JSON shape for one review change."""

    payload: dict[str, object] = {
        "bookmark": revision.bookmark,
        "change_id": revision.change_id,
        "status": _review_change_status(classify_review_status_revision(revision)),
        "subject": revision.subject,
    }
    if current:
        payload["current"] = True
    pull_request = review_pull_request_json(revision)
    if pull_request is not None:
        payload["pull_request"] = pull_request
    return payload


def review_pull_request_json(
    revision: ReviewStatusRevision,
) -> dict[str, object] | None:
    lookup = revision.pull_request_lookup
    if lookup is not None and lookup.pull_request is not None:
        pull_request = lookup.pull_request
        return _json_object(
            {
                "number": pull_request.number,
                "url": pull_request.html_url,
            }
        )
    return cached_pull_request_json(revision.cached_change)


def cached_pull_request_json(cached_change: CachedChange | None) -> dict[str, object] | None:
    if cached_change is None:
        return None
    payload = _json_object(
        {
            "number": cached_change.pr_number,
            "url": cached_change.pr_url,
        }
    )
    return payload or None


def _review_change_status(status: ReviewChangeStatus) -> str:
    if status.local == "divergent":
        return "divergent"
    if status.link == "unlinked":
        return "unlinked"
    if status.pr_lifecycle in {"ambiguous", "closed", "merged", "missing"}:
        return status.pr_lifecycle
    if status.has_pull_request_lookup_failure:
        return "unknown"
    if status.pr_lifecycle == "open":
        if status.pr_draft is True:
            return "draft"
        if status.pr_review_decision == "approved":
            return "approved"
        if status.pr_review_decision == "changes_requested":
            return "changes_requested"
        if status.pr_review_decision == "commented":
            return "commented"
        return "open"
    if status.saved_review_identity:
        return "submitted"
    return "unsubmitted"


def _json_object(values: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}
