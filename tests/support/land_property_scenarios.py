"""Scenario generation for land property tests.

Land scenarios compose the states `land` actually meets: a submitted, partially
approved stack that may have been edited since its last submit. Each scenario
starts from a submitted linear stack, optionally applies a short trace of local
stack edits (with or without a follow-up resubmit), approves a prefix of the
final live stack, then models the prefix `land` must consume and the boundary
where it must stop.

The walk model stays small because only two properties of an edited change can
stop the readiness walk: a change whose content no longer matches its remote
review branch (a rewrite target or a squash destination) is content-divergent,
and an inserted change without a resubmit has no pull request. Every other
rebased change is diff-equivalent, which land refreshes and lands.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Literal

from .stack_edit_scenarios import (
    StackEditOperation as LandEditOperation,
    StackEditOperationKind as LandEditKind,
    apply_stack_edit,
)

LandVia = Literal["push", "merge"]

DEFAULT_LAND_SCENARIO_SEED = 8675309
MAX_LAND_ATTEMPTS_MULTIPLIER = 40

INSERTED_LABEL = "insert-1"
BYSTANDER_LABELS = ("other-1", "other-2")


def initial_land_label(index: int) -> str:
    return f"feature-{index}"


def subject_for_land_label(label: str) -> str:
    return label.replace("-", " ", 1)


def filename_for_land_label(label: str) -> str:
    return f"{label}.txt"


def simulate_land_edits(
    *,
    edits: tuple[LandEditOperation, ...],
    initial_labels: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
    """Return (final live labels, orphaned labels, content-divergent labels).

    Raises ValueError for edit traces that are not reachable in order, so both
    scenario validation and random generation share one source of truth.
    """

    live = initial_labels
    orphaned: list[str] = []
    divergent: set[str] = set()
    for operation in edits:
        effect = apply_stack_edit(live, operation)
        live = effect.live_labels
        if effect.removed_label is not None:
            orphaned.append(effect.removed_label)
        divergent.update(effect.content_divergent_labels)
    return live, tuple(orphaned), frozenset(divergent)


@dataclass(frozen=True, slots=True)
class LandScenario:
    """A submitted stack, an edit trace, an approval prefix, and a transport."""

    name: str
    initial_size: int
    via: LandVia
    edits: tuple[LandEditOperation, ...]
    resubmit_after_edit: bool
    approved_prefix: int
    land_target_position: int | None = None
    with_second_stack: bool = False
    skip_cleanup: bool = False
    unmergeable_pull_number: int | None = None

    def __post_init__(self) -> None:
        if self.initial_size < 1:
            raise ValueError("land scenarios require at least one submitted change")
        if self.skip_cleanup and self.via != "push":
            raise ValueError("--skip-cleanup is only modeled for direct-push land")
        if not self.edits and self.resubmit_after_edit:
            raise ValueError("resubmit without an edit does not change the modeled state")
        final_live_labels = self.final_live_labels  # validates the edit trace
        if not 0 <= self.approved_prefix <= len(final_live_labels):
            raise ValueError("approved prefix must be within the final live stack")
        if self.land_target_position is not None:
            if not 1 <= self.land_target_position <= len(final_live_labels):
                raise ValueError("land target must be a final live stack position")
            target_label = final_live_labels[self.land_target_position - 1]
            if not self.label_has_pull_request(target_label):
                raise ValueError("land target must have a pull request to select")
        if self.unmergeable_pull_number is not None:
            if self.via != "merge":
                raise ValueError("only merge-transport scenarios can have an unmergeable PR")
            if self.edits or self.land_target_position is not None:
                raise ValueError("unmergeable scenarios keep the fixed no-edit shape")
            if not 1 <= self.unmergeable_pull_number <= self.initial_size:
                raise ValueError("unmergeable PR must be inside the submitted stack")

    @property
    def initial_labels(self) -> tuple[str, ...]:
        return tuple(initial_land_label(index) for index in range(1, self.initial_size + 1))

    def _simulate(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
        return simulate_land_edits(edits=self.edits, initial_labels=self.initial_labels)

    @property
    def final_live_labels(self) -> tuple[str, ...]:
        return self._simulate()[0]

    @property
    def orphaned_labels(self) -> tuple[str, ...]:
        return self._simulate()[1]

    @property
    def divergent_labels(self) -> frozenset[str]:
        return self._simulate()[2]

    def label_has_pull_request(self, label: str) -> bool:
        if label != INSERTED_LABEL:
            return True
        # The inserted change only gains a PR from a resubmit, and the resubmit
        # runs after the whole edit trace — an inserted change that was
        # abandoned or squashed away mid-trace is never submitted at all.
        return self.resubmit_after_edit and INSERTED_LABEL in self.final_live_labels

    def _walk_stops_at(self, index: int, label: str) -> bool:
        """Whether the land readiness walk stops before this final-stack change."""

        if index >= self.approved_prefix:
            return True
        if not self.label_has_pull_request(label):
            return True
        if not self.resubmit_after_edit and label in self.divergent_labels:
            # The remote review branch still holds the pre-edit commit, so the
            # local change differs from what reviewers approved.
            return True
        return False

    @property
    def expected_landed_labels(self) -> tuple[str, ...]:
        final_live_labels = self.final_live_labels
        cap = (
            len(final_live_labels)
            if self.land_target_position is None
            else self.land_target_position
        )
        landed: list[str] = []
        for index, label in enumerate(final_live_labels[:cap]):
            if self._walk_stops_at(index, label):
                break
            if (
                self.unmergeable_pull_number is not None
                and index + 1 == self.unmergeable_pull_number
            ):
                break
            landed.append(label)
        return tuple(landed)

    @property
    def expected_exit_code(self) -> int:
        if self.unmergeable_pull_number is not None:
            return 1
        return 0 if self.expected_landed_labels else 1

    @property
    def trace(self) -> str:
        parts = [f"via:{self.via}", f"size:{self.initial_size}"]
        for operation in self.edits:
            parts.append(f"edit:{operation.trace}")
        if self.edits:
            parts.append(f"resubmit:{str(self.resubmit_after_edit).lower()}")
        parts.append(f"approved:{self.approved_prefix}")
        if self.land_target_position is not None:
            parts.append(f"target:{self.land_target_position}")
        if self.with_second_stack:
            parts.append("second_stack")
        if self.skip_cleanup:
            parts.append("skip_cleanup")
        if self.unmergeable_pull_number is not None:
            parts.append(f"unmergeable:{self.unmergeable_pull_number}")
        return ",".join(parts)

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return (
            self.via,
            self.initial_size,
            tuple(operation.trace for operation in self.edits),
            self.resubmit_after_edit,
            self.approved_prefix,
            self.land_target_position,
            self.with_second_stack,
            self.skip_cleanup,
            self.unmergeable_pull_number,
        )

    def __str__(self) -> str:
        return f"{self.name}: {self.trace}"


def land_scenarios_from_environment() -> tuple[LandScenario, ...]:
    """Return deterministic land scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_LAND_PROPERTY_SCENARIOS",
            str(DEFAULT_LAND_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_LAND_PROPERTY_SEED",
            str(DEFAULT_LAND_SCENARIO_SEED),
        )
    )
    return generate_land_scenarios(count=count, seed=seed)


