"""Resolve jj's color configuration into Rich styles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from rich.style import Style

JjColorWhen = Literal["always", "debug", "never"]

_JJ_STYLE_ATTRIBUTES = frozenset({"bg", "bold", "dim", "fg", "italic", "reverse", "underline"})
_SEMANTIC_STYLE_FALLBACKS: tuple[tuple[frozenset[str], tuple[str, ...]], ...] = (
    (frozenset({"command"}), ("config_list", "name")),
    (frozenset({"revset"}), ("bookmark",)),
    (frozenset({"metavar"}), ("bookmark",)),
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


def semantic_styles(colors: Mapping[str, object]) -> SemanticStyles | None:
    """Build Rich styles from jj's resolved `colors` table.

    Each entry maps a label name such as `"diff added"` to either a color name or a table of
    style attributes (`fg`, `bg`, `bold`, ...), as `jj config list --include-defaults` reports
    them.
    """

    grouped_styles: dict[frozenset[str], Style] = {}
    for label_name, value in colors.items():
        label_set = _normalize_semantic_labels((label_name,))
        if not label_set:
            continue
        if isinstance(value, Mapping):
            fragments = (
                _style_from_config_value(attribute, attribute_value)
                for attribute, attribute_value in value.items()
                if attribute in _JJ_STYLE_ATTRIBUTES
            )
        else:
            fragments = (_style_from_config_value(None, value),)
        for style in fragments:
            if style is None:
                continue
            existing = grouped_styles.get(label_set)
            grouped_styles[label_set] = style if existing is None else existing + style

    rules = tuple(
        _SemanticStyleRule(labels=labels, style=style) for labels, style in grouped_styles.items()
    )
    return SemanticStyles(rules) if rules else None


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
