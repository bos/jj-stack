from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.commands.land.authority import land_authority_error
from jj_stack.commands.land.command import (
    _resolve_land_merge_method,
    _stack_not_on_trunk_error,
    land,
)
from jj_stack.commands.land.models import LandPlan, LandRevision
from jj_stack.commands.land.plan import (
    _collect_landable_prefix,
    _plan_review_bookmark_cleanup,
    validate_land_plan_merge_method,
)
from jj_stack.config import RepoConfig
from jj_stack.errors import CliError, UsageError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjCliArgs
from jj_stack.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_stack.models.github import GithubBranchRef, GithubPullRequest, GithubRepository
from jj_stack.models.review_state import LinkState, SubmittedBaseline
from jj_stack.review.observation import RepositoryObservation
from jj_stack.review.status import (
    PreparedStatus,
    PullRequestLookup,
    PullRequestLookupState,
    ReviewStatusRevision,
    StatusResult,
)
from jj_stack.ui import plain_text
from tests.support.review_state import make_review_identity


def _fake_context() -> CommandContext:
    return cast(
        CommandContext,
        SimpleNamespace(config=RepoConfig()),
    )


@dataclass(frozen=True, slots=True)
class _ProjectionCase:
    name: str
    expected_message: str | None
    baseline_commit_id: str | None = None
    bypass_readiness: bool = False
    landing_owners: frozenset[Literal["auto_merge", "merge_queue"]] | None = frozenset()
    link_state: LinkState = "active"
    pr_head_commit_id: str | None = None
    pull_request_state: PullRequestLookupState = "open"
    remote_target: str | None = None
    with_pr_head_sha: bool = True
    with_remote_state: bool = True
    with_submitted_baseline: bool = True


@pytest.mark.landing_recovery
def test_land_projection_table_covers_exactness_and_boundary_precedence() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    projection_message = "do not all identify the same exact commit"
    cases = (
        _ProjectionCase("exact", None),
        _ProjectionCase(
            "baseline mismatch",
            projection_message,
            baseline_commit_id="old-commit-1",
        ),
        _ProjectionCase(
            "baseline missing",
            projection_message,
            with_submitted_baseline=False,
        ),
        _ProjectionCase(
            "review branch mismatch",
            projection_message,
            remote_target="old-commit-1",
        ),
        _ProjectionCase(
            "review branch missing",
            projection_message,
            with_remote_state=False,
        ),
        _ProjectionCase(
            "PR head mismatch",
            projection_message,
            pr_head_commit_id="old-commit-1",
        ),
        _ProjectionCase("PR head missing", projection_message, with_pr_head_sha=False),
        _ProjectionCase(
            "unlinked precedence",
            "unlinked from review tracking",
            link_state="unlinked",
            remote_target="old-commit-1",
        ),
        _ProjectionCase(
            "bypass remains exact",
            projection_message,
            bypass_readiness=True,
            remote_target="old-commit-1",
        ),
        _ProjectionCase(
            "missing PR precedence",
            "GitHub no longer reports a pull request",
            pull_request_state="missing",
            remote_target="old-commit-1",
        ),
        _ProjectionCase(
            "unknown landing ownership",
            "Could not verify landing ownership",
            landing_owners=None,
        ),
    )
    for case in cases:
        revision = _status_revision(
            baseline_commit_id=case.baseline_commit_id,
            change_id="change-1",
            commit_id="commit-1",
            link_state=case.link_state,
            pr_head_commit_id=case.pr_head_commit_id,
            pull_request=_pull_request(landing_owners=case.landing_owners, number=1),
            pull_request_state=case.pull_request_state,
            remote_target=case.remote_target,
            review_decision="approved",
            subject="feature 1",
            with_pr_head_sha=case.with_pr_head_sha,
            with_remote_state=case.with_remote_state,
            with_submitted_baseline=case.with_submitted_baseline,
        )
        planned, boundary = _collect_landable_prefix(
            bypass_readiness=case.bypass_readiness,
            path_revisions=((prepared_revision, revision),),
        )

        if case.expected_message is None:
            assert boundary is None, case.name
            assert len(planned) == 1, case.name
            continue
        assert boundary is not None, case.name
        rendered = plain_text(boundary.body)
        assert case.expected_message in rendered, case.name
        if case.expected_message == projection_message:
            assert "jj-stack submit change-1" in rendered, case.name
        else:
            assert projection_message not in rendered, case.name


