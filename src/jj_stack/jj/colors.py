"""Load jj's color configuration and resolve color labels into Rich styles."""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from rich.style import Style

from jj_stack.jj.client import JjCliArgs

_JJ_COLORS_TEMPLATE = r'name ++ "\0" ++ json(value) ++ "\n"'
_JJ_STYLE_ATTRIBUTES = frozenset({"bg", "bold", "dim", "fg", "italic", "reverse", "underline"})
_SEMANTIC_STYLE_FALLBACKS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (frozenset({"command"}), ("config_list", "name")),
    (frozenset({"revset"}), ("bookmark",)),
    (frozenset({"code"}), ("config_list", "value")),
)


@dataclass(frozen=True, slots=True)
class _SemanticStyleRule:
    labels: frozenset[str]
    style: Style


class SemanticStyles:
    """Resolve jj color-label sets into Rich styles."""

    def __init__(self, rules: tuple[_SemanticStyleRule, ...]) -> None:
        self._rules = tuple(
            sorted(
                rules,
                key=lambda rule: (len(rule.labels), tuple(sorted(rule.labels))),
            )
        )

    def for_labels(self, labels: tuple[str, ...]) -> Style | None:
        normalized_labels = _normalize_semantic_labels(labels)
        if not normalized_labels:
            return None

        style, matched_labels = self._resolve_direct_style(normalized_labels)
        matched = style != Style.null()
        for trigger_labels, fallback_labels in _SEMANTIC_STYLE_FALLBACKS:
            if not trigger_labels.issubset(normalized_labels):
                continue
            if trigger_labels.issubset(matched_labels):
                continue
            fallback_style, _ = self._resolve_direct_style(frozenset(fallback_labels))
            if fallback_style == Style.null():
                continue
            style += fallback_style
            matched = True
        return style if matched else None

    def _resolve_direct_style(
        self,
        normalized_labels: frozenset[str],
    ) -> tuple[Style, frozenset[str]]:
        style = Style.null()
        matched_labels: set[str] = set()
        for rule in self._rules:
            if rule.labels.issubset(normalized_labels):
                style += rule.style
                matched_labels.update(rule.labels)
        return style, frozenset(matched_labels)


def load_semantic_styles(
    *,
    repository: Path | None,
    cli_args: JjCliArgs,
) -> SemanticStyles | None:
    """Load effective jj semantic color styles for Rich-authored output."""

    cwd = (
        repository
        if repository is not None and repository.exists() and repository.is_dir()
        else Path.cwd()
    )
    try:
        completed = subprocess.run(
            [
                "jj",
                *cli_args.to_argv(),
                "--ignore-working-copy",
                "config",
                "list",
                "--include-defaults",
                "colors",
                "-T",
                _JJ_COLORS_TEMPLATE,
            ],
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None

    if completed.returncode != 0:
        return None

    rules = _semantic_style_rules_from_config_list(completed.stdout)
    return SemanticStyles(rules) if rules else None


def _semantic_style_rules_from_config_list(stdout: str) -> tuple[_SemanticStyleRule, ...]:
    """Parse `jj config list colors` output into Rich style rules."""

    grouped_styles: dict[frozenset[str], Style] = {}
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        try:
            raw_name, raw_value = raw_line.split("\0", maxsplit=1)
        except ValueError:
            continue
        label_name, attribute = _parse_color_config_name(raw_name)
        if label_name is None:
            continue
        label_set = _normalize_semantic_labels((label_name,))
        if not label_set:
            continue

        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        style = _style_from_config_value(attribute, value)
        if style is None:
            continue
        existing = grouped_styles.get(label_set)
        grouped_styles[label_set] = style if existing is None else existing + style

    return tuple(
        _SemanticStyleRule(labels=labels, style=style)
        for labels, style in grouped_styles.items()
    )


def _style_from_config_value(attribute: str | None, value: object) -> Style | None:
    """Translate one jj color-config entry into a Rich style fragment."""

    if attribute is None or attribute == "fg":
        rich_color = _normalize_jj_color_value(value)
        return None if rich_color is None else Style(color=rich_color)
    if attribute == "bg":
        rich_color = _normalize_jj_color_value(value)
        return None if rich_color is None else Style(bgcolor=rich_color)
    if isinstance(value, bool):
        # The remaining attributes (bold, dim, ...) are valid Rich style words.
        return Style.parse(attribute if value else f"not {attribute}")
    return None


def _parse_color_config_name(name: str) -> tuple[str | None, str | None]:
    """Extract a jj color label name and optional style attribute."""

    try:
        parsed = tomllib.loads(f"{name} = 0\n")
    except tomllib.TOMLDecodeError:
        return None, None

    colors = parsed.get("colors")
    if not isinstance(colors, dict) or len(colors) != 1:
        return None, None

    label_name, value = next(iter(colors.items()))
    if not isinstance(label_name, str):
        return None, None
    if not isinstance(value, dict):
        return label_name, None
    if len(value) != 1:
        return None, None

    attribute = next(iter(value))
    if attribute not in _JJ_STYLE_ATTRIBUTES:
        return None, None
    return label_name, attribute


def _normalize_semantic_labels(labels: tuple[str, ...]) -> frozenset[str]:
    normalized: set[str] = set()
    for label in labels:
        normalized.update(part for part in label.split() if part)
    return frozenset(normalized)


def _normalize_jj_color_value(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("ansi-color-"):
        index = value.removeprefix("ansi-color-")
        return f"color({index})" if index.isdigit() else None
    if value.startswith("bright "):
        return value.replace(" ", "_", 1)
    return value
