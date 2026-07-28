from __future__ import annotations

from collections.abc import Iterator

import pytest

from jj_stack.config import DEFAULT_BRANCH_PREFIX
from jj_stack.review.branches import configured_review_namespace

pytest_plugins = ["tests.support.pytest_concurrency"]


@pytest.fixture(autouse=True)
def _review_namespace() -> Iterator[None]:
    """Give every test the namespace a real invocation has, and keep it out of the next test.

    `bootstrap` installs it process-wide without restoring, so a test that runs the real CLI would
    otherwise leave its prefix set for every later test sharing the xdist worker, making the tests
    that depend on an installed namespace vary with the random test order.
    """

    with configured_review_namespace(DEFAULT_BRANCH_PREFIX):
        yield
