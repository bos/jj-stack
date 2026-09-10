from __future__ import annotations

from pathlib import Path

from jj_stack.errors import CliError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.state.store import TrackingStore

from ..support.integration_helpers import (
    commit_file,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    remote_refs,
    run_command,
    selected_stack,
)
from .submit_command_helpers import (
    configure_submit_environment,
    issue_comments,
    read_remote_ref,
    run_main,
)


def test_cleanup_removes_closed_pr_after_local_change_is_abandoned(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    change_id = selected_stack(repo).changes[-1].change_id
    fake_repo.prs[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PR #1" in captured.out
    assert change_id[:8] in captured.out
    assert change_id not in TrackingStore.for_repo(repo).load().prs
    assert not any(
        ref.startswith("refs/heads/jj-stack/") for ref in remote_refs(fake_repo.git_dir)
    )


def test_cleanup_dry_run_leaves_an_unadopted_repo_untouched(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(repo, "feature 1", "feature-1.txt")
    capsys.readouterr()

    exit_code = run_main(repo, config_path, "cleanup", "--dry-run")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No cleanup actions needed." in captured.out
    assert not TrackingStore.for_repo(repo).is_in_use()


def test_cleanup_change_only_removes_leftovers_for_selected_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    first_change_id = selected_stack(repo).head.change_id

    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    second_change_id = selected_stack(repo).head.change_id
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    fake_repo.prs[1].state = "closed"
    fake_repo.prs[2].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup", second_change_id)
    state = TrackingStore.for_repo(repo).load()

    assert exit_code == 0
    assert second_change_id not in state.prs
    assert first_change_id in state.prs


def test_cleanup_pr_selects_a_saved_orphan_and_rejects_an_unlinked_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    fake_repo.prs[1].state = "closed"
    run_command(["jj", "abandon", change_id], repo)

    exit_code = run_main(repo, config_path, "cleanup", "--pull-request", "1")

    assert exit_code == 0
    assert change_id not in TrackingStore.for_repo(repo).load().prs

    outside = fake_repo.create_pr(
        base_ref="main",
        body="not created by jj-stack",
        head_ref="main",
        title="outside pull request",
    )
    capsys.readouterr()

    unlinked_exit_code = run_main(
        repo, config_path, "cleanup", "--pull-request", str(outside.number), "--close"
    )
    unlinked = capsys.readouterr()

    assert unlinked_exit_code == 1
    assert f"PR #{outside.number} is not linked to any local change" in unlinked.err
    assert fake_repo.prs[outside.number].state == "open"


def test_cleanup_close_finishes_open_and_terminal_orphans(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_ids = [selected_stack(repo).head.change_id]

    for index in (2, 3):
        run_command(["jj", "new", "main"], repo)
        commit_file(repo, f"feature {index}", f"feature-{index}.txt")
        change_ids.append(selected_stack(repo).head.change_id)
        assert run_main(repo, config_path, "submit") == 0
        capsys.readouterr()

    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    identities = tuple(initial_state.prs[change_id].pr_identity for change_id in change_ids)
    fake_repo.prs[identities[1].pr_number].state = "closed"
    merged_pr = fake_repo.prs[identities[2].pr_number]
    merged_pr.state = "closed"
    merged_pr.merged_at = "2026-08-13T12:00:00Z"
    run_command(["jj", "abandon", *change_ids], repo)

    preview_exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--pull-request",
        "orphans",
        "--close",
        "--dry-run",
    )
    preview = capsys.readouterr()

    assert preview_exit_code == 0
    assert f"close PR #{identities[0].pr_number}" in preview.out
    assert f"close PR #{identities[1].pr_number}" not in preview.out
    assert f"close PR #{identities[2].pr_number}" not in preview.out
    assert all(identity.head_ref in preview.out for identity in identities)
    assert fake_repo.prs[identities[0].pr_number].state == "open"
    assert state_store.load() == initial_state

    exit_code = run_main(
        repo,
        config_path,
        "cleanup",
        "--pull-request",
        "orphans",
        "--close",
    )
    applied = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert f"close PR #{identities[0].pr_number}" in applied.out
    assert f"close PR #{identities[1].pr_number}" not in applied.out
    assert f"close PR #{identities[2].pr_number}" not in applied.out
    assert all(change_id not in refreshed_state.prs for change_id in change_ids)
    assert all(
        f"refs/heads/{identity.head_ref}" not in remote_refs(fake_repo.git_dir)
        for identity in identities
    )
    assert all(fake_repo.prs[identity.pr_number].state == "closed" for identity in identities)


def test_cleanup_preserves_a_branch_while_its_closed_dependent_can_be_reopened(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_ids = tuple(change.change_id for change in stack.changes)
    state_store = TrackingStore.for_repo(repo)
    identities = tuple(state_store.load().prs[change_id].pr_identity for change_id in change_ids)
    bookmarks = tuple(identity.head_ref for identity in identities)

    for pr in fake_repo.prs.values():
        pr.state = "closed"
    # GitHub keeps a merged member in the stack forever, and the closed member above it
    # still names the merged member's branch as its base.
    fake_repo.prs[identities[0].pr_number].merged_at = "2026-08-13T12:00:00Z"
    assert fake_repo.prs[identities[1].pr_number].base_ref == bookmarks[0]
    run_command(["jj", "abandon", *change_ids], repo)
    fake_repo.github_stacks = {7: (1, 2)}
    state_before = state_store.load()

    selected_exit_code = run_main(
        repo, config_path, "cleanup", "--pull-request", str(identities[0].pr_number)
    )
    selected = capsys.readouterr()

    assert selected_exit_code == 1
    assert f"PR #{identities[1].pr_number} still uses" in " ".join(
        (selected.out + " " + selected.err).split()
    )
    assert state_store.load() == state_before
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    blocked_exit_code = run_main(repo, config_path, "cleanup")
    blocked = capsys.readouterr()
    normalized_blocked = " ".join(blocked.out.split())

    assert blocked_exit_code == 1
    assert "GitHub stack #7 still groups this pull request" in normalized_blocked
    assert "jj-stack unstack --stack 7" in normalized_blocked
    assert all(change_id in state_store.load().prs for change_id in change_ids)
    assert all(
        f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )

    # Dissolving the GitHub stack does not release the base branch. The closed member above
    # still names it, so cleaning up the one it does not block leaves the other refused.
    assert run_main(repo, config_path, "unstack", "--stack", "7") == 0
    capsys.readouterr()
    partial_exit_code = run_main(repo, config_path, "cleanup")
    partial = capsys.readouterr()
    normalized_partial = " ".join(partial.out.split())

    assert partial_exit_code == 1
    assert f"remote branch: delete {bookmarks[1]}@origin" in normalized_partial
    assert f"PR #{identities[1].pr_number} still uses" in normalized_partial
    assert f"refs/heads/{bookmarks[0]}" in remote_refs(fake_repo.git_dir)

    # That run deleted the closed dependent's own head branch, so GitHub can never reopen it
    # and its base no longer needs preserving; the rerun frees the branch.
    assert f"refs/heads/{bookmarks[1]}" not in remote_refs(fake_repo.git_dir)
    apply_exit_code = run_main(repo, config_path, "cleanup")
    applied = capsys.readouterr()
    normalized_applied = " ".join(applied.out.split())

    assert apply_exit_code == 0
    assert f"remote branch: delete {bookmarks[0]}@origin" in normalized_applied
    assert all(change_id not in state_store.load().prs for change_id in change_ids)
    assert all(
        f"refs/heads/{bookmark}" not in remote_refs(fake_repo.git_dir) for bookmark in bookmarks
    )


def test_cleanup_close_retargets_an_open_dependent_and_frees_its_base_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    bottom_change_id, top_change_id = (
        change.change_id for change in selected_stack(repo).changes
    )
    state_store = TrackingStore.for_repo(repo)
    state = state_store.load()
    identity = state.prs[bottom_change_id].pr_identity
    dependent = state.prs[top_change_id].pr_identity
    comments_before = issue_comments(fake_repo, identity.pr_number)
    fake_repo.prs[identity.pr_number].state = "closed"

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    output = " ".join(captured.out.split())

    assert exit_code == 1
    assert f"PR #{dependent.pr_number} still uses" in output
    assert "rerun jj-stack cleanup" in output
    assert state_store.load() == state
    assert issue_comments(fake_repo, identity.pr_number) == comments_before
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)

    # Closing the dependent through cleanup retargets it to trunk first, so GitHub could still
    # reopen it after its base branch goes. The GitHub stack is dissolved first, as cleanup asks.
    fake_repo.github_stacks = {}
    close_exit_code = run_main(
        repo, config_path, "cleanup", "--pull-request", str(dependent.pr_number), "--close"
    )
    capsys.readouterr()
    dependent_pr = fake_repo.prs[dependent.pr_number]

    assert close_exit_code == 0
    assert (dependent_pr.base_ref, dependent_pr.state) == ("main", "closed")
    assert top_change_id not in state_store.load().prs

    assert run_main(repo, config_path, "cleanup") == 0
    assert state_store.load().prs == {}
    assert not any(
        ref.startswith("refs/heads/jj-stack/") for ref in remote_refs(fake_repo.git_dir)
    )


def test_cleanup_preserves_closed_pr_branch_used_as_head_by_another_open_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    change_id = selected_stack(repo).head.change_id
    state_store = TrackingStore.for_repo(repo)
    state = state_store.load()
    identity = state.prs[change_id].pr_identity
    comments_before = issue_comments(fake_repo, identity.pr_number)
    fake_repo.prs[identity.pr_number].state = "closed"
    competing_pr = fake_repo.create_pr(
        base_ref="main",
        body="outside pull request sharing the head branch",
        head_ref=identity.head_ref,
        title="outside change on the same head branch",
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    output = " ".join(captured.out.split())

    assert exit_code == 1
    assert "also has open" in output
    assert state_store.load() == state
    assert issue_comments(fake_repo, identity.pr_number) == comments_before
    assert f"refs/heads/{identity.head_ref}" in remote_refs(fake_repo.git_dir)
    assert competing_pr.state == "open"


def test_cleanup_stops_later_prs_after_partial_mutation_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    state_store = TrackingStore.for_repo(repo)
    initial_state = state_store.load()
    stack_change_ids = {change.change_id for change in stack.changes}
    ordered_change_ids = tuple(
        change_id for change_id in initial_state.prs if change_id in stack_change_ids
    )
    blocking_change_id, later_change_id = ordered_change_ids
    blocking_identity = initial_state.prs[blocking_change_id].pr_identity
    later_identity = initial_state.prs[later_change_id].pr_identity
    fake_repo.github_stacks = {}
    fake_repo.prs[blocking_identity.pr_number].state = "closed"
    fake_repo.prs[later_identity.pr_number].state = "closed"
    # This case is about stopping after a failed mutation, not about base-branch dependencies.
    # Retarget the later PR so cleanup reaches the mutation it is here to fail.
    fake_repo.prs[later_identity.pr_number].base_ref = "main"
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=blocking_identity.pr_number,
    )

    async def reject_comment_delete(**_kwargs) -> bool:
        raise CliError("comment deletion failed")

    monkeypatch.setattr(
        "jj_stack.commands.cleanup.actions.delete_stack_overview_comment",
        reject_comment_delete,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 1
    assert "comment deletion failed" in captured.out
    assert blocking_change_id in refreshed_state.prs
    assert later_change_id in refreshed_state.prs
    assert f"refs/heads/{blocking_identity.head_ref}" not in remote_refs(fake_repo.git_dir)
    assert f"refs/heads/{later_identity.head_ref}" in remote_refs(fake_repo.git_dir)


def test_cleanup_preserves_open_orphan_record_and_remote_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[0].change_id
    state_store = TrackingStore.for_repo(repo)
    bookmark = state_store.load().prs[change_id].pr_identity.head_ref

    run_command(["jj", "abandon", change_id], repo)
    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()
    normalized_output = " ".join(captured.out.split())

    assert exit_code == 0
    assert "keep open orphan" in normalized_output
    assert change_id in refreshed_state.prs
    assert refreshed_state.prs[change_id].pr_identity.head_ref == bookmark
    assert f"refs/heads/{bookmark}" in remote_refs(fake_repo.git_dir)


def test_cleanup_removes_overview_comment_for_closed_pr(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)

    stack = selected_stack(repo)
    change_id = stack.changes[-1].change_id
    state_store = TrackingStore.for_repo(repo)
    fake_repo.prs[2].state = "closed"
    fake_repo.github_stacks = {}
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=2,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    refreshed_state = state_store.load()

    assert exit_code == 0
    assert "delete stack overview comment" in captured.out
    assert change_id not in refreshed_state.prs
    assert issue_comments(fake_repo, 2) == []


def test_cleanup_finishes_closed_prs_whose_branch_was_deleted_or_moved(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """GitHub's "Delete branch" leaves only the saved link; "Update branch" moves the head."""

    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = configure_submit_environment(monkeypatch, tmp_path, fake_repo)
    deleted = selected_stack(repo).head.change_id
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert run_main(repo, config_path, "submit") == 0
    capsys.readouterr()
    moved = selected_stack(repo).head.change_id
    state_store = TrackingStore.for_repo(repo)
    tracked_prs = state_store.load().prs
    deleted_identity = tracked_prs[deleted].pr_identity
    moved_identity = tracked_prs[moved].pr_identity
    for identity in (deleted_identity, moved_identity):
        fake_repo.prs[identity.pr_number].state = "closed"
    git = ["git", "--git-dir", str(fake_repo.git_dir), "update-ref"]
    run_command(
        [*git, "-d", f"refs/heads/{deleted_identity.head_ref}"],
        fake_repo.git_dir.parent,
    )
    # GitHub's "Update branch" button and its stack rebase both move a PR head off the
    # submitted commit; here the moved head lands on trunk.
    run_command(
        [
            *git,
            f"refs/heads/{moved_identity.head_ref}",
            read_remote_ref(fake_repo.git_dir, "main"),
        ],
        fake_repo.git_dir.parent,
    )
    fake_repo.create_issue_comment(
        body=f"{STACK_OVERVIEW_COMMENT_MARKER}\nstack overview",
        issue_number=moved_identity.pr_number,
    )

    exit_code = run_main(repo, config_path, "cleanup")
    captured = capsys.readouterr()
    output = " ".join(captured.out.split())

    assert exit_code == 0
    assert f"forget the saved link between PR #{deleted_identity.pr_number}" in output
    assert f"delete {moved_identity.head_ref}@origin" in output
    assert not {deleted, moved} & set(state_store.load().prs)
    assert fake_repo.prs[deleted_identity.pr_number].state == "closed"
    assert issue_comments(fake_repo, moved_identity.pr_number) == []
    assert f"refs/heads/{moved_identity.head_ref}" not in remote_refs(fake_repo.git_dir)
