from __future__ import annotations

import io
from pathlib import Path

from jj_stack.cli import main
from jj_stack.jj.client import JjClient
from jj_stack.state.store import ReviewStateStore, resolve_state_path

from ..support.fake_github import FakeGithubRepository
from ..support.integration_helpers import (
    TEST_REVIEW_NAMESPACE,
    commit_file,
    configure_fake_github_environment,
    init_fake_github_repo,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    selected_stack,
)


def test_checkout_pick_fetches_github_stack_then_adopts_and_edits_selected_change(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    publisher, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(publisher, "feature 1", "feature-1.txt")
    commit_file(publisher, "feature 2", "feature-2.txt")
    assert _main(publisher, config_path, "submit") == 0
    expected = ReviewStateStore.for_repo(publisher).load()
    expected_head = selected_stack(publisher).head

    repo = tmp_path / "consumer"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
    client = JjClient(repo)
    client.ensure_review_fetch_isolation(namespace=TEST_REVIEW_NAMESPACE, remote="origin")
    client.fetch_remote(remote="origin")
    run_command(["jj", "bookmark", "create", "main", "-r", "main@origin"], repo)
    assert client.query_revisions_by_commit_ids((expected_head.commit_id,)) == ()
    ReviewStateStore.for_repo(repo).relink_reviews(
        replacements={
            change_id: (
                identity,
                expected.submitted_baselines[change_id],
            )
            for change_id, identity in expected.review_identities.items()
        }
    )
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    captured = capsys.readouterr()
    assert "Available stacks:" in captured.out
    normalized = " ".join(captured.out.split())
    assert "GitHub stack #1 (GitHub only)" in normalized
    assert "Top: PR #2 feature 2" in normalized
    assert "Base: main" in normalized
    assert "Size: 2 PRs" in normalized
    assert "Status: 2 open" in normalized
    assert "Fetched tip commit:" in captured.out
    assert "Working copy now edits" in captured.out
    assert JjClient(repo).resolve_revision("@").change_id == expected_head.change_id
    assert ReviewStateStore.for_repo(repo).load() == expected
    assert client.visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE) == {}
    review_temp = client.review_temp_artifacts()
    assert (review_temp.ref_target, review_temp.bookmark_targets) == (None, ())


