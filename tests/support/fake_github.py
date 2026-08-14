"""Minimal fake GitHub server used for local integration tests."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from jj_stack.models.github import GithubStack

_FAKE_GITHUB_GIT_ENV = {
    "GIT_AUTHOR_EMAIL": "fake-github@example.com",
    "GIT_AUTHOR_NAME": "Fake GitHub",
    "GIT_COMMITTER_EMAIL": "fake-github@example.com",
    "GIT_COMMITTER_NAME": "Fake GitHub",
}


@dataclass(slots=True)
class FakeGithubPullRequest:
    """Mutable pull request state served by the fake API."""

    base_ref: str
    body: str
    head_label: str
    head_ref: str
    head_sha: str
    is_draft: bool
    merge_commit_sha: str | None
    merged_at: str | None
    node_id: str
    number: int
    title: str
    auto_merge_enabled: bool = False
    is_queued: bool = False
    labels: list[str] = field(default_factory=list)
    requested_reviewers: list[str] = field(default_factory=list)
    requested_team_reviewers: list[str] = field(default_factory=list)
    state: str = "open"

    @property
    def graphql_state(self) -> str:
        return "merged" if self.merged_at is not None else self.state

    def to_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        self._refresh_head_sha(repository)
        return {
            "base": {"label": f"{repository.full_name}:{self.base_ref}", "ref": self.base_ref},
            "body": self.body,
            "draft": self.is_draft,
            "head": {
                "label": self.head_label,
                "ref": self.head_ref,
                "sha": self.head_sha,
            },
            "html_url": f"{web_origin}/{repository.full_name}/pull/{self.number}",
            "merge_commit_sha": self.merge_commit_sha,
            "merged_at": self.merged_at,
            "node_id": self.node_id,
            "number": self.number,
            "state": self.state,
            "title": self.title,
        }

    def to_graphql_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        self._refresh_head_sha(repository)
        return {
            "autoMergeRequest": {"enabledAt": "now"} if self.auto_merge_enabled else None,
            "baseRefName": self.base_ref,
            "body": self.body,
            "headRefName": self.head_ref,
            "headRefOid": self.head_sha,
            "headRepositoryOwner": {"login": repository.owner},
            "id": self.node_id,
            "isDraft": self.is_draft,
            "mergeQueueEntry": {"id": "queue-entry"} if self.is_queued else None,
            "mergeCommit": (
                None if self.merge_commit_sha is None else {"oid": self.merge_commit_sha}
            ),
            "mergedAt": self.merged_at,
            "number": self.number,
            "state": self.graphql_state.upper(),
            "title": self.title,
            "url": f"{web_origin}/{repository.full_name}/pull/{self.number}",
        }

    def _refresh_head_sha(self, repository: FakeGithubRepository) -> None:
        if current_head := repository.ref_target(self.head_ref):
            self.head_sha = current_head


@dataclass(slots=True)
class FakeGithubPullRequestReview:
    """Mutable pull request review state served by the fake API."""

    id: int
    pull_request_number: int
    reviewer_login: str
    state: str

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "state": self.state,
            "user": {"login": self.reviewer_login},
        }

    def to_graphql_payload(self) -> dict[str, object]:
        return {
            "author": {"login": self.reviewer_login},
            "state": self.state,
        }


@dataclass(slots=True)
class FakeStackMergeOperation:
    expected_head_sha: str
    merge_action: str
    merge_method: str | None
    pull_number: int
    uuid: str
    final_sha: str | None = None
    message: str | None = None
    status: str = "pending"


@dataclass(slots=True, frozen=True)
class FakeGithubPullRequestEvent:
    """Observable PR mutation recorded by the fake API."""

    kind: str
    pull_request_number: int


@dataclass(slots=True)
class FakeGithubIssueComment:
    """Mutable issue comment state served by the fake API."""

    body: str
    id: int
    issue_number: int

    def to_payload(self) -> dict[str, object]:
        return {
            "body": self.body,
            "id": self.id,
        }

    def to_graphql_payload(self) -> dict[str, object]:
        return {
            "body": self.body,
            "databaseId": self.id,
        }


@dataclass(slots=True)
class FakeGithubRepository:
    """Repository metadata plus its backing bare Git repository."""

    default_branch: str | None
    git_dir: Path
    name: str
    owner: str
    # The default repository allows only squash merges. Tests that need other
    # repository policies flip these settings directly.
    allow_merge_commit: bool = False
    allow_rebase_merge: bool = False
    allow_squash_merge: bool = True
    merge_queue_enabled: bool = False
    stack_merge_operations: dict[int, FakeStackMergeOperation] = field(default_factory=dict)
    stack_merge_polls: list[tuple[int, str]] = field(default_factory=list)
    stack_merge_requests: list[tuple[int, str | None, str, str]] = field(default_factory=list)
    auto_merge_reachable_heads: bool = True
    next_issue_comment_id: int = 1
    next_github_stack_number: int = 1
    next_pull_request_number: int = 1
    next_pull_request_review_id: int = 1
    issue_comments: dict[int, list[FakeGithubIssueComment]] = field(default_factory=dict)
    github_stacks: dict[int, tuple[int, ...]] = field(default_factory=dict)
    pull_request_events: list[FakeGithubPullRequestEvent] = field(default_factory=list)
    pull_requests: dict[int, FakeGithubPullRequest] = field(default_factory=dict)
    pull_request_reviews: dict[int, list[FakeGithubPullRequestReview]] = field(
        default_factory=dict
    )
    # Test hook: PR numbers GitHub should report as not mergeable (pending
    # required checks, conflicts, or branch protection).
    unmergeable_pull_numbers: set[int] = field(default_factory=set)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_payload(self) -> dict[str, object]:
        return {
            "allow_merge_commit": self.allow_merge_commit,
            "allow_rebase_merge": self.allow_rebase_merge,
            "allow_squash_merge": self.allow_squash_merge,
            "default_branch": self.default_branch,
            "full_name": self.full_name,
        }

    def create_pull_request(
        self,
        *,
        base_ref: str,
        body: str,
        draft: bool = False,
        head_ref: str,
        title: str,
    ) -> FakeGithubPullRequest:
        number = self.next_pull_request_number
        self.next_pull_request_number += 1
        # Tests may construct a historical PR after deleting or without creating
        # its source branch. Real GitHub still retains the PR's last head OID.
        head_sha = self.ref_target(head_ref) or self.ref_target(base_ref)
        if head_sha is None:
            raise AssertionError(
                f"Fake GitHub branches {head_ref!r} and {base_ref!r} do not exist."
            )
        pull_request = FakeGithubPullRequest(
            base_ref=base_ref,
            body=body,
            head_label=f"{self.owner}:{head_ref}",
            head_ref=head_ref,
            head_sha=head_sha,
            is_draft=draft,
            merge_commit_sha=None,
            merged_at=None,
            node_id=f"PR_kwDO_fake_{number}",
            number=number,
            title=title,
        )
        self.pull_requests[number] = pull_request
        return pull_request

    def find_pull_request_by_node_id(self, node_id: str) -> FakeGithubPullRequest | None:
        for pull_request in self.pull_requests.values():
            if pull_request.node_id == node_id:
                return pull_request
        return None

    def stack_number_for_pull(self, pull_number: int) -> int | None:
        for stack_number, pull_numbers in self.github_stacks.items():
            if pull_number in pull_numbers:
                return stack_number
        return None

    def refresh_pull_request_state(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        branch_heads: dict[str, str] | None = None,
    ) -> None:
        # Known idealization: this fake marks an open PR merged whenever its
        # head commits become reachable from its base, on every refresh. Real
        # GitHub's merged-detection may not fire on a base retarget after a
        # direct push, so the closed-but-not-merged finalization family is
        # untestable against this fake. Do not infer real GitHub behavior from
        # this transition without an approved live experiment.
        if not self.auto_merge_reachable_heads or pull_request.state != "open":
            return
        if branch_heads is None:
            branch_heads = self.branch_heads()
        base_commit = branch_heads.get(pull_request.base_ref)
        head_commit = branch_heads.get(pull_request.head_ref)
        if base_commit is None or head_commit is None:
            return
        if not self.is_ancestor(head_commit, base_commit):
            return
        if pull_request.merged_at is None:
            pull_request.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.update_pull_request_state(
            pull_request,
            state="closed",
        )

    def refresh_pull_requests(
        self,
        pull_requests: Iterable[FakeGithubPullRequest],
    ) -> None:
        """Refresh many pull requests off one shared branch-head snapshot."""

        to_refresh = [candidate for candidate in pull_requests if candidate.state == "open"]
        if not to_refresh:
            return
        branch_heads = self.branch_heads()
        for pull_request in to_refresh:
            self.refresh_pull_request_state(pull_request, branch_heads=branch_heads)

    def update_pull_request_base(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        base_ref: str,
    ) -> None:
        if pull_request.base_ref == base_ref:
            return
        pull_request.base_ref = base_ref
        self.pull_request_events.append(
            FakeGithubPullRequestEvent(
                kind="base",
                pull_request_number=pull_request.number,
            )
        )

    def update_pull_request_state(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        state: str,
    ) -> None:
        if pull_request.state == state:
            return
        pull_request.state = state
        self.pull_request_events.append(
            FakeGithubPullRequestEvent(
                kind="state",
                pull_request_number=pull_request.number,
            )
        )

    def ref_target(self, branch: str) -> str | None:
        return self.branch_heads().get(branch)

    def apply_squash_merge(self, pull_request: FakeGithubPullRequest) -> str:
        """Squash-merge the PR's head into its base on the backing Git repo.

        Real GitHub computes a three-way merge before squashing. Using the head
        commit's tree matches that result whenever the base has not diverged
        beyond the PR's merge base, which holds for the merge scenarios these
        tests construct.
        """

        heads = self.branch_heads()
        head_commit = heads[pull_request.head_ref]
        base_commit = heads[pull_request.base_ref]
        tree = self._run_backing_git("rev-parse", f"{head_commit}^{{tree}}")
        squash_commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            "-m",
            f"{pull_request.title} (#{pull_request.number})",
            env=_FAKE_GITHUB_GIT_ENV,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pull_request.base_ref}",
            squash_commit,
        )
        pull_request.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pull_request.merge_commit_sha = squash_commit
        self.update_pull_request_state(
            pull_request,
            state="closed",
        )
        return squash_commit

    def apply_pull_request_merge(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        merge_method: str,
    ) -> str:
        """Apply one ordinary PR merge using GitHub's requested commit shape."""

        if merge_method == "merge":
            return self.apply_merge_commit((pull_request,))
        if merge_method == "rebase":
            return self.apply_rebase_merge(pull_request)
        if merge_method == "squash":
            return self.apply_squash_merge(pull_request)
        raise AssertionError(f"Unknown fake GitHub merge method {merge_method!r}.")

    def apply_rebase_merge(self, pull_request: FakeGithubPullRequest) -> str:
        """Replay one reviewed commit onto its base while preserving its message."""

        heads = self.branch_heads()
        rebase_commit = self._replay_commit(
            commit_id=heads[pull_request.head_ref],
            extra_header="x-fake-rebase-merge true",
            parent_commit_id=heads[pull_request.base_ref],
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pull_request.base_ref}",
            rebase_commit,
        )
        pull_request.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pull_request.merge_commit_sha = rebase_commit
        self.update_pull_request_state(
            pull_request,
            state="closed",
        )
        return rebase_commit

    def apply_merge_commit(
        self,
        pull_requests: tuple[FakeGithubPullRequest, ...],
    ) -> str:
        """Merge one PR or stack PR prefix through a shared merge commit."""

        if not pull_requests:
            raise AssertionError("A merge commit requires at least one pull request.")
        heads = self.branch_heads()
        base_ref = pull_requests[0].base_ref
        base_commit = heads[base_ref]
        head_commit = heads[pull_requests[-1].head_ref]
        tree = self._run_backing_git("rev-parse", f"{head_commit}^{{tree}}")
        merge_commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            "-p",
            head_commit,
            "-m",
            f"Merge through PR #{pull_requests[-1].number}",
            env=_FAKE_GITHUB_GIT_ENV,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{base_ref}",
            merge_commit,
        )
        merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for pull_request in pull_requests:
            pull_request.merged_at = merged_at
            pull_request.merge_commit_sha = merge_commit
            self.update_pull_request_state(
                pull_request,
                state="closed",
            )
        return merge_commit

    def rewrite_pull_request_onto_base(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        base_ref: str,
    ) -> str:
        """Model GitHub retargeting and replaying an active stack survivor."""

        heads = self.branch_heads()
        rewritten = self._replay_commit(
            commit_id=heads[pull_request.head_ref],
            extra_header="x-fake-stack-rewrite true",
            parent_commit_id=heads[base_ref],
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pull_request.head_ref}",
            rewritten,
        )
        self.update_pull_request_base(
            pull_request,
            base_ref=base_ref,
        )
        pull_request.head_sha = rewritten
        return rewritten

    def advance_branch(self, branch: str, *, path: str, contents: str) -> str:
        """Add one file in a new commit on a backing branch."""

        parent = self.ref_target(branch)
        if parent is None:
            raise AssertionError(f"Missing fake GitHub branch {branch}")
        parent_tree = self._run_backing_git("rev-parse", f"{parent}^{{tree}}")
        tree = self._tree_with_file(parent_tree, path=path, contents=contents)
        commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            parent,
            "-m",
            "advance trunk for stack rebase",
            env=_FAKE_GITHUB_GIT_ENV,
        )
        self._run_backing_git("update-ref", f"refs/heads/{branch}", commit)
        return commit

    def rebase_stack_onto_base(self, stack_number: int, *, base_ref: str) -> tuple[str, ...]:
        """Model GitHub's native stack rebase, which drops jj change-ID headers.

        A credentialed test against GitHub's stack UI confirmed this commit shape on 2026-08-13.
        """

        members = self.github_stacks[stack_number]
        original_heads = self.branch_heads()
        parent = original_heads[base_ref]
        rewritten_heads: list[str] = []
        expected_base = base_ref
        for pull_number in members:
            pull_request = self.pull_requests[pull_number]
            original = original_heads[pull_request.head_ref]
            original_parent = self._run_backing_git("rev-parse", f"{original}^")
            tree = self._run_backing_git(
                "merge-tree",
                "--write-tree",
                f"--merge-base={original_parent}",
                parent,
                original,
            )
            rewritten = self._replay_commit(
                commit_id=original,
                drop_change_id=True,
                extra_header="x-fake-github-stack-rebase true",
                parent_commit_id=parent,
                tree_id=tree,
            )
            self._run_backing_git(
                "update-ref",
                f"refs/heads/{pull_request.head_ref}",
                rewritten,
            )
            self.update_pull_request_base(pull_request, base_ref=expected_base)
            pull_request.head_sha = rewritten
            rewritten_heads.append(rewritten)
            expected_base = pull_request.head_ref
            parent = rewritten
        return tuple(rewritten_heads)

    def replace_pull_request_head_contents(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        path: str,
        contents: str,
    ) -> str:
        """Replace one rewritten PR head with a same-parent commit containing another file."""

        head = self.ref_target(pull_request.head_ref)
        if head is None:
            raise AssertionError(f"Missing fake GitHub branch {pull_request.head_ref}")
        parent = self._run_backing_git("rev-parse", f"{head}^")
        original_tree = self._run_backing_git("rev-parse", f"{head}^{{tree}}")
        tree = self._tree_with_file(original_tree, path=path, contents=contents)
        rewritten = self._replay_commit(
            commit_id=head,
            drop_change_id=True,
            extra_header="x-fake-github-content-edit true",
            parent_commit_id=parent,
            tree_id=tree,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pull_request.head_ref}",
            rewritten,
        )
        pull_request.head_sha = rewritten
        return rewritten

    def _tree_with_file(self, tree: str, *, path: str, contents: str) -> str:
        blob = self._run_backing_git("hash-object", "-w", "--stdin", stdin=contents)
        entries = self._run_backing_git("ls-tree", tree)
        return self._run_backing_git(
            "mktree",
            stdin=f"{entries}\n100644 blob {blob}\t{path}\n",
        )

    def force_push_pull_request_head(self, pull_request: FakeGithubPullRequest) -> str:
        """Rewrite one PR head externally while preserving its jj change ID."""

        head = self.ref_target(pull_request.head_ref)
        if head is None:
            raise AssertionError(f"Missing fake GitHub branch {pull_request.head_ref}")
        parent = self._run_backing_git("rev-parse", f"{head}^")
        rewritten = self._replay_commit(
            commit_id=head,
            extra_header="x-fake-force-push true",
            message_suffix="\nexternal rewrite",
            parent_commit_id=parent,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pull_request.head_ref}",
            rewritten,
        )
        pull_request.head_sha = rewritten
        return rewritten

    def _replay_commit(
        self,
        *,
        commit_id: str,
        drop_change_id: bool = False,
        extra_header: str | None = None,
        message_suffix: str = "",
        parent_commit_id: str,
        tree_id: str | None = None,
    ) -> str:
        tree = tree_id or self._run_backing_git("rev-parse", f"{commit_id}^{{tree}}")
        raw_commit = self._run_backing_git("cat-file", "commit", commit_id)
        headers, separator, message = raw_commit.partition("\n\n")
        rewritten_headers = [
            f"tree {tree}" if line.startswith("tree ") else line
            for line in headers.splitlines()
            if not line.startswith("parent ")
            and not (drop_change_id and line.startswith("change-id "))
        ]
        rewritten_headers.insert(1, f"parent {parent_commit_id}")
        if extra_header is not None:
            rewritten_headers.append(extra_header)
        return self._run_backing_git(
            "hash-object",
            "-t",
            "commit",
            "-w",
            "--stdin",
            stdin=f"{'\n'.join(rewritten_headers)}{separator}{message}{message_suffix}\n",
        )

    def _run_backing_git(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> str:
        completed = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), *args],
            capture_output=True,
            check=False,
            env=None if env is None else {**os.environ, **env},
            input=None if stdin is None else stdin.encode("utf-8"),
        )
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
        if completed.returncode != 0:
            raise AssertionError(
                f"fake github git {args} failed:\nstdout={stdout}\nstderr={stderr}"
            )
        return stdout.strip()

    def branch_heads(self) -> dict[str, str]:
        """Read every branch target in one git invocation."""

        completed = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "show-ref", "--heads"],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            return {}
        heads: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            commit_id, _, ref_name = line.partition(" ")
            if ref_name.startswith("refs/heads/"):
                heads[ref_name.removeprefix("refs/heads/")] = commit_id
        return heads

    def is_ancestor(self, ancestor_commit: str, descendant_commit: str) -> bool:
        completed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.git_dir),
                "merge-base",
                "--is-ancestor",
                ancestor_commit,
                descendant_commit,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        return completed.returncode == 0

    def list_pull_request_reviews(self, pull_number: int) -> list[FakeGithubPullRequestReview]:
        self._require_issue_number(pull_number)
        return list(self.pull_request_reviews.get(pull_number, ()))

    def create_pull_request_review(
        self,
        *,
        pull_number: int,
        reviewer_login: str,
        state: str,
    ) -> FakeGithubPullRequestReview:
        self._require_issue_number(pull_number)
        review = FakeGithubPullRequestReview(
            id=self.next_pull_request_review_id,
            pull_request_number=pull_number,
            reviewer_login=reviewer_login,
            state=state,
        )
        self.next_pull_request_review_id += 1
        self.pull_request_reviews.setdefault(pull_number, []).append(review)
        return review

    def list_issue_comments(self, issue_number: int) -> list[FakeGithubIssueComment]:
        self._require_issue_number(issue_number)
        return list(self.issue_comments.get(issue_number, ()))

    def create_issue_comment(
        self,
        *,
        body: str,
        issue_number: int,
    ) -> FakeGithubIssueComment:
        self._require_issue_number(issue_number)
        comment = FakeGithubIssueComment(
            body=body,
            id=self.next_issue_comment_id,
            issue_number=issue_number,
        )
        self.next_issue_comment_id += 1
        self.issue_comments.setdefault(issue_number, []).append(comment)
        return comment

    def update_issue_comment(
        self,
        *,
        body: str,
        comment_id: int,
    ) -> FakeGithubIssueComment | None:
        for comments in self.issue_comments.values():
            for comment in comments:
                if comment.id == comment_id:
                    comment.body = body
                    return comment
        return None

    def delete_issue_comment(self, *, comment_id: int) -> bool:
        for issue_number, comments in self.issue_comments.items():
            for index, comment in enumerate(comments):
                if comment.id == comment_id:
                    del comments[index]
                    if not comments:
                        self.issue_comments.pop(issue_number, None)
                    return True
        return False

    def _require_issue_number(self, issue_number: int) -> None:
        if issue_number not in self.pull_requests:
            raise HTTPException(status_code=404, detail="Not Found")


