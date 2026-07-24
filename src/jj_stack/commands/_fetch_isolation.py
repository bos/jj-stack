"""User-facing reporting for the shared remote-only fetch boundary."""

from __future__ import annotations

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.jj.client import ReviewFetchIsolation
from jj_stack.review.branches import REVIEW_BRANCH_PREFIX


def report_fetch_isolation(change: ReviewFetchIsolation) -> None:
    namespace = f"{REVIEW_BRANCH_PREFIX}/"
    if change.status == "applied":
        console.output(
            t"Reserved {ui.bookmark(namespace)} for jj-stack and added fetch exclusion "
            t"{ui.code(change.refspec)}."
        )
