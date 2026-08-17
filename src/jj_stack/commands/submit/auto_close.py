"""Prevent GitHub's reachability-based pull request auto-close."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.jj.client import JjClient

from .models import PreparedSubmitChange, PRSyncPlan


async def retarget_pr_bases_before_branch_push(
    *,
    github_client: GithubClient,
    plans: tuple[PRSyncPlan, ...],
    trunk_branch: str,
) -> None:
    """Move PR bases that would auto-close after the push to trunk first."""

    await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=plans,
        run_item=lambda plan: _retarget_pr_base_before_branch_push(
            github_client=github_client,
            plan=plan,
            trunk_branch=trunk_branch,
        ),
    )


def predict_prs_auto_closed_by_push(
    *,
    jj_client: JjClient,
    plans: tuple[PRSyncPlan, ...],
    prepared_changes: tuple[PreparedSubmitChange, ...],
    remote_targets: dict[str, str],
) -> tuple[PRSyncPlan, ...]:
    """Pending PRs that GitHub will auto-close (as merged) after the planned push.

    GitHub auto-closes an open PR when its head ref becomes contained in its base
    ref. The push moves head and, transitively through the planned stacked branch
    updates, base, so the prediction uses the post-push commit IDs each ref will
    hold.
    """

    push_targets = {
        prepared_change.branch: prepared_change.change.commit_id
        for prepared_change in prepared_changes
    }
    candidates: list[tuple[str, str, PRSyncPlan]] = []
    for plan in plans:
        pr = plan.discovered_pr
        if pr is None or pr.state != "open":
            continue
        head_after_push = push_targets.get(pr.head.ref)
        if head_after_push is None:
            continue
        base_after_push = _resolve_post_push_commit(
            ref=pr.base.ref,
            push_targets=push_targets,
            remote_targets=remote_targets,
        )
        if base_after_push is None:
            continue
        candidates.append((head_after_push, base_after_push, plan))

    if not candidates:
        return ()
    auto_close_heads = jj_client.query_paired_ancestor_membership(
        tuple((head, base) for head, base, _ in candidates),
    )
    return tuple(plan for head, _, plan in candidates if head in auto_close_heads)


def _resolve_post_push_commit(
    *,
    push_targets: dict[str, str],
    ref: str,
    remote_targets: dict[str, str],
) -> str | None:
    """Resolve the commit ID a ref will point at after the planned push lands."""

    if ref in push_targets:
        return push_targets[ref]
    return remote_targets.get(ref)


async def _retarget_pr_base_before_branch_push(
    *,
    github_client: GithubClient,
    plan: PRSyncPlan,
    trunk_branch: str,
) -> None:
    pr = plan.discovered_pr
    if pr is None:
        raise AssertionError("Pre-push retarget requires a discovered pull request.")
    try:
        await github_client.update_pr(
            pr_number=pr.number,
            base=trunk_branch,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not retarget PR #{pr.number} to "
            t"{ui.bookmark(trunk_branch)} before pushing PR branches"
        ) from error
