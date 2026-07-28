from __future__ import annotations

from collections.abc import Iterator

import pytest

import jj_stack.review.branches as review_branches
from jj_stack.config import DEFAULT_BRANCH_PREFIX

pytest_plugins = ["tests.support.pytest_concurrency"]


@pytest.fixture(autouse=True)
def _review_namespace(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test the namespace a real invocation has, and keep it out of the next test.

    `bootstrap` installs it process-wide without restoring, so a test that runs the real CLI would
    otherwise leave its prefix set for every later test sharing the xdist worker, making the tests
    that depend on an installed namespace vary with the random test order.
    """

    monkeypatch.setattr(review_branches, "_prefix", DEFAULT_BRANCH_PREFIX)
    yield
