from __future__ import annotations

import pytest

from jj_stack.errors import CliError
from jj_stack.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_stack.models.review_state import LinkState, ReviewIdentity
from jj_stack.models.stack import LocalRevision
from jj_stack.review.bookmarks import (
    BookmarkResolver,
    ResolvedBookmark,
    bookmark_matches_restart_change_id,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    find_changes_by_bookmark,
    generate_bookmark_name,
    match_bookmarks_for_revisions,
)


def test_generate_bookmark_name_normalizes_subject() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    assert generate_bookmark_name(revision) == "review/fix-cache-invalidation-zvlywqkx"
    assert (
        generate_bookmark_name(revision, prefix="bosullivan")
        == "bosullivan/fix-cache-invalidation-zvlywqkx"
    )


def test_generate_bookmark_name_falls_back_for_blank_subject() -> None:
    revision = _revision(change_id="abcdefghijklmno", description="\n")

    bookmark = generate_bookmark_name(revision)

    assert bookmark == "review/change-abcdefgh"


def test_restart_bookmark_matcher_accepts_two_digit_attempts() -> None:
    assert bookmark_matches_restart_change_id(
        "review/change-fresh-10-abcdefgh",
        "abcdefghijklmno",
    )


@pytest.mark.parametrize(
    "bookmark",
    (
        "review/change-fresh-0-abcdefgh",
        "review/change-fresh-1-abcdefgh",
        "review/change-fresh-01-abcdefgh",
    ),
)
def test_restart_bookmark_matcher_rejects_attempts_the_generator_cannot_make(
    bookmark: str,
) -> None:
    assert not bookmark_matches_restart_change_id(bookmark, "abcdefghijklmno")


def test_bookmark_resolver_generates_bookmark_when_no_identity_exists() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation\n",
    )

    resolutions = BookmarkResolver({}).resolve_revisions((revision,))

    assert resolutions[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert resolutions[0].source == "generated"


def test_bookmark_resolver_keeps_cached_bookmark_stable_after_subject_change() -> None:
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


def test_bookmark_resolver_uses_matched_bookmark_when_cache_is_missing() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation\n",
    )

    result = BookmarkResolver(
        {},
        matched_bookmarks={"zvlywqkxtmnpqrstu": "potato/custom-name"},
    ).resolve_revisions((revision,))

    assert result[0].bookmark == "potato/custom-name"
    assert result[0].source == "matched"


def test_bookmark_resolver_reuses_discovered_bookmark_when_cache_is_missing() -> None:
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


def test_discover_bookmarks_reuses_unique_remote_bookmark_by_change_id_suffix() -> None:
    bookmark = "review/original-title-zvlywqkx"
    bookmark_states = {
        bookmark: BookmarkState(
            name=bookmark,
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("abc123",)),),
        ),
    }
    revisions = (_revision(change_id="zvlywqkxtmnpqrstu", description=""),)

    bookmarks = discover_bookmarks_for_revisions(
        bookmark_states=bookmark_states,
        remote_name="origin",
        revisions=revisions,
    )

    assert bookmarks == {"zvlywqkxtmnpqrstu": bookmark}


def test_discover_bookmarks_for_revisions_rejects_ambiguous_matches() -> None:
    with pytest.raises(
        CliError,
        match="multiple existing bookmarks match",
    ):
        discover_bookmarks_for_revisions(
            bookmark_states={
                "review/first-zvlywqkx": BookmarkState(
                    name="review/first-zvlywqkx",
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("abc123",)),),
                ),
                "review/second-zvlywqkx": BookmarkState(
                    name="review/second-zvlywqkx",
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("def456",)),),
                ),
            },
            remote_name="origin",
            revisions=(_revision(change_id="zvlywqkxtmnpqrstu", description=""),),
        )


def test_discovery_does_not_prefer_an_arbitrary_local_suffix_match() -> None:
    change_id = "zvlywqkxtmnpqrstu"
    revision = _revision(change_id=change_id, description="")

    with pytest.raises(CliError, match="multiple existing bookmarks match"):
        discover_bookmarks_for_revisions(
            bookmark_states={
                "review/original-zvlywqkx": BookmarkState(
                    name="review/original-zvlywqkx",
                    local_targets=(revision.commit_id,),
                    remote_targets=(
                        RemoteBookmarkState(remote="origin", targets=(revision.commit_id,)),
                    ),
                ),
                "review/alternate-zvlywqkx": BookmarkState(
                    name="review/alternate-zvlywqkx",
                    local_targets=(revision.commit_id,),
                ),
            },
            remote_name="origin",
            revisions=(revision,),
        )


def test_match_bookmarks_for_revisions_matches_local_bookmark_by_pattern() -> None:
    bookmarks = match_bookmarks_for_revisions(
        bookmark_states={
            "potato/original-title": BookmarkState(
                name="potato/original-title",
                local_targets=("zvlywqkxtmnpqrstu-commit",),
            ),
        },
        patterns=("potato/*",),
        revisions=(_revision(change_id="zvlywqkxtmnpqrstu", description=""),),
        remote_name="origin",
    )

    assert bookmarks == {"zvlywqkxtmnpqrstu": "potato/original-title"}


def test_match_bookmarks_for_revisions_rejects_ambiguous_pattern_matches() -> None:
    with pytest.raises(CliError, match="multiple existing bookmarks match the configured"):
        match_bookmarks_for_revisions(
            bookmark_states={
                "potato/first": BookmarkState(
                    name="potato/first",
                    local_targets=("zvlywqkxtmnpqrstu-commit",),
                ),
                "potato/second": BookmarkState(
                    name="potato/second",
                    local_targets=("zvlywqkxtmnpqrstu-commit",),
                ),
            },
            patterns=("potato/*",),
            revisions=(_revision(change_id="zvlywqkxtmnpqrstu", description=""),),
            remote_name="origin",
        )


def test_ensure_unique_bookmarks_rejects_multiple_changes_resolving_to_same_bookmark() -> None:
    resolutions = (
        ResolvedBookmark(
            bookmark="review/shared-name",
            change_id="change-a",
            source="matched",
        ),
        ResolvedBookmark(
            bookmark="review/shared-name",
            change_id="change-b",
            source="saved",
        ),
    )

    with pytest.raises(
        CliError,
        match="multiple changes to the same bookmark",
    ):
        ensure_unique_bookmarks(resolutions)


def test_find_changes_by_bookmark_includes_unlinked_identity_to_block_overwrite() -> None:
    review_identities = {
        "change-unlinked": _identity(
            head_ref="review/shared",
            link_state="unlinked",
        )
    }

    assert find_changes_by_bookmark(review_identities, "review/shared") == ("change-unlinked",)


def _identity(
    *,
    head_ref: str,
    link_state: LinkState = "active",
) -> ReviewIdentity:
    return ReviewIdentity(
        github_host="github.test",
        repository_owner="octo-org",
        repository_name="stacked-review",
        pr_number=1,
        head_owner="octo-org",
        head_ref=head_ref,
        bookmark_ownership="managed",
        link_state=link_state,
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