def test_checkout_pick_completes_a_partly_local_github_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    publisher, fake_repo = init_fake_github_repo(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    commit_file(publisher, "feature 1", "feature-1.txt")
    commit_file(publisher, "feature 2", "feature-2.txt")
    assert _main(publisher, config_path, "submit") == 0
    expected = ReviewStateStore.for_repo(publisher).load()

    repo = tmp_path / "consumer"
    run_command(["jj", "git", "init", str(repo)], tmp_path)
    run_command(["jj", "git", "remote", "add", "origin", str(fake_repo.git_dir)], repo)
    client = JjClient(repo)
    client.ensure_review_fetch_isolation(namespace=TEST_REVIEW_NAMESPACE, remote="origin")
    client.fetch_remote(remote="origin")
    run_command(["jj", "bookmark", "create", "main", "-r", "main@origin"], repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 0
    assert len(ReviewStateStore.for_repo(repo).load().review_identities) == 1
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    output = " ".join(capsys.readouterr().out.split())
    assert output.count("GitHub stack #1") == 1
    assert "GitHub stack #1 (partly local)" in output
    assert ReviewStateStore.for_repo(repo).load() == expected


def test_checkout_pick_refetches_an_abandoned_tracked_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    run_command(["jj", "abandon", *(revision.change_id for revision in stack.revisions)], repo)
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    output = " ".join(capsys.readouterr().out.split())
    assert "GitHub stack #1 (GitHub only)" in output
    assert JjClient(repo).resolve_revision("@").change_id == stack.head.change_id


def test_checkout_accepts_a_matching_visible_review_bookmark(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    state = state_store.load()
    change_id, identity = next(iter(state.review_identities.items()))
    resolve_state_path(repo).unlink()
    run_command(["jj", "bookmark", "create", identity.head_ref, "-r", change_id], repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 0

    assert "Updated local tracking for 1 review" in capsys.readouterr().out
    assert state_store.load().review_identities == state.review_identities
    assert set(
        JjClient(repo).visible_review_bookmark_targets(namespace=TEST_REVIEW_NAMESPACE)
    ) == {identity.head_ref}


def test_checkout_rejects_a_locally_rewritten_pull_request_before_importing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state_store = ReviewStateStore.for_repo(repo)
    change_id = next(iter(state_store.load().review_identities))
    resolve_state_path(repo).unlink()
    run_command(["jj", "describe", "-r", change_id, "-m", "feature rewritten"], repo)
    capsys.readouterr()

    assert _main(repo, config_path, "checkout", "--pull-request", "1") == 1

    captured = capsys.readouterr()
    unwrapped = " ".join(captured.err.split())
    assert "checkout cannot choose between them" in unwrapped
    assert len(JjClient(repo).query_revisions(f"change_id({change_id})")) == 1


def test_checkout_rejects_a_rewritten_lower_pull_request_before_importing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    stack = selected_stack(repo)
    bottom_change_id = stack.revisions[0].change_id
    fake_repo.force_push_pull_request_head(fake_repo.pull_requests[1])
    resolve_state_path(repo).unlink()
    run_command(["jj", "abandon", stack.head.change_id], repo)
    run_command(
        [
            "git",
            "config",
            "--replace-all",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ],
        repo,
    )
    capsys.readouterr()

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "checkout cannot choose between them" in " ".join(capsys.readouterr().err.split())
    assert len(JjClient(repo).query_revisions(f"change_id({bottom_change_id})")) == 1


def test_checkout_pull_request_rejects_cross_repository_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    initial_state = ReviewStateStore.for_repo(repo).load()
    fake_repo.pull_requests[2].head_label = f"someone-else:{fake_repo.pull_requests[2].head_ref}"

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "does not belong to" in capsys.readouterr().err
    assert ReviewStateStore.for_repo(repo).load() == initial_state


def test_checkout_pull_request_rejects_ambiguous_top_head(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    initial_state = ReviewStateStore.for_repo(repo).load()
    top = fake_repo.pull_requests[2]
    fake_repo.create_pull_request(
        base_ref=top.base_ref,
        body="duplicate",
        head_ref=top.head_ref,
        title="duplicate",
    )

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "does not uniquely identify" in capsys.readouterr().err
    assert ReviewStateStore.for_repo(repo).load() == initial_state


def test_checkout_rejects_missing_parent_remote_branch_without_partial_tracking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    state = ReviewStateStore.for_repo(repo).load()
    stack = selected_stack(repo)
    bottom_branch = state.review_identities[stack.revisions[0].change_id].head_ref
    resolve_state_path(repo).unlink()
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "-d",
            f"refs/heads/{bottom_branch}",
        ],
        repo,
    )

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 1

    assert "no longer identify the same commit" in capsys.readouterr().err
    current = ReviewStateStore.for_repo(repo).load()
    assert current.review_identities == {}
    assert current.submitted_baselines == {}


def test_checkout_reports_up_to_date_for_an_already_attached_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)

    assert _main(repo, config_path, "checkout", "--pull-request", "2") == 0

    assert "Local tracking is already up to date for this stack." in capsys.readouterr().out


def test_checkout_pick_edits_selected_tracked_stack(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo, fake_repo = init_fake_github_repo_with_submitted_feature(tmp_path)
    config_path = _configure_checkout_environment(monkeypatch, tmp_path, fake_repo)
    feature_1_change_id = selected_stack(repo).head.change_id
    run_command(["jj", "new", "main"], repo)
    commit_file(repo, "feature 2", "feature-2.txt")
    assert _main(repo, config_path, "submit") == 0
    feature_2_change_id = selected_stack(repo).head.change_id
    feature_1_identity = (
        ReviewStateStore.for_repo(repo).load().review_identities[feature_1_change_id]
    )
    feature_1_head = fake_repo.ref_target(feature_1_identity.head_ref)
    assert feature_1_head is not None
    run_command(
        [
            "git",
            "--git-dir",
            str(fake_repo.git_dir),
            "update-ref",
            "refs/heads/not-managed",
            feature_1_head,
        ],
        repo,
    )
    unmanaged = fake_repo.create_pull_request(
        base_ref="main",
        head_ref="not-managed",
        title="not adoptable",
        body="",
    )
    fake_repo.github_stacks[9] = (unmanaged.number,)
    capsys.readouterr()

    ordered_heads = sorted((feature_1_change_id, feature_2_change_id))
    selection = ordered_heads.index(feature_1_change_id) + 1
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{selection}\n"))
    assert _main(repo, config_path, "checkout", "--pick") == 0

    captured = capsys.readouterr()
    assert "Available stacks:" in captured.out
    assert "feature 1" in captured.out
    assert "not adoptable" not in captured.out
    assert "Local tracking is already up to date for this stack." in captured.out
    assert "Working copy now edits" in captured.out
    assert JjClient(repo).resolve_revision("@").change_id == feature_1_change_id


def _configure_checkout_environment(
    monkeypatch,
    tmp_path: Path,
    fake_repo: FakeGithubRepository,
) -> Path:
    return configure_fake_github_environment(
        command_modules=(
            "jj_stack.commands.submit.command",
            "jj_stack.review.status",
            "jj_stack.commands.checkout",
        ),
        fake_repo=fake_repo,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )


def _main(repo: Path, config_path: Path, command: str, *command_args: str) -> int:
    return main(
        [
            "--config-file",
            str(config_path),
            "--repository",
            str(repo),
            command,
            *command_args,
        ]
    )