def generate_land_scenarios(*, count: int, seed: int) -> tuple[LandScenario, ...]:
    """Generate deterministic land scenarios, preserving fixed behavior coverage."""

    if count < 1:
        return ()

    scenarios: list[LandScenario] = []
    seen: set[tuple[object, ...]] = set()
    for scenario in _fixed_land_scenarios():
        if len(scenarios) >= count:
            return tuple(scenarios)
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    rng = random.Random(seed)
    attempts = 0
    max_attempts = max(count * MAX_LAND_ATTEMPTS_MULTIPLIER, 1)
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        scenario = _random_land_scenario(rng=rng, name=f"land-random-{attempts:03d}")
        if scenario.canonical_key in seen:
            continue
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    return tuple(scenarios)


def _fixed_land_scenarios() -> tuple[LandScenario, ...]:
    return (
        LandScenario(
            name="push-stops-at-first-unapproved-pr",
            initial_size=3,
            via="push",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-nothing-approved-blocks-without-mutation",
            initial_size=2,
            via="push",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=0,
        ),
        LandScenario(
            name="push-full-stack-retires-tracking",
            initial_size=2,
            via="push",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-skip-cleanup-keeps-local-bookmarks",
            initial_size=2,
            via="push",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=2,
            skip_cleanup=True,
        ),
        LandScenario(
            name="push-rewrite-without-resubmit-stops-at-stale-review",
            initial_size=3,
            via="push",
            edits=(LandEditOperation(kind="rewrite", label=initial_land_label(2)),),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-rewrite-with-resubmit-lands-full-stack",
            initial_size=2,
            via="push",
            edits=(LandEditOperation(kind="rewrite", label=initial_land_label(1)),),
            resubmit_after_edit=True,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-insert-without-resubmit-stops-at-unsubmitted-change",
            initial_size=2,
            via="push",
            edits=(
                LandEditOperation(
                    kind="insert_after",
                    label=initial_land_label(1),
                    new_label=INSERTED_LABEL,
                ),
            ),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-insert-before-with-resubmit-lands-full-stack",
            initial_size=2,
            via="push",
            edits=(
                LandEditOperation(
                    kind="insert_before",
                    label=initial_land_label(2),
                    new_label=INSERTED_LABEL,
                ),
            ),
            resubmit_after_edit=True,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-abandon-auto-resubmits-rebased-survivors",
            initial_size=3,
            via="push",
            edits=(LandEditOperation(kind="abandon", label=initial_land_label(2)),),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-reorder-without-resubmit-auto-resubmits-moved-prefix",
            initial_size=3,
            via="push",
            edits=(LandEditOperation(kind="move_to_top", label=initial_land_label(1)),),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-move-after-without-resubmit-auto-resubmits-moved-prefix",
            initial_size=3,
            via="push",
            edits=(
                LandEditOperation(
                    kind="move_after",
                    label=initial_land_label(1),
                    target_label=initial_land_label(2),
                ),
            ),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-squash-without-resubmit-stops-at-divergent-destination",
            initial_size=3,
            via="push",
            edits=(
                LandEditOperation(kind="squash_into_previous", label=initial_land_label(2)),
            ),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-squash-with-resubmit-lands-survivor-and-keeps-orphan",
            initial_size=2,
            via="push",
            edits=(
                LandEditOperation(kind="squash_into_previous", label=initial_land_label(2)),
            ),
            resubmit_after_edit=True,
            approved_prefix=1,
        ),
        LandScenario(
            name="push-two-edit-trace-stops-at-divergent-after-abandon",
            initial_size=4,
            via="push",
            edits=(
                LandEditOperation(kind="abandon", label=initial_land_label(2)),
                LandEditOperation(kind="rewrite", label=initial_land_label(3)),
            ),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-pull-request-lands-selected-sub-prefix",
            initial_size=3,
            via="push",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=3,
            land_target_position=2,
        ),
        LandScenario(
            name="push-bystander-stack-untouched-by-partial-land",
            initial_size=2,
            via="push",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=1,
            with_second_stack=True,
        ),
        LandScenario(
            name="merge-approval-prefix-keeps-merged-tracking",
            initial_size=3,
            via="merge",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="merge-blocked-at-unmergeable-pr-keeps-prefix-tracking",
            initial_size=2,
            via="merge",
            edits=(),
            resubmit_after_edit=False,
            approved_prefix=2,
            unmergeable_pull_number=2,
        ),
        LandScenario(
            name="merge-abandon-auto-resubmits-then-merges-survivors",
            initial_size=3,
            via="merge",
            edits=(LandEditOperation(kind="abandon", label=initial_land_label(2)),),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="merge-reorder-without-resubmit-merges-reordered-prefix",
            initial_size=3,
            via="merge",
            edits=(
                LandEditOperation(
                    kind="move_before",
                    label=initial_land_label(3),
                    target_label=initial_land_label(2),
                ),
            ),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
    )


DEFAULT_LAND_SCENARIO_COUNT = len(_fixed_land_scenarios())


def _random_land_scenario(*, rng: random.Random, name: str) -> LandScenario:
    via: LandVia = rng.choice(("push", "merge"))
    initial_size = rng.randint(2, 4)
    initial_labels = tuple(
        initial_land_label(index) for index in range(1, initial_size + 1)
    )
    edits = _random_land_edits(rng=rng, initial_labels=initial_labels)
    resubmit_after_edit = bool(edits) and rng.choice((False, True))
    final_live_labels, _, _ = simulate_land_edits(
        edits=edits, initial_labels=initial_labels
    )
    land_target_position: int | None = None
    if rng.random() < 0.25:
        eligible = [
            position
            for position, label in enumerate(final_live_labels, start=1)
            if label != INSERTED_LABEL or resubmit_after_edit
        ]
        if eligible:
            land_target_position = rng.choice(eligible)
    return LandScenario(
        name=name,
        initial_size=initial_size,
        via=via,
        edits=edits,
        resubmit_after_edit=resubmit_after_edit,
        approved_prefix=rng.randint(0, len(final_live_labels)),
        land_target_position=land_target_position,
        with_second_stack=rng.random() < 0.25,
    )


LandDriftKind = Literal[
    "changes_requested",
    "pr_closed",
    "pr_draft_toggled",
    "pr_merged_externally",
    "review_branch_deleted",
    "trunk_advanced",
]
LandDriftOutcome = Literal["fail_closed", "fetch_abandons", "prefix_stop"]
LandDriftDiagnosis = Literal["merged_ancestor_on_trunk", "stack_not_on_trunk"]

_FAIL_CLOSED_LAND_DRIFT_KINDS = frozenset({"pr_merged_externally", "trunk_advanced"})


@dataclass(frozen=True, slots=True)
class LandDriftScenario:
    """One external transition applied to a submitted, fully approved stack.

    Fail-closed kinds must stop land before any mutation. Prefix-stop kinds
    stop the readiness walk at the drifted change and land what sits below.

    A deleted review branch splits by position. Deleting the head's branch
    lets land's fetch abandon the local change (nothing else references it),
    so the re-resolved selection lands the untouched survivors below. A
    mid-stack change survives the fetch because descendants' bookmarks keep
    it reachable, and its externally closed PR stops the walk like any other
    prefix stop.
    """

    name: str
    initial_size: int
    kind: LandDriftKind
    target_position: int | None = None

    def __post_init__(self) -> None:
        if self.initial_size < 1:
            raise ValueError("land drift scenarios require a submitted change")
        if self.kind == "trunk_advanced":
            if self.target_position is not None:
                raise ValueError("trunk_advanced does not target one change")
            return
        if self.target_position is None:
            raise ValueError(f"{self.kind} requires a target position")
        if not 1 <= self.target_position <= self.initial_size:
            raise ValueError("drift target must be inside the submitted stack")
        if self.kind == "pr_merged_externally" and self.target_position != 1:
            # A stacked PR's base is the review branch below it, so only the
            # bottom PR squash-merges into trunk; merging higher PRs outside
            # the tool is a different (handoff-family) shape.
            raise ValueError("external squash merges target the bottom PR")
        if self.kind == "review_branch_deleted" and self.initial_size < 2:
            raise ValueError("branch deletion needs a surviving change to land")

    @property
    def initial_labels(self) -> tuple[str, ...]:
        return tuple(initial_land_label(index) for index in range(1, self.initial_size + 1))

    @property
    def outcome(self) -> LandDriftOutcome:
        if self.kind in _FAIL_CLOSED_LAND_DRIFT_KINDS:
            return "fail_closed"
        if self.kind == "review_branch_deleted" and self.target_position == self.initial_size:
            return "fetch_abandons"
        return "prefix_stop"

    @property
    def expected_landed_labels(self) -> tuple[str, ...]:
        if self.outcome == "fail_closed":
            return ()
        assert self.target_position is not None
        target_label = self.initial_labels[self.target_position - 1]
        if self.outcome == "fetch_abandons":
            return tuple(
                label for label in self.initial_labels if label != target_label
            )
        return self.initial_labels[: self.target_position - 1]

    @property
    def expected_exit_code(self) -> int:
        if self.outcome == "fail_closed" or not self.expected_landed_labels:
            return 1
        return 0

    @property
    def expected_diagnosis(self) -> LandDriftDiagnosis | None:
        if self.kind == "trunk_advanced":
            return "stack_not_on_trunk"
        if self.kind == "pr_merged_externally":
            return "merged_ancestor_on_trunk"
        return None

    @property
    def trace(self) -> str:
        parts = [f"kind:{self.kind}", f"size:{self.initial_size}"]
        if self.target_position is not None:
            parts.append(f"position:{self.target_position}")
        return ",".join(parts)

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return (self.kind, self.initial_size, self.target_position)

    def __str__(self) -> str:
        return f"{self.name}: {self.trace}"


def land_drift_scenarios_from_environment() -> tuple[LandDriftScenario, ...]:
    """Return deterministic land drift scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_LAND_DRIFT_PROPERTY_SCENARIOS",
            str(DEFAULT_LAND_DRIFT_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_LAND_PROPERTY_SEED",
            str(DEFAULT_LAND_SCENARIO_SEED),
        )
    )
    return generate_land_drift_scenarios(count=count, seed=seed)


def generate_land_drift_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[LandDriftScenario, ...]:
    if count < 1:
        return ()

    scenarios: list[LandDriftScenario] = []
    seen: set[tuple[object, ...]] = set()
    for scenario in _fixed_land_drift_scenarios():
        if len(scenarios) >= count:
            return tuple(scenarios)
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    rng = random.Random(seed)
    attempts = 0
    max_attempts = max(count * MAX_LAND_ATTEMPTS_MULTIPLIER, 1)
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        scenario = _random_land_drift_scenario(
            rng=rng, name=f"land-drift-random-{attempts:03d}"
        )
        if scenario.canonical_key in seen:
            continue
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    return tuple(scenarios)


def _fixed_land_drift_scenarios() -> tuple[LandDriftScenario, ...]:
    return (
        LandDriftScenario(
            name="drift-trunk-advanced-fails-closed",
            initial_size=2,
            kind="trunk_advanced",
        ),
        LandDriftScenario(
            name="drift-external-squash-merge-fails-closed-with-cleanup-path",
            initial_size=2,
            kind="pr_merged_externally",
            target_position=1,
        ),
        LandDriftScenario(
            name="drift-externally-closed-pr-stops-prefix",
            initial_size=3,
            kind="pr_closed",
            target_position=2,
        ),
        LandDriftScenario(
            name="drift-deleted-review-branch-abandons-change-and-lands-survivors",
            initial_size=2,
            kind="review_branch_deleted",
            target_position=2,
        ),
        LandDriftScenario(
            name="drift-draft-toggle-stops-prefix",
            initial_size=2,
            kind="pr_draft_toggled",
            target_position=1,
        ),
        LandDriftScenario(
            name="drift-changes-requested-stops-prefix",
            initial_size=3,
            kind="changes_requested",
            target_position=2,
        ),
    )


DEFAULT_LAND_DRIFT_SCENARIO_COUNT = len(_fixed_land_drift_scenarios())


def _random_land_drift_scenario(
    *,
    rng: random.Random,
    name: str,
) -> LandDriftScenario:
    kinds: tuple[LandDriftKind, ...] = (
        "changes_requested",
        "pr_closed",
        "pr_draft_toggled",
        "pr_merged_externally",
        "review_branch_deleted",
        "trunk_advanced",
    )
    kind = rng.choice(kinds)
    initial_size = rng.randint(2, 3)
    if kind == "trunk_advanced":
        return LandDriftScenario(name=name, initial_size=initial_size, kind=kind)
    target_position = 1 if kind == "pr_merged_externally" else rng.randint(1, initial_size)
    return LandDriftScenario(
        name=name,
        initial_size=initial_size,
        kind=kind,
        target_position=target_position,
    )


LandRetryFault = Literal[
    "after_push_ack_lost",
    "after_push_trunk",
    "after_retire",
    "mid_finalize",
]


@dataclass(frozen=True, slots=True)
class LandRetryScenario:
    """One interrupted direct-push land followed by a converging rerun.

    `after_push_ack_lost` moves remote trunk but fails before the successful push
    is journaled; `after_push_trunk` fails loading the first landed PR after the
    successful push is journaled;
    `mid_finalize` fails on the second landed PR after the first finalized;
    `after_retire` drops the completed marker after a fully successful run,
    reproducing a crash between tracking retirement and the marker write.
    """

    name: str
    initial_size: int
    approved_prefix: int
    fault: LandRetryFault

    def __post_init__(self) -> None:
        if self.initial_size < 1:
            raise ValueError("land retry scenarios require a submitted change")
        if not 1 <= self.approved_prefix <= self.initial_size:
            raise ValueError("the retried land must have a landable prefix")
        if self.fault == "mid_finalize" and self.approved_prefix < 2:
            raise ValueError("mid_finalize interrupts the second landed PR")

    @property
    def initial_labels(self) -> tuple[str, ...]:
        return tuple(initial_land_label(index) for index in range(1, self.initial_size + 1))

    @property
    def landed_labels(self) -> tuple[str, ...]:
        return self.initial_labels[: self.approved_prefix]

    @property
    def fault_pull_number(self) -> int | None:
        if self.fault == "after_push_trunk":
            return 1
        if self.fault == "mid_finalize":
            return 2
        return None

    @property
    def trace(self) -> str:
        return f"fault:{self.fault},size:{self.initial_size},prefix:{self.approved_prefix}"

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return (self.fault, self.initial_size, self.approved_prefix)

    def __str__(self) -> str:
        return f"{self.name}: {self.trace}"


def land_retry_scenarios_from_environment() -> tuple[LandRetryScenario, ...]:
    """Return deterministic land retry scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_LAND_RETRY_PROPERTY_SCENARIOS",
            str(DEFAULT_LAND_RETRY_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_LAND_PROPERTY_SEED",
            str(DEFAULT_LAND_SCENARIO_SEED),
        )
    )
    return generate_land_retry_scenarios(count=count, seed=seed)


