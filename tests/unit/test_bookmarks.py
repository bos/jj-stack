from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_stack.models.review_state import ReviewIdentity
from jj_stack.models.stack import LocalRevision
from jj_stack.review.bookmarks import (
    BookmarkResolver,
    ResolvedBookmark,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    find_changes_by_bookmark,
)
from jj_stack.review.branches import (
    generate_review_branch,
    restarted_review_branch,
    review_branch_matches_change,
)


def test_generate_review_branch_normalizes_subject() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    assert generate_review_branch(revision) == "review/fix-cache-invalidation-zvlywqkx"


def test_generate_review_branch_falls_back_for_blank_subject() -> None:
    revision = _revision(change_id="abcdefghijklmno", description="\n")

    assert generate_review_branch(revision) == "review/change-abcdefgh"


@pytest.mark.parametrize(
    ("branch", "matches"),
    (
        ("review/cache-fix-zvlywqkx", True),
        ("review/cache-fix-fresh-pr42-zvlywqkx", True),
        ("team/cache-fix-zvlywqkx", False),
        ("review/cache_fix-zvlywqkx", False),
        ("review/cache-fix-abcdefgh", False),
        ("review/-zvlywqkx", False),
        ("review/fresh-pr17-zvlywqkx", False),
        ("review/cache-fix-fresh-pr17-fresh-pr18-zvlywqkx", False),
    ),
)
def test_review_branch_matcher_enforces_managed_grammar(
    branch: str,
    matches: bool,
) -> None:
    assert review_branch_matches_change(branch, "zvlywqkxtmnpqrstu") is matches


def test_restarted_review_branch_replaces_prior_marker() -> None:
    assert (
        restarted_review_branch(
            change_id="zvlywqkxtmnpqrstu",
            previous_branch="review/cache-fix-fresh-pr42-zvlywqkx",
            previous_pull_request=57,
        )
        == "review/cache-fix-fresh-pr57-zvlywqkx"
    )


def test_generate_review_branch_disambiguates_reserved_restart_marker() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="fresh pr42\n",
    )

    assert generate_review_branch(revision) == "review/fresh-pr42-change-zvlywqkx"


def test_bookmark_resolver_generates_branch_when_no_identity_exists() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation\n",
    )

    resolutions = BookmarkResolver({}).resolve_revisions((revision,))

    assert resolutions[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert resolutions[0].source == "generated"


def test_bookmark_resolver_keeps_saved_branch_stable_after_subject_change() -> None:
    identities = {
        "zvlywqkxtmnpqrstu": _identity(head_ref="review/fix-cache-invalidation-zvlywqkx")
    }
    renamed_revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    resolutions = BookmarkResolver(identities).resolve_revisions((renamed_revision,))

    assert resolutions[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert resolutions[0].source == "saved"


def test_bookmark_resolver_reuses_discovered_remote_branch() -> None:
    renamed_revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    result = BookmarkResolver(
        {},
        discovered_bookmarks={"zvlywqkxtmnpqrstu": "review/fix-cache-invalidation-zvlywqkx"},
    ).resolve_revisions((renamed_revision,))

    assert result[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert result[0].source == "discovered"


def test_discover_bookmarks_reuses_unique_remote_branch_by_change_id_suffix() -> None:
    branch = "review/original-title-zvlywqkx"
    bookmark_states = {
        branch: BookmarkState(
            name=branch,
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("abc123",)),),
        ),
    }
    revisions = (_revision(change_id="zvlywqkxtmnpqrstu", description=""),)

    bookmarks = discover_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        remote_name="origin",
        revisions=revisions,
    )

    assert bookmarks == {"zvlywqkxtmnpqrstu": branch}


def test_discover_bookmarks_rejects_ambiguous_matches() -> None:
    with pytest.raises(CliError, match="multiple existing bookmarks match"):
        discover_bookmarks_for_revisions(
            bookmark_states={
                branch: BookmarkState(
                    name=branch,
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=(target,)),),
                )
                for branch, target in (
                    ("review/first-zvlywqkx", "abc123"),
                    ("review/second-zvlywqkx", "def456"),
                )
            },
            remote_name="origin",
            revisions=(_revision(change_id="zvlywqkxtmnpqrstu", description=""),),
        )


def test_ensure_unique_bookmarks_rejects_multiple_changes_on_same_branch() -> None:
    resolutions = (
        ResolvedBookmark(
            bookmark="review/shared-abcdefgh",
            change_id="abcdefghijklmno",
            source="generated",
        ),
        ResolvedBookmark(
            bookmark="review/shared-abcdefgh",
            change_id="qrstuvwxyzabcde",
            source="saved",
        ),
    )

    with pytest.raises(CliError, match="multiple changes to the same bookmark"):
        ensure_unique_bookmarks(resolutions)


def test_find_changes_by_bookmark_reports_every_saved_claim() -> None:
    review_identities = {
        "abcdefghijklmno": _identity(head_ref="review/shared-abcdefgh"),
    }

    assert find_changes_by_bookmark(review_identities, "review/shared-abcdefgh") == (
        "abcdefghijklmno",
    )


def _identity(*, head_ref: str) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=1,
        head_owner="octo-org",
        head_ref=head_ref,
    )


def _revision(*, change_id: str, description: str) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=f"{change_id}-commit",
        current_working_copy=False,
        description=description,
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("parent",),
    )
