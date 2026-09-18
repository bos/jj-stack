"""Display the review and check evidence collected during status inspection."""

from __future__ import annotations

from textwrap import shorten

import jj_stack.ui as ui
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import short_change_id
from jj_stack.models.github import GithubPR
from jj_stack.models.github_details import GithubPRMergeDetails
from jj_stack.stack.reporting import report_change, status_label
from jj_stack.stack.status import StatusResult


def merge_details_hint(result: StatusResult) -> ui.Message | None:
    if any(
        report.merge_warnings
        or report.lifecycle in {"review_required", "changes_requested"}
        or report.checks in {"failed", "pending"}
        for report in (report_change(change.state) for change in result.changes)
    ):
        head = short_change_id(result.changes[0].change_id)
        return (
            t"For review threads and check details, run "
            t"{ui.cmd(f'jj-stack view --verbose {head}')}."
        )
    return None


def render_merge_details(result: StatusResult) -> tuple[ui.Renderable, ...]:
    lines: list[ui.Renderable] = []
    for change in result.changes:
        pr = change.pr
        if pr is None or (evidence := pr.merge_details) is None:
            continue
        if isinstance(evidence, str):
            rows = [("Details unavailable", evidence)]
        else:
            rows = _evidence_rows(pr, evidence)
        if not rows:
            continue
        lines.extend(("", t"Merge details for {format_pr_label(pr.number, url=pr.html_url)}:"))
        lines.extend(t"  {kind}: {detail}" for kind, detail in rows)
    return tuple(lines)


def _evidence_rows(
    pr: GithubPR, details: GithubPRMergeDetails
) -> list[tuple[ui.Message, ui.Message]]:
    rows: list[tuple[ui.Message, ui.Message]] = []
    if pr.review_decision in {"review_required", "changes_requested"}:
        rows.append(
            (
                "Review",
                t"{status_label(pr.review_decision)}: {ui.hyperlink(pr.html_url, pr.html_url)}",
            )
        )
    for thread in details.unresolved_threads:
        location = f"{thread.path}:{thread.line}" if thread.line is not None else thread.path
        outdated = " (outdated)" if thread.is_outdated else ""
        excerpt = shorten(thread.body, width=160, placeholder=" ...")
        url = thread.url or pr.html_url
        rows.append(
            (
                "Unresolved thread",
                t"{ui.code(location)}{outdated}: {excerpt}\n  {ui.hyperlink(url, url)}",
            )
        )
    for name in details.missing_checks:
        rows.append(
            ("Missing required check", t"{name}: {ui.hyperlink(pr.html_url, pr.html_url)}")
        )
    for check in (*details.checks, *details.merge_checks):
        if check.state in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            continue
        url = check.url or f"{pr.html_url}/checks"
        rows.append(
            (
                "Check",
                t"{check.name}: {check.state.lower().replace('_', ' ')}\n"
                t"  {ui.hyperlink(url, url)}",
            )
        )
    return rows