def generate_land_retry_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[LandRetryScenario, ...]:
    if count < 1:
        return ()

    scenarios: list[LandRetryScenario] = []
    seen: set[tuple[object, ...]] = set()
    for scenario in _fixed_land_retry_scenarios():
        if len(scenarios) >= count:
            return tuple(scenarios)
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    rng = random.Random(seed)
    attempts = 0
    max_attempts = max(count * MAX_LAND_ATTEMPTS_MULTIPLIER, 1)
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        scenario = _random_land_retry_scenario(
            rng=rng, name=f"land-retry-random-{attempts:03d}"
        )
        if scenario.canonical_key in seen:
            continue
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    return tuple(scenarios)


def _fixed_land_retry_scenarios() -> tuple[LandRetryScenario, ...]:
    return (
        LandRetryScenario(
            name="retry-after-trunk-push-acknowledgement-loss-converges",
            initial_size=2,
            approved_prefix=2,
            fault="after_push_ack_lost",
        ),
        LandRetryScenario(
            name="retry-after-trunk-push-converges",
            initial_size=3,
            approved_prefix=2,
            fault="after_push_trunk",
        ),
        LandRetryScenario(
            name="retry-mid-finalize-converges-without-double-close",
            initial_size=3,
            approved_prefix=2,
            fault="mid_finalize",
        ),
        LandRetryScenario(
            name="retry-after-tracking-retire-converges",
            initial_size=2,
            approved_prefix=1,
            fault="after_retire",
        ),
    )


