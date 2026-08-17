from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import cast

import jj_stack.commands.view as view_module
import jj_stack.console as console_module
import jj_stack.ui as ui_module
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline
from jj_stack.stack.status import (
    PRLookup,
    PRLookupSource,
    PRLookupState,
    StackStatusChange,
    StatusResult,
)


def _lookup(
    *,
    state: PRLookupState,
    message: str | None = None,
    pr: object | None = None,
    review_decision: str | None = None,
    review_decision_error: str | None = None,
    source: PRLookupSource = "head",
) -> PRLookup:
    return PRLookup(
        message=message,
        pr=cast(GithubPR | None, pr),
        review_decision=review_decision,
        review_decision_error=review_decision_error,
        state=state,
        source=source,
    )


def _status_change(
    *,
    branch: str | None = None,
    change_id: str,
    commit_id: str = "commit-1",
    local_divergent: bool = False,
    pr_lookup: PRLookup | None = None,
    pr_identity: PRIdentity | None = None,
    remote_target: str | None = None,
    submitted_baseline: SubmittedBaseline | None = None,
    subject: str = "feature",
) -> StackStatusChange:
    return StackStatusChange(
        branch=branch,
        change_id=change_id,
        commit_id=commit_id,
        local_divergent=local_divergent,
        pr_lookup=pr_lookup,
        pr_identity=pr_identity,
        remote_target=remote_target,
        submitted_baseline=submitted_baseline,
        subject=subject,
    )


def _identity(*, branch: str, pr_number: int) -> PRIdentity:
    return PRIdentity(
        repo_owner="octo-org",
        repo_name="repo",
        pr_number=pr_number,
        head_owner="octo-org",
        head_ref=branch,
    )


def _render_lines(*lines: ui_module.Renderable) -> tuple[str, ...]:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color_mode="never"):
        for line in lines:
            console_module.output(line)
    return tuple(stdout.getvalue().splitlines())


def test_view_advises_cleanup_and_rebase_when_merged_pr_remains_in_stack() -> None:
    merged_change = _status_change(
        change_id="abcdefghijkl",
        pr_lookup=_lookup(
            pr=SimpleNamespace(
                base=SimpleNamespace(ref="team/feature-base"),
                number=5,
                state="merged",
            ),
            state="closed",
        ),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    changes=(merged_change,),
                    selected_revset="@",
                    submitted_state_disagreements=(),
                ),
            ),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "jj-stack sync @" in normalized_lines
    assert "jj-stack sync --dry-run @" in normalized_lines
    assert normalized_lines.index("jj-stack sync --dry-run @") < normalized_lines.index(
        "jj-stack sync @"
    )
    assert "PR #5 is merged" in normalized_lines
    assert "later local changes are still based on it" in normalized_lines


def test_view_advises_submit_when_selected_stack_changed_since_submit() -> None:
    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    changes=(),
                    selected_revset="ulxwxsqw",
                    submitted_state_disagreements=(
                        "abcdefghijkl",
                        "bcdefghijklm",
                    ),
                ),
            ),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "New commit IDs" in normalized_lines
    assert "abcdefgh" in normalized_lines
    assert "bcdefghi" in normalized_lines


def test_view_closed_pr_advisory_guides_reopen_relink_or_cleanup() -> None:
    change = _status_change(
        change_id="loqvlqrqabcdefghijkl",
        pr_lookup=_lookup(
            pr=SimpleNamespace(number=21216, state="closed"),
            state="closed",
        ),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    changes=(change,),
                    selected_revset="@",
                    submitted_state_disagreements=(),
                ),
            ),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Closed GitHub PR" in normalized_lines
    assert "GitHub reports a closed PR for the change shown above" in normalized_lines
    assert "Reopen the PR on GitHub to continue using it" in normalized_lines
    assert "relink an open replacement" in normalized_lines
    assert "jj-stack cleanup @" in normalized_lines
    assert "changes below" not in normalized_lines


