"""JSON projections for user-facing stack status."""

from __future__ import annotations

from jj_stack.models.github_details import GithubPRMergeDetails
from jj_stack.models.tracking import PRIdentity
from jj_stack.stack.reporting import report_change
from jj_stack.stack.status import StackStatusChange
from jj_stack.ui import plain_text


def stack_change_json(
    change: StackStatusChange,
    *,
    current: bool = False,
    merge_details: GithubPRMergeDetails | str | None = None,
) -> dict[str, object]:
    """Return the public JSON shape for one stack change."""

    report = report_change(change.state)
    payload: dict[str, object] = {
        "change_id": change.change_id,
        "status": report.status,
        "subject": change.subject,
        "needs_submit": report.needs_submit,
        "needs_sync": report.needs_sync,
    }
    if report.reason is not None:
        payload["reason"] = plain_text(report.reason)
    if report.repair is not None:
        payload["repair"] = plain_text(report.repair)
    if change.branch is not None:
        payload["branch"] = change.branch
    if current:
        payload["current"] = True
    pr = pr_json(change)
    if pr is not None:
        if isinstance(merge_details, str):
            pr["merge_details_error"] = merge_details
        elif merge_details is not None:
            pr["merge_details"] = merge_details.model_dump(mode="json")
        payload["pr"] = pr
    return payload


def pr_json(
    change: StackStatusChange,
) -> dict[str, object] | None:
    pr = change.pr
    if pr is not None:
        values: dict[str, object] = {
            "checks": pr.check_rollup_status,
            "merge_state_status": pr.merge_state_status,
            "number": pr.number,
            "url": pr.html_url,
        }
        return {key: value for key, value in values.items() if value is not None}
    return saved_pr_json(change.tracked.pr_identity) if change.tracked is not None else None


def saved_pr_json(
    pr_identity: PRIdentity,
) -> dict[str, object]:
    return {"number": pr_identity.pr_number}