DEFAULT_LAND_RETRY_SCENARIO_COUNT = len(_fixed_land_retry_scenarios())


def _random_land_retry_scenario(
    *,
    rng: random.Random,
    name: str,
) -> LandRetryScenario:
    faults: tuple[LandRetryFault, ...] = (
        "after_push_ack_lost",
        "after_push_trunk",
        "after_retire",
        "mid_finalize",
    )
    fault = rng.choice(faults)
    initial_size = rng.randint(2, 3)
    minimum_prefix = 2 if fault == "mid_finalize" else 1
    return LandRetryScenario(
        name=name,
        initial_size=initial_size,
        approved_prefix=rng.randint(minimum_prefix, initial_size),
        fault=fault,
    )


LandHandoffOrigin = Literal["external_squash_merge", "merge_land"]
LandHandoffRecovery = Literal["cleanup_rebase", "sync"]


@dataclass(frozen=True, slots=True)
class LandHandoffScenario:
    """A merged prefix handed off to sync/cleanup, resubmitted, then landed.

    The chain replays the documented recovery contract end to end: a prefix
    reaches trunk through GitHub merges — `land --via merge`, an interrupted
    merge land, or squash merges outside the tool — then `sync` or
    `cleanup --rebase` plus `submit` rebuilds the local suffix, and a final
    direct-push land consumes it.
    """

    name: str
    initial_size: int
    merged_prefix: int
    origin: LandHandoffOrigin
    recovery: LandHandoffRecovery
    merge_fault: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.merged_prefix <= self.initial_size - 1:
            raise ValueError("the handoff needs a merged prefix and a surviving suffix")
        if self.merge_fault and self.origin != "merge_land":
            raise ValueError("only a merge-transport land can be interrupted mid-merge")

    @property
    def initial_labels(self) -> tuple[str, ...]:
        return tuple(initial_land_label(index) for index in range(1, self.initial_size + 1))

    @property
    def merged_labels(self) -> tuple[str, ...]:
        return self.initial_labels[: self.merged_prefix]

    @property
    def suffix_labels(self) -> tuple[str, ...]:
        return self.initial_labels[self.merged_prefix :]

    @property
    def fault_pull_number(self) -> int | None:
        if not self.merge_fault:
            return None
        return self.merged_prefix + 1

    @property
    def withheld_position(self) -> int | None:
        """The 1-based position left unapproved to stop the merge land.

        An interrupted merge land approves everything and relies on the fault
        instead; the suffix approval then survives into the final land.
        """

        if self.origin == "merge_land" and not self.merge_fault:
            return self.merged_prefix + 1
        return None

    @property
    def trace(self) -> str:
        parts = [
            f"origin:{self.origin}",
            f"recovery:{self.recovery}",
            f"size:{self.initial_size}",
            f"merged:{self.merged_prefix}",
        ]
        if self.merge_fault:
            parts.append("merge_fault")
        return ",".join(parts)

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return (
            self.origin,
            self.recovery,
            self.initial_size,
            self.merged_prefix,
            self.merge_fault,
        )

    def __str__(self) -> str:
        return f"{self.name}: {self.trace}"


