"""Scenario generation for submit stack-edit property tests."""

from __future__ import annotations

import os
import random
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
BoundaryDriftKind = Literal[
    "closed_pr",
    "conflicted_rebase",
    "merge_commit",
    "wrong_saved_pr_number",
]

DEFAULT_STACK_EDIT_SCENARIO_COUNT = 8
DEFAULT_CROSS_STACK_SCENARIO_COUNT = 8
DEFAULT_STACK_MERGE_SCENARIO_COUNT = 8
DEFAULT_STACK_EDIT_SCENARIO_SEED = 8675309
MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER = 80


@dataclass(frozen=True, slots=True)
class StackEditOperation:
    """One supported local stack edit in a generated scenario."""

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
class StackEditScenario:
    """A generated stack-edit scenario plus its modeled final state."""

    name: str
    hazard_class: str
    initial_size: int
    operations: tuple[StackEditOperation, ...]
    final_live_labels: tuple[str, ...]
    orphaned_labels: tuple[str, ...]
    rewritten_initial_labels: tuple[str, ...]

    @property
    def trace(self) -> str:
        return ",".join(operation.trace for operation in self.operations)

    @property
    def canonical_key(
        self,
    ) -> tuple[
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            self.hazard_class,
            self.final_live_labels,
            self.orphaned_labels,
            self.rewritten_initial_labels,
        )

    def __str__(self) -> str:
        return f"{self.name}: {self.trace}"


@dataclass(frozen=True, slots=True)
class BoundaryDriftScenario:
    """A fail-closed scenario with one perturbed external boundary."""

    name: str
    drift_kind: BoundaryDriftKind
    initial_size: int
    label: str


@dataclass(frozen=True, slots=True)
class CrossStackSplitScenario:
    """A rewrite that splits one submitted stack into selected and deferred stacks."""

    name: str
    hazard_class: str
    initial_size: int
    source_label: str
    target_label: str
    selected_labels: tuple[str, ...]
    deferred_labels: tuple[str, ...]
    deferred_stack_labels: tuple[str, ...]
    rewritten_initial_labels: tuple[str, ...]

    @property
    def trace(self) -> str:
        return f"move_suffix_onto:{self.source_label}:{self.target_label}"

    @property
    def canonical_key(
        self,
    ) -> tuple[
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            self.hazard_class,
            self.selected_labels,
            self.deferred_labels,
            self.rewritten_initial_labels,
        )


@dataclass(frozen=True, slots=True)
class StackMergeScenario:
    """A rewrite that merges two independently submitted stacks into one stack."""

    name: str
    hazard_class: str
    first_stack_labels: tuple[str, ...]
    second_stack_labels: tuple[str, ...]
    selected_labels: tuple[str, ...]
    source_label: str
    target_label: str
    rewritten_initial_labels: tuple[str, ...]

    @property
    def initial_size(self) -> int:
        return len(self.first_stack_labels) + len(self.second_stack_labels)

    @property
    def trace(self) -> str:
        return f"merge_stack_onto:{self.source_label}:{self.target_label}"

    @property
    def canonical_key(
        self,
    ) -> tuple[
        str,
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            self.hazard_class,
            self.selected_labels,
            self.rewritten_initial_labels,
        )


