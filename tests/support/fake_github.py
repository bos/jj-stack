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
class FakeGithubPR:
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
        repo: FakeGithubRepo,
        web_origin: str,
    ) -> dict[str, object]:
        self._refresh_head_sha(repo)
        return {
            "base": {"label": f"{repo.full_name}:{self.base_ref}", "ref": self.base_ref},
            "body": self.body,
            "draft": self.is_draft,
            "head": {
                "label": self.head_label,
                "ref": self.head_ref,
                "sha": self.head_sha,
            },
            "html_url": f"{web_origin}/{repo.full_name}/pull/{self.number}",
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
        repo: FakeGithubRepo,
        web_origin: str,
    ) -> dict[str, object]:
        self._refresh_head_sha(repo)
        return {
            "autoMergeRequest": {"enabledAt": "now"} if self.auto_merge_enabled else None,
            "baseRefName": self.base_ref,
            "body": self.body,
            "headRefName": self.head_ref,
            "headRefOid": self.head_sha,
            "headRepositoryOwner": {"login": repo.owner},
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
            "url": f"{web_origin}/{repo.full_name}/pull/{self.number}",
        }

    def _refresh_head_sha(self, repo: FakeGithubRepo) -> None:
        if current_head := repo.ref_target(self.head_ref):
            self.head_sha = current_head


@dataclass(slots=True)
class FakeGithubPRReview:
    """Mutable pull request review state served by the fake API."""

    id: int
    pr_number: int
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
    pr_number: int
    uuid: str
    final_sha: str | None = None
    message: str | None = None
    status: str = "pending"


