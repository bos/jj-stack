"""Helpers for bounded async task execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from enum import Enum
from typing import Any, Literal

DEFAULT_BOUNDED_CONCURRENCY = 8


class _Missing(Enum):
    MISSING = "missing"


_MISSING: Literal[_Missing.MISSING] = _Missing.MISSING


async def run_bounded_tasks[TaskItemT, TaskResultT](
    *,
    concurrency: int,
    items: Sequence[TaskItemT],
    run_item: Callable[[TaskItemT], Coroutine[Any, Any, TaskResultT]],
    on_success: Callable[[int, TaskResultT], None] | None = None,
) -> list[TaskResultT]:
    """Run work with bounded in-flight tasks while preserving result order.

    New work stops launching after the first failure, but already-started work
    is allowed to complete so callers can checkpoint any successful results.
    """

    if not items:
        return []

    item_iter = iter(enumerate(items))
    in_flight: dict[asyncio.Task[TaskResultT], int] = {}
    results: list[TaskResultT | Literal[_Missing.MISSING]] = [_MISSING] * len(items)
    first_failure: tuple[int, Exception] | None = None

    def start_next() -> bool:
        try:
            index, item = next(item_iter)
        except StopIteration:
            return False
        in_flight[asyncio.create_task(run_item(item))] = index
        return True

    for _ in range(min(concurrency, len(items))):
        start_next()

    try:
        while in_flight:
            done, _ = await asyncio.wait(
                tuple(in_flight),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                index = in_flight.pop(task)
                try:
                    result = task.result()
                except Exception as error:
                    if first_failure is None or index < first_failure[0]:
                        first_failure = (index, error)
                    continue

                results[index] = result
                if on_success is None:
                    continue
                try:
                    on_success(index, result)
                except Exception as error:
                    if first_failure is None or index < first_failure[0]:
                        first_failure = (index, error)

            while first_failure is None and len(in_flight) < concurrency:
                if not start_next():
                    break
    finally:
        for task in in_flight:
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    if first_failure is not None:
        raise first_failure[1]

    completed_results: list[TaskResultT] = []
    for result in results:
        if result is _MISSING:
            raise AssertionError("Bounded task runner completed without a task result.")
        completed_results.append(result)
    return completed_results
