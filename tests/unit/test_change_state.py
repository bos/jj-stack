from __future__ import annotations

from typing import Any

import pytest

import jj_stack.ui as ui
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubPRHead, PRState
from jj_stack.models.github_details import GithubMergeQueueEntry
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR
from jj_stack.stack.change_state import (
    UNOBSERVED,
    BranchClaimed,
    BranchDisagrees,
    BranchMissing,
    ChangeObservation,
    Landed,
    LookupFailed,
    Merged,
    NotInspected,
    ObservationFailed,
    PRAmbiguous,
    PRHeadMoved,
    PRMissing,
    Published,
    PushedUnrecorded,
    Queued,
    Stop,
    Unpublished,
    UntrackedPRExists,
    classify,
    report_incomplete,
)
from tests.support.change_helpers import make_change
from tests.support.tracking import make_pr_identity

_BRANCH = "jj-stack/feature-abcdefgh"
_TRACKED = TrackedPR(
    pr_identity=make_pr_identity(head_ref=_BRANCH, pr_number=7),
    submitted_baseline=SubmittedBaseline(commit_id="baseline"),
)


def _pr(
    *,
    number: int = 7,
    state: PRState = "open",
    head_ref: str = _BRANCH,
    head_sha: str = "baseline",
    queued: bool = False,
) -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubPRHead(ref=head_ref, sha=head_sha),
        html_url=f"https://github.test/octo/repo/pull/{number}",
        merge_queue_entry=GithubMergeQueueEntry(id="entry") if queued else None,
        node_id=f"PR_{number}",
        number=number,
        state=state,
        title="feature",
    )


def _local(commit_id: str = "baseline", *, divergent: bool = False) -> LocalCommit:
    return make_change(
        change_id="abcdefghijkl", commit_id=commit_id, description="feature\n"
    ).model_copy(update={"divergent": divergent})


def _observe(**overrides: Any) -> ChangeObservation:
    local = _local()
    fields: dict[str, Any] = {
        "change_id": "abcdefghijkl",
        "tracked": _TRACKED,
        "branch": _BRANCH,
        "remote_name": "origin",
        "local": (local,),
        "selected": local,
        "pr": _pr(),
        "open_prs_on_branch": (_pr(),),
    }
    fields.update(overrides)
    return ChangeObservation(**fields)


_CLASSIFICATION_CASES: tuple[tuple[str, dict[str, object], type], ...] = (
    ("untracked, nothing on GitHub", {"tracked": None, "open_prs_on_branch": ()}, Unpublished),
    (
        "untracked, branch already at the local commit",
        {"tracked": None, "open_prs_on_branch": (), "remote_target": "baseline"},
        Unpublished,
    ),
    (
        "untracked, branch at a foreign commit",
        {"tracked": None, "open_prs_on_branch": (), "remote_target": "other"},
        BranchClaimed,
    ),
    ("untracked, open PR on the branch", {"tracked": None}, UntrackedPRExists),
    ("tracked, GitHub not consulted", {"pr": UNOBSERVED}, NotInspected),
    ("lookup failed", {"pr": ObservationFailed("GitHub returned 502")}, LookupFailed),
    ("saved PR gone", {"pr": None, "open_prs_on_branch": ()}, PRMissing),
    (
        "saved PR gone, two open PRs on the branch",
        {"pr": None, "open_prs_on_branch": (_pr(number=8), _pr(number=9))},
        PRAmbiguous,
    ),
    ("merged and on trunk", {"pr": _pr(state="merged"), "trunk_evidence": "rewritten"}, Landed),
    ("open, exact commit already on trunk", {"trunk_evidence": "exact"}, Landed),
    ("queued", {"pr": _pr(queued=True)}, Queued),
    ("queued but head moved", {"pr": _pr(queued=True, head_sha="elsewhere")}, PRHeadMoved),
    (
        "branch deleted while the head moved",
        {"remote_target": None, "pr": _pr(head_sha="elsewhere")},
        BranchMissing,
    ),
    ("branch and PR head disagree", {"remote_target": "other"}, BranchDisagrees),
)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [(fields, expected) for _label, fields, expected in _CLASSIFICATION_CASES],
    ids=[label for label, _fields, _expected in _CLASSIFICATION_CASES],
)
def test_classification_distinguishes_publication_and_link_states(
    overrides: dict[str, object], expected: type
) -> None:
    state = classify(_observe(**overrides))

    assert type(state) is expected
    if isinstance(state, Stop):
        assert ui.plain_text(state.reason).strip()
        assert ui.plain_text(state.repair).strip()


def test_unobserved_facts_never_produce_a_stop() -> None:
    state = classify(
        _observe(
            selected=None,
            open_prs_on_branch=UNOBSERVED,
            remote_target=UNOBSERVED,
            trunk_evidence=UNOBSERVED,
        )
    )

    assert isinstance(state, Published)


def test_merged_state_carries_the_reason_trunk_did_not_prove_it() -> None:
    state = classify(
        _observe(pr=_pr(state="merged"), trunk_evidence=None, trunk_evidence_reason="why")
    )

    assert isinstance(state, Merged) and state.trunk_evidence_reason == "why"


def test_a_pr_head_visible_locally_is_pushed_work_not_a_moved_head() -> None:
    # A fetched or checked-out copy of the remote head sits beside the selected commit.
    selected, fetched = _local("baseline"), _local("remote-rewrite")
    pr = _pr(head_sha="remote-rewrite")

    visible = classify(_observe(local=(selected, fetched), selected=selected, pr=pr))
    hidden = classify(_observe(local=(selected,), selected=selected, pr=pr))

    assert isinstance(visible, PushedUnrecorded)
    assert isinstance(hidden, PRHeadMoved)


def test_branch_missing_repair_matches_the_pull_request_state() -> None:
    still_open = classify(_observe(remote_target=None))
    already_closed = classify(_observe(remote_target=None, pr=_pr(state="closed")))

    assert isinstance(still_open, BranchMissing) and isinstance(already_closed, BranchMissing)
    assert "close PR #7" in ui.plain_text(still_open.repair)
    assert "reopen PR #7" in ui.plain_text(already_closed.repair)
    assert "jj-stack cleanup --pull-request 7" in ui.plain_text(already_closed.repair)


def test_report_incomplete_only_when_the_saved_pr_cannot_be_placed() -> None:
    assert report_incomplete(classify(_observe())) is False
    # A moved head is a complete report about the pull request; two open pull requests on one
    # branch are ambiguous; a closed saved PR beside one open competitor is only a warning.
    assert report_incomplete(classify(_observe(pr=_pr(head_sha="elsewhere")))) is False
    assert (
        report_incomplete(classify(_observe(open_prs_on_branch=(_pr(), _pr(number=8))))) is True
    )
    assert (
        report_incomplete(
            classify(_observe(pr=_pr(state="closed"), open_prs_on_branch=(_pr(number=8),)))
        )
        is False
    )
    # Divergent unmerged work cannot be placed; divergent merged work is history.
    divergent = _local(divergent=True)
    assert report_incomplete(classify(_observe(local=(divergent,), selected=divergent))) is True
    assert (
        report_incomplete(
            classify(_observe(local=(divergent,), selected=divergent, pr=_pr(state="merged")))
        )
        is False
    )