@pytest.mark.landing_recovery
def test_land_repository_authority_table_covers_repository_and_default_branch_drift() -> None:
    expected_repository = GithubRepoAddress(
        host="github.test",
        owner="acme",
        repo="widgets",
    )
    github_repository = _repository_with_merge_settings(
        allow_merge_commit=True,
        allow_rebase_merge=True,
        allow_squash_merge=True,
    )
    cases = (
        (
            "configured repository",
            GithubRepoAddress(host="github.test", owner="other", repo="widgets"),
            github_repository,
            "the configured Git remote no longer names the planned GitHub repository",
        ),
        (
            "GitHub repository",
            expected_repository,
            github_repository.model_copy(update={"full_name": "other/widgets"}),
            "GitHub no longer reports the planned repository",
        ),
        (
            "default branch",
            expected_repository,
            github_repository.model_copy(update={"default_branch": "release"}),
            "GitHub no longer reports the planned trunk branch as its default",
        ),
    )

    for name, configured_repository, observed_repository, expected_error in cases:
        observation = RepositoryObservation(
            configured_repository=configured_repository,
            duplicate_claim_change_ids=frozenset(),
            fetched_trunk=None,
            github_repository=observed_repository,
            remote=GitRemote(
                name="origin",
                fetch_url="https://github.test/acme/widgets.git",
                push_url="https://github.test/acme/widgets.git",
            ),
            remote_trunk_target=None,
            reviews={},
        )

        error = land_authority_error(
            bypass_readiness=False,
            expected_bases={},
            expected_repository=expected_repository,
            expected_trunk_branch="main",
            expected_trunk_commit_id="trunk-commit",
            observation=observation,
            remote_name="origin",
            revisions=(),
        )

        assert error == expected_error, name


def test_stack_not_on_trunk_error_recommends_rebase_when_no_changes_have_landed() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"), selected_revset="@-")
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-2",
                    commit_id="commit-2",
                    pull_request=_pull_request(number=2),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            ),
            selected_revset="@-",
        ),
    )

    error = _stack_not_on_trunk_error(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    assert plain_text(error.message) == "Selected stack is not based on the current trunk()."
    assert error.hint is not None
    rendered_hint = plain_text(error.hint)
    assert "jj rebase -s change-1 -d 'trunk()'" in rendered_hint


def test_stack_not_on_trunk_error_recommends_sync_when_stack_has_landed_change() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"), selected_revset="@-")
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-2",
                    commit_id="commit-2",
                    pull_request=_pull_request(number=2),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1).model_copy(
                        update={"state": "merged", "merged_at": "2026-03-22T12:00:00Z"}
                    ),
                    pull_request_state="closed",
                    subject="feature 1",
                ),
            ),
            selected_revset="@-",
        ),
    )

    error = _stack_not_on_trunk_error(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    assert plain_text(error.message) == "Selected stack is not based on the current trunk()."
    assert error.hint is not None
    rendered_hint = plain_text(error.hint)
    assert "jj-stack sync --dry-run @-" in rendered_hint
    assert "sync @-" in rendered_hint
    assert "jj rebase -s" not in rendered_hint


def test_plan_review_bookmark_cleanup_forgets_owned_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="bosullivan/feature-aaaaaaaa",
        bookmark_managed=True,
        cleanup_user_bookmarks=False,
        prefix="bosullivan",
        bookmark_state=BookmarkState(
            name="bosullivan/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        commit_id="commit-1",
    )

    assert plan is not None
    assert plain_text(plan.body) == "forget bosullivan/feature-aaaaaaaa"
    assert plan.status == "planned"


def test_plan_review_bookmark_cleanup_skips_external_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_managed=False,
        cleanup_user_bookmarks=False,
        prefix="review",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        commit_id="commit-1",
    )

    assert plan is None


