"""Failures after a real external effect, for generated submit recovery tests."""

import pytest

import jj_stack.commands.submit.command as submit_command
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError

from .fake_github import FakeGithubState, create_app
from .integration_helpers import patch_github_client_builders


def install_submit_fault(
    monkeypatch: pytest.MonkeyPatch,
    fake_repo,
    point: str,
    target_title: str,
) -> None:
    if point == "after_remote_push":
        _install_remote_push_fault(monkeypatch)
        return

    app = create_app(FakeGithubState.single_repo(fake_repo))
    failed = False

    class FaultingGithubClient(GithubClient):
        async def create_pr(self, *, base, body, draft=False, head, title):
            nonlocal failed
            pr = await super().create_pr(
                base=base,
                body=body,
                draft=draft,
                head=head,
                title=title,
            )
            if not failed and point == "create_pr" and title == target_title:
                failed = True
                raise GithubClientError(
                    "Simulated pull request creation failure",
                    status_code=500,
                )
            return pr

        async def update_pr(
            self,
            *,
            pr_number,
            base=None,
            body=None,
            title=None,
        ):
            nonlocal failed
            pr = await super().update_pr(
                pr_number=pr_number,
                base=base,
                body=body,
                title=title,
            )
            if not failed and point == "update_pr" and pr.title == target_title:
                failed = True
                raise GithubClientError("Simulated pull request update failure", status_code=500)
            return pr

    patch_github_client_builders(
        monkeypatch,
        app=app,
        fake_repo=fake_repo,
        client_type=FaultingGithubClient,
    )


def _install_remote_push_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = False
    original_mutate_pr_branch_refs = submit_command.JjClient.mutate_remote_pr_branch_refs

    def mutate_pr_branch_refs_then_fail(self, *, remote, updates) -> None:
        nonlocal failed
        original_mutate_pr_branch_refs(
            self,
            remote=remote,
            updates=updates,
        )
        if not failed:
            failed = True
            raise CliError("Simulated failure after remote branch push")

    monkeypatch.setattr(
        submit_command.JjClient,
        "mutate_remote_pr_branch_refs",
        mutate_pr_branch_refs_then_fail,
    )