def test_view_missing_pr_advisory_guides_fetch_relink_or_cleanup() -> None:
    change = _status_change(
        pr_identity=_identity(
            branch="jj-stack/feature-8-abcdefgh",
            pr_number=42,
        ),
        change_id="abcdefgh1234",
        pr_lookup=_lookup(
            pr=None,
            state="missing",
        ),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    changes=(change,),
                    selected_revset="@",
                    submitted_state_disagreements=(),
                ),
            ),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Missing GitHub PR" in normalized_lines
    assert "GitHub did not report a PR for the remembered PR branch" in normalized_lines
    assert "jj git fetch" in normalized_lines
    assert "Relink an open PR if one exists" in normalized_lines
    assert "jj-stack unstack --local @" in normalized_lines
    assert "GitHub did not report remembered PR #42 for this branch" in normalized_lines


def test_view_summary_does_not_call_tracked_missing_pr_not_submitted() -> None:
    change = _status_change(
        branch="jj-stack/feature-8-abcdefgh",
        pr_identity=_identity(
            branch="jj-stack/feature-8-abcdefgh",
            pr_number=8,
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pr_lookup=_lookup(
            pr=None,
            state="missing",
        ),
        subject="feature 8",
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_commit_log_lines=lambda current_change, *, color_when: (
                f"○  {current_change.change_id[:8]} {current_change.commit_id[:8]}",
                f"│  {current_change.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(changes=(change,)),
        verbose=False,
    )

    assert lines == (
        "Submitted stack:",
        "○  abcdefgh 12345678: saved PR #8, no PR found for branch",
        "│  feature 8",
        "",
    )


def test_view_summary_omits_review_decision_when_live_decision_lookup_fails() -> None:
    change = _status_change(
        branch="jj-stack/feature-7-abcdefgh",
        pr_identity=_identity(
            branch="jj-stack/feature-7-abcdefgh",
            pr_number=7,
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pr_lookup=_lookup(
            pr=SimpleNamespace(
                html_url="https://github.test/octo/repo/pull/7",
                is_draft=False,
                is_queued=False,
                number=7,
            ),
            review_decision=None,
            review_decision_error="review decision lookup failed",
            state="open",
        ),
        subject="feature 7",
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_commit_log_lines=lambda current_change, *, color_when: (
                f"○  {current_change.change_id[:8]} {current_change.commit_id[:8]}",
                f"│  {current_change.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(changes=(change,)),
        verbose=False,
    )

    normalized_lines = " ".join(lines)
    # Identity-only tracking has no saved decision to fall back on; a failed
    # live lookup must not claim one.
    assert "PR #7" in normalized_lines
    assert "approved" not in normalized_lines


def test_view_summary_labels_row_when_pr_lookup_fails() -> None:
    change = _status_change(
        branch="jj-stack/feature-1-abcdefgh",
        pr_identity=_identity(
            branch="jj-stack/feature-1-abcdefgh",
            pr_number=1,
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pr_lookup=_lookup(
            message="pull request lookup failed",
            pr=None,
            state="error",
        ),
        subject="feature 1",
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_commit_log_lines=lambda current_change, *, color_when: (
                f"○  {current_change.change_id[:8]} {current_change.commit_id[:8]}",
                f"│  {current_change.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(changes=(change,)),
        verbose=False,
    )

    normalized_lines = " ".join(lines)
    assert "saved PR #1, pull request lookup failed" in normalized_lines


def test_view_summary_truncates_middle_of_long_unsubmitted_sections() -> None:
    changes = tuple(
        _status_change(
            change_id=f"{index}" * 12,
            commit_id=f"commit-{index}",
            subject=f"feature {index}",
        )
        for index in range(8, 0, -1)
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_commit_log_lines=lambda change, *, color_when: (
                f"{change.subject} [{change.change_id[:8]}]",
                f"body for {change.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(changes=changes),
        verbose=False,
    )

    assert lines == (
        "Unsubmitted stack:",
        "feature 8 [88888888]",
        "body for feature 8",
        "feature 7 [77777777]",
        "body for feature 7",
        "feature 6 [66666666]",
        "body for feature 6",
        "   ... 2 changes omitted ...",
        "feature 3 [33333333]",
        "body for feature 3",
        "feature 2 [22222222]",
        "body for feature 2",
        "feature 1 [11111111]",
        "body for feature 1",
        "",
    )
