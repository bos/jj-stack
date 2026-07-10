"""Shared stack-edit vocabulary and pure order transition model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StackEditOperationKind = Literal[
    "abandon",
    "insert_after",
    "insert_before",
    "move_after",
    "move_before",
    "move_to_top",
    "rewrite",
    "squash_into_previous",
]


@dataclass(frozen=True, slots=True)
class StackEditOperation:
    """One user-reachable local edit applied to a linear stack."""

    kind: StackEditOperationKind
    label: str
    new_label: str | None = None
    target_label: str | None = None

    @property
    def trace(self) -> str:
        if self.kind in {"insert_after", "insert_before"}:
            if self.new_label is None:
                raise AssertionError(f"{self.kind} operation requires a new label.")
            return f"{self.kind}:{self.label}:{self.new_label}"
        if self.kind in {"move_after", "move_before"}:
            if self.target_label is None:
                raise AssertionError(f"{self.kind} operation requires a target label.")
            return f"{self.kind}:{self.label}:{self.target_label}"
        return f"{self.kind}:{self.label}"


@dataclass(frozen=True, slots=True)
class StackEditEffect:
    """Modeled order and rewrite consequences of one stack edit."""

    content_divergent_labels: frozenset[str]
    live_labels: tuple[str, ...]
    removed_label: str | None
    rewritten_labels: frozenset[str]


def move_after_candidates(live_labels: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Return moves that change the order instead of naming the current parent."""

    return tuple(
        (label, target_label)
        for index, label in enumerate(live_labels)
        for target_index, target_label in enumerate(live_labels)
        if target_label != label and index != target_index + 1
    )


def move_before_candidates(live_labels: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Return moves that change the order instead of naming the current child."""

    return tuple(
        (label, target_label)
        for index, label in enumerate(live_labels)
        for target_index, target_label in enumerate(live_labels)
        if target_label != label and index + 1 != target_index
    )


def apply_stack_edit(
    live_labels: tuple[str, ...],
    operation: StackEditOperation,
) -> StackEditEffect:
    """Apply one validated edit to label order and report its semantic effects."""

    live = list(live_labels)
    if operation.label not in live:
        raise ValueError(f"edit targets a change that is not live: {operation.trace}")
    index = live.index(operation.label)
    rewritten: set[str] = set()
    divergent: set[str] = set()
    removed_label: str | None = None

    if operation.kind == "abandon":
        if len(live) < 2:
            raise ValueError("abandon requires a surviving live change")
        rewritten.update(live[index + 1 :])
        removed_label = live.pop(index)
    elif operation.kind == "rewrite":
        rewritten.update(live[index:])
        divergent.add(operation.label)
    elif operation.kind in {"insert_after", "insert_before"}:
        new_label = operation.new_label
        if new_label is None:
            raise ValueError(f"{operation.kind} requires a new label")
        if new_label in live:
            raise ValueError(f"inserted label is already live: {new_label}")
        insert_at = index + 1 if operation.kind == "insert_after" else index
        rewritten.update(live[insert_at:])
        live.insert(insert_at, new_label)
    elif operation.kind == "move_to_top":
        if live[-1] == operation.label:
            raise ValueError("move_to_top target is already at the top")
        rewritten.update(live[index:])
        live.pop(index)
        live.append(operation.label)
    elif operation.kind in {"move_after", "move_before"}:
        target = operation.target_label
        if target is None or target == operation.label or target not in live:
            raise ValueError(f"move requires a distinct live target: {operation.trace}")
        target_index = live.index(target)
        if operation.kind == "move_after" and index == target_index + 1:
            raise ValueError("move_after target is already the current parent")
        if operation.kind == "move_before" and index + 1 == target_index:
            raise ValueError("move_before target is already the current child")
        rewritten.update(live[min(index, target_index) :])
        live.pop(index)
        target_index = live.index(target)
        insert_at = target_index + 1 if operation.kind == "move_after" else target_index
        live.insert(insert_at, operation.label)
    elif operation.kind == "squash_into_previous":
        if index == 0:
            raise ValueError("squash_into_previous requires a non-bottom change")
        destination = live[index - 1]
        rewritten.update(live[index - 1 :])
        divergent.add(destination)
        removed_label = live.pop(index)
    else:
        raise ValueError(f"unsupported stack edit kind: {operation.kind}")

    return StackEditEffect(
        content_divergent_labels=frozenset(divergent),
        live_labels=tuple(live),
        removed_label=removed_label,
        rewritten_labels=frozenset(rewritten),
    )
