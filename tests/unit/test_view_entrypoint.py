import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, Mock

import pytest

import jj_stack.commands.view as view_module
import jj_stack.console as console_module
from jj_stack.errors import EXIT_INCOMPLETE, CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.stack.preparation import PreparedLocalStack
from tests.support.change_helpers import make_change
from tests.support.contexts import fake_command_context
from tests.support.tracking import make_pr_identity


def test_change_id_selector_distinguishes_change_ids_from_revsets_and_bookmarks() -> None:
    bare_change_id = "klmnopqrstuv"
    bookmark = "zzzzzzzzzzzzzzzz"

    class JjClientStub:
        def resolve_commit(self, value: str) -> SimpleNamespace:
            change_id = "klmnopqrstuvwxyz" if value == bare_change_id else "otherchangeid"
            return SimpleNamespace(change_id=change_id)

    context = fake_command_context(jj_client=cast(JjClient, JjClientStub()))

    assert (
        view_module._change_id_selector(context=context, value=bare_change_id) == bare_change_id
    )
    assert view_module._change_id_selector(context=context, value=bookmark) is None
    assert (
        view_module._change_id_selector(
            context=context,
            value=f'change_id("{bare_change_id}")',
        )
        is None
    )


def test_view_shares_pr_observation_without_losing_selector_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = fake_command_context(tmp_path)
    trunk, parent, left, right = (
        make_change(commit_id=name, change_id=f"{name}-change", description=name)
        for name in ("trunk", "parent", "left", "right")
    )
    prs = tuple(
        GithubPR(
            base=GithubBranchRef(ref="main"),
            head=GithubPRHead(ref=f"jj-stack/{change.subject}", sha=change.commit_id),
            html_url=f"https://github.com/octo-org/stacked-prs/pull/{number}",
            node_id=f"PR_{number}",
            number=number,
            state="closed" if number == 1 else "open",
            title=change.subject,
        )
        for number, change in enumerate((parent, left, right), start=1)
    )
    state = TrackingState(
        prs={
            change.change_id: TrackedPR(
                pr_identity=make_pr_identity(head_ref=pr.head.ref, pr_number=pr.number),
                submitted_baseline=SubmittedBaseline(commit_id=change.commit_id),
            )
            for change, pr in zip((parent, left, right), prs, strict=True)
        }
    )

    def prepare_stack(*, revset, **_kwargs):
        if revset == "bad":
            raise CliError("bad selector")
        head = left if revset in {"left", "duplicate"} else right
        return PreparedLocalStack(
            client=context.jj_client,
            github_target=_GITHUB_TARGET,
            stack=LocalStack(
                base_parent=trunk,
                head=head,
                changes=(parent, head),
                selected_revset=revset,
                trunk=trunk,
            ),
            state=state,
        )

    github = MagicMock(spec=GithubClient)
    github.__aenter__.return_value = github
    github.get_open_prs_by_head_refs.return_value = {
        prs[0].head.ref: (),
        prs[1].head.ref: (prs[1],),
        prs[2].head.ref: (prs[2],),
    }
    github.get_prs_by_numbers.return_value = {1: prs[0]}
    monkeypatch.setattr(view_module, "bootstrap_context", lambda **_kwargs: context)
    monkeypatch.setattr(view_module, "prepare_local_stack", prepare_stack)
    monkeypatch.setattr("jj_stack.stack.status.build_github_client", lambda **_kwargs: github)

    stdout = StringIO()
    stderr = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=stderr, color="never"):
        exit_code = view_module.view(
            as_json=True,
            cli_args=JjCliArgs(),
            debug=False,
            repo=tmp_path,
            selectors=tuple(
                view_module.ViewSelector(kind="revset", value=value)
                for value in ("left", "bad", "duplicate", "right")
            ),
            verbose=False,
        )

    assert exit_code == EXIT_INCOMPLETE
    stacks = json.loads(stdout.getvalue())["stacks"]
    assert [stack["selector"] for stack in stacks] == ["left", "right"]
    assert [
        [
            (change["change_id"], change["pr"]["number"], change["status"])
            for change in stack["changes"]
        ]
        for stack in stacks
    ] == [
        [(left.change_id, 2, "open"), (parent.change_id, 1, "closed")],
        [(right.change_id, 3, "open"), (parent.change_id, 1, "closed")],
    ]
    assert "Error: bad selector" in stderr.getvalue()
    github.get_open_prs_by_head_refs.assert_awaited_once()
    assert set(github.get_open_prs_by_head_refs.await_args.kwargs["head_refs"]) == {
        pr.head.ref for pr in prs
    }
    github.get_prs_by_numbers.assert_awaited_once_with(pr_numbers=(1,))


def test_view_keeps_selector_errors_between_their_neighboring_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jj_client = Mock(spec=JjClient)
    jj_client.render_commit_log_blocks.side_effect = lambda changes, **_kwargs: {
        change.commit_id: (change.commit_id,) for change in changes
    }
    context = fake_command_context(tmp_path, jj_client=jj_client)
    trunk = make_change(commit_id="trunk", change_id="trunk-change", description="base")

    def prepare_stack(*, revset, **_kwargs):
        if revset == "bad":
            raise CliError("bad selector", hint="Choose a visible change.")
        change = make_change(
            commit_id=f"{revset}-commit", change_id=f"{revset}-change", description=revset
        )
        return PreparedLocalStack(
            client=jj_client,
            github_target=_GITHUB_TARGET,
            stack=LocalStack(
                base_parent=trunk,
                head=change,
                changes=(change,),
                selected_revset=revset,
                trunk=trunk,
            ),
            state=TrackingState(),
        )

    monkeypatch.setattr(view_module, "bootstrap_context", lambda **_kwargs: context)
    monkeypatch.setattr(view_module, "prepare_local_stack", prepare_stack)

    output = StringIO()
    with console_module.configured_console(stdout=output, stderr=output, color="never"):
        exit_code = view_module.view(
            as_json=False,
            cli_args=JjCliArgs(),
            debug=False,
            repo=tmp_path,
            selectors=tuple(
                view_module.ViewSelector(kind="revset", value=value)
                for value in ("good", "bad", "later")
            ),
            verbose=False,
        )

    assert exit_code == EXIT_INCOMPLETE
    text = output.getvalue()
    assert (
        text.index("good-commit")
        < text.index("Status for bad:")
        < text.index("Error: bad selector")
        < text.index("Hint: Choose a visible change.")
        < text.index("later-commit")
    )


_GITHUB_TARGET = GithubTarget(
    remote=GitRemote(
        name="origin",
        fetch_url="git@github.com:octo-org/stacked-prs.git",
        push_url="git@github.com:octo-org/stacked-prs.git",
    ),
    repo=GithubRepoAddress(owner="octo-org", repo="stacked-prs"),
)
