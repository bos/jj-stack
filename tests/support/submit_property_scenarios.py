"""Scenario generation for submit stack-edit property tests."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Literal

StackEditOperationKind = Literal["abandon", "insert_after", "move_to_top"]
BoundaryDriftKind = Literal["closed_pr", "wrong_saved_pr_number"]

DEFAULT_STACK_EDIT_SCENARIO_COUNT = 8
DEFAULT_STACK_EDIT_SCENARIO_SEED = 8675309
MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER = 80


@dataclass(frozen=True, slots=True)
class StackEditOperation:
    """One supported local stack edit in a generated scenario."""

    kind: StackEditOperationKind
    label: str
    new_label: str | None = None

    @property
    def trace(self) -> str:
        if self.kind == "insert_after":
            if self.new_label is None:
                raise AssertionError("insert_after operation requires a new label.")
            return f"insert_after:{self.label}:{self.new_label}"
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
            index = live_labels.index(operation.label)
            if index == len(live_labels) - 1:
                raise AssertionError("move_to_top requires a non-top label.")
            _mark_rewritten_initials(
                rewritten_initial_labels,
                live_labels[index:],
                initial_size=self.initial_size,
            )
            live_labels.pop(index)
            live_labels.append(operation.label)
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
            drift_kind="wrong_saved_pr_number",
            initial_size=3,
            label="c2",
            name="wrong-saved-pr-number",
        ),
    )


def initial_label(index: int) -> str:
    return f"c{index}"


def inserted_label(index: int) -> str:
    return f"i{index}"


def subject_for_label(label: str) -> str:
    prefix = label[0]
    number = int(label[1:])
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
        .append(StackEditOperation(kind="abandon", label="c2"))
        .to_scenario(hazard_class="abandon-middle", name="abandon-middle"),
    )


def _random_stack_edit_scenario(
    rng: random.Random,
    *,
    attempts: int,
) -> StackEditScenario:
    initial_size = rng.randint(2, 5)
    model = _model(initial_size)
    operation_count = rng.randint(1, 5)
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
        movable = tuple(label for label in model.live_labels[:-1])
        move_label = rng.choice(movable)
        operations.append(StackEditOperation(kind="move_to_top", label=move_label))

        abandonable = tuple(label for label in model.live_labels if label.startswith("c"))
        if abandonable:
            abandon_label = rng.choice(abandonable)
            operations.append(StackEditOperation(kind="abandon", label=abandon_label))

    if len(model.live_labels) < 6:
        after_label = rng.choice(model.live_labels)
        operations.append(
            StackEditOperation(
                kind="insert_after",
                label=after_label,
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


def _label_sort_key(label: str) -> tuple[str, int]:
    return (label[0], int(label[1:]))