def test_plan_review_bookmark_cleanup_forgets_external_bookmark_when_configured() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="potato/feature-aaaaaaaa",
        bookmark_managed=False,
        cleanup_user_bookmarks=True,
        prefix="review",
        bookmark_state=BookmarkState(
            name="potato/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.status == "planned"


def test_plan_review_bookmark_cleanup_blocks_conflicted_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_managed=True,
        cleanup_user_bookmarks=False,
        prefix="review",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1", "commit-2"),
        ),
        commit_id="commit-1",
    )

    assert plan is not None
    assert "is conflicted" in plain_text(plan.body)
    assert plan.status == "blocked"


def test_plan_review_bookmark_cleanup_blocks_moved_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_managed=True,
        cleanup_user_bookmarks=False,
        prefix="review",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-2",),
        ),
        commit_id="commit-1",
    )

    assert plan is not None
    assert "points to a different revision" in plain_text(plan.body)
    assert plan.status == "blocked"


def _status_revision(
    *,
    baseline_commit_id: str | None = None,
    change_id: str,
    commit_id: str,
    pr_head_commit_id: str | None = None,
    remote_target: str | None = None,
    with_remote_state: bool = True,
    pull_request: GithubPullRequest,
    pull_request_state: PullRequestLookupState,
    review_decision: str | None = None,
    review_decision_error: str | None = None,
    subject: str,
    link_state: LinkState = "active",
    with_pr_head_sha: bool = True,
    with_submitted_baseline: bool = True,
) -> ReviewStatusRevision:
    bookmark = f"review/{change_id}"
    resolved_pull_request = pull_request.model_copy(
        update={
            "head": pull_request.head.model_copy(
                update={
                    "label": f"octocat:{bookmark}",
                    "ref": bookmark,
                    "sha": (pr_head_commit_id or commit_id) if with_pr_head_sha else None,
                }
            )
        }
    )
    return ReviewStatusRevision(
        bookmark=bookmark,
        bookmark_source="generated",
        change_id=change_id,
        commit_id=commit_id,
        local_divergent=False,
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=resolved_pull_request,
            review_decision=review_decision,
            review_decision_error=review_decision_error,
            state=pull_request_state,
        ),
        remote_state=(
            RemoteBookmarkState(
                remote="origin",
                targets=((remote_target,) if remote_target is not None else (commit_id,)),
            )
            if with_remote_state
            else None
        ),
        review_identity=make_review_identity(
            head_ref=bookmark,
            head_owner="octocat",
            link_state=link_state,
            pr_number=pull_request.number,
        ),
        submitted_baseline=(
            SubmittedBaseline(commit_id=baseline_commit_id or commit_id)
            if with_submitted_baseline
            else None
        ),
        managed_comments_lookup=None,
        subject=subject,
    )


def _pull_request(
    *,
    number: int,
    state: str = "open",
    draft: bool = False,
    landing_owners: frozenset[Literal["auto_merge", "merge_queue"]] | None = frozenset(),
) -> GithubPullRequest:
    merged_at = "2026-03-22T12:00:00Z" if state == "merged" else None
    pr_state = "closed" if state == "merged" else state
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        draft=draft,
        head=GithubBranchRef(ref=f"review/{number}"),
        html_url=f"https://github.test/octo-org/stacked-review/pull/{number}",
        landing_owners=landing_owners,
        merged_at=merged_at,
        number=number,
        state=pr_state,
        title=f"feature {number}",
    )


