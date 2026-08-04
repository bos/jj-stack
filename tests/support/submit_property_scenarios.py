"""Scenario generation for submit stack-edit property tests."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Literal

from .stack_edit_scenarios import (
    StackEditOperation,
    apply_stack_edit,
    move_after_candidates,
    move_before_candidates,
)

DriftKind = Literal[
    "closed_pr",
    "foreign_branch_fetched",
    "pr_base_retargeted",
    "pr_draft_toggled",
    "pr_replaced",
    "remote_branch_deleted",
    "remote_branch_drift",
    "trunk_advanced",
]
DriftOutcome = Literal["fail_closed", "success"]
SubmitRetryFailurePoint = Literal[
    "after_remote_push",
    "create_pull_request",
    "update_pull_request",
    "pull_request_metadata",
]
DEFAULT_STACK_EDIT_SCENARIO_SEED = 8675309
MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER = 80


@dataclass(frozen=True, slots=True)
class SubmitInvariants:
    """The shared post-submit contract every replay shape asserts against.

    Scenario types differ in how the final state is reached, but the success
    invariants always read the same fields: the live labels in the selected
    stack, the abandoned-but-orphan labels, the size of the original submitted
    stack, and a trace string used in failure diagnostics.
    """

    final_live_labels: tuple[str, ...]
    initial_size: int
    orphaned_labels: tuple[str, ...]
    trace: str


@dataclass(frozen=True, slots=True)
class LifecycleScenario:
    name: str
    template: Literal["cleanup_sync", "closed_restart", "direct_merge"]
    stack_size: int
    merged_prefix: int
    merge_method: Literal["rebase", "squash"]


LIFECYCLE_SCENARIOS = (
    LifecycleScenario("external-squash-cleanup-sync", "cleanup_sync", 1, 1, "squash"),
    LifecycleScenario("closed-single-cleanup-resubmit", "closed_restart", 1, 1, "squash"),
    LifecycleScenario("direct-rebase-two-of-four", "direct_merge", 4, 2, "rebase"),
)


def lifecycle_scenarios_from_environment() -> tuple[LifecycleScenario, ...]:
    count = int(os.environ.get("JJ_STACK_SUBMIT_PROPERTY_LIFECYCLE_SCENARIOS", "3"))
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_lifecycle_scenarios(count=count, seed=seed)


def generate_lifecycle_scenarios(*, count: int, seed: int) -> tuple[LifecycleScenario, ...]:
    if count < 1:
        return ()
    if count <= len(LIFECYCLE_SCENARIOS):
        return LIFECYCLE_SCENARIOS[:count]

    fixed_direct = LIFECYCLE_SCENARIOS[-1]
    whole_stack = LifecycleScenario(
        "direct-squash-four-of-four",
        "direct_merge",
        4,
        4,
        "squash",
    )
    excluded = {
        (fixed_direct.stack_size, fixed_direct.merged_prefix, fixed_direct.merge_method),
        (whole_stack.stack_size, whole_stack.merged_prefix, whole_stack.merge_method),
    }
    generated = [
        LifecycleScenario(
            f"direct-{method}-{prefix}-of-{size}",
            "direct_merge",
            size,
            prefix,
            method,
        )
        for size in range(1, 6)
        for prefix in range(1, size + 1)
        for method in ("rebase", "squash")
        if (size, prefix, method) not in excluded
    ]
    random.Random(seed + 6).shuffle(generated)
    return (*LIFECYCLE_SCENARIOS, whole_stack, *generated)[:count]


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
    def invariants(self) -> SubmitInvariants:
        return SubmitInvariants(
            final_live_labels=self.final_live_labels,
            initial_size=self.initial_size,
            orphaned_labels=self.orphaned_labels,
            trace=self.trace,
        )

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
class DriftKindSpec:
    """Transition metadata for one external-drift kind.

    `boundary` names the state-holder the drift mutates: `github_prs` (the PR
    database), `remote_refs` (the remote Git branch namespace), or `local_jj`
    (the local DAG and bookmark view).
    `expected_outcome` is the model's verdict for a submit issued after the
    drift. Fail-closed kinds carry the contractual `(exit code, diagnosis)`
    pairs the CLI may report: a `DriftError` condition, an
    `unsupported_stack:<reason>`. Asserting the diagnosis
    keeps a stop that fired for the wrong reason — right exit code, misleading
    repair path — from satisfying the model.
    """

    boundary: Literal["github_prs", "local_jj", "remote_refs"]
    expected_outcome: DriftOutcome
    failures: tuple[tuple[int, str], ...]
    needs_label: bool


DRIFT_KIND_SPECS: dict[DriftKind, DriftKindSpec] = {
    "closed_pr": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="fail_closed",
        failures=((1, "pull_request_not_open"),),
        needs_label=True,
    ),
    # The fetched foreign ref pins the submitted commit: immutable when the
    # change is unrewritten, divergent when a local rewrite already replaced it
    # and the fetch resurrects the hidden predecessor.
    "foreign_branch_fetched": DriftKindSpec(
        boundary="local_jj",
        expected_outcome="fail_closed",
        failures=(
            (2, "unsupported_stack:divergent_change"),
            (2, "unsupported_stack:immutable_commit"),
        ),
        needs_label=True,
    ),
    "pr_base_retargeted": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="success",
        failures=(),
        needs_label=True,
    ),
    "pr_draft_toggled": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="success",
        failures=(),
        needs_label=True,
    ),
    "pr_replaced": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="fail_closed",
        failures=((1, "pull_request_ambiguous"),),
        needs_label=True,
    ),
    "remote_branch_deleted": DriftKindSpec(
        boundary="remote_refs",
        expected_outcome="fail_closed",
        failures=((1, "remote_branch_missing"),),
        needs_label=True,
    ),
    "remote_branch_drift": DriftKindSpec(
        boundary="remote_refs",
        expected_outcome="fail_closed",
        failures=((1, "remote_branch_moved"),),
        needs_label=True,
    ),
    "trunk_advanced": DriftKindSpec(
        boundary="remote_refs",
        expected_outcome="success",
        failures=(),
        needs_label=False,
    ),
}


@dataclass(frozen=True, slots=True)
class DriftOperation:
    """One external-actor transition applied to one boundary after submit."""

    kind: DriftKind
    label: str | None = None

    @property
    def spec(self) -> DriftKindSpec:
        return DRIFT_KIND_SPECS[self.kind]

    @property
    def trace(self) -> str:
        parts: list[str] = [self.kind]
        if self.label is not None:
            parts.append(self.label)
        return ":".join(parts)


@dataclass(frozen=True, slots=True)
class ExternalDriftScenario:
    """A submitted stack, an optional local edit, and one boundary drift.

    The scenario model predicts the submit outcome: a fail-closed drift must
    leave every boundary untouched, success drifts must converge on the normal
    post-submit contract. Every scenario also asserts that `view` still
    produces a report for the drifted state instead of crashing.
    """

    name: str
    hazard_class: str
    initial_size: int
    edit_operations: tuple[StackEditOperation, ...]
    drift: DriftOperation
    final_live_labels: tuple[str, ...]
    orphaned_labels: tuple[str, ...]
    rewritten_initial_labels: tuple[str, ...]

    @property
    def trace(self) -> str:
        parts = [operation.trace for operation in self.edit_operations]
        parts.append(self.drift.trace)
        return ",".join(parts)

    @property
    def invariants(self) -> SubmitInvariants:
        return SubmitInvariants(
            final_live_labels=self.final_live_labels,
            initial_size=self.initial_size,
            orphaned_labels=self.orphaned_labels,
            trace=self.trace,
        )

    @property
    def canonical_key(
        self,
    ) -> tuple[
        str,
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            self.hazard_class,
            self.drift.trace,
            self.final_live_labels,
            self.orphaned_labels,
            self.rewritten_initial_labels,
        )

    def __str__(self) -> str:
        return f"{self.name}: {self.trace}"


@dataclass(frozen=True, slots=True)
class SubmitRetryScenario:
    """A submit that fails after partial mutation and should converge on retry."""

    name: str
    failure_point: SubmitRetryFailurePoint
    initial_size: int
    failure_label: str

    @property
    def trace(self) -> str:
        return f"{self.failure_point}:{self.failure_label}"

    @property
    def final_live_labels(self) -> tuple[str, ...]:
        return tuple(initial_label(index) for index in range(1, self.initial_size + 1))

    @property
    def needs_initial_submit(self) -> bool:
        """Whether the fault fires on a resubmit instead of the first submit."""

        return self.failure_point == "update_pull_request"

    @property
    def invariants(self) -> SubmitInvariants:
        return SubmitInvariants(
            final_live_labels=self.final_live_labels,
            initial_size=self.initial_size,
            orphaned_labels=(),
            trace=self.trace,
        )

    @property
    def canonical_key(self) -> tuple[str, int, str]:
        return (self.failure_point, self.initial_size, self.failure_label)


@dataclass(frozen=True, slots=True)
class StackJoinScenario:
    """A rewrite that joins two independently submitted stacks into one stack."""

    name: str
    first_stack_labels: tuple[str, ...]
    second_stack_labels: tuple[str, ...]
    selected_labels: tuple[str, ...]
    source_label: str
    target_label: str

    @property
    def initial_size(self) -> int:
        return len(self.first_stack_labels) + len(self.second_stack_labels)

    @property
    def trace(self) -> str:
        return f"join_stack_onto:{self.source_label}:{self.target_label}"

    @property
    def invariants(self) -> SubmitInvariants:
        return SubmitInvariants(
            final_live_labels=self.selected_labels,
            initial_size=self.initial_size,
            orphaned_labels=(),
            trace=self.trace,
        )

    @property
    def canonical_key(
        self,
    ) -> tuple[str, ...]:
        return self.selected_labels


@dataclass(frozen=True, slots=True)
class StackMoveScenario:
    """A rewrite that moves one change between independently submitted stacks."""

    name: str
    first_stack_labels: tuple[str, ...]
    second_stack_labels: tuple[str, ...]
    source_label: str
    target_label: str
    placement: Literal["after", "before"]
    selected_labels: tuple[str, ...]
    deferred_labels: tuple[str, ...]

    @property
    def initial_size(self) -> int:
        return len(self.first_stack_labels) + len(self.second_stack_labels)

    @property
    def trace(self) -> str:
        return f"move_change_{self.placement}:{self.source_label}:{self.target_label}"

    @property
    def invariants(self) -> SubmitInvariants:
        return SubmitInvariants(
            final_live_labels=self.selected_labels,
            initial_size=self.initial_size,
            orphaned_labels=(),
            trace=self.trace,
        )

    @property
    def canonical_key(
        self,
    ) -> tuple[
        str,
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            self.placement,
            self.selected_labels,
            self.deferred_labels,
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
        orphaned_labels = set(self.orphaned_labels)
        rewritten_initial_labels = set(self.rewritten_initial_labels)
        next_insert_index = self.next_insert_index

        if operation.kind == "abandon" and not operation.label.startswith("c"):
            raise AssertionError("abandon requires an initially submitted label.")
        effect = apply_stack_edit(self.live_labels, operation)
        _mark_rewritten_initials(
            rewritten_initial_labels,
            effect.rewritten_labels,
            initial_size=self.initial_size,
        )
        if effect.removed_label is not None and effect.removed_label.startswith("c"):
            orphaned_labels.add(effect.removed_label)
        if operation.kind in {"insert_after", "insert_before"}:
            next_insert_index += 1

        return _ScenarioModel(
            initial_size=self.initial_size,
            live_labels=effect.live_labels,
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
            "JJ_STACK_SUBMIT_PROPERTY_SCENARIOS",
            str(DEFAULT_STACK_EDIT_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_stack_edit_scenarios(count=count, seed=seed)


def stack_join_scenarios_from_environment() -> tuple[StackJoinScenario, ...]:
    """Return deterministic stack-join scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_STACK_JOIN_SCENARIOS",
            str(DEFAULT_STACK_JOIN_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_stack_join_scenarios(count=count, seed=seed)


def stack_move_scenarios_from_environment() -> tuple[StackMoveScenario, ...]:
    """Return deterministic cross-stack move scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_STACK_MOVE_SCENARIOS",
            str(DEFAULT_STACK_MOVE_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_stack_move_scenarios(count=count, seed=seed)


def submit_retry_scenarios_from_environment() -> tuple[SubmitRetryScenario, ...]:
    """Return deterministic failed-submit retry scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_RETRY_SCENARIOS",
            str(DEFAULT_SUBMIT_RETRY_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_submit_retry_scenarios(count=count, seed=seed)


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


def generate_stack_join_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[StackJoinScenario, ...]:
    """Generate two-stack join scenarios that should preserve every PR identity."""

    if count < 1:
        return ()

    scenarios: list[StackJoinScenario] = []
    seen: set[tuple[str, ...]] = set()
    for scenario in _fixed_stack_join_scenarios():
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
        scenario = _stack_join_scenario(
            first_size=first_size,
            first_then_second=first_then_second,
            name=f"join-random-{attempts:03d}",
            second_size=second_size,
        )
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


def generate_submit_retry_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[SubmitRetryScenario, ...]:
    """Generate retry scenarios for one-shot submit failures."""

    if count < 1:
        return ()

    scenarios: list[SubmitRetryScenario] = []
    seen: set[tuple[str, int, str]] = set()
    for scenario in _fixed_submit_retry_scenarios():
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)
        if len(scenarios) >= count:
            return tuple(scenarios)

    rng = random.Random(seed + 4)
    max_attempts = count * MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER
    attempts = 0
    failure_points: tuple[SubmitRetryFailurePoint, ...] = (
        "after_remote_push",
        "create_pull_request",
        "update_pull_request",
        "pull_request_metadata",
    )
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        initial_size = rng.randint(2, 5)
        scenario = SubmitRetryScenario(
            failure_label=initial_label(rng.randint(1, initial_size)),
            failure_point=rng.choice(failure_points),
            initial_size=initial_size,
            name=f"retry-random-{attempts:03d}",
        )
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


def generate_stack_move_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[StackMoveScenario, ...]:
    """Generate scenarios that move one change between submitted stacks."""

    if count < 1:
        return ()

    scenarios: list[StackMoveScenario] = []
    seen: set[
        tuple[
            str,
            tuple[str, ...],
            tuple[str, ...],
        ]
    ] = set()
    for scenario in _fixed_stack_move_scenarios():
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)
        if len(scenarios) >= count:
            return tuple(scenarios)

    rng = random.Random(seed + 3)
    max_attempts = count * MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER
    attempts = 0
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        first_size = rng.randint(1, 5)
        second_size = rng.randint(1, 5)
        source_from_first = rng.choice((True, False))
        source_size = first_size if source_from_first else second_size
        target_size = second_size if source_from_first else first_size
        scenario = _stack_move_scenario(
            first_size=first_size,
            name=f"move-random-{attempts:03d}",
            placement=rng.choice(("after", "before")),
            second_size=second_size,
            source_from_first=source_from_first,
            source_index=rng.randrange(source_size),
            target_index=rng.randrange(target_size),
        )
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


