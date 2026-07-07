"""Classify GitHub protected-branch push rejections into next-step guidance.

GitHub marks these rejections with stable error codes — ``GH006`` for classic
branch protection, ``GH013`` for repository rulesets — but the reason lines
are human-facing prose with no compatibility guarantee. Classification
therefore only ever selects a hint: the error keeps the raw rejection lines,
anything unrecognized falls back to them alone, and no command changes what
it does based on the result.
"""

from __future__ import annotations

from typing import Literal

import jj_stack.ui as ui
from jj_stack.errors import ErrorHint

PushRejectionReason = Literal[
    "checks_not_passed",
    "merge_queue_required",
    "not_authorized",
    "pull_request_required",
]

_REJECTION_MARKERS = ("gh006:", "gh013:")


def classify_protected_branch_rejection(push_output: str) -> PushRejectionReason | None:
    """Classify a failed push's output, or return None when unrecognized."""

    text = push_output.lower()
    if not any(marker in text for marker in _REJECTION_MARKERS):
        return None
    # Order matters: a ruleset rejection can list several violations at once,
    # and a reason that rules out direct pushes entirely outranks one that
    # only delays them.
    if "merge queue" in text:
        return "merge_queue_required"
    if "pull request" in text:
        return "pull_request_required"
    if "status check" in text:
        return "checks_not_passed"
    if "not authorized" in text or "protected ref" in text:
        return "not_authorized"
    return None


def rejection_reason_lines(push_output: str) -> str:
    """Extract the remote's rejection lines from a failed push's output."""

    interesting: list[str] = []
    for raw_line in push_output.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith("remote:"):
            line = line[len("remote:") :].strip()
            if line.lower().startswith("error:"):
                line = line[len("error:") :].strip()
        elif not any(marker in lowered for marker in _REJECTION_MARKERS):
            continue
        if line and line not in interesting:
            interesting.append(line)
    return "\n".join(interesting)


def protected_branch_rejection_hint(reason: PushRejectionReason) -> ErrorHint:
    """Return the next-step hint for one classified rejection reason."""

    if reason == "checks_not_passed":
        return (
            t"Direct pushes are allowed once the required checks pass on the exact "
            t"commits being landed. Wait for the review-branch checks to finish, "
            t"then rerun {ui.cmd('land')}; {ui.cmd('land --via merge')} would not "
            t"help because the merge API enforces the same checks."
        )
    if reason == "pull_request_required":
        return (
            t"This trunk only accepts changes through pull requests: rerun with "
            t"{ui.cmd('land --via merge')}, then run {ui.cmd('sync')}."
        )
    if reason == "merge_queue_required":
        return (
            t"This trunk uses a merge queue, which jj-stack cannot drive yet. Merge "
            t"the ready PRs through the queue on GitHub, then run {ui.cmd('sync')}."
        )
    return "Pushing to this branch needs repository access jj-stack cannot work around."
