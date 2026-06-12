from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import cast

import jj_stack.commands.view as view_module
import jj_stack.console as console_module
import jj_stack.ui as ui_module
from jj_stack.config import RepoConfig
from jj_stack.models.bookmarks import RemoteBookmarkState
from jj_stack.models.github import GithubPullRequest
from jj_stack.models.review_state import CachedChange, LinkState
from jj_stack.review.change_status import SubmittedStateDisagreement
from jj_stack.review.status import (
    ManagedCommentsLookup,
    PullRequestLookup,
    PullRequestLookupSource,
    PullRequestLookupState,
    ReviewStatusRevision,
    StatusResult,
)


def _lookup(
    *,
    state: PullRequestLookupState,
    pull_request: object | None = None,
    review_decision: str | None = None,
    review_decision_error: str | None = None,
    source: PullRequestLookupSource = "head",
) -> PullRequestLookup:
    return PullRequestLookup(
        message=None,
        pull_request=cast(GithubPullRequest | None, pull_request),
        review_decision=review_decision,
        review_decision_error=review_decision_error,
        state=state,
        source=source,
    )


def _status_revision(
    *,
    bookmark: str = "",
    cached_change: CachedChange | None = None,
    change_id: str,
    commit_id: str = "commit-1",
    link_state: LinkState = "active",
    local_divergent: bool = False,
    managed_comments_lookup: ManagedCommentsLookup | None = None,
    pull_request_lookup: PullRequestLookup | None = None,
    remote_state: RemoteBookmarkState | None = None,
    subject: str = "feature",
) -> ReviewStatusRevision:
    return ReviewStatusRevision(
        bookmark=bookmark,
        bookmark_source="generated",
        cached_change=cached_change,
        change_id=change_id,
        commit_id=commit_id,
        link_state=link_state,
        local_divergent=local_divergent,
        managed_comments_lookup=managed_comments_lookup,
        pull_request_lookup=pull_request_lookup,
        remote_state=remote_state,
        subject=subject,
    )


def _render_lines(*lines: ui_module.Renderable) -> tuple[str, ...]:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color_mode="never"):
        for line in lines:
            console_module.output(line)
    return tuple(stdout.getvalue().splitlines())


def test_view_advises_cleanup_and_rebase_when_merged_pr_remains_in_stack() -> None:
    merged_revision = _status_revision(
        change_id="abcdefghijkl",
        pull_request_lookup=_lookup(
            pull_request=SimpleNamespace(
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
                    revisions=(merged_revision,),
                    selected_revset="@",
                    submitted_state_disagreements=(),
                ),
            ),
            config=RepoConfig(bookmark_prefix="team"),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "jj-stack cleanup --rebase @" in normalized_lines
    assert "jj-stack cleanup --rebase --dry-run @" in normalized_lines
    assert "PR #5 is merged" in normalized_lines
    assert "merged into team/feature-base" in normalized_lines


def test_view_advises_submit_when_selected_stack_changed_since_submit() -> None:
    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    revisions=(),
                    selected_revset="ulxwxsqw",
                    submitted_state_disagreements=(
                        SubmittedStateDisagreement(
                            change_id="abcdefghijkl",
                            commit_changed=True,
                        ),
                        SubmittedStateDisagreement(
                            change_id="bcdefghijklm",
                            parent_changed=True,
                            stack_head_changed=True,
                        ),
                    ),
                ),
            ),
            config=RepoConfig(),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "PR branches are behind the current local stack" in normalized_lines
    assert "Submit will push the current commit IDs and PR bases" in normalized_lines
    assert "jj-stack submit ulxwxsqw" in normalized_lines
    assert "New commit IDs abcdefgh" in normalized_lines
    assert "New PR bases bcdefghi" in normalized_lines
    assert "New stack head bcdefghi" in normalized_lines


def test_view_closed_pr_advisory_guides_reopen_relink_or_restart() -> None:
    revision = _status_revision(
        change_id="loqvlqrqabcdefghijkl",
        pull_request_lookup=_lookup(
            pull_request=SimpleNamespace(number=21216, state="closed"),
            state="closed",
        ),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    revisions=(revision,),
                    selected_revset="@",
                    submitted_state_disagreements=(),
                ),
            ),
            config=RepoConfig(),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Closed GitHub PR" in normalized_lines
    assert "GitHub reports a closed PR for the change shown above" in normalized_lines
    assert "Reopen the PR on GitHub to continue that review" in normalized_lines
    assert "relink an open replacement" in normalized_lines
    assert "jj-stack submit --restart @" in normalized_lines
    assert "changes below" not in normalized_lines


