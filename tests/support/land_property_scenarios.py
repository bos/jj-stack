"""Scenario generation for land property tests.

Land scenarios compose the states `land` actually meets: a submitted, partially
approved stack that may have been edited since its last submit. Each scenario
starts from a submitted linear stack, optionally applies one local stack edit
(with or without a follow-up resubmit), approves a prefix of the final live
stack, then models the prefix `land` must consume and the boundary where it
must stop.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Literal

LandVia = Literal["push", "merge"]
LandEditKind = Literal["abandon", "insert_after", "rewrite"]

DEFAULT_LAND_SCENARIO_COUNT = 11
DEFAULT_LAND_SCENARIO_SEED = 8675309
MAX_LAND_ATTEMPTS_MULTIPLIER = 40

INSERTED_LABEL = "insert-1"


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

    @property
    def trace(self) -> str:
        return f"{self.kind}:{self.label}"


@dataclass(frozen=True, slots=True)
class LandScenario:
    """A submitted stack, an optional edit, an approval prefix, and a transport."""

    name: str
    initial_size: int
    via: LandVia
    edit: LandEditOperation | None
    resubmit_after_edit: bool
    approved_prefix: int
    skip_cleanup: bool = False
    unmergeable_pull_number: int | None = None

    def __post_init__(self) -> None:
        if self.initial_size < 1:
            raise ValueError("land scenarios require at least one submitted change")
        if self.skip_cleanup and self.via != "push":
            raise ValueError("--skip-cleanup is only modeled for direct-push land")
        if self.edit is None and self.resubmit_after_edit:
            raise ValueError("resubmit without an edit does not change the modeled state")
        if self.edit is not None:
            if self.edit.label not in self.initial_labels:
                raise ValueError("edits must target an initially submitted change")
            if self.edit.kind == "abandon" and self.initial_size < 2:
                raise ValueError("abandon requires a surviving live change")
        if not 0 <= self.approved_prefix <= len(self.final_live_labels):
            raise ValueError("approved prefix must be within the final live stack")
        if self.unmergeable_pull_number is not None:
            if self.via != "merge":
                raise ValueError("only merge-transport scenarios can have an unmergeable PR")
            if self.edit is not None:
                raise ValueError("unmergeable scenarios keep the fixed no-edit shape")
            if not 1 <= self.unmergeable_pull_number <= self.initial_size:
                raise ValueError("unmergeable PR must be inside the submitted stack")

    @property
    def initial_labels(self) -> tuple[str, ...]:
        return tuple(initial_land_label(index) for index in range(1, self.initial_size + 1))

    @property
    def final_live_labels(self) -> tuple[str, ...]:
        labels = list(self.initial_labels)
        if self.edit is None:
            return tuple(labels)
        index = labels.index(self.edit.label)
        if self.edit.kind == "abandon":
            del labels[index]
        elif self.edit.kind == "insert_after":
            labels.insert(index + 1, INSERTED_LABEL)
        return tuple(labels)

    @property
    def orphaned_labels(self) -> tuple[str, ...]:
        if self.edit is not None and self.edit.kind == "abandon":
            return (self.edit.label,)
        return ()

    def label_has_pull_request(self, label: str) -> bool:
        if label != INSERTED_LABEL:
            return True
        return self.resubmit_after_edit

    def _walk_stops_at(self, index: int, label: str) -> bool:
        """Whether the land readiness walk stops before this final-stack change."""

        if index >= self.approved_prefix:
            return True
        if not self.label_has_pull_request(label):
            return True
        if (
            self.edit is not None
            and self.edit.kind == "rewrite"
            and not self.resubmit_after_edit
            and label == self.edit.label
        ):
            # The remote review branch still holds the pre-rewrite commit, so
            # the local change differs from what reviewers approved.
            return True
        return False

    @property
    def expected_landed_labels(self) -> tuple[str, ...]:
        landed: list[str] = []
        for index, label in enumerate(self.final_live_labels):
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
        if self.edit is not None:
            parts.append(f"edit:{self.edit.trace}")
            parts.append(f"resubmit:{str(self.resubmit_after_edit).lower()}")
        parts.append(f"approved:{self.approved_prefix}")
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
            None if self.edit is None else self.edit.trace,
            self.resubmit_after_edit,
            self.approved_prefix,
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
            edit=None,
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-nothing-approved-blocks-without-mutation",
            initial_size=2,
            via="push",
            edit=None,
            resubmit_after_edit=False,
            approved_prefix=0,
        ),
        LandScenario(
            name="push-full-stack-retires-tracking",
            initial_size=2,
            via="push",
            edit=None,
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-skip-cleanup-keeps-local-bookmarks",
            initial_size=2,
            via="push",
            edit=None,
            resubmit_after_edit=False,
            approved_prefix=2,
            skip_cleanup=True,
        ),
        LandScenario(
            name="push-rewrite-without-resubmit-stops-at-stale-review",
            initial_size=3,
            via="push",
            edit=LandEditOperation(kind="rewrite", label=initial_land_label(2)),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-rewrite-with-resubmit-lands-full-stack",
            initial_size=2,
            via="push",
            edit=LandEditOperation(kind="rewrite", label=initial_land_label(1)),
            resubmit_after_edit=True,
            approved_prefix=2,
        ),
        LandScenario(
            name="push-insert-without-resubmit-stops-at-unsubmitted-change",
            initial_size=2,
            via="push",
            edit=LandEditOperation(kind="insert_after", label=initial_land_label(1)),
            resubmit_after_edit=False,
            approved_prefix=3,
        ),
        LandScenario(
            name="push-abandon-auto-resubmits-rebased-survivors",
            initial_size=3,
            via="push",
            edit=LandEditOperation(kind="abandon", label=initial_land_label(2)),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="merge-approval-prefix-keeps-merged-tracking",
            initial_size=3,
            via="merge",
            edit=None,
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
        LandScenario(
            name="merge-blocked-at-unmergeable-pr-keeps-prefix-tracking",
            initial_size=2,
            via="merge",
            edit=None,
            resubmit_after_edit=False,
            approved_prefix=2,
            unmergeable_pull_number=2,
        ),
        LandScenario(
            name="merge-abandon-auto-resubmits-then-merges-survivors",
            initial_size=3,
            via="merge",
            edit=LandEditOperation(kind="abandon", label=initial_land_label(2)),
            resubmit_after_edit=False,
            approved_prefix=2,
        ),
    )


def _random_land_scenario(*, rng: random.Random, name: str) -> LandScenario:
    via: LandVia = rng.choice(("push", "merge"))
    initial_size = rng.randint(2, 4)
    edit = _random_land_edit(rng=rng, initial_size=initial_size)
    resubmit_after_edit = edit is not None and rng.choice((False, True))
    final_size = initial_size
    if edit is not None and edit.kind == "abandon":
        final_size -= 1
    elif edit is not None and edit.kind == "insert_after":
        final_size += 1
    return LandScenario(
        name=name,
        initial_size=initial_size,
        via=via,
        edit=edit,
        resubmit_after_edit=resubmit_after_edit,
        approved_prefix=rng.randint(0, final_size),
    )


def _random_land_edit(
    *,
    rng: random.Random,
    initial_size: int,
) -> LandEditOperation | None:
    kinds: tuple[LandEditKind | None, ...] = (None, "abandon", "insert_after", "rewrite")
    kind = rng.choice(kinds)
    if kind is None:
        return None
    return LandEditOperation(
        kind=kind,
        label=initial_land_label(rng.randint(1, initial_size)),
    )
