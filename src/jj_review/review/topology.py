"""Compatibility exports for review topology helpers.

Per-change submitted-baseline comparison and orphan-record discovery now live
with the derived review lifecycle classifier.
"""

from __future__ import annotations

from jj_review.review.change_status import (
    OrphanedRecord,
    SubmittedStateDisagreement,
    enumerate_orphaned_records,
    is_open_pr_record,
    submitted_state_disagreement,
    submitted_state_disagreements,
)

__all__ = [
    "OrphanedRecord",
    "SubmittedStateDisagreement",
    "enumerate_orphaned_records",
    "is_open_pr_record",
    "submitted_state_disagreement",
    "submitted_state_disagreements",
]