def land_handoff_scenarios_from_environment() -> tuple[LandHandoffScenario, ...]:
    """Return deterministic land handoff scenarios for the pytest adapter."""

    count = int(
        os.environ.get(
            "JJ_STACK_LAND_HANDOFF_PROPERTY_SCENARIOS",
            str(DEFAULT_LAND_HANDOFF_SCENARIO_COUNT),
        )
    )
    seed = int(
        os.environ.get(
            "JJ_STACK_LAND_PROPERTY_SEED",
            str(DEFAULT_LAND_SCENARIO_SEED),
        )
    )
    return generate_land_handoff_scenarios(count=count, seed=seed)


def generate_land_handoff_scenarios(
    *,
    count: int,
    seed: int,
) -> tuple[LandHandoffScenario, ...]:
    if count < 1:
        return ()

    scenarios: list[LandHandoffScenario] = []
    seen: set[tuple[object, ...]] = set()
    for scenario in _fixed_land_handoff_scenarios():
        if len(scenarios) >= count:
            return tuple(scenarios)
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    rng = random.Random(seed)
    attempts = 0
    max_attempts = max(count * MAX_LAND_ATTEMPTS_MULTIPLIER, 1)
    while len(scenarios) < count and attempts < max_attempts:
        attempts += 1
        scenario = _random_land_handoff_scenario(
            rng=rng, name=f"land-handoff-random-{attempts:03d}"
        )
        if scenario.canonical_key in seen:
            continue
        scenarios.append(scenario)
        seen.add(scenario.canonical_key)

    return tuple(scenarios)


