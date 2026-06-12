from __future__ import annotations

import asyncio

import pytest

from jj_stack.concurrency import run_bounded_tasks


def test_run_bounded_tasks_preserves_result_order_while_bounding_concurrency() -> None:
    active = 0
    max_active = 0

    async def run_item(item: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return item * 2

    results = asyncio.run(
        run_bounded_tasks(
            concurrency=2,
            items=(1, 2, 3, 4),
            run_item=run_item,
        )
    )

    assert results == [2, 4, 6, 8]
    assert max_active == 2


def test_run_bounded_tasks_does_not_launch_additional_items_after_first_failure() -> None:
    started: list[int] = []
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def run_item(item: int) -> int:
        started.append(item)
        if item == 1:
            await release_first.wait()
            return item
        if item == 2:
            release_second.set()
            raise RuntimeError("boom")
        await asyncio.sleep(0)
        return item

    async def run_case() -> None:
        task = asyncio.create_task(
            run_bounded_tasks(
                concurrency=2,
                items=(1, 2, 3),
                run_item=run_item,
            )
        )
        await release_second.wait()
        release_first.set()
        with pytest.raises(RuntimeError, match="boom"):
            await task

    asyncio.run(run_case())

    assert started == [1, 2]
