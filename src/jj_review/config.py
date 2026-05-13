"""Configuration loading for `jj-review`."""

from __future__ import annotations

import difflib
import logging
import tomllib
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jj_review.errors import CliError
from jj_review.jj.client import JjClient, JjCommandError

CONFIG_SECTION = "jj-review"
DEFAULT_BOOKMARK_PREFIX = "review"
_TYPO_CUTOFF = 0.75


class RepoConfig(BaseModel):
    """Repository defaults resolved before command planning."""

    model_config = ConfigDict(extra="ignore")

    bookmark_prefix: str = DEFAULT_BOOKMARK_PREFIX
    cleanup_user_bookmarks: bool = False
    labels: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    team_reviewers: list[str] = Field(default_factory=list)
    use_bookmarks: list[str] = Field(default_factory=list)

    @field_validator("bookmark_prefix")
    @classmethod
    def _validate_bookmark_prefix(cls, value: str) -> str:
        prefix = value.strip()
        if not prefix:
            raise ValueError("bookmark_prefix must not be empty")
        if "/" in prefix:
            raise ValueError("bookmark_prefix must not contain '/'")
        return prefix

    @field_validator("use_bookmarks")
    @classmethod
    def _validate_use_bookmarks(cls, values: list[str]) -> list[str]:
        patterns: list[str] = []
        seen: set[str] = set()
        for value in values:
            pattern = value.strip()
            if not pattern:
                continue
            if pattern in seen:
                continue
            seen.add(pattern)
            patterns.append(pattern)
        return patterns


class LoggingConfig(BaseModel):
    """User-configurable logging defaults."""

    model_config = ConfigDict(extra="ignore")

    http_debug: bool = False
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


def load_config(*, jj_client: JjClient) -> AppConfig:
    """Load `jj-review` config by delegating resolution to `jj` itself.

    `jj config list 'jj-review'` respects user/repo/workspace scopes plus any
    `--config` / `--config-file` overrides already attached to `jj_client`, so
    jj-review and every downstream `jj` invocation see the same resolved view.
    """

    try:
        stdout = jj_client.read_jj_review_config_list_output()
    except JjCommandError as error:
        raise CliError(
            f"Could not load jj-review config: {_jj_error_detail(error)}"
        ) from error
    raw = parse_jj_review_config_toml(stdout)
    _raise_on_likely_config_typos(config_data=raw, source="jj config")
    return _validate_config(raw, source="jj config")


def _jj_error_detail(error: JjCommandError) -> str:
    """Strip the inner command trace from a JjCommandError when surfacing it."""

    message = str(error)
    marker = " failed: "
    index = message.find(marker)
    if index == -1:
        return message
    return message[index + len(marker) :]


def parse_jj_review_config_toml(text: str) -> dict[str, object]:
    """Parse the TOML-formatted output of `jj config list 'jj-review'`.

    Returns the contents of the ``[jj-review]`` table as a mapping, or an empty
    mapping when jj has no matching keys set.
    """

    stripped = text.strip()
    if not stripped:
        return {}
    try:
        parsed = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError as error:
        raise CliError(
            f"Could not parse jj-review config from jj: {error}"
        ) from error
    section = parsed.get(CONFIG_SECTION, {})
    if not isinstance(section, Mapping):
        raise CliError(
            f"Invalid jj-review config from jj: [{CONFIG_SECTION}] must be a table."
        )
    return dict(section)


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
            f"Invalid jj-review config in {source}: unknown key {table_path}.{key}. "
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
    return f"Invalid jj-review config in {source}: {'; '.join(details)}"


def _format_validation_issue(location: tuple[str, ...], message: str) -> str:
    if len(location) == 1:
        return f"[{CONFIG_SECTION}].{location[0]}: {message}"
    if location[:1] == ("logging",) and len(location) == 2:
        return f"[{CONFIG_SECTION}.logging].{location[1]}: {message}"
    if not location:
        return message
    return f"[{CONFIG_SECTION}].{'.'.join(location)}: {message}"
