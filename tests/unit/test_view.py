from __future__ import annotations

from io import StringIO

import pytest

import jj_stack.commands.list_ as list_module
import jj_stack.commands.view as view_module
import jj_stack.console as console_module
import jj_stack.ui as ui_module
from jj_stack.commands._json_status import stack_change_json
from jj_stack.commands.view_details import merge_details_hint
from jj_stack.identifiers import ChangeId
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead, PRState
from jj_stack.models.github_details import GithubMergeQueueEntry
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.change_state import ChangeObservation, classify
from jj_stack.stack.status import (
    StackStatusChange,
    StatusResult,
)
from tests.support.change_helpers import make_change
from tests.support.tracking import make_pr_identity


def _pr(*, base_ref: str = "main", number: int, state: PRState) -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref=base_ref),
        head=GithubPRHead(ref="jj-stack/feature", sha="commit-1"),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        node_id=f"PR_{number}",
        number=number,
        state=state,
        title="feature",
    )


def _status_result(*, changes: tuple[StackStatusChange, ...]) -> StatusResult:
    return StatusResult(
        changes=changes,
        github_error=None,
        github_repo=None,
        incomplete=False,
        remote=None,
        remote_error=None,
    )


def _status_change(
    *,
    change_id: str,
    commit_id: str = "commit-1",
    divergent: bool = False,
    conflict: bool = False,
    pr: GithubPR,
    competitors: tuple[GithubPR, ...] = (),
    pr_identity: PRIdentity,
    submitted_baseline: SubmittedBaseline | None = None,
) -> StackStatusChange:
    change = make_change(
        change_id=change_id,
        commit_id=commit_id,
        description="feature\n",
    ).model_copy(update={"divergent": divergent, "conflict": conflict})
    tracked = TrackedPR(
        pr_identity=pr_identity,
        submitted_baseline=submitted_baseline or SubmittedBaseline(commit_id=commit_id),
    )
    open_prs = (pr,) if pr.state == "open" else ()
    observation = ChangeObservation(
        change_id=ChangeId(change_id),
        tracked=tracked,
        branch=pr_identity.head_ref,
        local=(change,),
        selected=change,
        pr=pr,
        open_prs_on_branch=(*open_prs, *competitors),
    )
    return StackStatusChange(change=change, state=classify(observation))


def _render_lines(*lines: ui_module.Renderable) -> tuple[str, ...]:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color="never"):
        for line in lines:
            console_module.output(line)
    return tuple(stdout.getvalue().splitlines())


@pytest.mark.parametrize(
    ("merge_status", "merge_warning"), (("BLOCKED", None), ("DIRTY", "merge conflicts"))
)
def test_reporting_prefers_required_review_but_preserves_specific_merge_warnings(
    merge_status: str, merge_warning: str | None
) -> None:
    change = _status_change(
        change_id="abcdefghijkl",
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=5),
        pr=_pr(number=5, state="open").model_copy(
            update={"review_decision": "review_required", "merge_state_status": merge_status}
        ),
    )
    payload = stack_change_json(change)
    assert payload["status"] == "review_required"
    pr = payload["pr"]
    assert isinstance(pr, dict)
    assert pr["merge_state_status"] == merge_status
    for rendered in (
        list_module._status_fragments(
            github_error=None, remote_error=None, states=(change.state,)
        ),
        view_module._format_status_summary(change, repo=None),
    ):
        summary = ui_module.plain_text(rendered)
        assert "needs review" in summary
        assert "merge blocked" not in summary
        if merge_warning is not None:
            assert merge_warning in summary
    hint = merge_details_hint(_status_result(changes=(change,)))
    assert hint is not None
    assert "jj-stack view --verbose abcdefgh" in ui_module.plain_text(hint)


@pytest.mark.parametrize("other_needs_review", (True, False))
def test_list_keeps_review_counts_without_generic_merge_warning(other_needs_review: bool) -> None:
    changes = tuple(
        _status_change(
            change_id=change_id,
            pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=number),
            pr=_pr(number=number, state="open").model_copy(
                update={
                    "review_decision": "review_required" if needs_review else None,
                    "merge_state_status": "BLOCKED",
                }
            ),
        )
        for number, change_id, needs_review in (
            (1, "abcdefghijkl", True),
            (2, "bcdefghijklm", other_needs_review),
        )
    )
    summary = ui_module.plain_text(
        list_module._status_fragments(
            github_error=None, remote_error=None, states=tuple(change.state for change in changes)
        )
    )
    assert ("2 need review" if other_needs_review else "needs review") in summary
    assert "merge blocked" not in summary


def test_reporting_advises_sync_for_merged_divergent_copies() -> None:
    merged_change = _status_change(
        change_id="abcdefghijkl",
        divergent=True,
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=5),
        pr=_pr(base_ref="team/feature-base", number=5, state="merged"),
    )

    assert stack_change_json(merged_change)["status"] == "merged"
    summary = ui_module.plain_text(
        list_module._status_fragments(
            github_error=None,
            remote_error=None,
            states=(merged_change.state,),
        )
    )
    assert "sync needed" in summary
    assert "divergent" not in summary

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=(merged_change,)),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "jj-stack sync abcdefgh" in normalized_lines
    assert "jj-stack sync --dry-run abcdefgh" in normalized_lines
    assert normalized_lines.index("jj-stack sync --dry-run abcdefgh") < normalized_lines.index(
        "jj-stack sync abcdefgh"
    )
    assert "PR #5 is merged" in normalized_lines


