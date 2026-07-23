"""Resolve native GitHub stack support for one local repository."""

from __future__ import annotations

from dataclasses import dataclass

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubStack
from jj_stack.state.store import ReviewStateStore


@dataclass(frozen=True, slots=True)
class GithubStackSupport:
    supported: bool
    observed_stacks: tuple[GithubStack, ...] | None = None


async def resolve_github_stack_support(
    *,
    github_client: GithubClient,
    state_store: ReviewStateStore,
    persist: bool = True,
) -> GithubStackSupport:
    repository = github_client.repository
    repository_key = f"{repository.host}/{repository.owner}/{repository.repo}".casefold()
    cached = state_store.get_stacked_pull_requests(repository_key)
    if cached is not None:
        return GithubStackSupport(supported=cached)

    try:
        stacks = await github_client.list_stacks()
    except GithubClientError as error:
        if error.status_code != 404:
            raise
        if persist:
            state_store.set_stacked_pull_requests(repository_key, False)
        return GithubStackSupport(supported=False)

    if persist:
        state_store.set_stacked_pull_requests(repository_key, True)
    return GithubStackSupport(supported=True, observed_stacks=stacks)
