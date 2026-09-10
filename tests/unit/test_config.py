from __future__ import annotations

import tomllib

import pytest

from jj_stack.config import load_config, parse_comma_separated_flag_values
from jj_stack.errors import CliError
from jj_stack.jj.settings import JjSettings


def _settings(listing: str) -> JjSettings:
    """Build settings from lines shaped like `jj config list` output."""

    return JjSettings(tomllib.loads(listing))


def test_load_config_returns_defaults_when_no_keys_set() -> None:
    config = load_config(settings=_settings(""))

    assert config.logging.level == "WARNING"
    assert config.branch_prefix == "jj-stack"
    assert config.labels == []
    assert config.reviewers == []
    assert config.team_reviewers == []


def test_load_config_parses_and_normalizes_the_resolved_jj_stack_section() -> None:
    listing = "\n".join(
        [
            'jj-stack.branch_prefix = "Team/prs_v2"',
            'jj-stack.reviewers = ["", "octocat", "octocat"]',
            'jj-stack.team_reviewers = ["platform", ""]',
            'jj-stack.labels = ["", "needs-review", "needs-review"]',
            'jj-stack.logging.level = "info"',
            'jj-stack.potato = "round"',
            "",
        ]
    )
    config = load_config(settings=_settings(listing))

    assert config.logging.level == "INFO"
    assert config.branch_prefix == "Team/prs_v2"
    assert config.reviewers == ["octocat"]
    assert config.team_reviewers == ["platform"]
    assert config.labels == ["needs-review"]


def test_load_config_rejects_likely_top_level_typo() -> None:
    with pytest.raises(CliError, match=r"Did you mean \[jj-stack\]\.reviewers\?"):
        load_config(settings=_settings('jj-stack.reviewrs = ["octocat"]\n'))


def test_load_config_rejects_invalid_branch_prefixes() -> None:
    # Each rejection names a next step that survives being followed: `my prs` needs shell
    # quoting, and git accepts `foo|main` and an overlong prefix, so those must not cite git.
    for prefix, next_step in (
        ("my prs", "`git check-ref-format --branch 'my prs'`"),
        ("foo|main", "Remove the '|'"),
        ("p" * 235, "`jj config set --repo jj-stack.branch_prefix <prefix>`"),
    ):
        with pytest.raises(CliError, match=r"\[jj-stack\]\.branch_prefix") as caught:
            load_config(settings=_settings(f'jj-stack.branch_prefix = "{prefix}"\n'))

        message = str(caught.value)

        assert "Invalid PR branch prefix" in message, prefix
        assert next_step in message, message


def test_load_config_rejects_invalid_logging_level() -> None:
    with pytest.raises(CliError, match="Invalid logging level"):
        load_config(settings=_settings('jj-stack.logging.level = "DEBIG"\n'))


def test_parse_comma_separated_flag_values_dedupes_keeping_first_occurrence_order() -> None:
    assert parse_comma_separated_flag_values(["alice,bob", "carol,bob", "alice"]) == [
        "alice",
        "bob",
        "carol",
    ]
