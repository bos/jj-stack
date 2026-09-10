from __future__ import annotations

import pytest

from jj_stack.commands.merge.command import _resolve_merge_method
from jj_stack.commands.merge.models import MergeChange
from jj_stack.commands.merge.preconditions import merge_precondition_error
from jj_stack.errors import CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead, GithubRepo
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.change_state import TrackedPRObservation
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.ui import plain_text
from tests.support.change_helpers import make_change


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


@pytest.mark.merge_recovery
@pytest.mark.parametrize(
    ("rebase", "squash", "expected"),
    ((True, True, "rebase"), (False, True, "squash")),
)
def test_resolve_merge_method_prefers_rebase_then_squash_then_merge(
    rebase: bool,
    squash: bool,
    expected: str,
) -> None:
    repo = _repo(
        allow_merge_commit=True,
        allow_rebase_merge=rebase,
        allow_squash_merge=squash,
    )

    assert (
        _resolve_merge_method(changes=(), configured=None, merge_method=None, repo_state=repo)
        == expected
    )


@pytest.mark.merge_recovery
def test_signed_stack_uses_the_only_allowed_method() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )
    signed = make_change(
        commit_id="submitted", change_id="signed-change", description="feature\n"
    ).model_copy(update={"signed": True})

    assert (
        _resolve_merge_method(
            changes=(signed,), configured=None, merge_method=None, repo_state=repo
        )
        == "squash"
    )


@pytest.mark.merge_recovery
def test_resolve_merge_method_rejects_a_repo_with_no_allowed_method() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=False,
    )

    with pytest.raises(CliError, match="does not allow any pull request merge method"):
        _resolve_merge_method(changes=(), configured=None, merge_method=None, repo_state=repo)


@pytest.mark.merge_recovery
def test_unreported_merge_methods_require_an_explicit_choice() -> None:
    repo = _repo(
        allow_merge_commit=None,
        allow_rebase_merge=None,
        allow_squash_merge=None,
    )

    with pytest.raises(CliError, match="did not report which merge methods"):
        _resolve_merge_method(changes=(), configured=None, merge_method=None, repo_state=repo)

    assert (
        _resolve_merge_method(changes=(), configured="squash", merge_method=None, repo_state=repo)
        == "squash"
    )


@pytest.mark.merge_recovery
def test_resolve_merge_method_rejects_a_method_the_repo_disallows() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    with pytest.raises(CliError, match="does not allow"):
        _resolve_merge_method(changes=(), configured="rebase", merge_method=None, repo_state=repo)


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
        prs_by_base={},
        rewrite_args=JjCliArgs(),
        remote=GitRemote(
            name="origin",
            fetch_url="https://github.test/acme/widgets.git",
            push_url="https://github.test/acme/widgets.git",
        ),
        repo=expected_repo,
        prs={},
    )

    error = merge_precondition_error(
        expected_repo=expected_repo,
        expected_trunk_branch="main",
        observation=observation,
        remote_name="origin",
        change=MergeChange(
            base_ref="main",
            change_id=ChangeId("a" * 32),
            commit_id=CommitId("c" * 40),
            identity=PRIdentity(pr_number=1, head_ref="jj-stack/feature-aaaaaaaa"),
        ),
    )

    assert error is not None
    assert error.reason == "the configured Git remote no longer names the planned GitHub repo"
    assert error.recovery == "inspect"


@pytest.mark.merge_recovery
def test_merge_preconditions_name_a_closed_pull_request() -> None:
    """A closed pull request is reported as closed, not as unspecified drift."""

    repo = GithubRepoAddress(owner="acme", repo="widgets")
    remote = GitRemote(
        name="origin",
        fetch_url="https://github.test/acme/widgets.git",
        push_url="https://github.test/acme/widgets.git",
    )
    identity = PRIdentity(pr_number=1, head_ref="jj-stack/feature-abcdefgh")
    change = make_change(change_id="abcdefghijkl", commit_id="submitted", description="feature\n")
    closed_pr = GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubPRHead(ref=identity.head_ref, sha=change.commit_id),
        html_url="https://github.test/acme/widgets/pull/1",
        node_id="PR_1",
        number=1,
        state="closed",
        title="feature",
    )
    observation = RepoFacts(
        configured_repo=repo,
        github_repo=_repo(
            allow_merge_commit=False, allow_rebase_merge=False, allow_squash_merge=True
        ),
        prs_by_base={},
        rewrite_args=JjCliArgs(),
        remote=remote,
        repo=repo,
        prs={
            change.change_id: TrackedPRObservation(
                change_id=change.change_id,
                branch=identity.head_ref,
                remote_name=remote.name,
                open_prs_on_branch=(),
                local=(change,),
                pr=closed_pr,
                remote_target=change.commit_id,
                tracked=TrackedPR(
                    pr_identity=identity,
                    submitted_baseline=SubmittedBaseline(commit_id=change.commit_id),
                ),
            )
        },
    )

    error = merge_precondition_error(
        expected_repo=repo,
        expected_trunk_branch="main",
        observation=observation,
        remote_name="origin",
        change=MergeChange(
            base_ref="main",
            change_id=change.change_id,
            commit_id=change.commit_id,
            identity=identity,
        ),
    )

    assert error is not None
    assert "#1 is closed" in plain_text(error.reason)
    assert error.recovery == "inspect"
