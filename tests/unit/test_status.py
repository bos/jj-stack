from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from jj_review import console as console_module
from jj_review.commands import status as status_module
from jj_review.config import RepoConfig
from jj_review.models.review_state import CachedChange
from jj_review.review.change_status import SubmittedStateDisagreement
from jj_review.review.status import StatusResult
from jj_review.state.journal import SubmitOperationRecord


def _render_lines(*lines: object) -> tuple[str, ...]:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color_mode="never"):
        for line in lines:
            console_module.output(line)
    return tuple(stdout.getvalue().splitlines())


def _submit_operation_record(
    *,
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
) -> SubmitOperationRecord:
    return SubmitOperationRecord(
        path=Path("/tmp/submit.jsonl"),
        pid=99999999,
        display_revset="@-",
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        bookmarks={},
        started_at="2026-04-25T11:00:00+00:00",
    )


def test_status_advises_cleanup_and_rebase_when_merged_pr_remains_in_stack() -> None:
    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="abcdefghijkl",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="team/feature-base"),
                number=5,
                state="merged",
            ),
            state="closed",
        ),
        pull_request_number=lambda: 5,
        managed_comments_lookup=None,
    )

    lines = _render_lines(
        *status_module.render_status_advisory_lines(
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
    assert "jj-review cleanup --rebase @" in normalized_lines
    assert "jj-review cleanup --rebase --dry-run @" in normalized_lines
    assert "PR #5 is merged" in normalized_lines
    assert "merged into team/feature-base" in normalized_lines


def test_status_advises_submit_when_selected_stack_changed_since_submit() -> None:
    lines = _render_lines(
        *status_module.render_status_advisory_lines(
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
    assert "jj-review submit ulxwxsqw" in normalized_lines
    assert "New commit IDs abcdefgh" in normalized_lines
    assert "New PR bases bcdefghi" in normalized_lines
    assert "New stack head bcdefghi" in normalized_lines


def test_status_closed_pr_advisory_guides_reopen_relink_or_restart() -> None:
    revision = SimpleNamespace(
        cached_change=None,
        change_id="loqvlqrqabcdefghijkl",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(number=21216, state="closed"),
            state="closed",
        ),
        pull_request_number=lambda: 21216,
        managed_comments_lookup=None,
    )

    lines = _render_lines(
        *status_module.render_status_advisory_lines(
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
    assert "jj-review submit --restart @" in normalized_lines
    assert "changes below" not in normalized_lines


def test_status_missing_pr_advisory_guides_fetch_relink_or_restart() -> None:
    revision = SimpleNamespace(
        cached_change=CachedChange(
            bookmark="review/feature-8-abcdefgh",
            pr_number=42,
        ),
        change_id="abcdefgh1234",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=None,
            state="missing",
        ),
        pull_request_number=lambda: None,
        managed_comments_lookup=None,
    )

    lines = _render_lines(
        *status_module.render_status_advisory_lines(
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
    assert "jj-review status --fetch <change>" in normalized_lines
    assert "Relink an open PR if one exists" in normalized_lines
    assert "jj-review submit --restart @" in normalized_lines
    assert "GitHub did not report remembered PR #42 for this branch" in normalized_lines


def test_status_summary_does_not_call_tracked_missing_pr_not_submitted() -> None:
    revision = SimpleNamespace(
        bookmark="review/feature-8-abcdefgh",
        cached_change=CachedChange(
            bookmark="review/feature-8-abcdefgh",
            last_submitted_commit_id="submitted-commit",
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=None,
            state="missing",
        ),
        managed_comments_lookup=None,
        subject="feature 8",
    )

    lines = status_module.render_status_summary_lines(
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


def test_render_status_operation_lines_reports_stale_and_interrupted_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_pid_is_alive(pid: int) -> bool:
        return pid == 101

    monkeypatch.setattr(status_module, "pid_is_alive", fake_pid_is_alive)
    prepared_status = SimpleNamespace(
        stale_operations=(
            SimpleNamespace(
                operation=SimpleNamespace(label="submit on @", pid=101),
                path=Path("/tmp/stale-submit.json"),
            ),
        ),
        outstanding_operations=(
            SimpleNamespace(
                operation=SimpleNamespace(label="land on @", pid=202),
                path=Path("/tmp/outstanding-land.json"),
            ),
        ),
        prepared=SimpleNamespace(status_revisions=()),
    )

    lines = _render_lines(
        *status_module.render_status_operation_lines(prepared_status=prepared_status)
    )

    assert "Stale incomplete operations (change IDs no longer in repo):" in lines
    assert any("submit on @" in line and "process alive" in line for line in lines)
    assert any("land on @" in line and "started at unknown time" in line for line in lines)
    assert any("inspect with jj-review status" in line for line in lines)


def test_render_status_operation_lines_guides_submit_for_different_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_module, "pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        status_module,
        "_now_utc",
        lambda: datetime(2026, 4, 29, 12, tzinfo=UTC),
    )
    operation = _submit_operation_record(
        ordered_change_ids=("nnszmtlxaaaaaaaa",),
        ordered_commit_ids=("recordedcommit",),
    )
    prepared_status = SimpleNamespace(
        stale_operations=(),
        outstanding_operations=(SimpleNamespace(operation=operation),),
        prepared=SimpleNamespace(
            status_revisions=(
                SimpleNamespace(
                    revision=SimpleNamespace(
                        change_id="xvxxlmonaaaaaaaa",
                        commit_id="currentcommit",
                    )
                ),
            ),
            remote=None,
        ),
        github_repository=None,
    )

    lines = _render_lines(
        *status_module.render_status_operation_lines(prepared_status=prepared_status)
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "submit for nnszmtlx, started 4d ago from @-" in normalized_lines
    assert "this is not the stack shown above" in normalized_lines
    assert "inspect with jj-review status nnszmtlx" in normalized_lines
    assert "finish with jj-review submit nnszmtlx" in normalized_lines
    assert "preview backout with jj-review abort --dry-run" in normalized_lines
    assert "recorded stack differs from the current selection" not in normalized_lines


def test_render_status_operation_lines_avoids_missing_recorded_submit_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(status_module, "pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        status_module,
        "_now_utc",
        lambda: datetime(2026, 4, 29, 12, tzinfo=UTC),
    )

    class MissingHeadClient:
        def query_revisions_by_change_ids(self, change_ids):
            return {change_id: () for change_id in change_ids}

    operation = _submit_operation_record(
        ordered_change_ids=("aaaaaaaabbbbbbbb", "nnszmtlxaaaaaaaa"),
        ordered_commit_ids=("bottomcommit", "headcommit"),
    )
    prepared_status = SimpleNamespace(
        stale_operations=(),
        outstanding_operations=(SimpleNamespace(operation=operation),),
        prepared=SimpleNamespace(
            client=MissingHeadClient(),
            status_revisions=(
                SimpleNamespace(
                    revision=SimpleNamespace(
                        change_id="xvxxlmonaaaaaaaa",
                        commit_id="currentcommit",
                    )
                ),
            ),
            remote=None,
        ),
        github_repository=None,
    )

    lines = _render_lines(
        *status_module.render_status_operation_lines(prepared_status=prepared_status)
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert (
        "change nnszmtlx from this interrupted submit is no longer visible in jj"
        in normalized_lines
    )
    assert "jj-review abort --dry-run" in normalized_lines
    assert "jj-review abort" in normalized_lines
    assert "jj-review cleanup" not in normalized_lines
    assert "jj-review submit nnszmtlx" not in normalized_lines
    assert "jj-review close --cleanup nnszmtlx" not in normalized_lines


@pytest.mark.parametrize(
    ("started_at", "expected"),
    [
        ("2026-04-29T11:59:35+00:00", "just now"),
        ("2026-04-29T11:47:00+00:00", "13m ago"),
        ("2026-04-29T07:30:00+00:00", "4h ago"),
        ("2026-04-25T11:00:00+00:00", "4d ago"),
        ("2026-04-20T11:00:00+00:00", "2026-04-20"),
    ],
)
def test_format_operation_age_uses_relative_time_for_recent_records(
    started_at: str,
    expected: str,
) -> None:
    assert (
        status_module._format_operation_age(
            started_at,
            now=datetime(2026, 4, 29, 12, tzinfo=UTC),
        )
        == expected
    )


def test_status_summary_truncates_middle_of_long_unsubmitted_sections() -> None:
    revisions = tuple(
        SimpleNamespace(
            bookmark=f"review/feature-{index}",
            cached_change=None,
            change_id=f"{index}" * 12,
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            managed_comments_lookup=None,
            subject=f"feature {index}",
        )
        for index in range(8, 0, -1)
    )

    lines = status_module.render_status_summary_lines(
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


def test_render_status_summary_lines_show_empty_sections_in_verbose_mode() -> None:
    lines = status_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_revision_log_lines=lambda revision, *, color_when: (),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=()),
        verbose=True,
    )

    assert lines == (
        "Unsubmitted stack:",
        "  (none)",
        "",
        "Submitted stack:",
        "  (none)",
        "",
    )


def test_render_status_summary_lines_links_submitted_header_to_top_pr() -> None:
    lines = status_module.render_status_summary_lines(
        client=SimpleNamespace(
            resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
            render_revision_log_lines=lambda revision, *, color_when: (revision.subject,),
        ),
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(
            revisions=(
                SimpleNamespace(
                    bookmark="review/feature-8",
                    cached_change=None,
                    change_id="abcdefgh1234",
                    link_state="active",
                    local_divergent=False,
                    pull_request_lookup=SimpleNamespace(
                        pull_request=SimpleNamespace(
                            html_url="https://github.com/bos/jj-review/pull/8",
                            is_draft=False,
                            number=8,
                        ),
                        review_decision=None,
                        review_decision_error=None,
                        state="open",
                    ),
                    managed_comments_lookup=None,
                    subject="feature 8",
                ),
                SimpleNamespace(
                    bookmark="review/feature-7",
                    cached_change=None,
                    change_id="bcdefghi1234",
                    link_state="active",
                    local_divergent=False,
                    pull_request_lookup=SimpleNamespace(
                        pull_request=SimpleNamespace(
                            html_url="https://github.com/bos/jj-review/pull/7",
                            is_draft=False,
                            number=7,
                        ),
                        review_decision=None,
                        review_decision_error=None,
                        state="open",
                    ),
                    managed_comments_lookup=None,
                    subject="feature 7",
                ),
            ),
        ),
        verbose=False,
    )

    assert lines == (
        "Submitted stack (https://github.com/bos/jj-review/pull/8):",
        "feature 8: PR #8",
        "feature 7: PR #7",
        "",
    )


def test_status_summary_hides_managed_review_bookmark_but_keeps_other_bookmarks() -> None:
    revision = SimpleNamespace(
        bookmark="review/feature-8-abcdefgh",
        cached_change=None,
        change_id="abcdefgh1234",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=None,
        managed_comments_lookup=None,
        subject="feature 8",
    )

    lines = status_module.render_status_summary_lines(
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
        verbose=False,
    )

    assert lines == (
        "Unsubmitted stack:",
        "○  abcdefgh bos 2026-01-01 keep/one keep/two 12345678",
        "│  feature 8",
        "",
    )


def test_status_verbose_keeps_managed_review_bookmark_in_native_log_output() -> None:
    revision = SimpleNamespace(
        bookmark="review/feature-8-abcdefgh",
        cached_change=None,
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=None,
        managed_comments_lookup=None,
        subject="feature 8",
    )

    lines = status_module.render_status_summary_lines(
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
