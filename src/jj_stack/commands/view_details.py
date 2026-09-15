"""Observe and display review and check evidence requested by verbose view."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from textwrap import shorten

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClientError
from jj_stack.identifiers import short_change_id
from jj_stack.models.github import GithubPR
from jj_stack.models.github_details import GithubPRMergeDetails
from jj_stack.stack.reporting import report_change
from jj_stack.stack.status import StatusResult

type MergeDetails = Mapping[int, GithubPRMergeDetails | str]


def observe_merge_details(
    context: CommandContext, results: Sequence[StatusResult]
) -> MergeDetails:
    prs: dict[int, GithubPR] = {}
    for result in results:
        for change in result.changes:
            report = report_change(change.state)
            pr = change.pr
            if (
                pr is not None
                and pr.state == "open"
                and not (pr.is_draft or pr.is_queued or report.divergent)
                and report.problem is None
            ):
                prs[pr.number] = pr
    repo = next((result.github_repo for result in results if result.github_repo), None)
    if not prs or repo is None:
        return {}

    async def read() -> MergeDetails:
        async with context.open_github_client(repo=repo) as github:
            try:
                observed = await github.get_pr_merge_details(prs=tuple(prs.values()))
            except GithubClientError as error:
                return dict.fromkeys(prs, error.user_facing_reason())
        return {
            number: details
            if details is not None
            else "GitHub could not provide details for the observed PR head; "
            "rerun jj-stack view --verbose"
            for number, details in observed.items()
        }

    return asyncio.run(read())


def merge_details_hint(result: StatusResult) -> ui.Message | None:
    if any(
        report.merge_status == "BLOCKED"
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


def render_merge_details(
    result: StatusResult, details: MergeDetails
) -> tuple[ui.Renderable, ...]:
    lines: list[ui.Renderable] = []
    for change in result.changes:
        pr = change.pr
        if pr is None or (evidence := details.get(pr.number)) is None:
            continue
        if isinstance(evidence, str):
            rows = [("Details unavailable", evidence)]
        else:
            rows = _evidence_rows(pr, evidence)
            if not rows and pr.merge_state_status == "BLOCKED":
                rows.append(
                    (
                        "GitHub",
                        t"GitHub did not expose a specific blocking requirement. See "
                        t"{ui.hyperlink(pr.html_url, pr.html_url)}",
                    )
                )
        if not rows:
            continue
        lines.extend(("", t"Merge details for {format_pr_label(pr.number, url=pr.html_url)}:"))
        lines.extend(t"  {kind}: {detail}" for kind, detail in rows)
    if lines:
        lines.extend(
            (
                "",
                "Checks shown here are the results GitHub has received. A required check that "
                "has not reported yet is absent, and other repo rules can still block merging.",
            )
        )
    return tuple(lines)


def _evidence_rows(
    pr: GithubPR, details: GithubPRMergeDetails
) -> list[tuple[ui.Message, ui.Message]]:
    rows: list[tuple[ui.Message, ui.Message]] = []
    if pr.review_decision in {"review_required", "changes_requested"}:
        rows.append(
            (
                "Review",
                t"{pr.review_decision.replace('_', ' ')}: "
                t"{ui.hyperlink(pr.html_url, pr.html_url)}",
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
    for check in details.checks:
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