def _fixed_land_handoff_scenarios() -> tuple[LandHandoffScenario, ...]:
    return (
        LandHandoffScenario(
            name="handoff-merge-land-then-sync-lands-suffix",
            initial_size=3,
            merged_prefix=1,
            origin="merge_land",
            recovery="sync",
        ),
        LandHandoffScenario(
            name="handoff-merge-land-then-cleanup-rebase-lands-suffix",
            initial_size=3,
            merged_prefix=2,
            origin="merge_land",
            recovery="cleanup_rebase",
        ),
        LandHandoffScenario(
            name="handoff-external-squash-merge-then-sync-recovers",
            initial_size=3,
            merged_prefix=1,
            origin="external_squash_merge",
            recovery="sync",
        ),
        LandHandoffScenario(
            name="handoff-external-squash-merge-then-cleanup-rebase-recovers",
            initial_size=2,
            merged_prefix=1,
            origin="external_squash_merge",
            recovery="cleanup_rebase",
        ),
        LandHandoffScenario(
            name="handoff-interrupted-merge-land-recovers-through-sync",
            initial_size=2,
            merged_prefix=1,
            origin="merge_land",
            recovery="sync",
            merge_fault=True,
        ),
    )


DEFAULT_LAND_HANDOFF_SCENARIO_COUNT = len(_fixed_land_handoff_scenarios())