@dataclass(slots=True, frozen=True)
class FakeGithubPREvent:
    """Observable PR mutation recorded by the fake API."""

    kind: str
    pr_number: int


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
class FakeGithubRepo:
    """Repo metadata plus its backing bare Git repo."""

    default_branch: str | None
    git_dir: Path
    name: str
    owner: str
    # The default repo allows only squash merges. Tests that need other repo policies flip these
    # settings directly.
    allow_merge_commit: bool = False
    allow_rebase_merge: bool = False
    allow_squash_merge: bool = True
    merge_queue_enabled: bool = False
    stack_merge_operations: dict[int, FakeStackMergeOperation] = field(default_factory=dict)
    stack_merge_requests: list[tuple[int, str | None, str, str]] = field(default_factory=list)
    auto_merge_reachable_heads: bool = True
    next_issue_comment_id: int = 1
    next_github_stack_number: int = 1
    next_pr_number: int = 1
    next_pr_review_id: int = 1
    issue_comments: dict[int, list[FakeGithubIssueComment]] = field(default_factory=dict)
    github_stacks: dict[int, tuple[int, ...]] = field(default_factory=dict)
    pr_events: list[FakeGithubPREvent] = field(default_factory=list)
    prs: dict[int, FakeGithubPR] = field(default_factory=dict)
    pr_reviews: dict[int, list[FakeGithubPRReview]] = field(default_factory=dict)
    # Test hook: PR numbers GitHub should report as not mergeable (pending
    # required checks, conflicts, or branch protection).
    unmergeable_pr_numbers: set[int] = field(default_factory=set)

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

    def create_pr(
        self,
        *,
        base_ref: str,
        body: str,
        draft: bool = False,
        head_ref: str,
        title: str,
    ) -> FakeGithubPR:
        number = self.next_pr_number
        self.next_pr_number += 1
        # Tests may construct a historical PR after deleting or without creating
        # its source branch. Real GitHub still retains the PR's last head OID.
        head_sha = self.ref_target(head_ref) or self.ref_target(base_ref)
        if head_sha is None:
            raise AssertionError(
                f"Fake GitHub branches {head_ref!r} and {base_ref!r} do not exist."
            )
        pr = FakeGithubPR(
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
        self.prs[number] = pr
        return pr

    def find_pr_by_node_id(self, node_id: str) -> FakeGithubPR | None:
        for pr in self.prs.values():
            if pr.node_id == node_id:
                return pr
        return None

    def stack_number_for_pr(self, pr_number: int) -> int | None:
        for stack_number, pr_numbers in self.github_stacks.items():
            if pr_number in pr_numbers:
                return stack_number
        return None

    def refresh_pr_state(
        self,
        pr: FakeGithubPR,
        *,
        branch_heads: dict[str, str] | None = None,
    ) -> None:
        # Known idealization: this fake marks an open PR merged whenever its
        # head commits become reachable from its base, on every refresh. Real
        # GitHub's merged-detection may not fire on a base retarget after a
        # direct push, so the closed-but-not-merged finalization family is
        # untestable against this fake. Do not infer real GitHub behavior from
        # this transition without an approved live experiment.
        if not self.auto_merge_reachable_heads or pr.state != "open":
            return
        if branch_heads is None:
            branch_heads = self.branch_heads()
        base_commit = branch_heads.get(pr.base_ref)
        head_commit = branch_heads.get(pr.head_ref)
        if base_commit is None or head_commit is None:
            return
        if not self.is_ancestor(head_commit, base_commit):
            return
        if pr.merged_at is None:
            pr.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.update_pr_state(
            pr,
            state="closed",
        )

    def refresh_prs(
        self,
        prs: Iterable[FakeGithubPR],
    ) -> None:
        """Refresh many pull requests off one shared branch-head snapshot."""

        to_refresh = [candidate for candidate in prs if candidate.state == "open"]
        if not to_refresh:
            return
        branch_heads = self.branch_heads()
        for pr in to_refresh:
            self.refresh_pr_state(pr, branch_heads=branch_heads)

    def update_pr_base(
        self,
        pr: FakeGithubPR,
        *,
        base_ref: str,
    ) -> None:
        if pr.base_ref == base_ref:
            return
        pr.base_ref = base_ref
        self.pr_events.append(
            FakeGithubPREvent(
                kind="base",
                pr_number=pr.number,
            )
        )

    def update_pr_state(
        self,
        pr: FakeGithubPR,
        *,
        state: str,
    ) -> None:
        if pr.state == state:
            return
        pr.state = state
        self.pr_events.append(
            FakeGithubPREvent(
                kind="state",
                pr_number=pr.number,
            )
        )

    def ref_target(self, branch: str) -> str | None:
        return self.branch_heads().get(branch)

    def apply_squash_merge(self, pr: FakeGithubPR) -> str:
        """Squash-merge the PR's head into its base on the backing Git repo.

        Real GitHub computes a three-way merge before squashing. Using the head
        commit's tree matches that result whenever the base has not diverged
        beyond the PR's merge base, which holds for the merge scenarios these
        tests construct.
        """

        heads = self.branch_heads()
        head_commit = heads[pr.head_ref]
        base_commit = heads[pr.base_ref]
        tree = self._run_backing_git("rev-parse", f"{head_commit}^{{tree}}")
        squash_commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            "-m",
            f"{pr.title} (#{pr.number})",
            env=_FAKE_GITHUB_GIT_ENV,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.base_ref}",
            squash_commit,
        )
        pr.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pr.merge_commit_sha = squash_commit
        self.update_pr_state(
            pr,
            state="closed",
        )
        return squash_commit

    def apply_pr_merge(
        self,
        pr: FakeGithubPR,
        *,
        merge_method: str,
    ) -> str:
        """Apply one ordinary PR merge using GitHub's requested commit shape."""

        if merge_method == "merge":
            return self.apply_merge_commit((pr,))
        if merge_method == "rebase":
            return self.apply_rebase_merge(pr)
        if merge_method == "squash":
            return self.apply_squash_merge(pr)
        raise AssertionError(f"Unknown fake GitHub merge method {merge_method!r}.")

    def apply_rebase_merge(self, pr: FakeGithubPR) -> str:
        """Replay one submitted commit onto its base while preserving its message."""

        heads = self.branch_heads()
        rebase_commit = self._replay_commit(
            commit_id=heads[pr.head_ref],
            extra_header="x-fake-rebase-merge true",
            parent_commit_id=heads[pr.base_ref],
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.base_ref}",
            rebase_commit,
        )
        pr.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        pr.merge_commit_sha = rebase_commit
        self.update_pr_state(
            pr,
            state="closed",
        )
        return rebase_commit

    def apply_merge_commit(
        self,
        prs: tuple[FakeGithubPR, ...],
    ) -> str:
        """Merge one PR or stack PR prefix through a shared merge commit."""

        if not prs:
            raise AssertionError("A merge commit requires at least one pull request.")
        heads = self.branch_heads()
        base_ref = prs[0].base_ref
        base_commit = heads[base_ref]
        head_commit = heads[prs[-1].head_ref]
        tree = self._run_backing_git("rev-parse", f"{head_commit}^{{tree}}")
        merge_commit = self._run_backing_git(
            "commit-tree",
            tree,
            "-p",
            base_commit,
            "-p",
            head_commit,
            "-m",
            f"Merge through PR #{prs[-1].number}",
            env=_FAKE_GITHUB_GIT_ENV,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{base_ref}",
            merge_commit,
        )
        merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for pr in prs:
            pr.merged_at = merged_at
            pr.merge_commit_sha = merge_commit
            self.update_pr_state(
                pr,
                state="closed",
            )
        return merge_commit

    def rewrite_pr_onto_base(
        self,
        pr: FakeGithubPR,
        *,
        base_ref: str,
    ) -> str:
        """Model GitHub retargeting and replaying an active stack survivor."""

        heads = self.branch_heads()
        rewritten = self._replay_commit(
            commit_id=heads[pr.head_ref],
            extra_header="x-fake-stack-rewrite true",
            parent_commit_id=heads[base_ref],
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.head_ref}",
            rewritten,
        )
        self.update_pr_base(
            pr,
            base_ref=base_ref,
        )
        pr.head_sha = rewritten
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
        for pr_number in members:
            pr = self.prs[pr_number]
            original = original_heads[pr.head_ref]
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
                f"refs/heads/{pr.head_ref}",
                rewritten,
            )
            self.update_pr_base(pr, base_ref=expected_base)
            pr.head_sha = rewritten
            rewritten_heads.append(rewritten)
            expected_base = pr.head_ref
            parent = rewritten
        return tuple(rewritten_heads)

    def replace_pr_head_contents(
        self,
        pr: FakeGithubPR,
        *,
        path: str,
        contents: str,
    ) -> str:
        """Replace one rewritten PR head with a same-parent commit containing another file."""

        head = self.ref_target(pr.head_ref)
        if head is None:
            raise AssertionError(f"Missing fake GitHub branch {pr.head_ref}")
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
            f"refs/heads/{pr.head_ref}",
            rewritten,
        )
        pr.head_sha = rewritten
        return rewritten

    def _tree_with_file(self, tree: str, *, path: str, contents: str) -> str:
        blob = self._run_backing_git("hash-object", "-w", "--stdin", stdin=contents)
        entries = self._run_backing_git("ls-tree", tree)
        return self._run_backing_git(
            "mktree",
            stdin=f"{entries}\n100644 blob {blob}\t{path}\n",
        )

    def force_push_pr_head(self, pr: FakeGithubPR) -> str:
        """Rewrite one PR head externally while preserving its jj change ID."""

        head = self.ref_target(pr.head_ref)
        if head is None:
            raise AssertionError(f"Missing fake GitHub branch {pr.head_ref}")
        parent = self._run_backing_git("rev-parse", f"{head}^")
        rewritten = self._replay_commit(
            commit_id=head,
            extra_header="x-fake-force-push true",
            message_suffix="\nexternal rewrite",
            parent_commit_id=parent,
        )
        self._run_backing_git(
            "update-ref",
            f"refs/heads/{pr.head_ref}",
            rewritten,
        )
        pr.head_sha = rewritten
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

    def has_commit(self, commit_id: str) -> bool:
        """Return whether the backing repo contains the commit."""

        completed = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "cat-file", "-e", f"{commit_id}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0

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

    def list_pr_reviews(self, pr_number: int) -> list[FakeGithubPRReview]:
        self._require_issue_number(pr_number)
        return list(self.pr_reviews.get(pr_number, ()))

    def create_pr_review(
        self,
        *,
        pr_number: int,
        reviewer_login: str,
        state: str,
    ) -> FakeGithubPRReview:
        self._require_issue_number(pr_number)
        review = FakeGithubPRReview(
            id=self.next_pr_review_id,
            pr_number=pr_number,
            reviewer_login=reviewer_login,
            state=state,
        )
        self.next_pr_review_id += 1
        self.pr_reviews.setdefault(pr_number, []).append(review)
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
        if issue_number not in self.prs:
            raise HTTPException(status_code=404, detail="Not Found")