@dataclass(frozen=True)
class _ScenarioModel:
    initial_size: int
    live_labels: tuple[str, ...]
    operations: tuple[StackEditOperation, ...] = ()
    orphaned_labels: tuple[str, ...] = ()
    rewritten_initial_labels: tuple[str, ...] = ()
    next_insert_index: int = 1

    def append(self, operation: StackEditOperation) -> _ScenarioModel:
        live_labels = list(self.live_labels)
        orphaned_labels = set(self.orphaned_labels)
        rewritten_initial_labels = set(self.rewritten_initial_labels)
        next_insert_index = self.next_insert_index

        if operation.kind == "move_to_top":
            _move_to_top(
                live_labels,
                operation.label,
                rewritten_initial_labels,
                initial_size=self.initial_size,
            )
        elif operation.kind == "move_after":
            _move_after(
                live_labels,
                operation.label,
                _require_target_label(operation),
                rewritten_initial_labels,
                initial_size=self.initial_size,
            )
        elif operation.kind == "move_before":
            _move_before(
                live_labels,
                operation.label,
                _require_target_label(operation),
                rewritten_initial_labels,
                initial_size=self.initial_size,
            )
        elif operation.kind == "insert_after":
            if operation.new_label is None:
                raise AssertionError("insert_after operation requires a new label.")
            index = live_labels.index(operation.label)
            _mark_rewritten_initials(
                rewritten_initial_labels,
                live_labels[index + 1 :],
                initial_size=self.initial_size,
            )
            live_labels.insert(index + 1, operation.new_label)
            next_insert_index += 1
        elif operation.kind == "insert_before":
            if operation.new_label is None:
                raise AssertionError("insert_before operation requires a new label.")
            index = live_labels.index(operation.label)
            _mark_rewritten_initials(
                rewritten_initial_labels,
                live_labels[index:],
                initial_size=self.initial_size,
            )
            live_labels.insert(index, operation.new_label)
            next_insert_index += 1
        elif operation.kind == "abandon":
            index = live_labels.index(operation.label)
            if len(live_labels) == 1:
                raise AssertionError("abandon requires a surviving live change.")
            if not operation.label.startswith("c"):
                raise AssertionError("abandon requires an initially submitted label.")
            _mark_rewritten_initials(
                rewritten_initial_labels,
                live_labels[index + 1 :],
                initial_size=self.initial_size,
            )
            live_labels.pop(index)
            orphaned_labels.add(operation.label)
        elif operation.kind == "rewrite":
            index = live_labels.index(operation.label)
            _mark_rewritten_initials(
                rewritten_initial_labels,
                live_labels[index:],
                initial_size=self.initial_size,
            )
        elif operation.kind == "squash_into_previous":
            index = live_labels.index(operation.label)
            if index == 0:
                raise AssertionError("squash_into_previous requires a non-bottom label.")
            _mark_rewritten_initials(
                rewritten_initial_labels,
                live_labels[index - 1 :],
                initial_size=self.initial_size,
            )
            live_labels.pop(index)
            if operation.label.startswith("c"):
                orphaned_labels.add(operation.label)
        else:
            raise AssertionError(f"unsupported operation kind: {operation.kind}")

        return _ScenarioModel(
            initial_size=self.initial_size,
            live_labels=tuple(live_labels),
            operations=(*self.operations, operation),
            orphaned_labels=tuple(sorted(orphaned_labels, key=_label_sort_key)),
            rewritten_initial_labels=tuple(sorted(rewritten_initial_labels, key=_label_sort_key)),
            next_insert_index=next_insert_index,
        )

    def to_scenario(self, *, hazard_class: str, name: str) -> StackEditScenario:
        return StackEditScenario(
            final_live_labels=self.live_labels,
            hazard_class=hazard_class,
            initial_size=self.initial_size,
            name=name,
            operations=self.operations,
            orphaned_labels=self.orphaned_labels,
            rewritten_initial_labels=self.rewritten_initial_labels,
        )


