"""Minimal fake GitHub server used for local integration tests."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Response


@dataclass(slots=True)
class FakeGithubPullRequest:
    """Mutable pull request state served by the fake API."""

    base_ref: str
    body: str
    head_label: str
    head_ref: str
    is_draft: bool
    merged_at: str | None
    node_id: str
    number: int
    title: str
    labels: list[str] = field(default_factory=list)
    requested_reviewers: list[str] = field(default_factory=list)
    requested_team_reviewers: list[str] = field(default_factory=list)
    state: str = "open"

    def to_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        return {
            "base": {"label": f"{repository.full_name}:{self.base_ref}", "ref": self.base_ref},
            "body": self.body,
            "draft": self.is_draft,
            "head": {"label": self.head_label, "ref": self.head_ref},
            "html_url": f"{web_origin}/{repository.full_name}/pull/{self.number}",
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
        return {
            "baseRefName": self.base_ref,
            "body": self.body,
            "headRefName": self.head_ref,
            "headRepositoryOwner": {"login": repository.owner},
            "id": self.node_id,
            "isDraft": self.is_draft,
            "mergedAt": self.merged_at,
            "number": self.number,
            "state": self.state.upper(),
            "title": self.title,
            "url": f"{web_origin}/{repository.full_name}/pull/{self.number}",
        }


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


@dataclass(slots=True, frozen=True)
class FakeGithubPullRequestEvent:
    """Observable PR mutation recorded by the fake API."""

    kind: str
    pull_request_number: int
    new_base_ref: str | None = None
    new_state: str | None = None
    old_base_ref: str | None = None
    old_state: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class FakeGithubIssueComment:
    """Mutable issue comment state served by the fake API."""

    body: str
    id: int
    issue_number: int

    def to_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        return {
            "body": self.body,
            "html_url": (
                f"{web_origin}/{repository.full_name}/issues/{self.issue_number}"
                f"#issuecomment-{self.id}"
            ),
            "id": self.id,
        }

    def to_graphql_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        payload = self.to_payload(repository=repository, web_origin=web_origin)
        return {
            "body": payload["body"],
            "databaseId": payload["id"],
            "url": payload["html_url"],
        }


@dataclass(slots=True)
class FakeGithubRepository:
    """Repository metadata plus its backing bare Git repository."""

    default_branch: str
    git_dir: Path
    name: str
    owner: str
    next_issue_comment_id: int = 1
    next_pull_request_number: int = 1
    next_pull_request_review_id: int = 1
    issue_comments: dict[int, list[FakeGithubIssueComment]] = field(default_factory=dict)
    pull_request_events: list[FakeGithubPullRequestEvent] = field(default_factory=list)
    pull_requests: dict[int, FakeGithubPullRequest] = field(default_factory=dict)
    pull_request_reviews: dict[int, list[FakeGithubPullRequestReview]] = field(
        default_factory=dict
    )

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_payload(self, *, api_origin: str, web_origin: str) -> dict[str, object]:
        return {
            "clone_url": f"{web_origin}/{self.full_name}.git",
            "default_branch": self.default_branch,
            "full_name": self.full_name,
            "html_url": f"{web_origin}/{self.full_name}",
            "name": self.name,
            "private": True,
            "url": f"{api_origin}/repos/{self.full_name}",
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
        pull_request = FakeGithubPullRequest(
            base_ref=base_ref,
            body=body,
            head_label=f"{self.owner}:{head_ref}",
            head_ref=head_ref,
            is_draft=draft,
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

    def refresh_pull_request_state(self, pull_request: FakeGithubPullRequest) -> None:
        if pull_request.state != "open":
            return
        base_commit = self.ref_target(pull_request.base_ref)
        head_commit = self.ref_target(pull_request.head_ref)
        if base_commit is None or head_commit is None:
            return
        if not self.is_ancestor(head_commit, base_commit):
            return
        if pull_request.merged_at is None:
            pull_request.merged_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.update_pull_request_state(
            pull_request,
            state="closed",
            reason="head_reachable_from_base",
        )

    def update_pull_request_base(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        base_ref: str,
        reason: str,
    ) -> None:
        if pull_request.base_ref == base_ref:
            return
        old_base_ref = pull_request.base_ref
        pull_request.base_ref = base_ref
        self.pull_request_events.append(
            FakeGithubPullRequestEvent(
                kind="base",
                new_base_ref=base_ref,
                old_base_ref=old_base_ref,
                pull_request_number=pull_request.number,
                reason=reason,
            )
        )

    def update_pull_request_state(
        self,
        pull_request: FakeGithubPullRequest,
        *,
        state: str,
        reason: str,
    ) -> None:
        if pull_request.state == state:
            return
        old_state = pull_request.state
        pull_request.state = state
        self.pull_request_events.append(
            FakeGithubPullRequestEvent(
                kind="state",
                new_state=state,
                old_state=old_state,
                pull_request_number=pull_request.number,
                reason=reason,
            )
        )

    def ref_target(self, branch: str) -> str | None:
        completed = subprocess.run(
            ["git", "--git-dir", str(self.git_dir), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None

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

    def get_issue_comment(self, *, comment_id: int) -> FakeGithubIssueComment | None:
        for comments in self.issue_comments.values():
            for comment in comments:
                if comment.id == comment_id:
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
    api_origin: str = "https://api.github.test"
    web_origin: str = "https://github.test"

    @classmethod
    def single_repository(cls, repository: FakeGithubRepository) -> FakeGithubState:
        return cls(repositories={(repository.owner, repository.name): repository})


def create_app(fake_state: FakeGithubState) -> FastAPI:
    """Create a FastAPI app that serves the configured fake GitHub state."""

    app = FastAPI(docs_url=None, redoc_url=None, title="fake-github")
    _register_repository_routes(app, fake_state)
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
        return repository.to_payload(
            api_origin=fake_state.api_origin,
            web_origin=fake_state.web_origin,
        )


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

    @app.get("/repos/{owner}/{repo}/pulls")
    async def list_pull_requests(
        owner: str,
        repo: str,
        head: str | None = None,
        state: str = "open",
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        requested_state = state or "open"
        pull_requests = list(repository.pull_requests.values())
        for pull_request in pull_requests:
            repository.refresh_pull_request_state(pull_request)
        if head is not None:
            pull_requests = [
                candidate for candidate in pull_requests if candidate.head_label == head
            ]
        if requested_state != "all":
            pull_requests = [
                candidate for candidate in pull_requests if candidate.state == requested_state
            ]
        return [
            pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)
            for pull_request in sorted(pull_requests, key=lambda candidate: candidate.number)
        ]

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
        repository.refresh_pull_request_state(pull_request)
        if "title" in payload:
            pull_request.title = _require_string(payload, "title")
        if "body" in payload:
            pull_request.body = _optional_string(payload, "body") or ""
        if "base" in payload:
            repository.update_pull_request_base(
                pull_request,
                base_ref=_require_string(payload, "base"),
                reason="api_update",
            )
            _require_branch(repository, pull_request.base_ref)
        repository.refresh_pull_request_state(pull_request)
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

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
            reason="issue_update",
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
            comment.to_payload(repository=repository, web_origin=fake_state.web_origin)
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
        return comment.to_payload(repository=repository, web_origin=fake_state.web_origin)

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
        return comment.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.get("/repos/{owner}/{repo}/issues/comments/{comment_id}")
    async def get_issue_comment(
        owner: str,
        repo: str,
        comment_id: int,
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        comment = repository.get_issue_comment(comment_id=comment_id)
        if comment is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return comment.to_payload(repository=repository, web_origin=fake_state.web_origin)

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


_HEAD_REF_QUERY_PATTERN = re.compile(
    r"(?m)^\s*(?P<alias>\w+):\s*pullRequests\([^)]*headRefName:\s*"
    r'(?P<head_ref>"(?:\\.|[^"\\])*")[^)]*\)\s*\{'
)
_PULL_REQUEST_NUMBER_QUERY_PATTERN = re.compile(
    r"(?m)^\s*(?P<alias>\w+):\s*pullRequest\(number:\s*(?P<number>\d+)\)\s*\{"
)


def _graphql_repository_payload(
    *,
    query: str,
    repository: FakeGithubRepository,
    web_origin: str,
) -> dict[str, object]:
    head_ref_matches = list(_HEAD_REF_QUERY_PATTERN.finditer(query))
    if head_ref_matches:
        payload: dict[str, object] = {}
        for match in head_ref_matches:
            alias = match.group("alias")
            head_ref = json.loads(match.group("head_ref"))
            matching_pull_requests = [
                _graphql_pull_request_payload(
                    pull_request=pull_request,
                    repository=repository,
                    web_origin=web_origin,
                )
                for pull_request in sorted(
                    repository.pull_requests.values(),
                    key=lambda candidate: candidate.number,
                )
                if pull_request.head_ref == head_ref
            ]
            payload[alias] = {"nodes": matching_pull_requests[:2]}
        return payload

    pull_request_number_matches = list(_PULL_REQUEST_NUMBER_QUERY_PATTERN.finditer(query))
    if not pull_request_number_matches:
        raise HTTPException(status_code=422, detail="Unsupported GraphQL query.")

    payload: dict[str, object] = {}
    include_latest_opinionated_reviews = "latestOpinionatedReviews" in query
    for match in pull_request_number_matches:
        alias = match.group("alias")
        pull_number = int(match.group("number"))
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            payload[alias] = None
            continue
        graphql_payload = _graphql_pull_request_payload(
            pull_request=pull_request,
            repository=repository,
            web_origin=web_origin,
        )
        if include_latest_opinionated_reviews:
            graphql_payload["latestOpinionatedReviews"] = {
                "nodes": _latest_opinionated_review_payloads(repository, pull_number)
            }
        if "comments(" in query:
            graphql_payload["comments"] = {
                "nodes": [
                    comment.to_graphql_payload(
                        repository=repository,
                        web_origin=web_origin,
                    )
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
) -> dict[str, object]:
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
