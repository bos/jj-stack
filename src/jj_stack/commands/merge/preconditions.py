"""Merge preconditions checked against fresh PR facts."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.commands.merge.models import MergeChange
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.stack.pr_facts import PRFacts, RepoFacts
from jj_stack.ui import Message


def merge_precondition_error(
    *,
    expected_repo: GithubRepoAddress,
    expected_trunk_branch: str,
    observation: RepoFacts,
    remote_name: str,
    changes: tuple[MergeChange, ...],
    inactive_allowed: frozenset[str] = frozenset(),
) -> str | None:
    """Explain why fresh facts do not permit the next mutation."""

    remote = observation.remote
    if remote is None or remote.name != remote_name:
        return f"Git remote {remote_name} is no longer configured"
    if observation.configured_repo != expected_repo:
        return "the configured Git remote no longer names the planned GitHub repo"
    github_repo = observation.github_repo
    assert github_repo is not None
    if github_repo.full_name.casefold() != expected_repo.full_name.casefold():
        return "GitHub no longer reports the planned repo"
    if github_repo.default_branch not in (None, "", expected_trunk_branch):
        return "GitHub no longer reports the planned trunk branch as its default"
    for change in changes:
        error = _merge_change_precondition_error(
            expected_repo=expected_repo,
            observed=observation.prs[change.change_id],
            planned=change,
            inactive_allowed=change.change_id in inactive_allowed,
        )
        if error is not None:
            return error
    return None


def explain_precondition(reason: str, *, change_id: str, sync_target: str) -> Message:
    """Restate a precondition reason so it names the command that resolves it.

    Planning and execution both stop on these reasons, so they share one wording rather than each
    deciding what to tell the user.
    """

    # Every boundary already names the change, so these do not repeat its ID.
    submit = ui.cmd(f"jj-stack submit {short_change_id(change_id)}")
    if "unresolved conflicts" in reason:
        return t"it has unresolved conflicts; resolve them with jj, then run {submit}"
    if "more than one visible commit" in reason:
        return (
            t"it has more than one visible commit; reconcile them, for example with "
            t"{ui.cmd('jj log -r')} {ui.revset(f'change_id({short_change_id(change_id)})')}, "
            t"then run {submit}"
        )
    if "no longer visible locally" in reason:
        return (
            t"it is no longer visible locally; run {ui.cmd('jj-stack view')} to find where it "
            t"went, or {ui.cmd(f'jj-stack sync {sync_target}')} if it already merged"
        )
    if "last submitted commit" in reason:
        return (
            t"the local change, the commit last submitted for it, and its PR branch do not "
            t"all name the same commit; run {submit}"
        )
    if "is already merged" in reason:
        return (
            t"{reason}, so this stack still holds a local copy of work already on trunk; run "
            t"{ui.cmd(f'jj-stack sync {sync_target}')}"
        )
    return t"{reason}; inspect it and rerun {ui.cmd('merge')}"


def _merge_change_precondition_error(
    *,
    expected_repo: GithubRepoAddress,
    observed: PRFacts,
    planned: MergeChange,
    inactive_allowed: bool,
) -> str | None:
    return _local_precondition_error(
        expected_repo=expected_repo,
        observed=observed,
        planned=planned,
    ) or _github_pr_precondition_error(
        observed=observed,
        planned=planned,
        inactive_allowed=inactive_allowed,
    )


def _local_precondition_error(
    *,
    expected_repo: GithubRepoAddress,
    observed: PRFacts,
    planned: MergeChange,
) -> str | None:
    """Explain why the local change and its PR branch do not match the plan."""

    identity = observed.identity
    local_commits = observed.local_commits
    label = planned.change_id
    if (
        identity != planned.identity
        or identity is None
        or identity.repo_key != expected_repo.repo_key
    ):
        return f"saved PR tracking for {label} changed"
    if not local_commits:
        return f"{label} is no longer visible locally"
    # Stack discovery normally rejects a divergent change first; this covers one that diverged
    # after the plan was built.
    if len(local_commits) > 1 or local_commits[0].divergent:
        return f"{label} has more than one visible commit"
    local = local_commits[0]
    # Conflicts come before the commit comparison: a rebase that conflicts also changes the
    # commit, and resolving is what has to happen first either way.
    if local.conflict:
        return f"{label} has unresolved conflicts"
    if (
        observed.baseline is None
        or observed.baseline.commit_id != planned.commit_id
        or local.commit_id != planned.commit_id
        or observed.remote_pr_branch_target != planned.commit_id
    ):
        return f"the last submitted commit for {label} changed"
    return None


def _github_pr_precondition_error(
    *,
    observed: PRFacts,
    planned: MergeChange,
    inactive_allowed: bool,
) -> str | None:
    """Explain why GitHub's view of the pull request does not match the plan."""

    pr = observed.pr
    if pr is None:
        return f"GitHub no longer reports the saved pull request for {planned.change_id}"
    pr = pr.normalize_state()
    # The head commit is deliberately not compared here. GitHub is given the expected head with
    # the merge request and rejects a stale one atomically, which a check made beforehand cannot
    # do; the PR branch is still compared against the submitted baseline above.
    if not planned.identity.matches_pr(pr):
        return f"the pull request linked to {planned.change_id} changed"
    if pr.state == "merged" and not inactive_allowed:
        return f"pull request #{pr.number} is already merged"
    if pr.state != "open" and not inactive_allowed:
        return f"pull request #{pr.number} state or base branch changed"
    if pr.is_draft and not inactive_allowed:
        return f"pull request #{pr.number} is now a draft"
    return None
