"""Shared policy for GitHub-delegated landing ownership."""

from collections.abc import Iterable

from jj_stack.models.github import GithubPullRequest


def delegated_landing_mutation_error(pull_requests: Iterable[GithubPullRequest]) -> str | None:
    """Return the first live landing delegation that blocks review mutation."""

    for pull_request in pull_requests:
        if pull_request.normalize_state().state != "open":
            continue
        owners = pull_request.landing_owners
        if owners is None:
            return f"Could not verify landing ownership for PR #{pull_request.number}."
        if not owners:
            continue
        owner = " and ".join(
            {"auto_merge": "GitHub auto-merge", "merge_queue": "GitHub's merge queue"}[key]
            for key in sorted(owners)
        )
        remediation = " and ".join(
            action
            for key, action in (
                ("auto_merge", "disable auto-merge"),
                ("merge_queue", "remove it from the merge queue"),
            )
            if key in owners
        )
        return (
            f"PR #{pull_request.number} is controlled by {owner}; {remediation} "
            "before retrying."
        )
    return None
