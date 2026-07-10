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
DriftKind = Literal[
    "agent_recreated_change",
    "closed_pr",
    "conflicted_rebase",
    "foreign_branch_fetched",
    "merge_commit",
    "merged_pr",
    "pr_base_retargeted",
    "pr_draft_toggled",
    "pr_replaced",
    "remote_branch_deleted",
    "remote_branch_drift",
    "trunk_advanced",
    "unlinked_change",
    "wrong_saved_pr_number",
]
DriftOutcome = Literal["fail_closed", "success"]
SubmitRetryFailurePoint = Literal[
    "after_remote_push",
    "create_pull_request",
    "update_pull_request",
    "pull_request_metadata",
]

DEFAULT_STACK_EDIT_SCENARIO_COUNT = 8
DEFAULT_CROSS_STACK_SCENARIO_COUNT = 8
DEFAULT_STACK_MERGE_SCENARIO_COUNT = 8
DEFAULT_STACK_MOVE_SCENARIO_COUNT = 8
DEFAULT_SUBMIT_RETRY_SCENARIO_COUNT = 8
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
    database), `remote_refs` (the remote Git branch namespace), `tracking_store`
    (jj-stack's saved beliefs), or `local_jj` (the local DAG and bookmark view).
    `expected_outcome` is the model's verdict for a submit issued after the
    drift. Fail-closed kinds carry the contractual exit codes the CLI may use
    and the diagnoses the CLI may report: a `DriftError` condition, an
    `unsupported_stack:<reason>`, or `conflicted_stack`. Asserting the diagnosis
    keeps a stop that fired for the wrong reason — right exit code, misleading
    repair path — from satisfying the model. Non-composable kinds change the
    stack shape or selection and only appear in hand-written fixed scenarios.
    """

    boundary: Literal["github_prs", "local_jj", "remote_refs", "tracking_store"]
    expected_outcome: DriftOutcome
    exit_codes: tuple[int, ...]
    diagnoses: tuple[str, ...]
    composable: bool
    needs_label: bool


DRIFT_KIND_SPECS: dict[DriftKind, DriftKindSpec] = {
    "agent_recreated_change": DriftKindSpec(
        boundary="local_jj",
        expected_outcome="fail_closed",
        exit_codes=(2,),
        diagnoses=("unsupported_stack:immutable_commit",),
        composable=False,
        needs_label=True,
    ),
    "closed_pr": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("pull_request_not_open",),
        composable=True,
        needs_label=True,
    ),
    "conflicted_rebase": DriftKindSpec(
        boundary="local_jj",
        expected_outcome="fail_closed",
        exit_codes=(3,),
        diagnoses=("conflicted_stack",),
        composable=False,
        needs_label=True,
    ),
    # The fetched foreign ref pins the submitted commit: immutable when the
    # change is unrewritten, divergent when a local rewrite already replaced it
    # and the fetch resurrects the hidden predecessor.
    "foreign_branch_fetched": DriftKindSpec(
        boundary="local_jj",
        expected_outcome="fail_closed",
        exit_codes=(2,),
        diagnoses=(
            "unsupported_stack:divergent_change",
            "unsupported_stack:immutable_commit",
        ),
        composable=True,
        needs_label=True,
    ),
    "merge_commit": DriftKindSpec(
        boundary="local_jj",
        expected_outcome="fail_closed",
        exit_codes=(2,),
        diagnoses=("unsupported_stack:merge_commit",),
        composable=False,
        needs_label=True,
    ),
    "merged_pr": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("pull_request_not_open",),
        composable=True,
        needs_label=True,
    ),
    "pr_base_retargeted": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="success",
        exit_codes=(),
        diagnoses=(),
        composable=True,
        needs_label=True,
    ),
    "pr_draft_toggled": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="success",
        exit_codes=(),
        diagnoses=(),
        composable=True,
        needs_label=True,
    ),
    "pr_replaced": DriftKindSpec(
        boundary="github_prs",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("pull_request_ambiguous",),
        composable=True,
        needs_label=True,
    ),
    "remote_branch_deleted": DriftKindSpec(
        boundary="remote_refs",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("remote_branch_missing",),
        composable=True,
        needs_label=True,
    ),
    "remote_branch_drift": DriftKindSpec(
        boundary="remote_refs",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("remote_branch_moved",),
        composable=True,
        needs_label=True,
    ),
    "trunk_advanced": DriftKindSpec(
        boundary="remote_refs",
        expected_outcome="success",
        exit_codes=(),
        diagnoses=(),
        composable=True,
        needs_label=False,
    ),
    "unlinked_change": DriftKindSpec(
        boundary="tracking_store",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("change_unlinked",),
        composable=True,
        needs_label=True,
    ),
    "wrong_saved_pr_number": DriftKindSpec(
        boundary="tracking_store",
        expected_outcome="fail_closed",
        exit_codes=(1,),
        diagnoses=("saved_pull_request_mismatch",),
        composable=True,
        needs_label=True,
    ),
}


@dataclass(frozen=True, slots=True)
class DriftOperation:
    """One external-actor transition applied to one boundary after submit."""

    kind: DriftKind
    label: str | None = None
    new_label: str | None = None

    @property
    def spec(self) -> DriftKindSpec:
        return DRIFT_KIND_SPECS[self.kind]

    @property
    def trace(self) -> str:
        parts: list[str] = [self.kind]
        if self.label is not None:
            parts.append(self.label)
        if self.new_label is not None:
            parts.append(self.new_label)
        return ":".join(parts)


@dataclass(frozen=True, slots=True)
class ExternalDriftScenario:
    """A submitted stack, an optional local edit, and one or more boundary drifts.

    The scenario model predicts the submit outcome: fail-closed drifts must
    leave every boundary untouched, success drifts must converge on the normal
    post-submit contract. Every scenario also asserts that `view` still
    produces a report for the drifted state instead of crashing.
    """

    name: str
    hazard_class: str
    initial_size: int
    edit_operations: tuple[StackEditOperation, ...]
    drifts: tuple[DriftOperation, ...]
    final_live_labels: tuple[str, ...]
    orphaned_labels: tuple[str, ...]
    rewritten_initial_labels: tuple[str, ...]

    @property
    def expected_outcome(self) -> DriftOutcome:
        if any(drift.spec.expected_outcome == "fail_closed" for drift in self.drifts):
            return "fail_closed"
        return "success"

    @property
    def expected_exit_codes(self) -> tuple[int, ...]:
        codes: set[int] = set()
        for drift in self.drifts:
            codes.update(drift.spec.exit_codes)
        return tuple(sorted(codes))

    @property
    def expected_diagnoses(self) -> tuple[str, ...]:
        """Diagnoses the CLI may report, unioned because check order picks the winner."""

        diagnoses: set[str] = set()
        for drift in self.drifts:
            diagnoses.update(drift.spec.diagnoses)
        return tuple(sorted(diagnoses))

    @property
    def trace(self) -> str:
        parts = [operation.trace for operation in self.edit_operations]
        parts.extend(drift.trace for drift in self.drifts)
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
        tuple[str, ...],
    ]:
        return (
            self.hazard_class,
            self.expected_outcome,
            tuple(sorted(drift.trace for drift in self.drifts)),
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
            self.hazard_class,
            self.selected_labels,
            self.rewritten_initial_labels,
        )


@dataclass(frozen=True, slots=True)
class StackMoveScenario:
    """A rewrite that moves one change between independently submitted stacks."""

    name: str
    hazard_class: str
    first_stack_labels: tuple[str, ...]
    second_stack_labels: tuple[str, ...]
    source_label: str
    target_label: str
    placement: Literal["after", "before"]
    selected_labels: tuple[str, ...]
    deferred_labels: tuple[str, ...]
    deferred_stack_labels: tuple[str, ...]
    rewritten_initial_labels: tuple[str, ...]

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
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        return (
            self.hazard_class,
            self.placement,
            self.selected_labels,
            self.deferred_labels,
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


def cross_stack_scenarios_from_environment() -> tuple[CrossStackSplitScenario, ...]:
    """Return deterministic cross-stack split scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_CROSS_STACK_SCENARIOS",
            str(DEFAULT_CROSS_STACK_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_cross_stack_split_scenarios(count=count, seed=seed)


def stack_merge_scenarios_from_environment() -> tuple[StackMergeScenario, ...]:
    """Return deterministic stack-merge scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_STACK_MERGE_SCENARIOS",
            str(DEFAULT_STACK_MERGE_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_SUBMIT_PROPERTY_SEED",
            str(DEFAULT_STACK_EDIT_SCENARIO_SEED),
        )
    )
    return generate_stack_merge_scenarios(count=count, seed=seed)


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
            str,
            tuple[str, ...],
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
            hazard_class="random",
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
    """Generate scenarios that perturb one or two boundaries after submit."""

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
        if scenario is None or scenario.canonical_key in seen:
            continue
        seen.add(scenario.canonical_key)
        scenarios.append(scenario)

    return tuple(scenarios)


_COMPOSABLE_DRIFT_KINDS: tuple[DriftKind, ...] = tuple(
    sorted(kind for kind, spec in DRIFT_KIND_SPECS.items() if spec.composable)
)


def _fixed_external_drift_scenarios() -> tuple[ExternalDriftScenario, ...]:
    return (
        _drift_scenario(
            drifts=(DriftOperation(kind="closed_pr", label="c2"),),
            hazard_class="github-external-close",
            name="closed-pr",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="merged_pr", label="c1"),),
            hazard_class="github-external-merge",
            name="merged-pr",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="pr_replaced", label="c2"),),
            hazard_class="github-replaced-pr",
            name="pr-replaced",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="wrong_saved_pr_number", label="c2"),),
            hazard_class="store-wrong-pr-number",
            name="wrong-saved-pr-number",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="wrong_saved_pr_number", label="c2"),),
            edit_operations=(StackEditOperation(kind="rewrite", label="c2"),),
            hazard_class="store-wrong-pr-number",
            name="wrong-saved-pr-number-after-rewrite",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="unlinked_change", label="c2"),),
            hazard_class="store-unlinked",
            name="unlinked-change",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="remote_branch_drift", label="c2"),),
            hazard_class="remote-branch-drift",
            name="remote-branch-drift",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="remote_branch_deleted", label="c3"),),
            hazard_class="remote-branch-deleted",
            name="remote-branch-deleted",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="foreign_branch_fetched", label="c2"),),
            hazard_class="local-foreign-fetch",
            name="foreign-branch-fetched",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="foreign_branch_fetched", label="c2"),),
            edit_operations=(StackEditOperation(kind="rewrite", label="c2"),),
            hazard_class="local-foreign-fetch-divergent",
            name="foreign-branch-fetched-after-rewrite",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="conflicted_rebase", label="c3"),),
            hazard_class="local-conflicted-rebase",
            name="conflicted-rebase",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="merge_commit", label="c3"),),
            hazard_class="local-merge-commit",
            name="merge-commit",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="closed_pr", label="c2"),),
            edit_operations=(StackEditOperation(kind="move_to_top", label="c1"),),
            hazard_class="github-external-close",
            initial_size=4,
            name="closed-pr-after-reorder",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="closed_pr", label="c2"),),
            edit_operations=(
                StackEditOperation(kind="insert_after", label="c1", new_label="i1"),
            ),
            hazard_class="github-external-close-with-unsubmitted-change",
            name="closed-pr-after-insert",
        ),
        _drift_scenario(
            drifts=(
                DriftOperation(kind="closed_pr", label="c1"),
                DriftOperation(kind="remote_branch_deleted", label="c3"),
            ),
            hazard_class="multi-boundary",
            name="closed-pr-and-deleted-branch",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="trunk_advanced"),),
            hazard_class="remote-trunk-advance",
            name="trunk-advanced",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="trunk_advanced"),),
            edit_operations=(StackEditOperation(kind="move_to_top", label="c1"),),
            hazard_class="remote-trunk-advance",
            name="trunk-advanced-after-reorder",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="pr_base_retargeted", label="c2"),),
            hazard_class="github-base-retarget",
            name="pr-base-retargeted",
        ),
        _drift_scenario(
            drifts=(DriftOperation(kind="pr_draft_toggled", label="c3"),),
            hazard_class="github-draft-toggle",
            name="pr-draft-toggled",
        ),
        _agent_recreated_change_scenario(),
    )


def _agent_recreated_change_scenario() -> ExternalDriftScenario:
    """The observed incident: a PR and its jj change replaced outside jj-stack.

    An agent closed a reviewed PR, deleted its review branch, abandoned the
    local change, recreated the same work as a new change, pushed it with plain
    git, opened a replacement PR with `gh`, and fetched. The fetch imports the
    replacement branch as an untracked remote bookmark, which makes the
    recreated change immutable, so the stack is no longer reviewable. `submit`
    must refuse without touching any boundary, and `view` must still report.
    """

    return _drift_scenario(
        drifts=(
            DriftOperation(
                kind="agent_recreated_change",
                label="c2",
                new_label="i1",
            ),
        ),
        edit_operations=(
            StackEditOperation(kind="abandon", label="c2"),
            StackEditOperation(kind="insert_after", label="c1", new_label="i1"),
        ),
        hazard_class="incident-recreated-pr",
        name="agent-recreated-pr",
    )


def _drift_scenario(
    *,
    drifts: tuple[DriftOperation, ...],
    hazard_class: str,
    name: str,
    edit_operations: tuple[StackEditOperation, ...] = (),
    initial_size: int = 3,
) -> ExternalDriftScenario:
    model = _model(initial_size)
    for operation in edit_operations:
        model = model.append(operation)
    return ExternalDriftScenario(
        drifts=drifts,
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
) -> ExternalDriftScenario | None:
    initial_size = rng.randint(2, 5)
    model = _model(initial_size)
    edit_operations: tuple[StackEditOperation, ...] = ()
    if rng.random() < 0.5:
        operations = _available_operations(model, rng)
        if operations:
            operation = rng.choice(operations)
            model = model.append(operation)
            edit_operations = (operation,)

    drifts = _random_drift_operations(rng, model=model)
    if not drifts:
        return None
    return ExternalDriftScenario(
        drifts=drifts,
        edit_operations=edit_operations,
        final_live_labels=model.live_labels,
        hazard_class="random",
        initial_size=initial_size,
        name=f"drift-random-{attempts:03d}",
        orphaned_labels=model.orphaned_labels,
        rewritten_initial_labels=model.rewritten_initial_labels,
    )


def _random_drift_operations(
    rng: random.Random,
    *,
    model: _ScenarioModel,
) -> tuple[DriftOperation, ...]:
    live_initial_labels = [
        label for label in model.live_labels if label.startswith("c")
    ]
    drift_count = rng.choice((1, 1, 2))
    kinds = rng.sample(
        _COMPOSABLE_DRIFT_KINDS,
        k=min(drift_count, len(_COMPOSABLE_DRIFT_KINDS)),
    )
    drifts: list[DriftOperation] = []
    available_labels = list(live_initial_labels)
    for kind in kinds:
        if not DRIFT_KIND_SPECS[kind].needs_label:
            drifts.append(DriftOperation(kind=kind))
            continue
        candidates = [
            label
            for label in available_labels
            if _drift_label_is_valid(kind, label=label, model=model)
        ]
        if not candidates:
            continue
        label = rng.choice(candidates)
        available_labels.remove(label)
        drifts.append(DriftOperation(kind=kind, label=label))
    return tuple(drifts)


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
        SubmitRetryScenario(
            failure_label="c2",
            failure_point="create_pull_request",
            initial_size=3,
            name="retry-create-middle-pr",
        ),
        SubmitRetryScenario(
            failure_label="c2",
            failure_point="update_pull_request",
            initial_size=3,
            name="retry-update-middle-pr",
        ),
        SubmitRetryScenario(
            failure_label="c1",
            failure_point="pull_request_metadata",
            initial_size=2,
            name="retry-metadata-sync",
        ),
    )


def _fixed_stack_move_scenarios() -> tuple[StackMoveScenario, ...]:
    return (
        _stack_move_scenario(
            first_size=3,
            hazard_class="move-middle-into-head",
            name="move-first-middle-after-second-head",
            placement="after",
            second_size=2,
            source_from_first=True,
            source_index=1,
            target_index=1,
        ),
        _stack_move_scenario(
            first_size=3,
            hazard_class="move-head-into-middle",
            name="move-first-head-before-second-head",
            placement="before",
            second_size=3,
            source_from_first=True,
            source_index=2,
            target_index=2,
        ),
        _stack_move_scenario(
            first_size=2,
            hazard_class="move-bottom-into-bottom",
            name="move-second-bottom-before-first-bottom",
            placement="before",
            second_size=3,
            source_from_first=False,
            source_index=0,
            target_index=0,
        ),
        _stack_move_scenario(
            first_size=3,
            hazard_class="move-single-source",
            name="move-single-second-after-first-middle",
            placement="after",
            second_size=1,
            source_from_first=False,
            source_index=0,
            target_index=1,
        ),
    )


def _stack_move_scenario(
    *,
    first_size: int,
    hazard_class: str,
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
    deferred_stack_labels = tuple(label for label in source_labels if label != source_label)
    rewritten_initial_labels = _stack_move_rewritten_labels(
        placement=placement,
        source_index=source_index,
        source_label=source_label,
        source_labels=source_labels,
        target_index=target_index,
        target_labels=target_labels,
    )
    return StackMoveScenario(
        deferred_labels=deferred_stack_labels,
        deferred_stack_labels=deferred_stack_labels,
        first_stack_labels=first_labels,
        hazard_class=hazard_class,
        name=name,
        placement=placement,
        rewritten_initial_labels=rewritten_initial_labels,
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


def _stack_move_rewritten_labels(
    *,
    placement: Literal["after", "before"],
    source_index: int,
    source_label: str,
    source_labels: tuple[str, ...],
    target_index: int,
    target_labels: tuple[str, ...],
) -> tuple[str, ...]:
    target_rewrite_start = target_index + 1 if placement == "after" else target_index
    rewritten = {
        source_label,
        *source_labels[source_index + 1 :],
        *target_labels[target_rewrite_start:],
    }
    return tuple(sorted(rewritten, key=_label_sort_key))


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


# The default must cover the whole fixed corpus so an unconfigured run never
# silently drops a hazard representative (such as the incident scenario).
DEFAULT_EXTERNAL_DRIFT_SCENARIO_COUNT = len(_fixed_external_drift_scenarios())