@dataclass(slots=True, frozen=True)
class FakeGithubState:
    """Static state served by the fake GitHub app."""

    repositories: dict[tuple[str, str], FakeGithubRepository]
    web_origin: str = "https://github.test"

    @classmethod
    def single_repository(cls, repository: FakeGithubRepository) -> FakeGithubState:
        return cls(repositories={(repository.owner, repository.name): repository})


def create_app(fake_state: FakeGithubState) -> FastAPI:
    """Create a FastAPI app that serves the configured fake GitHub state."""

    app = FastAPI(docs_url=None, redoc_url=None, title="fake-github")

    @app.exception_handler(HTTPException)
    async def _github_shaped_error(_request: Request, error: HTTPException) -> JSONResponse:
        """Report errors the way GitHub does, as `message`, not FastAPI's `detail`.

        Production code reads GitHub's reason out of this body to quote back to the user, so the
        shape has to be GitHub's or the fake would exercise a path real GitHub never takes.
        """

        return JSONResponse(
            {"message": error.detail, "status": str(error.status_code)},
            status_code=error.status_code,
        )

    _register_repository_routes(app, fake_state)
    _register_github_stack_routes(app, fake_state)
    _register_graphql_routes(app, fake_state)
    _register_pull_request_routes(app, fake_state)
    _register_issue_comment_routes(app, fake_state)
    return app


