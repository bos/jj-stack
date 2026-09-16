from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx2
import pytest

from jj_stack.commands.merge.plan import MergeChange, MergeExecutionInputs
from jj_stack.commands.merge.wait import wait_for_merge
from jj_stack.console import configured_console
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead, GithubStackMerge
from jj_stack.models.github_details import GithubMergeQueueEntry
from jj_stack.models.tracking import PRIdentity
from jj_stack.ui import plain_text

# The fake GitHub server drives ordinary queue outcomes through the CLI. These snapshots cover
# what it cannot produce: a PR whose exit GitHub never explains, a head rewritten mid-wait, and
# the wait ending on an exception.
pytestmark = pytest.mark.merge_recovery


def _queued_pr(number: int) -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubPRHead(ref=f"feature-{number}", sha=f"sha-{number}"),
        html_url=f"https://github.test/acme/repo/pull/{number}",
        node_id=f"PR_{number}",
        number=number,
        state="open",
        title="feature",
        merge_queue_entry=GithubMergeQueueEntry(id=f"queue-{number}"),
    )


async def _wait(snapshots: Iterator[tuple[GithubPR, ...] | BaseException]):
    class ObservedClient(GithubClient):
        async def get_prs_by_numbers(
            self, *, pr_numbers, merge_progress=False
        ) -> dict[int, GithubPR | None]:
            snapshot = next(snapshots)
            if isinstance(snapshot, BaseException):
                raise snapshot
            return {pr.number: pr for pr in snapshot}

    repo = GithubRepoAddress(owner="acme", repo="repo")
    changes = tuple(
        MergeChange(
            base_ref="main",
            change_id=ChangeId(f"change-{n}"),
            commit_id=CommitId(f"sha-{n}"),
            identity=PRIdentity(pr_number=n, head_ref=f"feature-{n}"),
        )
        for n in (1, 2)
    )
    with configured_console(color="never"):
        async with ObservedClient(httpx2.AsyncClient(), repo=repo) as client:
            return await wait_for_merge(
                client,
                GithubStackMerge.model_validate({"status": "enqueued", "details": {}}),
                changes,
                MergeExecutionInputs(
                    repo=repo,
                    selected_head="target",
                    sync_head="stackhead",
                    trunk_branch="main",
                    trunk_subject="trunk",
                ),
            )


def test_queue_wait_gives_up_on_a_pr_that_stays_unqueued_without_a_recorded_reason(monkeypatch):
    monkeypatch.setattr("jj_stack.commands.merge.wait._QUEUE_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("jj_stack.commands.merge.wait._UNEXPLAINED_POLLS", 2)
    bottom, top = _queued_pr(1), _queued_pr(2)
    leaving = (bottom.model_copy(update={"merge_queue_entry": None}), top)
    with pytest.raises(CliError, match="has not recorded why") as caught:
        asyncio.run(_wait(iter([leaving, leaving, leaving])))
    assert "jj-stack merge target" in plain_text(caught.value.hint or "")


def test_queue_wait_stops_if_a_submitted_head_changes():
    moved = _queued_pr(2).model_copy(update={"head": GithubPRHead(ref="feature-2", sha="new")})
    with pytest.raises(CliError, match="changed or became unavailable"):
        asyncio.run(_wait(iter([(_queued_pr(1), moved)])))


def test_wait_ends_early_without_cancelling(capsys):
    with pytest.raises(CliError) as caught:
        asyncio.run(_wait(iter([GithubClientError("connection lost")])))
    assert "request may still complete" in str(caught.value)
    assert "jj-stack sync stackhead" in plain_text(caught.value.hint or "")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_wait(iter([asyncio.CancelledError()])))
    output = " ".join(capsys.readouterr().err.split())
    assert "not cancelled" in output
    assert "jj-stack sync stackhead" in output