@dataclass(slots=True, frozen=True)
class FakeGithubState:
    """Static state served by the fake GitHub app."""

    repos: dict[tuple[str, str], FakeGithubRepo]
    web_origin: str = "https://github.test"

    @classmethod
    def single_repo(cls, repo: FakeGithubRepo) -> FakeGithubState:
        return cls(repos={(repo.owner, repo.name): repo})


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

    _register_repo_routes(app, fake_state)
    _register_github_stack_routes(app, fake_state)
    _register_graphql_routes(app, fake_state)
    _register_pr_routes(app, fake_state)
    _register_issue_comment_routes(app, fake_state)
    return app


def _register_repo_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register repo metadata routes on the fake GitHub app."""

    @app.get("/repos/{owner}/{repo_name}")
    async def get_repo(owner: str, repo_name: str) -> dict[str, object]:
        repo = fake_state.repos.get((owner, repo_name))
        if repo is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return repo.to_payload()

    @app.get("/repos/{owner}/{repo_name}/commits/{commit_sha}/branches-where-head")
    async def list_branches_for_head_commit(
        owner: str,
        repo_name: str,
        commit_sha: str,
    ) -> list[dict[str, str]]:
        repo = _get_repo(fake_state, owner, repo_name)
        if not repo.has_commit(commit_sha):
            raise HTTPException(status_code=422, detail=f"No commit found for SHA: {commit_sha}")
        return [
            {"name": branch}
            for branch, target in repo.branch_heads().items()
            if target == commit_sha
        ]


def _register_github_stack_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register the observed stack routes."""

    @app.get("/repos/{owner}/{repo_name}/stacks")
    async def list_stacks(owner: str, repo_name: str) -> list[dict[str, object]]:
        repo = _get_repo(fake_state, owner, repo_name)
        return [
            _stack_payload(repo, number, members)
            for number, members in sorted(_github_stacks(repo).items())
        ]

    @app.get("/repos/{owner}/{repo_name}/stacks/{stack_number}")
    async def get_stack(
        owner: str,
        repo_name: str,
        stack_number: int,
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        members = _github_stacks(repo).get(stack_number)
        if members is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return _stack_payload(repo, stack_number, members)

    @app.post("/repos/{owner}/{repo_name}/stacks", status_code=201)
    async def create_stack(
        owner: str,
        repo_name: str,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        members = _require_int_list(payload, "pull_requests")
        if len(members) < 2:
            raise HTTPException(status_code=422, detail="A stack requires two pull requests.")
        already = sorted(
            set(members).intersection(
                member for existing in _github_stacks(repo).values() for member in existing
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
            repo,
            admitted_members=members,
            chained_members=members,
            complete_members=members,
        )
        stacks = _github_stacks(repo)
        number = repo.next_github_stack_number
        while number in stacks:
            number += 1
        repo.next_github_stack_number = number + 1
        stacks[number] = members
        return _stack_payload(repo, number, members)

    @app.post("/repos/{owner}/{repo_name}/stacks/{stack_number}/add")
    async def append_to_stack(
        owner: str,
        repo_name: str,
        stack_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        stacks = _github_stacks(repo)
        existing = stacks.get(stack_number)
        added = _require_int_list(payload, "pull_requests")
        if existing is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if not added:
            raise HTTPException(status_code=422, detail="No pull requests to append.")
        members = (*existing, *added)
        active_existing = GithubStack.model_validate(
            _stack_payload(repo, stack_number, existing)
        ).active_pr_numbers
        _validate_stack_members(
            repo,
            admitted_members=added,
            allowed_stack=stack_number,
            chained_members=(*active_existing, *added),
            complete_members=members,
        )
        stacks[stack_number] = members
        return _stack_payload(repo, stack_number, members)

    @app.post(
        "/repos/{owner}/{repo_name}/stacks/{stack_number}/unstack",
        response_model=None,
    )
    async def unstack(owner: str, repo_name: str, stack_number: int) -> Response:
        repo = _get_repo(fake_state, owner, repo_name)
        stacks = _github_stacks(repo)
        if stack_number not in stacks:
            raise HTTPException(status_code=404, detail="Not Found")
        members = stacks[stack_number]
        prs = repo.prs
        retained = tuple(
            number for number in members if prs[number].is_queued or prs[number].merged_at
        )
        if retained == members and any(prs[number].is_queued for number in members):
            raise HTTPException(status_code=422, detail="No pull requests can be removed.")
        if retained:
            stacks[stack_number] = retained
            return JSONResponse(_stack_payload(repo, stack_number, retained))
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
            pr_id = _require_graphql_variable(raw_variables, "pullRequestId")
            pr, repo = _find_pr_by_node_id(
                fake_state,
                pr_id,
            )
            repo.refresh_pr_state(pr)
            pr.is_draft = False
            return {
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": pr.to_graphql_payload(
                            repo=repo,
                            web_origin=fake_state.web_origin,
                        )
                    }
                }
            }
        if "convertPullRequestToDraft" in query:
            pr_id = _require_graphql_variable(raw_variables, "pullRequestId")
            pr, repo = _find_pr_by_node_id(
                fake_state,
                pr_id,
            )
            repo.refresh_pr_state(pr)
            pr.is_draft = True
            return {
                "data": {
                    "convertPullRequestToDraft": {
                        "pullRequest": pr.to_graphql_payload(
                            repo=repo,
                            web_origin=fake_state.web_origin,
                        )
                    }
                }
            }
        owner = _require_graphql_variable(raw_variables, "owner")
        repo_name = _require_graphql_variable(raw_variables, "repo")
        repo = _get_repo(fake_state, owner, repo_name)
        return {
            "data": {
                "repository": _graphql_repo_payload(
                    query=query,
                    repo=repo,
                    web_origin=fake_state.web_origin,
                )
            }
        }


def _register_pr_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register pull-request, issue, label, and review routes."""

    @app.post("/repos/{owner}/{repo_name}/pulls", status_code=201)
    async def create_pr(
        owner: str,
        repo_name: str,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        title = _require_string(payload, "title")
        head_ref = _require_string(payload, "head")
        base_ref = _require_string(payload, "base")
        body = _optional_string(payload, "body") or ""
        draft = _optional_bool(payload, "draft") or False
        _require_branch(repo, head_ref)
        _require_branch(repo, base_ref)
        pr = repo.create_pr(
            base_ref=base_ref,
            body=body,
            draft=draft,
            head_ref=head_ref,
            title=title,
        )
        return pr.to_payload(repo=repo, web_origin=fake_state.web_origin)

    @app.get("/repos/{owner}/{repo_name}/pulls/{pr_number}")
    async def get_pr(
        owner: str,
        repo_name: str,
        pr_number: int,
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        pr = repo.prs.get(pr_number)
        if pr is None:
            raise HTTPException(status_code=404, detail="Not Found")
        repo.refresh_pr_state(pr)
        return pr.to_payload(repo=repo, web_origin=fake_state.web_origin)

    @app.patch("/repos/{owner}/{repo_name}/pulls/{pr_number}")
    async def update_pr(
        owner: str,
        repo_name: str,
        pr_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        pr = repo.prs.get(pr_number)
        if pr is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if "base" in payload and repo.stack_number_for_pr(pr_number) is not None:
            raise HTTPException(
                status_code=422,
                detail="A stacked pull request's base cannot be updated directly.",
            )
        repo.refresh_pr_state(pr)
        title = _require_string(payload, "title") if "title" in payload else None
        body = (_optional_string(payload, "body") or "") if "body" in payload else None
        base_ref = _require_string(payload, "base") if "base" in payload else None
        if base_ref is not None:
            _require_branch(repo, base_ref)
        if title is not None:
            pr.title = title
        if body is not None:
            pr.body = body
        if base_ref is not None:
            repo.update_pr_base(
                pr,
                base_ref=base_ref,
            )
        repo.refresh_pr_state(pr)
        return pr.to_payload(repo=repo, web_origin=fake_state.web_origin)

    @app.put("/repos/{owner}/{repo_name}/pulls/{pr_number}/merge-async")
    async def submit_stack_merge(
        owner: str,
        repo_name: str,
        pr_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> Response:
        repo = _get_repo(fake_state, owner, repo_name)
        pr = repo.prs.get(pr_number)
        if pr is None:
            raise HTTPException(status_code=404, detail="Not Found")
        merge_action = _require_string(payload, "merge_action")
        merge_method = _optional_string(payload, "merge_method")
        expected_head_sha = _require_string(payload, "sha")
        allowed = {
            "merge": repo.allow_merge_commit,
            "rebase": repo.allow_rebase_merge,
            "squash": repo.allow_squash_merge,
        }
        if merge_action not in {"direct_merge", "merge_queue"}:
            raise HTTPException(status_code=400, detail="Unknown merge action.")
        if repo.merge_queue_enabled != (merge_action == "merge_queue"):
            raise HTTPException(status_code=400, detail="Merge action does not match policy.")
        if merge_action == "direct_merge" and not allowed.get(merge_method or "", False):
            raise HTTPException(status_code=400, detail="Merge method is not allowed.")
        live_head = repo.ref_target(pr.head_ref)
        if live_head != expected_head_sha:
            raise HTTPException(status_code=400, detail="Target head changed.")
        existing = repo.stack_merge_operations.get(pr_number)
        if existing is not None:
            if (
                existing.expected_head_sha != expected_head_sha
                or existing.merge_action != merge_action
                or existing.merge_method != merge_method
            ):
                return JSONResponse(_stack_merge_payload(existing), status_code=409)
            status_code = 409 if existing.status == "pending" else 200
            return JSONResponse(_stack_merge_payload(existing), status_code=status_code)
        stack_number = repo.stack_number_for_pr(pr_number)
        active_pr_numbers = (
            GithubStack.model_validate(
                _stack_payload(
                    repo,
                    stack_number,
                    _github_stacks(repo)[stack_number],
                )
            ).active_pr_numbers
            if stack_number is not None
            else (pr_number,)
        )
        if pr_number not in active_pr_numbers or pr.is_draft:
            raise HTTPException(status_code=400, detail="Target is not mergeable.")
        operation = FakeStackMergeOperation(
            expected_head_sha=expected_head_sha,
            merge_action=merge_action,
            merge_method=merge_method,
            pr_number=pr_number,
            uuid=f"fake-stack-merge-{len(repo.stack_merge_operations) + 1}",
        )
        repo.stack_merge_operations[pr_number] = operation
        repo.stack_merge_requests.append(
            (pr_number, merge_method, merge_action, expected_head_sha)
        )
        return JSONResponse(_stack_merge_payload(operation), status_code=202)

    @app.get("/repos/{owner}/{repo_name}/pulls/{pr_number}/merge-async/{operation_uuid}")
    async def poll_stack_merge(
        owner: str,
        repo_name: str,
        pr_number: int,
        operation_uuid: str,
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        operation = repo.stack_merge_operations.get(pr_number)
        if operation is None or operation.uuid != operation_uuid:
            raise HTTPException(status_code=404, detail="Not Found")
        if operation.status == "pending":
            _complete_stack_merge(repo, operation)
        return _stack_merge_payload(operation)

    @app.patch("/repos/{owner}/{repo_name}/issues/{issue_number}")
    async def update_issue(
        owner: str,
        repo_name: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        pr = repo.prs.get(issue_number)
        if pr is None:
            raise HTTPException(status_code=404, detail="Not Found")
        state = _require_string(payload, "state")
        if state not in {"open", "closed"}:
            raise HTTPException(status_code=422, detail="Unsupported issue state.")
        repo.update_pr_state(
            pr,
            state=state,
        )
        if state == "closed":
            repo.refresh_pr_state(pr)
        return pr.to_payload(repo=repo, web_origin=fake_state.web_origin)

    @app.post(
        "/repos/{owner}/{repo_name}/pulls/{pr_number}/requested_reviewers",
        status_code=201,
    )
    async def request_reviewers(
        owner: str,
        repo_name: str,
        pr_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        pr = repo.prs.get(pr_number)
        if pr is None:
            raise HTTPException(status_code=404, detail="Not Found")
        reviewers = payload.get("reviewers", [])
        team_reviewers = payload.get("team_reviewers", [])
        if isinstance(reviewers, list):
            for reviewer in reviewers:
                normalized = str(reviewer)
                if normalized not in pr.requested_reviewers:
                    pr.requested_reviewers.append(normalized)
        if isinstance(team_reviewers, list):
            for team_reviewer in team_reviewers:
                normalized = str(team_reviewer)
                if normalized not in pr.requested_team_reviewers:
                    pr.requested_team_reviewers.append(normalized)
        return pr.to_payload(repo=repo, web_origin=fake_state.web_origin)

    @app.post("/repos/{owner}/{repo_name}/issues/{issue_number}/labels")
    async def add_labels(
        owner: str,
        repo_name: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> list[dict[str, object]]:
        repo = _get_repo(fake_state, owner, repo_name)
        pr = repo.prs.get(issue_number)
        if pr is None:
            raise HTTPException(status_code=404, detail="Not Found")
        labels = payload.get("labels", [])
        if isinstance(labels, list):
            pr.labels = [str(label) for label in labels]
        return [{"name": label} for label in pr.labels]

    @app.get("/repos/{owner}/{repo_name}/pulls/{pr_number}/reviews")
    async def list_pr_reviews(
        owner: str,
        repo_name: str,
        pr_number: int,
    ) -> list[dict[str, object]]:
        repo = _get_repo(fake_state, owner, repo_name)
        reviews = repo.list_pr_reviews(pr_number)
        return [
            review.to_payload() for review in sorted(reviews, key=lambda candidate: candidate.id)
        ]


def _register_issue_comment_routes(app: FastAPI, fake_state: FakeGithubState) -> None:
    """Register issue comment routes on the fake GitHub app."""

    @app.get("/repos/{owner}/{repo_name}/issues/{issue_number}/comments")
    async def list_issue_comments(
        owner: str,
        repo_name: str,
        issue_number: int,
    ) -> list[dict[str, object]]:
        repo = _get_repo(fake_state, owner, repo_name)
        comments = repo.list_issue_comments(issue_number)
        return [
            comment.to_payload()
            for comment in sorted(comments, key=lambda candidate: candidate.id)
        ]

    @app.post("/repos/{owner}/{repo_name}/issues/{issue_number}/comments", status_code=201)
    async def create_issue_comment(
        owner: str,
        repo_name: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        comment = repo.create_issue_comment(
            body=_require_string(payload, "body"),
            issue_number=issue_number,
        )
        return comment.to_payload()

    @app.patch("/repos/{owner}/{repo_name}/issues/comments/{comment_id}")
    async def update_issue_comment(
        owner: str,
        repo_name: str,
        comment_id: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repo = _get_repo(fake_state, owner, repo_name)
        comment = repo.update_issue_comment(
            body=_require_string(payload, "body"),
            comment_id=comment_id,
        )
        if comment is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return comment.to_payload()

    @app.delete(
        "/repos/{owner}/{repo_name}/issues/comments/{comment_id}",
        response_model=None,
        status_code=204,
    )
    async def delete_issue_comment(
        owner: str,
        repo_name: str,
        comment_id: int,
    ) -> Response:
        repo = _get_repo(fake_state, owner, repo_name)
        deleted = repo.delete_issue_comment(comment_id=comment_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Not Found")
        return Response(status_code=204)


def initialize_bare_repo(
    root_dir: Path,
    *,
    owner: str,
    name: str,
    default_branch: str = "main",
) -> FakeGithubRepo:
    """Create a bare Git repo that the fake server can expose."""

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

    return FakeGithubRepo(
        default_branch=default_branch,
        git_dir=git_dir,
        name=name,
        owner=owner,
    )


def _get_repo(state: FakeGithubState, owner: str, repo_name: str) -> FakeGithubRepo:
    repo = state.repos.get((owner, repo_name))
    if repo is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return repo


def _find_pr_by_node_id(
    state: FakeGithubState,
    node_id: str,
) -> tuple[FakeGithubPR, FakeGithubRepo]:
    for repo in state.repos.values():
        pr = repo.find_pr_by_node_id(node_id)
        if pr is not None:
            return pr, repo
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


def _github_stacks(repo: FakeGithubRepo) -> dict[int, tuple[int, ...]]:
    # GitHub refuses to put one pull request in two stacks: creating a second reports
    # "Pull requests #N are already part of a stack" (422, confirmed against the API). Tests
    # assign this mapping directly, so refuse the impossible shape here rather than let a fixture
    # justify production code defending against it. Unstack can leave one member.
    seen: set[int] = set()
    for members in repo.github_stacks.values():
        assert len(members) >= 1, f"fake GitHub was given an empty stack {members}"
        overlap = seen.intersection(members)
        assert not overlap, (
            f"fake GitHub was given pull requests {sorted(overlap)} in more than one stack, "
            "which GitHub rejects"
        )
        seen.update(members)
    return repo.github_stacks


def _stack_payload(
    repo: FakeGithubRepo,
    number: int,
    members: tuple[int, ...],
) -> dict[str, object]:
    return {
        "number": number,
        "pull_requests": [_stack_pr_payload(repo, pr_number) for pr_number in members],
    }


def _stack_pr_payload(
    repo: FakeGithubRepo,
    pr_number: int,
) -> dict[str, object]:
    pr = repo.prs.get(pr_number)
    if pr is None:
        return {
            "head": {"ref": f"jj-stack/pull-{pr_number}", "sha": f"head-{pr_number}"},
            "merged_at": None,
            "number": pr_number,
            "state": "open",
        }
    pr._refresh_head_sha(repo)
    return {
        "head": {"ref": pr.head_ref, "sha": pr.head_sha},
        "merged_at": pr.merged_at,
        "number": pr_number,
        "state": pr.state,
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
    repo: FakeGithubRepo,
    operation: FakeStackMergeOperation,
) -> None:
    stack_number = repo.stack_number_for_pr(operation.pr_number)
    if stack_number is None:
        candidate_numbers = (operation.pr_number,)
        survivors: tuple[int, ...] = ()
    else:
        stack = GithubStack.model_validate(
            _stack_payload(repo, stack_number, _github_stacks(repo)[stack_number])
        )
        target_index = stack.active_pr_numbers.index(operation.pr_number)
        candidate_numbers = stack.active_pr_numbers[: target_index + 1]
        survivors = stack.active_pr_numbers[len(candidate_numbers) :]
    candidates = tuple(repo.prs[number] for number in candidate_numbers)
    if any(
        pr.state != "open" or pr.is_draft or pr.number in repo.unmergeable_pr_numbers
        for pr in candidates
    ):
        operation.status = "failed"
        operation.message = "The GitHub stack prefix is not mergeable."
        return
    if operation.merge_action == "merge_queue":
        for pr in candidates:
            pr.is_queued = True
        operation.status = "enqueued"
        operation.message = "Pull requests were added to the merge queue."
        return
    for pr in candidates:
        if pr.base_ref != repo.default_branch:
            repo.update_pr_base(
                pr,
                base_ref=repo.default_branch or "main",
            )
    if operation.merge_method == "merge":
        repo.apply_merge_commit(candidates)
    else:
        assert operation.merge_method is not None
        for pr in candidates:
            repo.apply_pr_merge(
                pr,
                merge_method=operation.merge_method,
            )
    previous_base = repo.default_branch or "main"
    for pr_number in survivors:
        pr = repo.prs[pr_number]
        repo.rewrite_pr_onto_base(
            pr,
            base_ref=previous_base,
        )
        previous_base = pr.head_ref
    operation.final_sha = repo.ref_target(repo.default_branch or "main")
    operation.status = "merged"


def _validate_stack_members(
    repo: FakeGithubRepo,
    *,
    admitted_members: tuple[int, ...],
    allowed_stack: int | None = None,
    chained_members: tuple[int, ...],
    complete_members: tuple[int, ...],
) -> None:
    prs = {number: repo.prs.get(number) for number in complete_members}
    if len(set(complete_members)) != len(complete_members):
        raise HTTPException(status_code=422, detail="Duplicate pull request.")
    if any(pr is None for pr in prs.values()):
        raise HTTPException(status_code=422, detail="Pull request does not exist.")
    resolved = {number: pr for number, pr in prs.items() if pr is not None}
    repo.refresh_prs(resolved.values())
    if any(
        (pr := resolved[number]).state != "open" or pr.auto_merge_enabled or pr.is_queued
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
    for number, existing in _github_stacks(repo).items():
        if number != allowed_stack and not set(existing).isdisjoint(admitted_members):
            raise HTTPException(
                status_code=422, detail="Pull request already belongs to a stack."
            )


def _require_branch(repo: FakeGithubRepo, branch: str) -> None:
    completed = subprocess.run(
        [
            "git",
            "--git-dir",
            str(repo.git_dir),
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


def _graphql_repo_payload(
    *,
    query: str,
    repo: FakeGithubRepo,
    web_origin: str,
) -> dict[str, object]:
    if "BaseBranchMergeQueue" in query:
        return {
            "mergeQueue": ({"id": "merge-queue"} if repo.merge_queue_enabled else None),
            "ref": {
                "rules": {
                    "nodes": ([{"type": "MERGE_QUEUE"}] if repo.merge_queue_enabled else [])
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
            matching_prs = [
                pr
                for pr in repo.prs.values()
                if (pr.head_ref if ref_kind == "head" else pr.base_ref) == ref_value
            ]
            repo.refresh_prs(matching_prs)
            matching_prs = sorted(
                (pr for pr in matching_prs if not states or pr.graphql_state in states),
                key=lambda candidate: candidate.number,
            )
            payload[alias] = {
                "nodes": [
                    _graphql_pr_payload(
                        pr=pr,
                        repo=repo,
                        web_origin=web_origin,
                        refreshed=True,
                    )
                    for pr in matching_prs
                ][:first]
            }
        return payload

    pr_number_queries: list[tuple[str, int]] = []
    for line in lines:
        alias, separator, selection = line.strip().partition(":")
        if not separator or not alias.isidentifier():
            continue
        selection = selection.lstrip()
        if not selection.startswith("pullRequest(number:"):
            continue
        number_text = selection.removeprefix("pullRequest(number:").partition(")")[0]
        pr_number_queries.append((alias, int(number_text.strip())))

    if not pr_number_queries:
        raise HTTPException(status_code=422, detail="Unsupported GraphQL query.")

    payload: dict[str, object] = {}
    requested_prs = [
        pr
        for _alias, pr_number in pr_number_queries
        if (pr := repo.prs.get(pr_number)) is not None
    ]
    repo.refresh_prs(requested_prs)
    for alias, pr_number in pr_number_queries:
        pr = repo.prs.get(pr_number)
        if pr is None:
            payload[alias] = None
            continue
        graphql_payload = _graphql_pr_payload(
            pr=pr,
            repo=repo,
            web_origin=web_origin,
            refreshed=True,
        )
        if "comments(" in query:
            graphql_payload["comments"] = {
                "nodes": [
                    comment.to_graphql_payload()
                    for comment in sorted(
                        repo.list_issue_comments(pr_number),
                        key=lambda candidate: candidate.id,
                    )
                ],
                "pageInfo": {"hasNextPage": False},
            }
        payload[alias] = graphql_payload
    return payload


def _graphql_pr_payload(
    *,
    pr: FakeGithubPR,
    repo: FakeGithubRepo,
    web_origin: str,
    refreshed: bool = False,
) -> dict[str, object]:
    if not refreshed:
        repo.refresh_pr_state(pr)
    payload = pr.to_graphql_payload(
        repo=repo,
        web_origin=web_origin,
    )
    payload["reviewDecision"] = _graphql_review_decision(repo, pr.number)
    return payload


def _graphql_review_decision(
    repo: FakeGithubRepo,
    pr_number: int,
) -> str | None:
    review_states = {
        str(raw_review["state"]).upper()
        for raw_review in _latest_opinionated_review_payloads(repo, pr_number)
    }
    if "CHANGES_REQUESTED" in review_states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in review_states:
        return "APPROVED"
    return None


def _latest_opinionated_review_payloads(
    repo: FakeGithubRepo,
    pr_number: int,
) -> list[dict[str, object]]:
    latest_by_reviewer: dict[str, FakeGithubPRReview] = {}
    reviews = sorted(
        repo.list_pr_reviews(pr_number),
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
