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
        merge_queue_entry=GithubMergeQueueEntry.model_validate(
            {
                "id": f"queue-{number}",
                "position": number + 2,
                "state": "AWAITING_CHECKS",
                "estimatedTimeToMerge": 120,
                "mergeQueue": {"entries": {"totalCount": 8}},
                "headCommit": {
                    "statusCheckRollup": {
                        "contexts": {
                            "totalCount": 4,
                            "checkRunCountsByState": [
                                {"state": "IN_PROGRESS", "count": 1},
                                {"state": "SUCCESS", "count": 2},
                            ],
                            "statusContextCountsByState": [{"state": "EXPECTED", "count": 1}],
                        }
                    }
                },
            }
        ),
    )


def _merged(pr: GithubPR) -> GithubPR:
    return pr.model_copy(
        update={
            "state": "merged",
            "merge_queue_entry": None,
            "merge_commit_sha": "landed",
        }
    )


def _removed(pr: GithubPR) -> GithubPR:
    return pr.model_copy(
        update={
            "merge_queue_entry": None,
            "queue_removal_reason": "failed_checks",
            "queue_test_commit": "abc123def4567890",
        }
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


def test_queue_wait_follows_every_selected_pr_through_separate_merge_groups(monkeypatch, capsys):
    bottom, top = _queued_pr(1), _queued_pr(2)
    # GitHub drops the queue entry before it records the merge; neither state is a removal.
    leaving = bottom.model_copy(update={"merge_queue_entry": None})
    recorded = leaving.model_copy(update={"queue_removal_reason": "merged"})
    monkeypatch.setattr("jj_stack.commands.merge.wait._QUEUE_POLL_INTERVAL_SECONDS", 0)
    result = asyncio.run(
        _wait(
            iter(
                [
                    (bottom, top),
                    (leaving, top),
                    (recorded, top),
                    (_merged(bottom), top),
                    (_merged(bottom), _merged(top)),
                ]
            )
        )
    )
    assert result.status == "merged"
    assert result.details.sha == "landed"
    output = " ".join(capsys.readouterr().err.split())
    assert "queue position 3 of 8" in output
    assert "2/4 checks remaining" in output
    assert "PR #1: merged" in output and "PR #2: merged" in output


def test_queue_removal_names_the_test_commit_and_the_next_step():
    bottom, top = _queued_pr(1), _queued_pr(2)
    unqueued_top = top.model_copy(update={"merge_queue_entry": None})
    with pytest.raises(CliError) as caught:
        asyncio.run(_wait(iter([(_removed(bottom), unqueued_top)])))
    assert "removed PR #1 from the merge queue: failed checks" in str(caught.value)
    hint = plain_text(caught.value.hint or "")
    assert "https://github.com/acme/repo/commit/abc123def4567890/checks" in hint
    assert "jj-stack merge target" in hint and "sync" not in hint

    with pytest.raises(CliError) as caught:
        asyncio.run(_wait(iter([(_merged(bottom), _removed(top))])))
    hint = plain_text(caught.value.hint or "")
    assert "PR #1 already merged" in hint
    assert "jj-stack sync stackhead" in hint and "jj-stack merge" not in hint


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


def test_wait_ends_early_without_cancelling_and_can_resume(capsys):
    with pytest.raises(CliError) as caught:
        asyncio.run(_wait(iter([GithubClientError("connection lost")])))
    assert "request may still complete" in str(caught.value)
    assert "jj-stack sync stackhead" in plain_text(caught.value.hint or "")

    snapshots = iter([asyncio.CancelledError(), (_merged(_queued_pr(1)), _merged(_queued_pr(2)))])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_wait(snapshots))
    output = " ".join(capsys.readouterr().err.split())
    assert "not cancelled" in output
    assert "jj-stack sync stackhead" in output
    assert asyncio.run(_wait(snapshots)).status == "merged"