def external_drift_scenarios_from_environment() -> tuple[ExternalDriftScenario, ...]:
    """Return deterministic external-drift scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_DRIFT_SCENARIOS",
            str(DEFAULT_EXTERNAL_DRIFT_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_external_drift_scenarios(count=count, seed=seed)


def generate_external_drift_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[ExternalDriftScenario, ...]:
    """Generate scenarios that perturb one boundary after submit."""

    if count < 1:
        return ()

    scenarios: list[ExternalDriftScenario] = []
    seen: set[
        tuple[
            str,
            str,
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
        ]
    ] = set()
    for scenario in _fixed_external_drift_scenarios():
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)
        if len(scenarios) >= count:
            return tuple(scenarios)

    rng = random.Random(seed + 5)
    max_attempts = count * MAX_STACK_EDIT_ATTEMPTS_MULTIPLIER
    attempts = 0
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        scenario = _random_external_drift_scenario(rng, attempts=attempts)
        if scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


_GENERATED_DRIFT_KINDS: tuple[DriftKind, ...] = tuple(sorted(DRIFT_KIND_SPECS))


def _fixed_external_drift_scenarios() -> tuple[ExternalDriftScenario, ...]:
    return (_closed_pr_after_insert_scenario(),)


def _closed_pr_after_insert_scenario() -> ExternalDriftScenario:
    return _drift_scenario(
        drift=DriftOperation(kind="closed_pr", label="c2"),
        edit_operations=(StackEditOperation(kind="insert_after", label="c1", new_label="i1"),),
        hazard_class="github-external-close-with-unsubmitted-change",
        name="closed-pr-after-insert",
    )


def _drift_scenario(
    *,
    drift: DriftOperation,
    hazard_class: str,
    name: str,
    edit_operations: tuple[StackEditOperation, ...] = (),
    initial_size: int = 3,
) -> ExternalDriftScenario:
    model = _model(initial_size)
    for operation in edit_operations:
        model = model.append(operation)
    return ExternalDriftScenario(
        drift=drift,
        edit_operations=edit_operations,
        final_live_labels=model.live_labels,
        hazard_class=hazard_class,
        initial_size=initial_size,
        name=name,
        orphaned_labels=model.orphaned_labels,
        rewritten_initial_labels=model.rewritten_initial_labels,
    )


def _random_external_drift_scenario(
    rng: random.Random,
    *,
    attempts: int,
) -> ExternalDriftScenario:
    initial_size = rng.randint(2, 5)
    model = _model(initial_size)
    edit_operations: tuple[StackEditOperation, ...] = ()
    if rng.random() < 0.5:
        operations = _available_operations(model, rng)
        if operations:
            operation = rng.choice(operations)
            model = model.append(operation)
            edit_operations = (operation,)

    return ExternalDriftScenario(
        drift=_random_drift_operation(rng, model=model),
        edit_operations=edit_operations,
        final_live_labels=model.live_labels,
        hazard_class="random",
        initial_size=initial_size,
        name=f"drift-random-{attempts:03d}",
        orphaned_labels=model.orphaned_labels,
        rewritten_initial_labels=model.rewritten_initial_labels,
    )


def _random_drift_operation(
    rng: random.Random,
    *,
    model: _ScenarioModel,
) -> DriftOperation:
    live_initial_labels = [label for label in model.live_labels if label.startswith("c")]
    candidates = [
        DriftOperation(kind=kind, label=label)
        for kind in _GENERATED_DRIFT_KINDS
        if DRIFT_KIND_SPECS[kind].needs_label
        for label in live_initial_labels
        if _drift_label_is_valid(kind, label=label, model=model)
    ]
    candidates.extend(
        DriftOperation(kind=kind)
        for kind in _GENERATED_DRIFT_KINDS
        if not DRIFT_KIND_SPECS[kind].needs_label
    )
    return rng.choice(candidates)


def _drift_label_is_valid(kind: DriftKind, *, label: str, model: _ScenarioModel) -> bool:
    if kind == "pr_base_retargeted":
        # The drift retargets the PR base to trunk, so the PR must have had a
        # stacked base originally and must still be expected to have one.
        return label != "c1" and model.live_labels.index(label) > 0
    return True


def _fixed_submit_retry_scenarios() -> tuple[SubmitRetryScenario, ...]:
    return (
        SubmitRetryScenario(
            failure_label="c1",
            failure_point="after_remote_push",
            initial_size=3,
            name="retry-after-remote-push",
        ),
    )


def _fixed_stack_move_scenarios() -> tuple[StackMoveScenario, ...]:
    return (
        _stack_move_scenario(
            first_size=3,
            name="move-first-middle-after-second-head",
            placement="after",
            second_size=2,
            source_from_first=True,
            source_index=1,
            target_index=1,
        ),
    )


def _stack_move_scenario(
    *,
    first_size: int,
    name: str,
    placement: Literal["after", "before"],
    second_size: int,
    source_from_first: bool,
    source_index: int,
    target_index: int,
) -> StackMoveScenario:
    first_labels = tuple(_stack_label("a", index) for index in range(1, first_size + 1))
    second_labels = tuple(_stack_label("b", index) for index in range(1, second_size + 1))
    source_labels = first_labels if source_from_first else second_labels
    target_labels = second_labels if source_from_first else first_labels
    source_label = source_labels[source_index]
    target_label = target_labels[target_index]
    selected_labels = _insert_moved_label(
        moved_label=source_label,
        placement=placement,
        target_label=target_label,
        target_labels=target_labels,
    )
    deferred_labels = tuple(label for label in source_labels if label != source_label)
    return StackMoveScenario(
        deferred_labels=deferred_labels,
        first_stack_labels=first_labels,
        name=name,
        placement=placement,
        second_stack_labels=second_labels,
        selected_labels=selected_labels,
        source_label=source_label,
        target_label=target_label,
    )


def _insert_moved_label(
    *,
    moved_label: str,
    placement: Literal["after", "before"],
    target_label: str,
    target_labels: tuple[str, ...],
) -> tuple[str, ...]:
    target_position = target_labels.index(target_label)
    insert_position = target_position + 1 if placement == "after" else target_position
    return (
        *target_labels[:insert_position],
        moved_label,
        *target_labels[insert_position:],
    )


def _fixed_stack_join_scenarios() -> tuple[StackJoinScenario, ...]:
    return (
        _stack_join_scenario(
            first_size=2,
            first_then_second=True,
            name="join-second-after-first",
            second_size=2,
        ),
    )


def _stack_join_scenario(
    *,
    first_size: int,
    first_then_second: bool,
    name: str,
    second_size: int,
) -> StackJoinScenario:
    first_labels = tuple(_stack_label("a", index) for index in range(1, first_size + 1))
    second_labels = tuple(_stack_label("b", index) for index in range(1, second_size + 1))
    if first_then_second:
        selected_labels = (*first_labels, *second_labels)
        source_label = second_labels[0]
        target_label = first_labels[-1]
    else:
        selected_labels = (*second_labels, *first_labels)
        source_label = first_labels[0]
        target_label = second_labels[-1]
    return StackJoinScenario(
        first_stack_labels=first_labels,
        name=name,
        second_stack_labels=second_labels,
        selected_labels=selected_labels,
        source_label=source_label,
        target_label=target_label,
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

        after_candidates = move_after_candidates(model.live_labels)
        if after_candidates:
            label, target_label = rng.choice(after_candidates)
            operations.append(
                StackEditOperation(
                    kind="move_after",
                    label=label,
                    target_label=target_label,
                )
            )

        before_candidates = move_before_candidates(model.live_labels)
        if before_candidates:
            label, target_label = rng.choice(before_candidates)
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
        operations.append(StackEditOperation(kind="squash_into_previous", label=squash_label))

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
    labels: frozenset[str],
    *,
    initial_size: int,
) -> None:
    for label in labels:
        if label.startswith("c") and int(label[1:]) <= initial_size:
            target.add(label)


def _label_sort_key(label: str) -> tuple[str, int]:
    return (label[0], int(label[1:]))


# Unconfigured pytest runs exercise only the fixed observable-risk representatives.
# Larger counts continue into deterministic randomized generation through the opt-in runner.
DEFAULT_STACK_EDIT_SCENARIO_COUNT = len(_fixed_stack_edit_scenarios())
DEFAULT_STACK_JOIN_SCENARIO_COUNT = len(_fixed_stack_join_scenarios())
DEFAULT_STACK_MOVE_SCENARIO_COUNT = len(_fixed_stack_move_scenarios())
DEFAULT_SUBMIT_RETRY_SCENARIO_COUNT = len(_fixed_submit_retry_scenarios())
DEFAULT_EXTERNAL_DRIFT_SCENARIO_COUNT = len(_fixed_external_drift_scenarios())