def _prepared_status(
    change_ids: tuple[str, ...],
    *,
    commit_ids: tuple[str, ...] | None = None,
    selected_revset: str = "@-",
) -> PreparedStatus:
    resolved_commit_ids = commit_ids or tuple(
        f"commit-{index + 1}" for index, _change_id in enumerate(change_ids)
    )
    status_revisions = tuple(
        SimpleNamespace(
            revision=SimpleNamespace(
                change_id=change_id,
                commit_id=commit_id,
                conflict=False,
            )
        )
        for change_id, commit_id in zip(change_ids, resolved_commit_ids, strict=True)
    )
    return cast(
        PreparedStatus,
        SimpleNamespace(
            github_inspection_count=lambda *, discover_remote_review=False: 0,
            prepared=SimpleNamespace(
                stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                status_revisions=status_revisions,
            ),
            selected_revset=selected_revset,
        ),
    )


def _repository_with_merge_settings(
    *,
    allow_merge_commit: bool | None,
    allow_rebase_merge: bool | None,
    allow_squash_merge: bool | None,
) -> GithubRepository:
    return GithubRepository(
        allow_merge_commit=allow_merge_commit,
        allow_rebase_merge=allow_rebase_merge,
        allow_squash_merge=allow_squash_merge,
        clone_url="https://github.test/acme/widgets.git",
        default_branch="main",
        full_name="acme/widgets",
        html_url="https://github.test/acme/widgets",
        name="widgets",
        private=True,
        url="https://api.github.test/repos/acme/widgets",
    )


def test_land_merge_method_requires_via_merge_before_bootstrap() -> None:
    with pytest.raises(UsageError, match="--merge-method.*--via merge"):
        land(
            bypass_readiness=False,
            cli_args=JjCliArgs(),
            debug=False,
            dry_run=False,
            merge_method="squash",
            pull_request=None,
            repository=None,
            revset=None,
            skip_cleanup=False,
            via="push",
        )


def test_resolve_land_merge_method_uses_the_only_allowed_method() -> None:
    repository = _repository_with_merge_settings(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    assert _resolve_land_merge_method(merge_method=None, repository_state=repository) == "squash"


def test_resolve_land_merge_method_requires_choice_when_several_are_allowed() -> None:
    repository = _repository_with_merge_settings(
        allow_merge_commit=False,
        allow_rebase_merge=True,
        allow_squash_merge=True,
    )

    with pytest.raises(CliError, match="more than one merge method"):
        _resolve_land_merge_method(merge_method=None, repository_state=repository)


def test_resolve_land_merge_method_requires_flag_when_settings_are_unknown() -> None:
    repository = _repository_with_merge_settings(
        allow_merge_commit=None,
        allow_rebase_merge=None,
        allow_squash_merge=None,
    )

    with pytest.raises(CliError, match="did not report which merge methods"):
        _resolve_land_merge_method(merge_method=None, repository_state=repository)


def test_resolve_land_merge_method_rejects_repo_that_allows_no_method() -> None:
    repository = _repository_with_merge_settings(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=False,
    )

    with pytest.raises(CliError, match="does not allow any pull request merge method"):
        _resolve_land_merge_method(merge_method=None, repository_state=repository)


def test_land_plan_rejects_rebase_merge_for_multi_pr_prefix() -> None:
    revisions = tuple(
        LandRevision(
            base_ref="main",
            change_id=f"change-{number}",
            commit_id=f"commit-{number}",
            identity=make_review_identity(
                head_ref=f"review/feature-{number}",
                pr_number=number,
            ),
            subject=f"feature {number}",
        )
        for number in (1, 2)
    )
    plan = LandPlan(
        blocked=False,
        boundary_action=None,
        planned_revisions=revisions,
        trunk_branch="main",
        via="merge",
    )

    with pytest.raises(CliError, match="rebase merge cannot land more than one PR"):
        validate_land_plan_merge_method(merge_method="rebase", plan=plan)
