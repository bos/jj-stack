from __future__ import annotations

import pytest

from jj_stack.cli import build_parser, main
from jj_stack.commands.merge.command import _resolve_merge_method
from jj_stack.commands.merge.preconditions import merge_precondition_error
from jj_stack.errors import EXIT_USAGE, CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubRepo
from jj_stack.stack.pr_facts import RepoFacts


def _repo(
    *,
    allow_merge_commit: bool | None,
    allow_rebase_merge: bool | None,
    allow_squash_merge: bool | None,
) -> GithubRepo:
    return GithubRepo(
        allow_merge_commit=allow_merge_commit,
        allow_rebase_merge=allow_rebase_merge,
        allow_squash_merge=allow_squash_merge,
        default_branch="main",
        full_name="acme/widgets",
    )


def test_command_surface_has_merge_without_land_or_transport_flags(capsys) -> None:
    parser = build_parser()

    args = parser.parse_args(["merge", "--dry-run", "--method", "squash"])
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


@pytest.mark.merge_recovery
def test_resolve_merge_method_uses_the_only_allowed_method() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    assert _resolve_merge_method(configured=None, merge_method=None, repo_state=repo) == "squash"


@pytest.mark.merge_recovery
@pytest.mark.parametrize(
    ("repo", "message"),
    (
        (
            _repo(
                allow_merge_commit=True,
                allow_rebase_merge=True,
                allow_squash_merge=False,
            ),
            "more than one merge method",
        ),
        (
            _repo(
                allow_merge_commit=None,
                allow_rebase_merge=None,
                allow_squash_merge=None,
            ),
            "did not report which merge methods",
        ),
        (
            _repo(
                allow_merge_commit=False,
                allow_rebase_merge=False,
                allow_squash_merge=False,
            ),
            "does not allow any pull request merge method",
        ),
    ),
)
def test_resolve_merge_method_rejects_ambiguous_or_absent_settings(
    repo: GithubRepo,
    message: str,
) -> None:
    with pytest.raises(CliError, match=message):
        _resolve_merge_method(configured=None, merge_method=None, repo_state=repo)


@pytest.mark.merge_recovery
def test_resolve_merge_method_prefers_the_flag_over_configuration() -> None:
    """A repo allowing several methods is the normal case, so config has to settle it.

    GitHub reports which methods it allows but never which to prefer, so without a configured
    default every merge in such a repo needs the flag typed out.
    """

    repo = _repo(
        allow_merge_commit=True,
        allow_rebase_merge=True,
        allow_squash_merge=True,
    )

    assert (
        _resolve_merge_method(configured="squash", merge_method=None, repo_state=repo) == "squash"
    )
    assert (
        _resolve_merge_method(configured="squash", merge_method="merge", repo_state=repo)
        == "merge"
    )


@pytest.mark.merge_recovery
def test_resolve_merge_method_rejects_a_method_the_repo_disallows() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    with pytest.raises(CliError, match="does not allow"):
        _resolve_merge_method(configured="rebase", merge_method=None, repo_state=repo)


@pytest.mark.merge_recovery
def test_merge_preconditions_reject_repo_drift() -> None:
    expected_repo = GithubRepoAddress(
        owner="acme",
        repo="widgets",
    )
    observation = RepoFacts(
        configured_repo=GithubRepoAddress(
            owner="other",
            repo="widgets",
        ),
        github_repo=_repo(
            allow_merge_commit=False,
            allow_rebase_merge=False,
            allow_squash_merge=True,
        ),
        open_prs_by_base=None,
        remote=GitRemote(
            name="origin",
            fetch_url="https://github.test/acme/widgets.git",
            push_url="https://github.test/acme/widgets.git",
        ),
        repo=expected_repo,
        prs={},
    )

    assert (
        merge_precondition_error(
            expected_repo=expected_repo,
            expected_trunk_branch="main",
            observation=observation,
            remote_name="origin",
            changes=(),
        )
        == "the configured Git remote no longer names the planned GitHub repo"
    )
