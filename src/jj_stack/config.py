"""Configuration loading for `jj-stack`."""

from __future__ import annotations

import difflib
import logging
import shlex
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jj_stack.errors import CliError
from jj_stack.jj.settings import JjSettings
from jj_stack.pr_branch_namespace import MAX_BRANCH_PREFIX_BYTES
from jj_stack.stack.selection import parse_comma_separated_flag_values

CONFIG_SECTION = "jj-stack"
DEFAULT_BRANCH_PREFIX = "jj-stack"
_TYPO_CUTOFF = 0.75
_REJECTED_REF_CHARS = frozenset(" ~^:?*[\\\x7f") | frozenset(map(chr, range(32)))


MergeMethod = Literal["merge", "rebase", "squash"]


class RepoConfig(BaseModel):
    """Repo defaults resolved before command planning."""

    model_config = ConfigDict(extra="ignore")

    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    labels: list[str] = Field(default_factory=list)
    merge_method: MergeMethod | None = None
    reviewers: list[str] = Field(default_factory=list)
    team_reviewers: list[str] = Field(default_factory=list)

    @field_validator("labels", "reviewers", "team_reviewers")
    @classmethod
    def _normalize_requested_names(cls, value: list[str]) -> list[str]:
        # GitHub rejects label names with embedded commas, so splitting configured values cannot
        # change a valid label name.
        return parse_comma_separated_flag_values(value) or []

    @field_validator("branch_prefix")
    @classmethod
    def _validate_branch_prefix(cls, value: str) -> str:
        if "|" in value:
            raise ValueError(
                f"Invalid PR branch prefix {value!r}. Remove the '|', which jj reads as a "
                "separator between name patterns"
            )
        if not _is_git_branch_path(value):
            raise ValueError(
                f"Invalid PR branch prefix {value!r}. Git rejects it as a branch name; see "
                f"`git check-ref-format --branch {shlex.quote(value)}`"
            )
        if len(value.encode()) > MAX_BRANCH_PREFIX_BYTES:
            raise ValueError(
                f"Invalid PR branch prefix {value!r}. It is longer than "
                f"{MAX_BRANCH_PREFIX_BYTES} bytes, so no PR branch name fits GitHub's 255-byte "
                "limit. Shorten it with `jj config set --repo jj-stack.branch_prefix <prefix>`"
            )
        return value


def _is_git_branch_path(value: str) -> bool:
    """Whether a value can lead a Git branch name, per `git check-ref-format`."""

    return bool(value) and all(
        segment
        and not segment.startswith(".")
        and not segment.endswith((".", ".lock"))
        and ".." not in segment
        and "@{" not in segment
        and _REJECTED_REF_CHARS.isdisjoint(segment)
        for segment in value.split("/")
    )


class LoggingConfig(BaseModel):
    """User-configurable logging defaults."""

    model_config = ConfigDict(extra="ignore")

    level: str = "WARNING"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        level_name = value.upper()
        level_names = logging.getLevelNamesMapping()
        if level_name not in level_names:
            valid_levels = ", ".join(sorted(level_names))
            raise ValueError(f"Invalid logging level {value}. Expected one of: {valid_levels}")
        return level_name


class AppConfig(RepoConfig):
    """Top-level configuration model."""

    model_config = ConfigDict(extra="ignore")

    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(*, settings: JjSettings) -> AppConfig:
    """Build jj-stack's configuration from jj's resolved config listing.

    jj already merged the user, repo, and workspace scopes with any `--config` or
    `--config-file` overrides, so jj-stack and every downstream `jj` invocation see the
    same values.
    """

    raw = dict(settings.table(CONFIG_SECTION))
    _raise_on_likely_config_typos(config_data=raw, source="jj config")
    return _validate_config(raw, source="jj config")


def _raise_on_likely_config_typos(*, config_data: Mapping[str, object], source: str) -> None:
    _raise_on_likely_unknown_keys(
        table_path=f"[{CONFIG_SECTION}]",
        config_data=config_data,
        allowed_keys=(*RepoConfig.model_fields, "logging"),
        source=source,
    )

    logging_config = config_data.get("logging")
    if isinstance(logging_config, Mapping):
        _raise_on_likely_unknown_keys(
            table_path=f"[{CONFIG_SECTION}.logging]",
            config_data=logging_config,
            allowed_keys=tuple(LoggingConfig.model_fields),
            source=source,
        )


def _raise_on_likely_unknown_keys(
    *,
    table_path: str,
    config_data: Mapping[str, object],
    allowed_keys: tuple[str, ...],
    source: str,
) -> None:
    allowed_key_set = set(allowed_keys)
    for key in config_data:
        if key in allowed_key_set:
            continue
        suggestion = difflib.get_close_matches(key, allowed_keys, n=1, cutoff=_TYPO_CUTOFF)
        if not suggestion:
            continue
        raise CliError(
            f"Invalid jj-stack config in {source}: unknown key {table_path}.{key}. "
            f"Did you mean {table_path}.{suggestion[0]}?"
        )


def _validate_config(config_data: Mapping[str, object], *, source: str) -> AppConfig:
    try:
        return AppConfig.model_validate(config_data)
    except ValidationError as error:
        raise CliError(_format_validation_error(source=source, error=error)) from error


def _format_validation_error(*, source: str, error: ValidationError) -> str:
    details = [
        _format_validation_issue(tuple(str(part) for part in issue["loc"]), str(issue["msg"]))
        for issue in error.errors(include_url=False)
    ]
    return f"Invalid jj-stack config in {source}: {'; '.join(details)}"


def _format_validation_issue(location: tuple[str, ...], message: str) -> str:
    if len(location) == 1:
        return f"[{CONFIG_SECTION}].{location[0]}: {message}"
    if location[:1] == ("logging",) and len(location) == 2:
        return f"[{CONFIG_SECTION}.logging].{location[1]}: {message}"
    if not location:
        return message
    return f"[{CONFIG_SECTION}].{'.'.join(location)}: {message}"