def _random_land_handoff_scenario(
    *,
    rng: random.Random,
    name: str,
) -> LandHandoffScenario:
    origins: tuple[LandHandoffOrigin, ...] = ("external_squash_merge", "merge_land")
    recoveries: tuple[LandHandoffRecovery, ...] = ("cleanup_rebase", "sync")
    initial_size = rng.randint(2, 4)
    return LandHandoffScenario(
        name=name,
        initial_size=initial_size,
        merged_prefix=rng.randint(1, initial_size - 1),
        origin=rng.choice(origins),
        recovery=rng.choice(recoveries),
    )


def _random_land_edits(
    *,
    rng: random.Random,
    initial_labels: tuple[str, ...],
) -> tuple[LandEditOperation, ...]:
    count = rng.choice((0, 1, 1, 2))
    live = list(initial_labels)
    inserted = False
    edits: list[LandEditOperation] = []
    for _ in range(count):
        kinds: list[LandEditKind] = ["rewrite"]
        if len(live) >= 2:
            kinds.extend(
                ("abandon", "move_after", "move_before", "move_to_top",
                 "squash_into_previous")
            )
        if not inserted:
            kinds.extend(("insert_after", "insert_before"))
        kind = rng.choice(kinds)
        if kind == "rewrite":
            operation = LandEditOperation(kind=kind, label=rng.choice(live))
        elif kind in {"insert_after", "insert_before"}:
            operation = LandEditOperation(
                kind=kind,
                label=rng.choice(live),
                new_label=INSERTED_LABEL,
            )
            inserted = True
        elif kind == "abandon":
            operation = LandEditOperation(kind=kind, label=rng.choice(live))
        elif kind == "move_to_top":
            operation = LandEditOperation(kind=kind, label=rng.choice(live[:-1]))
        elif kind in {"move_after", "move_before"}:
            label = rng.choice(live)
            target = rng.choice([candidate for candidate in live if candidate != label])
            operation = LandEditOperation(kind=kind, label=label, target_label=target)
        else:
            operation = LandEditOperation(kind=kind, label=rng.choice(live[1:]))
        edits.append(operation)
        live = list(
            simulate_land_edits(
                edits=tuple(edits), initial_labels=initial_labels
            )[0]
        )
    return tuple(edits)
