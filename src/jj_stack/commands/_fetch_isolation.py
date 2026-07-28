"""User-facing reporting for the shared remote-only fetch boundary."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.jj.client import ReviewFetchIsolation
from jj_stack.review.branches import (
    review_fetch_refspec,
    review_namespace,
)


def report_fetch_isolation(change: ReviewFetchIsolation) -> None:
    if change.status == "applied":
        console.output(
            t"Reserved {ui.bookmark(review_namespace())} for jj-stack and added fetch "
            t"exclusion {ui.code(review_fetch_refspec())}."
        )
