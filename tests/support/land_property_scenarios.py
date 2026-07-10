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

LandVia = Literal["push", "merge"]
LandEditKind = Literal[
    "abandon",
    "insert_after",
    "move_after",
    "move_before",
    "move_to_top",
    "rewrite",
    "squash_into_previous",
]

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


@dataclass(frozen=True, slots=True)
class LandEditOperation:
    """One local stack edit applied after the initial submit."""

    kind: LandEditKind
    label: str
    target_label: str | None = None

    @property
    def trace(self) -> str:
        if self.kind in {"move_after", "move_before"}:
            return f"{self.kind}:{self.label}:{self.target_label}"
        return f"{self.kind}:{self.label}"


def simulate_land_edits(
    *,
    edits: tuple[LandEditOperation, ...],
    initial_labels: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
    """Return (final live labels, orphaned labels, content-divergent labels).

    Raises ValueError for edit traces that are not reachable in order, so both
    scenario validation and random generation share one source of truth.
    """

    live = list(initial_labels)
    orphaned: list[str] = []
    divergent: set[str] = set()
    for operation in edits:
        if operation.label not in live:
            raise ValueError(f"edit targets a change that is not live: {operation.trace}")
        index = live.index(operation.label)
        if operation.kind == "abandon":
            if len(live) < 2:
                raise ValueError("abandon requires a surviving live change")
            live.pop(index)
            orphaned.append(operation.label)
        elif operation.kind == "rewrite":
            divergent.add(operation.label)
        elif operation.kind == "insert_after":
            if INSERTED_LABEL in live:
                raise ValueError("only one insert per trace is modeled")
            live.insert(index + 1, INSERTED_LABEL)
        elif operation.kind == "move_to_top":
            if live[-1] == operation.label:
                raise ValueError("move_to_top target is already at the top")
            live.pop(index)
            live.append(operation.label)
        elif operation.kind in {"move_after", "move_before"}:
            target = operation.target_label
            if target is None or target == operation.label or target not in live:
                raise ValueError(f"move requires a distinct live target: {operation.trace}")
            live.pop(index)
            target_index = live.index(target)
            insert_at = target_index + 1 if operation.kind == "move_after" else target_index
            live.insert(insert_at, operation.label)
        elif operation.kind == "squash_into_previous":
            if index == 0:
                raise ValueError("squash_into_previous requires a non-bottom change")
            destination = live[index - 1]
            live.pop(index)
            orphaned.append(operation.label)
            divergent.add(destination)
        else:
            raise ValueError(f"unsupported land edit kind: {operation.kind}")
    return tuple(live), tuple(orphaned), frozenset(divergent)


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
            edits=(LandEditOperation(kind="insert_after", label=initial_land_label(1)),),
            resubmit_after_edit=False,
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
            kinds.append("insert_after")
        kind = rng.choice(kinds)
        if kind == "rewrite":
            operation = LandEditOperation(kind=kind, label=rng.choice(live))
        elif kind == "insert_after":
            operation = LandEditOperation(kind=kind, label=rng.choice(live))
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
