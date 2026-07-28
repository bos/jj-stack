from __future__ import annotations

import pytest

from jj_stack.cli import build_parser, main
from jj_stack.commands.merge.command import _resolve_merge_method
from jj_stack.commands.merge.models import MergePlan, MergeRevision
from jj_stack.commands.merge.plan import validate_merge_plan_method
from jj_stack.commands.merge.preconditions import merge_precondition_error
from jj_stack.errors import EXIT_USAGE, CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepository
from jj_stack.review.observation import RepositoryObservation
from tests.support.review_state import make_review_identity


def _repository(
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


def test_command_surface_has_merge_without_land_or_transport_flags(capsys) -> None:
    parser = build_parser()

    args = parser.parse_args(["merge", "--dry-run", "--merge-method", "squash"])
    assert args.command == "merge"
    assert args.dry_run is True
    assert args.merge_method == "squash"

    assert main(["land"]) == EXIT_USAGE
    assert "Unknown command land" in capsys.readouterr().err
    with pytest.raises(CliError):
        parser.parse_args(["merge", "--via", "push"])
    with pytest.raises(CliError):
        parser.parse_args(["merge", "--bypass-readiness"])
    with pytest.raises(CliError):
        parser.parse_args(["merge", "--skip-cleanup"])


@pytest.mark.landing_recovery
def test_resolve_merge_method_uses_the_only_allowed_method() -> None:
    repository = _repository(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    assert _resolve_merge_method(merge_method=None, repository_state=repository) == "squash"


@pytest.mark.landing_recovery
@pytest.mark.parametrize(
    ("repository", "message"),
    (
        (
            _repository(
                allow_merge_commit=True,
                allow_rebase_merge=True,
                allow_squash_merge=False,
            ),
            "more than one merge method",
        ),
        (
            _repository(
                allow_merge_commit=None,
                allow_rebase_merge=None,
                allow_squash_merge=None,
            ),
            "did not report which merge methods",
        ),
        (
            _repository(
                allow_merge_commit=False,
                allow_rebase_merge=False,
                allow_squash_merge=False,
            ),
            "does not allow any pull request merge method",
        ),
    ),
)
def test_resolve_merge_method_rejects_ambiguous_or_absent_settings(
    repository: GithubRepository,
    message: str,
) -> None:
    with pytest.raises(CliError, match=message):
        _resolve_merge_method(merge_method=None, repository_state=repository)


@pytest.mark.landing_recovery
def test_merge_plan_rejects_rebase_for_a_multi_pr_prefix() -> None:
    revisions = tuple(
        MergeRevision(
            base_ref="main",
            change_id=f"change-{number}",
            commit_id=f"commit-{number}",
            identity=make_review_identity(
                head_ref=f"jj-stack/feature-{number}",
                pr_number=number,
            ),
            subject=f"feature {number}",
        )
        for number in (1, 2)
    )
    plan = MergePlan(
        blocked=False,
        boundary_action=None,
        planned_revisions=revisions,
        reviewed_revisions=revisions,
        trunk_branch="main",
    )

    with pytest.raises(CliError, match="rebase merge cannot merge more than one ordinary PR"):
        validate_merge_plan_method(merge_method="rebase", plan=plan)


@pytest.mark.landing_recovery
def test_merge_preconditions_reject_repository_drift() -> None:
    expected_repository = GithubRepoAddress(
        owner="acme",
        repo="widgets",
    )
    observation = RepositoryObservation(
        configured_repository=GithubRepoAddress(
            owner="other",
            repo="widgets",
        ),
        duplicate_claim_change_ids=frozenset(),
        fetched_trunk_commit_id=None,
        github_repository=_repository(
            allow_merge_commit=False,
            allow_rebase_merge=False,
            allow_squash_merge=True,
        ),
        open_pull_requests_by_base=None,
        remote=GitRemote(
            name="origin",
            fetch_url="https://github.test/acme/widgets.git",
            push_url="https://github.test/acme/widgets.git",
        ),
        remote_trunk_target="trunk-commit",
        repository=expected_repository,
        reviews={},
    )

    assert (
        merge_precondition_error(
            expected_bases={},
            expected_repository=expected_repository,
            expected_trunk_branch="main",
            expected_trunk_commit_id="trunk-commit",
            observation=observation,
            remote_name="origin",
            revisions=(),
        )
        == "the configured Git remote no longer names the planned GitHub repository"
    )