def stack_edit_scenarios_from_environment() -> tuple[StackEditScenario, ...]:
    """Return the default deterministic scenario set for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_REVIEW_SUBMIT_PROPERTY_SCENARIOS",
            str(DEFAULT_STACK_EDIT_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_REVIEW_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_stack_edit_scenarios(count=count, seed=seed)


def cross_stack_scenarios_from_environment() -> tuple[CrossStackSplitScenario, ...]:
    """Return deterministic cross-stack split scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_REVIEW_SUBMIT_PROPERTY_CROSS_STACK_SCENARIOS",
            str(DEFAULT_CROSS_STACK_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_REVIEW_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_cross_stack_split_scenarios(count=count, seed=seed)


def stack_merge_scenarios_from_environment() -> tuple[StackMergeScenario, ...]:
    """Return deterministic stack-merge scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_REVIEW_SUBMIT_PROPERTY_STACK_MERGE_SCENARIOS",
            str(DEFAULT_STACK_MERGE_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_REVIEW_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_stack_merge_scenarios(count=count, seed=seed)


def generate_stack_edit_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[StackEditScenario, ...]:
    """Generate deterministic stack-edit scenarios, preserving fixed hazard coverage."""

    if count < 1:
        return ()

    scenarios: list[StackEditScenario] = []
    seen: set[
        tuple[
            str,
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
        ]
    ] = set()
    for scenario in _fixed_stack_edit_scenarios():
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)
        if len(scenarios) >= count:
            return tuple(scenarios)

    rng = random.Random(seed)
    max_attempts = count * MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER
    attempts = 0
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        scenario = _random_stack_edit_scenario(rng, attempts=attempts)
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


def generate_stack_merge_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[StackMergeScenario, ...]:
    """Generate two-stack merge scenarios that should preserve every PR identity."""

    if count < 1:
        return ()

    scenarios: list[StackMergeScenario] = []
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    for scenario in _fixed_stack_merge_scenarios():
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)
        if len(scenarios) >= count:
            return tuple(scenarios)

    rng = random.Random(seed + 2)
    max_attempts = count * MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER
    attempts = 0
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        first_size = rng.randint(1, 5)
        second_size = rng.randint(1, 5)
        first_then_second = rng.choice((True, False))
        scenario = _stack_merge_scenario(
            first_size=first_size,
            first_then_second=first_then_second,
            hazard_class="random",
            name=f"merge-random-{attempts:03d}",
            second_size=second_size,
        )
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


def generate_cross_stack_split_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[CrossStackSplitScenario, ...]:
    """Generate suffix-move scenarios that split one submitted stack into two stacks."""

    if count < 1:
        return ()

    scenarios: list[CrossStackSplitScenario] = []
    seen: set[
        tuple[
            str,
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
        ]
    ] = set()
    for scenario in _fixed_cross_stack_split_scenarios():
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)
        if len(scenarios) >= count:
            return tuple(scenarios)

    rng = random.Random(seed + 1)
    max_attempts = count * MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER
    attempts = 0
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        initial_size = rng.randint(4, 8)
        source_index = rng.randint(2, initial_size - 1)
        target_index = rng.randint(0, source_index - 2)
        scenario = _cross_stack_split_scenario(
            initial_size=initial_size,
            source_index=source_index,
            target_index=target_index,
            hazard_class="random",
            name=f"cross-random-{attempts:03d}",
        )
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


def boundary_drift_scenarios() -> tuple[BoundaryDriftScenario, ...]:
    """Representative fail-closed boundary-drift scenarios."""

    return (
        BoundaryDriftScenario(
            drift_kind="closed_pr",
            initial_size=3,
            label="c2",
            name="closed-pr",
        ),
        BoundaryDriftScenario(
            drift_kind="conflicted_rebase",
            initial_size=3,
            label="c3",
            name="conflicted-rebase",
        ),
        BoundaryDriftScenario(
            drift_kind="merge_commit",
            initial_size=3,
            label="c3",
            name="merge-commit",
        ),
        BoundaryDriftScenario(
            drift_kind="wrong_saved_pr_number",
            initial_size=3,
            label="c2",
            name="wrong-saved-pr-number",
        ),
    )


def _fixed_stack_merge_scenarios() -> tuple[StackMergeScenario, ...]:
    return (
        _stack_merge_scenario(
            first_size=2,
            first_then_second=True,
            hazard_class="append-second",
            name="merge-second-after-first",
            second_size=2,
        ),
        _stack_merge_scenario(
            first_size=2,
            first_then_second=False,
            hazard_class="append-first",
            name="merge-first-after-second",
            second_size=2,
        ),
        _stack_merge_scenario(
            first_size=1,
            first_then_second=True,
            hazard_class="single-first",
            name="merge-single-first",
            second_size=3,
        ),
        _stack_merge_scenario(
            first_size=3,
            first_then_second=False,
            hazard_class="single-second",
            name="merge-single-second",
            second_size=1,
        ),
    )


def _stack_merge_scenario(
    *,
    first_size: int,
    first_then_second: bool,
    hazard_class: str,
    name: str,
    second_size: int,
) -> StackMergeScenario:
    first_labels = tuple(_stack_label("a", index) for index in range(1, first_size + 1))
    second_labels = tuple(_stack_label("b", index) for index in range(1, second_size + 1))
    if first_then_second:
        selected_labels = (*first_labels, *second_labels)
        source_label = second_labels[0]
        target_label = first_labels[-1]
        rewritten_initial_labels = second_labels
    else:
        selected_labels = (*second_labels, *first_labels)
        source_label = first_labels[0]
        target_label = second_labels[-1]
        rewritten_initial_labels = first_labels
    return StackMergeScenario(
        first_stack_labels=first_labels,
        hazard_class=hazard_class,
        name=name,
        rewritten_initial_labels=rewritten_initial_labels,
        second_stack_labels=second_labels,
        selected_labels=selected_labels,
        source_label=source_label,
        target_label=target_label,
    )


def _fixed_cross_stack_split_scenarios() -> tuple[CrossStackSplitScenario, ...]:
    return (
        _cross_stack_split_scenario(
            initial_size=4,
            source_index=2,
            target_index=0,
            hazard_class="split-middle",
            name="split-middle-deferred-one",
        ),
        _cross_stack_split_scenario(
            initial_size=5,
            source_index=3,
            target_index=1,
            hazard_class="split-middle",
            name="split-middle-after-two",
        ),
        _cross_stack_split_scenario(
            initial_size=5,
            source_index=2,
            target_index=0,
            hazard_class="split-long-selected",
            name="split-long-selected",
        ),
        _cross_stack_split_scenario(
            initial_size=6,
            source_index=4,
            target_index=1,
            hazard_class="split-long-deferred",
            name="split-long-deferred",
        ),
    )


def _cross_stack_split_scenario(
    *,
    initial_size: int,
    source_index: int,
    target_index: int,
    hazard_class: str,
    name: str,
) -> CrossStackSplitScenario:
    labels = tuple(initial_label(index) for index in range(1, initial_size + 1))
    if target_index + 1 >= source_index:
        raise AssertionError("cross-stack split requires at least one deferred label.")
    selected_labels = (*labels[: target_index + 1], *labels[source_index:])
    deferred_labels = labels[target_index + 1 : source_index]
    return CrossStackSplitScenario(
        deferred_labels=deferred_labels,
        deferred_stack_labels=(*labels[: target_index + 1], *deferred_labels),
        hazard_class=hazard_class,
        initial_size=initial_size,
        name=name,
        rewritten_initial_labels=labels[source_index:],
        selected_labels=selected_labels,
        source_label=labels[source_index],
        target_label=labels[target_index],
    )


def initial_label(index: int) -> str:
    return f"c{index}"


def inserted_label(index: int) -> str:
    return f"i{index}"


def _stack_label(prefix: str, index: int) -> str:
    return f"{prefix}{index}"


def subject_for_label(label: str) -> str:
    prefix = label[0]
    number = int(label[1:])
    if prefix == "a":
        return f"stack a feature {number}"
    if prefix == "b":
        return f"stack b feature {number}"
    if prefix == "c":
        return f"feature {number}"
    if prefix == "i":
        return f"feature inserted {number}"
    raise AssertionError(f"unsupported scenario label: {label}")


def filename_for_label(label: str) -> str:
    return f"{label}.txt"


def _fixed_stack_edit_scenarios() -> tuple[StackEditScenario, ...]:
    return (
        _model(4)
        .append(StackEditOperation(kind="move_to_top", label="c1"))
        .to_scenario(hazard_class="move-old-bottom", name="move-old-bottom"),
        _model(4)
        .append(StackEditOperation(kind="move_to_top", label="c2"))
        .to_scenario(hazard_class="move-middle", name="move-middle"),
        _model(3)
        .append(
            StackEditOperation(
                kind="insert_after",
                label="c1",
                new_label="i1",
            )
        )
        .to_scenario(hazard_class="insert-middle", name="insert-middle"),
        _model(3)
        .append(
            StackEditOperation(
                kind="insert_before",
                label="c2",
                new_label="i1",
            )
        )
        .to_scenario(hazard_class="insert-before-middle", name="insert-before-middle"),
        _model(3)
        .append(StackEditOperation(kind="abandon", label="c2"))
        .to_scenario(hazard_class="abandon-middle", name="abandon-middle"),
        _model(4)
        .append(StackEditOperation(kind="move_before", label="c4", target_label="c2"))
        .to_scenario(hazard_class="move-before-middle", name="move-before-middle"),
        _model(3)
        .append(StackEditOperation(kind="rewrite", label="c2"))
        .to_scenario(hazard_class="rewrite-middle", name="rewrite-middle"),
        _model(3)
        .append(StackEditOperation(kind="squash_into_previous", label="c2"))
        .to_scenario(hazard_class="squash-middle", name="squash-middle-into-previous"),
    )


def _random_stack_edit_scenario(
    rng: random.Random,
    *,
    attempts: int,
) -> StackEditScenario:
    initial_size = rng.randint(2, 6)
    model = _model(initial_size)
    operation_count = rng.randint(1, 7)
    for _ in range(operation_count):
        operations = _available_operations(model, rng)
        if not operations:
            break
        model = model.append(rng.choice(operations))

    return model.to_scenario(
        hazard_class="random",
        name=f"random-{attempts:03d}",
    )


def _available_operations(
    model: _ScenarioModel,
    rng: random.Random,
) -> tuple[StackEditOperation, ...]:
    operations: list[StackEditOperation] = []
    if len(model.live_labels) > 1:
        movable_to_top = tuple(label for label in model.live_labels[:-1])
        move_label = rng.choice(movable_to_top)
        operations.append(StackEditOperation(kind="move_to_top", label=move_label))

        move_after_candidates = _move_after_candidates(model.live_labels)
        if move_after_candidates:
            label, target_label = rng.choice(move_after_candidates)
            operations.append(
                StackEditOperation(
                    kind="move_after",
                    label=label,
                    target_label=target_label,
                )
            )

        move_before_candidates = _move_before_candidates(model.live_labels)
        if move_before_candidates:
            label, target_label = rng.choice(move_before_candidates)
            operations.append(
                StackEditOperation(
                    kind="move_before",
                    label=label,
                    target_label=target_label,
                )
            )

        abandonable = tuple(label for label in model.live_labels if label.startswith("c"))
        if abandonable:
            abandon_label = rng.choice(abandonable)
            operations.append(StackEditOperation(kind="abandon", label=abandon_label))

        squashable = tuple(model.live_labels[1:])
        squash_label = rng.choice(squashable)
        operations.append(
            StackEditOperation(kind="squash_into_previous", label=squash_label)
        )

    rewrite_label = rng.choice(model.live_labels)
    operations.append(StackEditOperation(kind="rewrite", label=rewrite_label))

    if len(model.live_labels) < 8:
        after_label = rng.choice(model.live_labels)
        operations.append(
            StackEditOperation(
                kind="insert_after",
                label=after_label,
                new_label=inserted_label(model.next_insert_index),
            )
        )
        before_label = rng.choice(model.live_labels)
        operations.append(
            StackEditOperation(
                kind="insert_before",
                label=before_label,
                new_label=inserted_label(model.next_insert_index),
            )
        )

    return tuple(operations)


def _model(initial_size: int) -> _ScenarioModel:
    return _ScenarioModel(
        initial_size=initial_size,
        live_labels=tuple(initial_label(index) for index in range(1, initial_size + 1)),
    )


def _mark_rewritten_initials(
    target: set[str],
    labels: list[str],
    *,
    initial_size: int,
) -> None:
    for label in labels:
        if label.startswith("c") and int(label[1:]) <= initial_size:
            target.add(label)


def _require_target_label(operation: StackEditOperation) -> str:
    if operation.target_label is None:
        raise AssertionError(f"{operation.kind} operation requires a target label.")
    return operation.target_label


def _move_to_top(
    live_labels: list[str],
    label: str,
    rewritten_initial_labels: set[str],
    *,
    initial_size: int,
) -> None:
    index = live_labels.index(label)
    if index == len(live_labels) - 1:
        raise AssertionError("move_to_top requires a non-top label.")
    _mark_rewritten_initials(
        rewritten_initial_labels,
        live_labels[index:],
        initial_size=initial_size,
    )
    live_labels.pop(index)
    live_labels.append(label)


def _move_after(
    live_labels: list[str],
    label: str,
    target_label: str,
    rewritten_initial_labels: set[str],
    *,
    initial_size: int,
) -> None:
    index = live_labels.index(label)
    target_index = live_labels.index(target_label)
    if target_label == label:
        raise AssertionError("move_after requires a different target label.")
    if index == target_index + 1:
        raise AssertionError("move_after requires a non-current parent target.")
    _mark_rewritten_initials(
        rewritten_initial_labels,
        live_labels[min(index, target_index) :],
        initial_size=initial_size,
    )
    moved = live_labels.pop(index)
    target_index = live_labels.index(target_label)
    live_labels.insert(target_index + 1, moved)


def _move_before(
    live_labels: list[str],
    label: str,
    target_label: str,
    rewritten_initial_labels: set[str],
    *,
    initial_size: int,
) -> None:
    index = live_labels.index(label)
    target_index = live_labels.index(target_label)
    if target_label == label:
        raise AssertionError("move_before requires a different target label.")
    if index + 1 == target_index:
        raise AssertionError("move_before requires a non-current child target.")
    _mark_rewritten_initials(
        rewritten_initial_labels,
        live_labels[min(index, target_index) :],
        initial_size=initial_size,
    )
    moved = live_labels.pop(index)
    target_index = live_labels.index(target_label)
    live_labels.insert(target_index, moved)


def _move_after_candidates(live_labels: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for index, label in enumerate(live_labels):
        for target_index, target_label in enumerate(live_labels):
            if target_label == label or index == target_index + 1:
                continue
            candidates.append((label, target_label))
    return tuple(candidates)


def _move_before_candidates(live_labels: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    for index, label in enumerate(live_labels):
        for target_index, target_label in enumerate(live_labels):
            if target_label == label or index + 1 == target_index:
                continue
            candidates.append((label, target_label))
    return tuple(candidates)


def _label_sort_key(label: str) -> tuple[str, int]:
    return (label[0], int(label[1:]))
