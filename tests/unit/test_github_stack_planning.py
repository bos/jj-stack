from __future__ import annotations

import pytest

from jj_stack.commands.submit.github_stack import plan_github_stack
from jj_stack.errors import CliError, error_hint, error_message
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.github import GithubStack
from jj_stack.ui import plain_text


def _stack(
    number: int,
    *pr_numbers: int,
    historical: tuple[int, ...] = (),
) -> GithubStack:
    return GithubStack.model_validate(
        {
            "number": number,
            "pull_requests": tuple(
                {
                    "head": {
                        "ref": f"jj-stack/pull-{pr_number}",
                        "sha": f"head-{pr_number}",
                    },
                    "merged_at": ("2026-07-23T12:00:00Z" if pr_number in historical else None),
                    "number": pr_number,
                }
                for pr_number in pr_numbers
            ),
        }
    )


@pytest.mark.parametrize(
    (
        "desired",
        "observed",
        "base_updates",
        "is_maximal_path",
        "expected_action",
        "expected_stack_numbers",
    ),
    (
        ((1,), (), frozenset(), True, "none", ()),
        # A stack GitHub left unusable blocks only selections that touch it.
        ((1,), (_stack(9, 2, 3, historical=(3,)),), frozenset(), True, "none", ()),
        ((1, None), (_stack(5, 9, 10),), frozenset(), True, "create", ()),
        ((1, 2), (_stack(7, 1, 2),), frozenset(), True, "none", ()),
        ((1, 2, None, 3), (_stack(7, 1, 2),), frozenset(), True, "append", (7,)),
        ((1, 2, 3), (_stack(7, 1, 2),), frozenset({3}), True, "append", (7,)),
        ((1, 2), (_stack(7, 1, 2),), frozenset({1}), True, "replace", (7,)),
        ((2, 1), (_stack(7, 1, 2),), frozenset(), True, "replace", (7,)),
        ((None, 1, 2), (_stack(7, 1, 2),), frozenset(), True, "replace", (7,)),
        ((1,), (_stack(7, 1, 2),), frozenset(), True, "replace", (7,)),
        ((2,), (_stack(7, 1, 2, historical=(1,)),), frozenset(), True, "none", ()),
        (
            (1, 2, 3, 4),
            (_stack(9, 3, 4), _stack(7, 1, 2)),
            frozenset(),
            True,
            "replace",
            (7, 9),
        ),
    ),
)
def test_github_stack_plan_classifies_selected_membership(
    desired: tuple[int | None, ...],
    observed: tuple[GithubStack, ...],
    base_updates: frozenset[int],
    is_maximal_path: bool,
    expected_action: str,
    expected_stack_numbers: tuple[int, ...],
) -> None:
    plan = plan_github_stack(
        desired=desired,
        is_maximal_path=is_maximal_path,
        observed_stacks=observed,
        orphaned_pr_snapshots=frozenset(),
        pr_numbers_requiring_base_update=base_updates,
        repo=GithubRepoAddress(owner="octo-org", repo="stacked-prs"),
    )

    assert plan.action == expected_action
    assert tuple(stack.number for stack in plan.affected_stacks) == expected_stack_numbers


@pytest.mark.parametrize(
    (
        "desired",
        "is_maximal_path",
        "observed",
        "message_parts",
        "hint_parts",
    ),
    (
        ((1, 1), True, (), ("same pull request",), ()),
        (
            (1, 2, 3),
            True,
            (_stack(7, 1, 2), _stack(9, 3, 4)),
            ("pull requests in GitHub stack #9",),
            ("local stack that contains the rest of",),
        ),
        (
            (1, 2),
            False,
            (_stack(7, 1, 2, 9),),
            ("stop below the top of the local stack", "PR #9", "GitHub stack #7"),
            ("local stack that contains PR #9",),
        ),
        (
            (3, 2),
            True,
            (_stack(7, 1, 2),),
            ("pull requests in GitHub stack #7", "pull requests outside"),
            ("local stack that contains the rest of",),
        ),
        (
            (1, 2),
            True,
            (_stack(7, 1, 2, historical=(2,)),),
            ("GitHub stack #7", "merged pull request above"),
            ("unstack --stack 7",),
        ),
    ),
)
def test_github_stack_plan_rejects_ambiguous_selected_membership(
    desired: tuple[int, ...],
    is_maximal_path: bool,
    observed: tuple[GithubStack, ...],
    message_parts: tuple[str, ...],
    hint_parts: tuple[str, ...],
) -> None:
    with pytest.raises(CliError) as caught:
        plan_github_stack(
            desired=desired,
            is_maximal_path=is_maximal_path,
            observed_stacks=observed,
            orphaned_pr_snapshots=frozenset(),
            pr_numbers_requiring_base_update=frozenset(),
            repo=GithubRepoAddress(owner="octo-org", repo="stacked-prs"),
        )

    message = plain_text(error_message(caught.value))
    hint = plain_text(error_hint(caught.value) or "")
    assert all(part in message for part in message_parts)
    assert all(part in hint for part in hint_parts)
