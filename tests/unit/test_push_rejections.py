"""Protected-branch push rejection classification."""

from __future__ import annotations

from jj_stack.github.push_rejections import (
    classify_protected_branch_rejection,
    rejection_reason_lines,
)

_CHECKS_EXPECTED = """\
jj git push --remote origin --bookmark main failed: Changes to push to origin:
  Move forward bookmark main from 1111111111 to 2222222222
remote: error: GH006: Protected branch update failed for refs/heads/main.\x20\x20\x20\x20
remote: error: 7 of 7 required status checks are expected.\x20\x20\x20\x20
error: failed to push some refs to 'https://github.com/acme/widgets.git'
"""

_PULL_REQUEST_REQUIRED = """\
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote: error: Changes must be made through a pull request.
"""

_RULESET_MIXED = """\
remote: error: GH013: Repository rule violations found for refs/heads/main.
remote: - Required status check "ci" is expected.
remote: - Changes must be made through a pull request.
"""

_MERGE_QUEUE = """\
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote: error: Changes must be made through the merge queue.
"""

_NOT_AUTHORIZED = """\
remote: error: GH006: Protected branch update failed for refs/heads/main.
remote: error: You're not authorized to push to this branch.
"""


def test_required_checks_rejection_classifies_as_checks_not_passed() -> None:
    assert classify_protected_branch_rejection(_CHECKS_EXPECTED) == "checks_not_passed"


def test_single_named_check_rejection_classifies_as_checks_not_passed() -> None:
    output = (
        'remote: error: GH006: Protected branch update failed for refs/heads/main.\n'
        'remote: error: Required status check "ci/build" is expected.\n'
    )

    assert classify_protected_branch_rejection(output) == "checks_not_passed"


def test_pull_request_required_rejection_classifies_as_pull_request_required() -> None:
    assert (
        classify_protected_branch_rejection(_PULL_REQUEST_REQUIRED)
        == "pull_request_required"
    )


def test_ruleset_rejection_with_both_violations_prefers_pull_request_required() -> None:
    # A pull-request requirement rules direct pushes out entirely, so it must
    # outrank the checks violation listed beside it.
    assert classify_protected_branch_rejection(_RULESET_MIXED) == "pull_request_required"


def test_merge_queue_rejection_classifies_as_merge_queue_required() -> None:
    assert classify_protected_branch_rejection(_MERGE_QUEUE) == "merge_queue_required"


def test_authorization_rejection_classifies_as_not_authorized() -> None:
    assert classify_protected_branch_rejection(_NOT_AUTHORIZED) == "not_authorized"


def test_non_protection_push_failure_is_not_classified() -> None:
    output = (
        "jj git push --remote origin --bookmark main failed: \n"
        "Error: Refusing to push a bookmark that unexpectedly moved on the remote.\n"
    )

    assert classify_protected_branch_rejection(output) is None


def test_unrecognized_protection_reason_is_not_classified() -> None:
    output = (
        "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
        "remote: error: Some future reason wording.\n"
    )

    assert classify_protected_branch_rejection(output) is None


def test_rejection_reason_lines_keep_only_the_remote_rejection_text() -> None:
    assert rejection_reason_lines(_CHECKS_EXPECTED) == (
        "GH006: Protected branch update failed for refs/heads/main.\n"
        "7 of 7 required status checks are expected."
    )
