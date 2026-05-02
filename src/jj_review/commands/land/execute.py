"""Live execution helpers for the land command."""

from __future__ import annotations

from jj_review import ui
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.models.github import GithubPullRequest
from jj_review.models.review_state import CachedChange
from jj_review.review.status import normalize_pull_request_state

from .models import BookmarkRestorer, BookmarkStateReader, LandAction, LandRevision


def restore_local_trunk_bookmark(
    *,
    client: BookmarkRestorer,
    original_target: str | None,
    trunk_branch: str,
) -> None:
    if original_target is None:
        client.forget_bookmarks((trunk_branch,))
        return
    client.set_bookmark(trunk_branch, original_target, allow_backwards=True)


def ensure_trunk_branch_matches_selected_trunk(
    *,
    client: BookmarkStateReader,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> None:
    bookmark_state = client.get_bookmark_state(trunk_branch)
    if len(bookmark_state.local_targets) > 1:
        raise CliError(
            t"Local trunk bookmark {ui.bookmark(trunk_branch)} is conflicted.",
            hint="Resolve it before landing.",
        )
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != trunk_commit_id:
        inspect_command = f"jj log -r '{trunk_branch}|trunk()'"
        raise CliError(
            t"Local bookmark {ui.bookmark(trunk_branch)} points to a different "
            t"revision than {ui.revset('trunk()')}.",
            hint=(
                t"Inspect both with {ui.cmd(inspect_command)} and move "
                t"{ui.bookmark(trunk_branch)} back to {ui.revset('trunk()')} before "
                t"retrying."
            ),
        )

    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is "
            t"conflicted.",
            hint="Resolve it before landing.",
        )
    if remote_state.target is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available.",
            hint="Fetch and retry.",
        )
    if remote_state.target != trunk_commit_id:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} moved since "
            t"the selected path was resolved.",
            hint="Fetch, rebase if needed, and retry.",
        )


async def check_post_resubmit_approvals(
    *,
    bypass_readiness: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    resubmit_revisions: tuple[LandRevision, ...],
    trunk_branch: str,
) -> LandAction | None:
    """Return a blocking action if the resubmit push dismissed any approval."""

    if bypass_readiness or not resubmit_revisions:
        return None
    try:
        decisions = await github_client.get_review_decisions_by_pull_request_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=tuple(
                revision.pull_request_number for revision in resubmit_revisions
            ),
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not re-check PR review decisions after refreshing review branches"
        ) from error
    for revision in resubmit_revisions:
        decision = decisions.get(revision.pull_request_number)
        if decision != "approved":
            return LandAction(
                kind="boundary",
                body=t"before pushing {ui.bookmark(trunk_branch)} because refreshing "
                t"{ui.bookmark(revision.bookmark)} dismissed the approval on "
                t"PR #{revision.pull_request_number}; request re-review and rerun "
                t"{ui.cmd('land')}",
                status="blocked",
            )
    return None


async def finalize_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    landed_revision: LandRevision,
    trunk_branch: str,
) -> GithubPullRequest:
    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land"
        ) from error
    pull_request = normalize_pull_request_state(pull_request)
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}"
            ) from error
        pull_request = normalize_pull_request_state(pull_request)
    if pull_request.state == "open":
        try:
            await github_client.close_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(t"Could not close PR #{pull_request.number} after landing") from error
        pull_request = normalize_pull_request_state(pull_request)
    if cached_change is not None:
        for comment_id, label in (
            (cached_change.navigation_comment_id, "stack navigation comment"),
            (cached_change.overview_comment_id, "stack overview comment"),
        ):
            if comment_id is None:
                continue
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=comment_id,
                )
            except GithubClientError as error:
                if error.status_code != 404:
                    raise CliError(t"Could not delete {label} #{comment_id}") from error
    return pull_request


def updated_landed_change(
    *,
    bookmark: str,
    bookmark_managed: bool,
    cached_change: CachedChange | None,
    commit_id: str,
    parent_change_id: str | None,
    pull_request: GithubPullRequest,
    stack_head_change_id: str | None,
) -> CachedChange:
    pr_state = pull_request.state
    if pull_request.merged_at is not None:
        pr_state = "merged"
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            bookmark_ownership="managed" if bookmark_managed else "external",
            last_submitted_commit_id=commit_id,
            last_submitted_parent_change_id=parent_change_id,
            last_submitted_stack_head_change_id=stack_head_change_id,
            pr_number=pull_request.number,
            pr_state=pr_state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "bookmark_ownership": "managed" if bookmark_managed else "external",
            "last_submitted_commit_id": commit_id,
            "last_submitted_parent_change_id": parent_change_id,
            "last_submitted_stack_head_change_id": stack_head_change_id,
            "pr_number": pull_request.number,
            "pr_review_decision": None,
            "pr_state": pr_state,
            "pr_url": pull_request.html_url,
            "navigation_comment_id": None,
            "overview_comment_id": None,
        }
    )
