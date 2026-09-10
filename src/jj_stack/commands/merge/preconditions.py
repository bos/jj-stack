"""Merge preconditions checked against fresh PR facts."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.commands.merge.models import MergeChange, MergePrecondition
from jj_stack.formatting import format_pr_number
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.models.stack import LocalCommit
from jj_stack.stack.change_state import (
    BranchDisagrees,
    BranchMissing,
    PRAmbiguous,
    PRHeadMoved,
    PRIdentityMismatch,
    PRMissing,
    classify,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.ui import Message


def merge_precondition_error(
    *,
    expected_repo: GithubRepoAddress,
    expected_trunk_branch: str,
    observation: RepoFacts,
    remote_name: str,
    change: MergeChange,
) -> MergePrecondition | None:
    """Explain which observed precondition prevents the next action."""

    remote = observation.remote
    if remote is None or remote.name != remote_name:
        return MergePrecondition(f"Git remote {remote_name} is no longer configured")
    if observation.configured_repo != expected_repo:
        return MergePrecondition(
            "the configured Git remote no longer names the planned GitHub repo"
        )
    github_repo = observation.github_repo
    if github_repo.full_name.casefold() != expected_repo.full_name.casefold():
        return MergePrecondition("GitHub no longer reports the planned repo")
    if github_repo.default_branch not in (None, "", expected_trunk_branch):
        return MergePrecondition(
            "GitHub no longer reports the planned trunk branch as its default"
        )
    return _merge_change_precondition_error(observation=observation, planned=change)


def explain_precondition(
    precondition: MergePrecondition,
    *,
    change_id: str,
    sync_target: str,
) -> Message:
    """Restate a precondition reason so it names the command that resolves it.

    Planning and execution both stop on these reasons, so they share one wording rather than each
    deciding what to tell the user.
    """

    reason = precondition.reason
    submit = ui.cmd(f"jj-stack submit {short_change_id(change_id)}")
    if precondition.recovery == "explained":
        return reason
    if precondition.recovery == "resolve":
        return t"it has unresolved conflicts; resolve them with jj, then run {submit}"
    if precondition.recovery == "reconcile":
        hint = divergence_recovery_hint(change_id, retry=t"run {submit}")
        return t"it has more than one local version; {hint}"
    if precondition.recovery == "view":
        return (
            t"it is no longer visible locally; find where it went with {ui.cmd('jj-stack view')}"
        )
    if precondition.recovery == "submit":
        return (
            t"the local change or its PR branch no longer matches the last submitted commit; "
            t"run {submit}"
        )
    if precondition.recovery == "sync":
        return t"{reason}; update the local stack with {ui.cmd(f'jj-stack sync {sync_target}')}"
    return t"{reason}; inspect it and rerun {ui.cmd('jj-stack merge')}"


def _merge_change_precondition_error(
    *,
    observation: RepoFacts,
    planned: MergeChange,
) -> MergePrecondition | None:
    """Explain why the pull request, or the local copy behind it, does not match the plan.

    GitHub's report of the pull request comes first: a merged pull request is a stop by itself,
    wherever its branch and the local copy have ended up since. Only a candidate that can still
    merge goes on to the commit comparison.
    """

    observed = observation.prs[planned.change_id]
    label = short_change_id(planned.change_id)
    selected = next(
        (commit for commit in observed.local if commit.commit_id == planned.commit_id),
        None,
    )
    state = classify(observed, selected=selected)
    if isinstance(state, (PRMissing, PRAmbiguous, PRIdentityMismatch)):
        return MergePrecondition(t"{state.reason}; {state.repair}", recovery="explained")
    pr = state.pr
    pr_number = format_pr_number(pr.number, url=pr.html_url)
    if pr.state != "open":
        return MergePrecondition(
            t"pull request {pr_number} is {pr.state}",
            recovery="sync" if pr.state == "merged" else "inspect",
        )
    if pr.is_draft:
        return MergePrecondition(t"pull request {pr_number} is now a draft")
    shape_error = _local_shape_error(observed.local, label=label)
    if shape_error is not None:
        return shape_error
    if isinstance(state, (PRHeadMoved, BranchMissing, BranchDisagrees)):
        return MergePrecondition(t"{state.reason}; {state.repair}", recovery="explained")
    # Merge only the exact submitted commit: the planned commit must be the local commit, the
    # submitted baseline, and the PR branch target alike.
    if selected is None or state.has_local_edits or state.remote_target != planned.commit_id:
        return MergePrecondition(
            f"the last submitted commit for {label} changed",
            recovery="submit",
        )
    return None


def _local_shape_error(
    local_commits: tuple[LocalCommit, ...],
    *,
    label: str,
) -> MergePrecondition | None:
    if not local_commits:
        return MergePrecondition(f"{label} is no longer visible locally", recovery="view")
    # Stack discovery normally rejects a divergent change first; this covers one that diverged
    # after the plan was built.
    if len(local_commits) > 1 or local_commits[0].divergent:
        return MergePrecondition(
            f"{label} has more than one visible commit",
            recovery="reconcile",
        )
    # Conflicts come before the commit comparison: a rebase that conflicts also changes the
    # commit, and resolving is what has to happen first either way.
    if local_commits[0].conflict:
        return MergePrecondition(f"{label} has unresolved conflicts", recovery="resolve")
    return None