def test_view_advises_submit_when_selected_stack_changed_since_submit() -> None:
    edited = tuple(
        _status_change(
            change_id=change_id,
            commit_id=f"rewritten-{change_id}",
            pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=number),
            submitted_baseline=SubmittedBaseline(commit_id=f"submitted-{change_id}"),
            pr=_pr(number=number, state="open").model_copy(
                update={
                    "head": GithubPRHead(ref="jj-stack/feature", sha=f"submitted-{change_id}")
                }
            ),
        )
        for change_id, number in (("abcdefghijkl", 1), ("bcdefghijklm", 2))
    )
    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=edited),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "jj-stack submit abcdefgh" in normalized_lines
    assert "abcdefgh" in normalized_lines
    assert "bcdefghi" in normalized_lines

    queued = _status_change(
        change_id="cdefghijklmn",
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=3),
        pr=_pr(number=3, state="open").model_copy(
            update={"merge_queue_entry": GithubMergeQueueEntry()}
        ),
    )
    waiting_lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=(*edited, queued)),
        )
    )
    assert "Submit needed" not in " ".join(waiting_lines)


@pytest.mark.parametrize(
    ("moved_count", "with_submitted_child", "conflicted"),
    ((1, False, False), (2, True, False), (2, False, True)),
)
def test_view_sends_moved_pr_heads_to_sync_or_after_conflicts_to_submit(
    moved_count: int, with_submitted_child: bool, conflicted: bool
) -> None:
    """View cannot tell GitHub's rewrite from other work, so it names the command that can.

    Sync would stop on a conflict again; submit checks the PR heads once it is resolved.
    """

    head_id = "uowkpmtkykptovmvunrxrywxynlnwpoo"
    moved = (head_id, "ztzvrmuknyosvtltrtwmvpuwunsqmymu")[:moved_count]
    changes = tuple(
        _status_change(
            change_id=change_id,
            conflict=conflicted and index == len(moved) - 1,
            pr_identity=make_pr_identity(head_ref=f"jj-stack/{number}", pr_number=number),
            pr=_pr(number=number, state="open").model_copy(
                update={"head": GithubPRHead(ref=f"jj-stack/{number}", sha="f" * 40)}
            ),
        )
        for index, (change_id, number) in enumerate(zip(moved, (8, 7), strict=False))
    )
    if with_submitted_child:
        head_id = "v" * 32
        changes = (
            _status_change(
                change_id=head_id,
                pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=9),
                pr=_pr(number=9, state="open"),
            ),
            *changes,
        )
    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=changes),
        )
    )
    normalized = " ".join(" ".join(line.split()) for line in lines)

    assert normalized.count(f"jj-stack sync {head_id[:8]}") == (0 if conflicted else 1)
    assert normalized.count(f"jj-stack submit {head_id[:8]}") == (1 if conflicted else 0)
    assert ("Resolve the conflicts" in normalized) == conflicted
    assert normalized.count("matches neither this change") == moved_count
    assert head_id not in normalized
    assert "jj-stack checkout" not in normalized
    assert "jj-stack relink" not in normalized
    assert "Submit needed" not in normalized


def test_view_closed_pr_advisory_guides_reopen_relink_or_cleanup() -> None:
    change = _status_change(
        change_id="loqvlqrqabcdefghijkl",
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=21216),
        pr=_pr(number=21216, state="closed"),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=(change,)),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Closed GitHub PR" in normalized_lines
    assert "Reopen the PR on GitHub to continue using it" in normalized_lines
    assert "jj-stack relink" in normalized_lines
    assert "jj-stack cleanup loqvlqrq" in normalized_lines
    assert "changes below" not in normalized_lines


@pytest.mark.parametrize(
    ("status", "label"),
    (("link_mismatch", "saved PR needs repair"), ("ambiguous", "ambiguous PR")),
)
def test_reporting_surfaces_broken_links_before_suggesting_submit(
    status: str, label: str
) -> None:
    pr = _pr(number=7, state="open")
    if status == "link_mismatch":
        pr = pr.model_copy(update={"head": GithubPRHead(ref="other", sha="commit-1")})
    change = _status_change(
        change_id="abcdefgh1234",
        commit_id="rewritten",
        submitted_baseline=SubmittedBaseline(commit_id="commit-1"),
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=7),
        pr=pr,
        competitors=(_pr(number=8, state="open"),) if status == "ambiguous" else (),
    )

    assert stack_change_json(change)["status"] == status
    assert label in ui_module.plain_text(
        list_module._status_fragments(
            github_error=None,
            remote_error=None,
            states=(change.state,),
        )
    )
    assert label in ui_module.plain_text(view_module._format_status_summary(change, repo=None))
    advisory = " ".join(
        " ".join(line.split())
        for line in _render_lines(
            *view_module.render_status_advisory_lines(result=_status_result(changes=(change,))),
        )
    )
    assert "jj-stack relink" in advisory
    assert "Submit needed" not in advisory
