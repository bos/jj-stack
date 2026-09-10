from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest

from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import wait_for_read_tasks
from jj_stack.config import AppConfig
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.jj.client import JjClient
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.pr_facts import observe_prs
from jj_stack.state.store import TrackingStore


@pytest.mark.parametrize("failure", ("request", "outer_batch", "cancellation"))
def test_failed_observation_finishes_reads_and_local_worker_before_returning(
    failure: Literal["request", "outer_batch", "cancellation"],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_local = threading.Event()
    local_finished = threading.Event()
    jj_client = Mock(spec=JjClient)
    jj_client.list_git_remotes.return_value = []
    context = CommandContext(
        config=AppConfig(),
        jj_client=jj_client,
        repo_root=tmp_path,
        state_store=TrackingStore(tmp_path / "state.json"),
    )
    github = Mock(spec=GithubClient)
    github.repo = GithubRepoAddress(owner="owner", repo="repo")

    async def run_case() -> None:
        loop = asyncio.get_running_loop()
        local_started = asyncio.Event()
        repo_started = asyncio.Event()
        repo_finished = asyncio.Event()

        def observe_local(**_kwargs) -> None:
            loop.call_soon_threadsafe(local_started.set)
            try:
                assert release_local.wait(timeout=5)
                raise RuntimeError("local observation failed too")
            finally:
                local_finished.set()

        async def get_repo() -> None:
            repo_started.set()
            try:
                await asyncio.Future()
            finally:
                repo_finished.set()

        async def fail_when_started(**_kwargs) -> None:
            await local_started.wait()
            await repo_started.wait()
            raise GithubClientError("request failed")

        monkeypatch.setattr("jj_stack.stack.pr_facts.observe_change_copies", observe_local)
        github.get_repo.side_effect = get_repo
        if failure == "request":
            github.get_prs_by_numbers.side_effect = fail_when_started
        else:
            github.get_prs_by_numbers.return_value = {}
        observing = asyncio.create_task(
            observe_prs(
                change_ids=(),
                context=context,
                github_client=github,
                remote_name="origin",
                state=TrackingState(),
            )
        )
        if failure != "request":
            sibling = (
                asyncio.create_task(fail_when_started())
                if failure == "outer_batch"
                else asyncio.create_task(asyncio.Event().wait())
            )
            observing = asyncio.create_task(wait_for_read_tasks(observing, sibling))
        try:
            async with asyncio.timeout(5):
                if failure == "cancellation":
                    await local_started.wait()
                    await repo_started.wait()
                    observing.cancel()
                await repo_finished.wait()
                finished, _pending = await asyncio.wait((observing,), timeout=0.01)
                assert not finished
                assert not local_finished.is_set()
                release_local.set()
                if failure == "cancellation":
                    with pytest.raises(asyncio.CancelledError):
                        await observing
                else:
                    with pytest.raises(GithubClientError, match="request failed"):
                        await observing
                assert local_finished.is_set()
        finally:
            release_local.set()
            observing.cancel()
            await asyncio.gather(observing, return_exceptions=True)

    asyncio.run(run_case())