def test_view_missing_pr_advisory_guides_fetch_relink_or_restart() -> None:
    revision = _status_revision(
        cached_change=CachedChange(
            bookmark="review/feature-8-abcdefgh",
            pr_number=42,
        ),
        change_id="abcdefgh1234",
        pull_request_lookup=_lookup(
            pull_request=None,
            state="missing",
        ),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=cast(
                StatusResult,
                SimpleNamespace(
                    revisions=(revision,),
                    selected_revset="@",
                    submitted_state_disagreements=(),
                ),
            ),
            config=RepoConfig(),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Missing GitHub PR" in normalized_lines
    assert "GitHub did not report a PR for the remembered review branch" in normalized_lines
    assert "jj-stack view --fetch <change>" in normalized_lines
    assert "Relink an open PR if one exists" in normalized_lines
    assert "jj-stack submit --restart @" in normalized_lines
    assert "GitHub did not report remembered PR #42 for this branch" in normalized_lines


def test_view_summary_does_not_call_tracked_missing_pr_not_submitted() -> None:
    revision = _status_revision(
        bookmark="review/feature-8-abcdefgh",
        cached_change=CachedChange(
            bookmark="review/feature-8-abcdefgh",
            last_submitted_commit_id="submitted-commit",
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pull_request_lookup=_lookup(
            pull_request=None,
            state="missing",
        ),
        subject="feature 8",
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_revision_log_lines=lambda current_revision, *, color_when: (
                f"○  {current_revision.change_id[:8]} {current_revision.commit_id[:8]}",
                f"│  {current_revision.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=(revision,)),
        verbose=False,
    )

    assert lines == (
        "Submitted stack:",
        "○  abcdefgh 12345678: submitted, no PR found for branch",
        "│  feature 8",
        "",
    )


def test_view_summary_uses_cached_review_decision_when_live_decision_lookup_fails() -> None:
    revision = _status_revision(
        bookmark="review/feature-7-abcdefgh",
        cached_change=CachedChange(
            bookmark="review/feature-7-abcdefgh",
            pr_number=7,
            pr_review_decision="approved",
            pr_state="open",
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pull_request_lookup=_lookup(
            pull_request=SimpleNamespace(
                html_url="https://github.test/octo/repo/pull/7",
                is_draft=False,
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
            render_revision_log_lines=lambda current_revision, *, color_when: (
                f"○  {current_revision.change_id[:8]} {current_revision.commit_id[:8]}",
                f"│  {current_revision.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=(revision,)),
        verbose=False,
    )

    normalized_lines = " ".join(lines)
    assert "PR #7 approved" in normalized_lines


def test_view_summary_truncates_middle_of_long_unsubmitted_sections() -> None:
    revisions = tuple(
        _status_revision(
            bookmark=f"review/feature-{index}",
            change_id=f"{index}" * 12,
            commit_id=f"commit-{index}",
            subject=f"feature {index}",
        )
        for index in range(8, 0, -1)
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_revision_log_lines=lambda revision, *, color_when: (
                f"{revision.subject} [{revision.change_id[:8]}]",
                f"body for {revision.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=revisions),
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


def test_view_verbose_keeps_managed_review_bookmark_in_native_log_output() -> None:
    revision = _status_revision(
        bookmark="review/feature-8-abcdefgh",
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        subject="feature 8",
    )

    lines = view_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_revision_log_lines=lambda current_revision, *, color_when: (
                (
                    "○  abcdefgh bos 2026-01-01 keep/one "
                    f"{current_revision.bookmark} keep/two 12345678"
                ),
                f"│  {current_revision.subject}",
            ),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=(revision,)),
        verbose=True,
    )

    assert lines == (
        "Unsubmitted stack:",
        "○  abcdefgh bos 2026-01-01 keep/one review/feature-8-abcdefgh keep/two 12345678",
        "│  feature 8",
        "",
        "Submitted stack:",
        "  (none)",
        "",
    )
