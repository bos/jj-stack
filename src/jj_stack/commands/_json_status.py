"""JSON projections for user-facing stack status."""

from __future__ import annotations

from jj_stack.models.tracking import PRIdentity
from jj_stack.stack.change_status import (
    ChangeStatus,
    classify_stack_status_change,
)
from jj_stack.stack.status import StackStatusChange


def stack_change_json(
    change: StackStatusChange,
    *,
    current: bool = False,
) -> dict[str, object]:
    """Return the public JSON shape for one stack change."""

    payload: dict[str, object] = {
        "change_id": change.change_id,
        "status": _change_status(classify_stack_status_change(change)),
        "subject": change.subject,
    }
    if change.branch is not None:
        payload["branch"] = change.branch
    if current:
        payload["current"] = True
    pr = pr_json(change)
    if pr is not None:
        payload["pr"] = pr
    return payload


def pr_json(
    change: StackStatusChange,
) -> dict[str, object] | None:
    lookup = change.pr_lookup
    if lookup is not None and lookup.pr is not None:
        pr = lookup.pr
        return _json_object(
            {
                "number": pr.number,
                "url": pr.html_url,
            }
        )
    return saved_pr_json(change.pr_identity)


def saved_pr_json(
    pr_identity: PRIdentity | None,
) -> dict[str, object] | None:
    if pr_identity is None:
        return None
    payload = _json_object({"number": pr_identity.pr_number})
    return payload or None


def _change_status(status: ChangeStatus) -> str:
    if status.local == "divergent":
        return "divergent"
    if status.pr_lifecycle in {"ambiguous", "closed", "merged", "missing"}:
        return status.pr_lifecycle
    if status.has_pr_lookup_failure:
        return "unknown"
    if status.pr_lifecycle == "open":
        if status.pr_queued is True:
            return "queued"
        if status.pr_draft is True:
            return "draft"
        if status.pr_review_decision == "approved":
            return "approved"
        if status.pr_review_decision == "changes_requested":
            return "changes_requested"
        if status.pr_review_decision == "commented":
            return "commented"
        return "open"
    if status.saved_pr_identity:
        return "submitted"
    return "unsubmitted"


def _json_object(values: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}