def _register_repository_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register repository metadata routes on the fake GitHub app."""

    @app.get("/repos/{owner}/{repo}")
    async def get_repository(owner: str, repo: str) -> dict[str, object]:
        repository = fake_state.repositories.get((owner, repo))
        if repository is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return repository.to_payload()


def _register_github_stack_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register the observed stack routes."""

    @app.get("/repos/{owner}/{repo}/stacks")
    async def list_stacks(owner: str, repo: str) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        return [
            _stack_payload(repository, number, members)
            for number, members in sorted(_github_stacks(repository).items())
        ]

    @app.get("/repos/{owner}/{repo}/stacks/{stack_number}")
    async def get_stack(
        owner: str,
        repo: str,
        stack_number: int,
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        members = _github_stacks(repository).get(stack_number)
        if members is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return _stack_payload(repository, stack_number, members)

    @app.post("/repos/{owner}/{repo}/stacks", status_code=201)
    async def create_stack(
        owner: str,
        repo: str,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        members = _require_int_list(payload, "pull_requests")
        if len(members) < 2:
            raise HTTPException(status_code=422, detail="A stack requires two pull requests.")
        already = sorted(
            set(members).intersection(
                member for existing in _github_stacks(repository).values() for member in existing
            )
        )
        if already:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Pull requests "
                    + ", ".join(f"#{number}" for number in already)
                    + " are already part of a stack"
                ),
            )
        _validate_stack_members(
            repository,
            admitted_members=members,
            chained_members=members,
            complete_members=members,
        )
        stacks = _github_stacks(repository)
        number = repository.next_github_stack_number
        while number in stacks:
            number += 1
        repository.next_github_stack_number = number + 1
        stacks[number] = members
        return _stack_payload(repository, number, members)

    @app.post("/repos/{owner}/{repo}/stacks/{stack_number}/add")
    async def append_to_stack(
        owner: str,
        repo: str,
        stack_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        stacks = _github_stacks(repository)
        existing = stacks.get(stack_number)
        added = _require_int_list(payload, "pull_requests")
        if existing is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if not added:
            raise HTTPException(status_code=422, detail="No pull requests to append.")
        members = (*existing, *added)
        active_existing = GithubStack.model_validate(
            _stack_payload(repository, stack_number, existing)
        ).active_pull_request_numbers
        _validate_stack_members(
            repository,
            admitted_members=added,
            allowed_stack=stack_number,
            chained_members=(*active_existing, *added),
            complete_members=members,
        )
        stacks[stack_number] = members
        return _stack_payload(repository, stack_number, members)

    @app.post(
        "/repos/{owner}/{repo}/stacks/{stack_number}/unstack",
        response_model=None,
    )
    async def unstack(owner: str, repo: str, stack_number: int) -> Response:
        repository = _get_repository(fake_state, owner, repo)
        stacks = _github_stacks(repository)
        if stack_number not in stacks:
            raise HTTPException(status_code=404, detail="Not Found")
        members = stacks[stack_number]
        prs = repository.pull_requests
        retained = tuple(
            number for number in members if prs[number].is_queued or prs[number].merged_at
        )
        if retained == members and any(prs[number].is_queued for number in members):
            raise HTTPException(status_code=422, detail="No pull requests can be removed.")
        if retained:
            stacks[stack_number] = retained
            return JSONResponse(_stack_payload(repository, stack_number, retained))
        del stacks[stack_number]
        return Response(status_code=204)


def _register_graphql_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register GraphQL routes on the fake GitHub app."""

    @app.post("/graphql")
    async def graphql(
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        query = _require_string(payload, "query")
        raw_variables = payload.get("variables")
        if raw_variables is None:
            raw_variables = {}
        if not isinstance(raw_variables, dict):
            raise HTTPException(status_code=422, detail="Expected 'variables' to be an object.")
        if "markPullRequestReadyForReview" in query:
            pull_request_id = _require_graphql_variable(raw_variables, "pullRequestId")
            pull_request, repository = _find_pull_request_by_node_id(
                fake_state,
                pull_request_id,
            )
            repository.refresh_pull_request_state(pull_request)
            pull_request.is_draft = False
            return {
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": pull_request.to_graphql_payload(
                            repository=repository,
                            web_origin=fake_state.web_origin,
                        )
                    }
                }
            }
        if "convertPullRequestToDraft" in query:
            pull_request_id = _require_graphql_variable(raw_variables, "pullRequestId")
            pull_request, repository = _find_pull_request_by_node_id(
                fake_state,
                pull_request_id,
            )
            repository.refresh_pull_request_state(pull_request)
            pull_request.is_draft = True
            return {
                "data": {
                    "convertPullRequestToDraft": {
                        "pullRequest": pull_request.to_graphql_payload(
                            repository=repository,
                            web_origin=fake_state.web_origin,
                        )
                    }
                }
            }
        owner = _require_graphql_variable(raw_variables, "owner")
        repo = _require_graphql_variable(raw_variables, "repo")
        repository = _get_repository(fake_state, owner, repo)
        return {
            "data": {
                "repository": _graphql_repository_payload(
                    query=query,
                    repository=repository,
                    web_origin=fake_state.web_origin,
                )
            }
        }


def _register_pull_request_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register pull-request, issue, label, and review routes."""

    @app.post("/repos/{owner}/{repo}/pulls", status_code=201)
    async def create_pull_request(
        owner: str,
        repo: str,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        title = _require_string(payload, "title")
        head_ref = _require_string(payload, "head")
        base_ref = _require_string(payload, "base")
        body = _optional_string(payload, "body") or ""
        draft = _optional_bool(payload, "draft") or False
        _require_branch(repository, head_ref)
        _require_branch(repository, base_ref)
        pull_request = repository.create_pull_request(
            base_ref=base_ref,
            body=body,
            draft=draft,
            head_ref=head_ref,
            title=title,
        )
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.get("/repos/{owner}/{repo}/pulls/{pull_number}")
    async def get_pull_request(
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        repository.refresh_pull_request_state(pull_request)
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.patch("/repos/{owner}/{repo}/pulls/{pull_number}")
    async def update_pull_request(
        owner: str,
        repo: str,
        pull_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if "base" in payload and repository.stack_number_for_pull(pull_number) is not None:
            raise HTTPException(
                status_code=422,
                detail="A stacked pull request's base cannot be updated directly.",
            )
        repository.refresh_pull_request_state(pull_request)
        title = _require_string(payload, "title") if "title" in payload else None
        body = (_optional_string(payload, "body") or "") if "body" in payload else None
        base_ref = _require_string(payload, "base") if "base" in payload else None
        if base_ref is not None:
            _require_branch(repository, base_ref)
        if title is not None:
            pull_request.title = title
        if body is not None:
            pull_request.body = body
        if base_ref is not None:
            repository.update_pull_request_base(
                pull_request,
                base_ref=base_ref,
            )
        repository.refresh_pull_request_state(pull_request)
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.put("/repos/{owner}/{repo}/pulls/{pull_number}/merge-async")
    async def submit_stack_merge(
        owner: str,
        repo: str,
        pull_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> Response:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        merge_action = _require_string(payload, "merge_action")
        merge_method = _optional_string(payload, "merge_method")
        expected_head_sha = _require_string(payload, "sha")
        allowed = {
            "merge": repository.allow_merge_commit,
            "rebase": repository.allow_rebase_merge,
            "squash": repository.allow_squash_merge,
        }
        if merge_action not in {"direct_merge", "merge_queue"}:
            raise HTTPException(status_code=400, detail="Unknown merge action.")
        if repository.merge_queue_enabled != (merge_action == "merge_queue"):
            raise HTTPException(status_code=400, detail="Merge action does not match policy.")
        if merge_action == "direct_merge" and not allowed.get(merge_method or "", False):
            raise HTTPException(status_code=400, detail="Merge method is not allowed.")
        live_head = repository.ref_target(pull_request.head_ref)
        if live_head != expected_head_sha:
            raise HTTPException(status_code=400, detail="Target head changed.")
        existing = repository.stack_merge_operations.get(pull_number)
        if existing is not None:
            if (
                existing.expected_head_sha != expected_head_sha
                or existing.merge_action != merge_action
                or existing.merge_method != merge_method
            ):
                return JSONResponse(_stack_merge_payload(existing), status_code=409)
            status_code = 409 if existing.status == "pending" else 200
            return JSONResponse(_stack_merge_payload(existing), status_code=status_code)
        stack_number = repository.stack_number_for_pull(pull_number)
        active_pull_numbers = (
            GithubStack.model_validate(
                _stack_payload(
                    repository,
                    stack_number,
                    _github_stacks(repository)[stack_number],
                )
            ).active_pull_request_numbers
            if stack_number is not None
            else (pull_number,)
        )
        if pull_number not in active_pull_numbers or pull_request.is_draft:
            raise HTTPException(status_code=400, detail="Target is not mergeable.")
        operation = FakeStackMergeOperation(
            expected_head_sha=expected_head_sha,
            merge_action=merge_action,
            merge_method=merge_method,
            pull_number=pull_number,
            uuid=f"fake-stack-merge-{len(repository.stack_merge_operations) + 1}",
        )
        repository.stack_merge_operations[pull_number] = operation
        repository.stack_merge_requests.append(
            (pull_number, merge_method, merge_action, expected_head_sha)
        )
        return JSONResponse(_stack_merge_payload(operation), status_code=202)

    @app.get("/repos/{owner}/{repo}/pulls/{pull_number}/merge-async/{operation_uuid}")
    async def poll_stack_merge(
        owner: str,
        repo: str,
        pull_number: int,
        operation_uuid: str,
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        operation = repository.stack_merge_operations.get(pull_number)
        if operation is None or operation.uuid != operation_uuid:
            raise HTTPException(status_code=404, detail="Not Found")
        repository.stack_merge_polls.append((pull_number, operation_uuid))
        if operation.status == "pending":
            _complete_stack_merge(repository, operation)
        return _stack_merge_payload(operation)

    @app.patch("/repos/{owner}/{repo}/issues/{issue_number}")
    async def update_issue(
        owner: str,
        repo: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(issue_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        state = _require_string(payload, "state")
        if state not in {"open", "closed"}:
            raise HTTPException(status_code=422, detail="Unsupported issue state.")
        repository.update_pull_request_state(
            pull_request,
            state=state,
        )
        if state == "closed":
            repository.refresh_pull_request_state(pull_request)
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.post(
        "/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers",
        status_code=201,
    )
    async def request_reviewers(
        owner: str,
        repo: str,
        pull_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        reviewers = payload.get("reviewers", [])
        team_reviewers = payload.get("team_reviewers", [])
        if isinstance(reviewers, list):
            for reviewer in reviewers:
                normalized = str(reviewer)
                if normalized not in pull_request.requested_reviewers:
                    pull_request.requested_reviewers.append(normalized)
        if isinstance(team_reviewers, list):
            for team_reviewer in team_reviewers:
                normalized = str(team_reviewer)
                if normalized not in pull_request.requested_team_reviewers:
                    pull_request.requested_team_reviewers.append(normalized)
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.post("/repos/{owner}/{repo}/issues/{issue_number}/labels")
    async def add_labels(
        owner: str,
        repo: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(issue_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        labels = payload.get("labels", [])
        if isinstance(labels, list):
            pull_request.labels = [str(label) for label in labels]
        return [{"name": label} for label in pull_request.labels]

    @app.get("/repos/{owner}/{repo}/pulls/{pull_number}/reviews")
    async def list_pull_request_reviews(
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        reviews = repository.list_pull_request_reviews(pull_number)
        return [
            review.to_payload() for review in sorted(reviews, key=lambda candidate: candidate.id)
        ]


def _register_issue_comment_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register issue comment routes on the fake GitHub app."""

    @app.get("/repos/{owner}/{repo}/issues/{issue_number}/comments")
    async def list_issue_comments(
        owner: str,
        repo: str,
        issue_number: int,
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        comments = repository.list_issue_comments(issue_number)
        return [
            comment.to_payload()
            for comment in sorted(comments, key=lambda candidate: candidate.id)
        ]

    @app.post("/repos/{owner}/{repo}/issues/{issue_number}/comments", status_code=201)
    async def create_issue_comment(
        owner: str,
        repo: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        comment = repository.create_issue_comment(
            body=_require_string(payload, "body"),
            issue_number=issue_number,
        )
        return comment.to_payload()

    @app.patch("/repos/{owner}/{repo}/issues/comments/{comment_id}")
    async def update_issue_comment(
        owner: str,
        repo: str,
        comment_id: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        comment = repository.update_issue_comment(
            body=_require_string(payload, "body"),
            comment_id=comment_id,
        )
        if comment is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return comment.to_payload()

    @app.delete(
        "/repos/{owner}/{repo}/issues/comments/{comment_id}",
        response_model=None,
        status_code=204,
    )
    async def delete_issue_comment(
        owner: str,
        repo: str,
        comment_id: int,
    ) -> Response:
        repository = _get_repository(fake_state, owner, repo)
        deleted = repository.delete_issue_comment(comment_id=comment_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Not Found")
        return Response(status_code=204)


def initialize_bare_repository(
    root_dir: Path,
    *,
    owner: str,
    name: str,
    default_branch: str = "main",
) -> FakeGithubRepository:
    """Create a bare Git repository that the fake server can expose."""

    owner_dir = root_dir / owner
    owner_dir.mkdir(parents=True, exist_ok=True)
    git_dir = owner_dir / f"{name}.git"

    subprocess.run(
        ["git", "init", "--bare", str(git_dir)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", f"refs/heads/{default_branch}"],
        capture_output=True,
        check=True,
        cwd=git_dir,
        text=True,
    )

    return FakeGithubRepository(
        default_branch=default_branch,
        git_dir=git_dir,
        name=name,
        owner=owner,
    )


def _get_repository(state: FakeGithubState, owner: str, repo: str) -> FakeGithubRepository:
    repository = state.repositories.get((owner, repo))
    if repository is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return repository


def _find_pull_request_by_node_id(
    state: FakeGithubState,
    node_id: str,
) -> tuple[FakeGithubPullRequest, FakeGithubRepository]:
    for repository in state.repositories.values():
        pull_request = repository.find_pull_request_by_node_id(node_id)
        if pull_request is not None:
            return pull_request, repository
    raise HTTPException(status_code=404, detail="Not Found")


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise HTTPException(status_code=422, detail=f"Expected {key!r} to be a string.")


def _optional_bool(payload: dict[str, object], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise HTTPException(status_code=422, detail=f"Expected {key!r} to be a boolean.")


def _require_int_list(payload: dict[str, object], key: str) -> tuple[int, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in value
    ):
        raise HTTPException(status_code=422, detail=f"Expected {key!r} to be an integer array.")
    return tuple(value)


def _github_stacks(repository: FakeGithubRepository) -> dict[int, tuple[int, ...]]:
    # GitHub refuses to put one pull request in two stacks: creating a second reports
    # "Pull requests #N are already part of a stack" (422, confirmed against the API). Tests
    # assign this mapping directly, so refuse the impossible shape here rather than let a fixture
    # justify production code defending against it. Unstack can leave one member.
    seen: set[int] = set()
    for members in repository.github_stacks.values():
        assert len(members) >= 1, f"fake GitHub was given an empty stack {members}"
        overlap = seen.intersection(members)
        assert not overlap, (
            f"fake GitHub was given pull requests {sorted(overlap)} in more than one stack, "
            "which GitHub rejects"
        )
        seen.update(members)
    return repository.github_stacks


def _stack_payload(
    repository: FakeGithubRepository,
    number: int,
    members: tuple[int, ...],
) -> dict[str, object]:
    return {
        "number": number,
        "pull_requests": [
            _stack_pull_request_payload(repository, pull_number) for pull_number in members
        ],
    }


def _stack_pull_request_payload(
    repository: FakeGithubRepository,
    pull_number: int,
) -> dict[str, object]:
    pull_request = repository.pull_requests.get(pull_number)
    if pull_request is None:
        return {
            "head": {"ref": f"jj-stack/pull-{pull_number}", "sha": f"head-{pull_number}"},
            "merged_at": None,
            "number": pull_number,
            "state": "open",
        }
    pull_request._refresh_head_sha(repository)
    return {
        "head": {"ref": pull_request.head_ref, "sha": pull_request.head_sha},
        "merged_at": pull_request.merged_at,
        "number": pull_number,
        "state": pull_request.state,
    }


def _stack_merge_payload(operation: FakeStackMergeOperation) -> dict[str, object]:
    return {
        "status": operation.status,
        "details": {
            "expected_head_sha": operation.expected_head_sha,
            "merge_action": operation.merge_action,
            "merge_method": operation.merge_method,
            "message": operation.message,
            "sha": operation.final_sha,
            "uuid": operation.uuid,
        },
    }


def _complete_stack_merge(
    repository: FakeGithubRepository,
    operation: FakeStackMergeOperation,
) -> None:
    stack_number = repository.stack_number_for_pull(operation.pull_number)
    if stack_number is None:
        candidate_numbers = (operation.pull_number,)
        survivors: tuple[int, ...] = ()
    else:
        stack = GithubStack.model_validate(
            _stack_payload(repository, stack_number, _github_stacks(repository)[stack_number])
        )
        target_index = stack.active_pull_request_numbers.index(operation.pull_number)
        candidate_numbers = stack.active_pull_request_numbers[: target_index + 1]
        survivors = stack.active_pull_request_numbers[len(candidate_numbers) :]
    candidates = tuple(repository.pull_requests[number] for number in candidate_numbers)
    if any(
        pull_request.state != "open"
        or pull_request.is_draft
        or pull_request.number in repository.unmergeable_pull_numbers
        for pull_request in candidates
    ):
        operation.status = "failed"
        operation.message = "The GitHub stack prefix is not mergeable."
        return
    if operation.merge_action == "merge_queue":
        for pull_request in candidates:
            pull_request.is_queued = True
        operation.status = "enqueued"
        operation.message = "Pull requests were added to the merge queue."
        return
    for pull_request in candidates:
        if pull_request.base_ref != repository.default_branch:
            repository.update_pull_request_base(
                pull_request,
                base_ref=repository.default_branch or "main",
            )
    if operation.merge_method == "merge":
        repository.apply_merge_commit(candidates)
    else:
        assert operation.merge_method is not None
        for pull_request in candidates:
            repository.apply_pull_request_merge(
                pull_request,
                merge_method=operation.merge_method,
            )
    previous_base = repository.default_branch or "main"
    for pull_number in survivors:
        pull_request = repository.pull_requests[pull_number]
        repository.rewrite_pull_request_onto_base(
            pull_request,
            base_ref=previous_base,
        )
        previous_base = pull_request.head_ref
    operation.final_sha = repository.ref_target(repository.default_branch or "main")
    operation.status = "merged"


def _validate_stack_members(
    repository: FakeGithubRepository,
    *,
    admitted_members: tuple[int, ...],
    allowed_stack: int | None = None,
    chained_members: tuple[int, ...],
    complete_members: tuple[int, ...],
) -> None:
    pull_requests = {number: repository.pull_requests.get(number) for number in complete_members}
    if len(set(complete_members)) != len(complete_members):
        raise HTTPException(status_code=422, detail="Duplicate pull request.")
    if any(pull_request is None for pull_request in pull_requests.values()):
        raise HTTPException(status_code=422, detail="Pull request does not exist.")
    resolved = {
        number: pull_request
        for number, pull_request in pull_requests.items()
        if pull_request is not None
    }
    repository.refresh_pull_requests(resolved.values())
    if any(
        (pull_request := resolved[number]).state != "open"
        or pull_request.auto_merge_enabled
        or pull_request.is_queued
        for number in admitted_members
    ):
        raise HTTPException(status_code=422, detail="Pull request is not admissible.")
    if any(
        resolved[current_number].base_ref != resolved[previous_number].head_ref
        for previous_number, current_number in zip(
            chained_members,
            chained_members[1:],
            strict=False,
        )
    ):
        raise HTTPException(status_code=422, detail="Pull request bases do not form a chain.")
    for number, existing in _github_stacks(repository).items():
        if number != allowed_stack and not set(existing).isdisjoint(admitted_members):
            raise HTTPException(
                status_code=422, detail="Pull request already belongs to a stack."
            )


def _require_branch(repository: FakeGithubRepository, branch: str) -> None:
    completed = subprocess.run(
        [
            "git",
            "--git-dir",
            str(repository.git_dir),
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode == 0:
        return
    raise HTTPException(status_code=422, detail=f"Branch {branch!r} does not exist.")


def _require_string(payload: dict[str, object], key: str) -> str:
    value = _optional_string(payload, key)
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing required field {key!r}.")
    return value


def _require_graphql_variable(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    raise HTTPException(status_code=422, detail=f"Expected GraphQL variable {key!r}.")


def _graphql_repository_payload(
    *,
    query: str,
    repository: FakeGithubRepository,
    web_origin: str,
) -> dict[str, object]:
    if "BaseBranchMergeQueue" in query:
        return {
            "mergeQueue": ({"id": "merge-queue"} if repository.merge_queue_enabled else None),
            "ref": {
                "rules": {
                    "nodes": ([{"type": "MERGE_QUEUE"}] if repository.merge_queue_enabled else [])
                }
            },
        }
    lines = query.splitlines()
    ref_queries: list[tuple[str, str, str, int, frozenset[str]]] = []
    for index, line in enumerate(lines):
        alias, separator, selection = line.strip().partition(":")
        if not separator or not alias.isidentifier():
            continue
        if not selection.lstrip().startswith("pullRequests("):
            continue
        first = 100
        ref_kind: str | None = None
        ref_value: str | None = None
        states: frozenset[str] = frozenset()
        for argument_line in lines[index + 1 :]:
            stripped_argument = argument_line.strip()
            if stripped_argument.startswith(")"):
                break
            if stripped_argument.startswith("first:"):
                first = int(stripped_argument.removeprefix("first:").strip().removesuffix(","))
            elif stripped_argument.startswith("states:"):
                raw_states = stripped_argument.removeprefix("states:").strip().removesuffix(",")
                states = frozenset(
                    state.strip().lower() for state in raw_states.strip("[]").split(",")
                )
            elif stripped_argument.startswith("headRefName:"):
                ref_kind = "head"
                ref_value = json.loads(stripped_argument.removeprefix("headRefName:").strip())
            elif stripped_argument.startswith("baseRefName:"):
                ref_kind = "base"
                ref_value = json.loads(stripped_argument.removeprefix("baseRefName:").strip())
        if ref_kind is not None and ref_value is not None:
            ref_queries.append((alias, ref_kind, ref_value, first, states))

    if ref_queries:
        payload: dict[str, object] = {}
        for alias, ref_kind, ref_value, first, states in ref_queries:
            matching_pull_requests = [
                pull_request
                for pull_request in repository.pull_requests.values()
                if (pull_request.head_ref if ref_kind == "head" else pull_request.base_ref)
                == ref_value
            ]
            repository.refresh_pull_requests(matching_pull_requests)
            matching_pull_requests = sorted(
                (
                    pull_request
                    for pull_request in matching_pull_requests
                    if not states or pull_request.graphql_state in states
                ),
                key=lambda candidate: candidate.number,
            )
            payload[alias] = {
                "nodes": [
                    _graphql_pull_request_payload(
                        pull_request=pull_request,
                        repository=repository,
                        web_origin=web_origin,
                        refreshed=True,
                    )
                    for pull_request in matching_pull_requests
                ][:first]
            }
        return payload

    pull_request_number_queries: list[tuple[str, int]] = []
    for line in lines:
        alias, separator, selection = line.strip().partition(":")
        if not separator or not alias.isidentifier():
            continue
        selection = selection.lstrip()
        if not selection.startswith("pullRequest(number:"):
            continue
        number_text = selection.removeprefix("pullRequest(number:").partition(")")[0]
        pull_request_number_queries.append((alias, int(number_text.strip())))

    if not pull_request_number_queries:
        raise HTTPException(status_code=422, detail="Unsupported GraphQL query.")

    payload: dict[str, object] = {}
    requested_pull_requests = [
        pull_request
        for _alias, pull_number in pull_request_number_queries
        if (pull_request := repository.pull_requests.get(pull_number)) is not None
    ]
    repository.refresh_pull_requests(requested_pull_requests)
    for alias, pull_number in pull_request_number_queries:
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            payload[alias] = None
            continue
        graphql_payload = _graphql_pull_request_payload(
            pull_request=pull_request,
            repository=repository,
            web_origin=web_origin,
            refreshed=True,
        )
        if "comments(" in query:
            graphql_payload["comments"] = {
                "nodes": [
                    comment.to_graphql_payload()
                    for comment in sorted(
                        repository.list_issue_comments(pull_number),
                        key=lambda candidate: candidate.id,
                    )
                ],
                "pageInfo": {"hasNextPage": False},
            }
        payload[alias] = graphql_payload
    return payload


def _graphql_pull_request_payload(
    *,
    pull_request: FakeGithubPullRequest,
    repository: FakeGithubRepository,
    web_origin: str,
    refreshed: bool = False,
) -> dict[str, object]:
    if not refreshed:
        repository.refresh_pull_request_state(pull_request)
    payload = pull_request.to_graphql_payload(
        repository=repository,
        web_origin=web_origin,
    )
    payload["reviewDecision"] = _graphql_review_decision(repository, pull_request.number)
    return payload


def _graphql_review_decision(
    repository: FakeGithubRepository,
    pull_number: int,
) -> str | None:
    review_states = {
        str(raw_review["state"]).upper()
        for raw_review in _latest_opinionated_review_payloads(repository, pull_number)
    }
    if "CHANGES_REQUESTED" in review_states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in review_states:
        return "APPROVED"
    return None


def _latest_opinionated_review_payloads(
    repository: FakeGithubRepository,
    pull_number: int,
) -> list[dict[str, object]]:
    latest_by_reviewer: dict[str, FakeGithubPullRequestReview] = {}
    reviews = sorted(
        repository.list_pull_request_reviews(pull_number),
        key=lambda item: item.id,
    )
    for review in reviews:
        if review.state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest_by_reviewer[review.reviewer_login] = review
    return [
        review.to_graphql_payload()
        for review in sorted(latest_by_reviewer.values(), key=lambda item: item.id)
    ]
