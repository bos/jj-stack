"""Shared action recorder for command planning and execution output."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(slots=True)
class ActionRecorder[ActionT]:
    """Collect actions and optionally stream them as they are recorded."""

    on_action: Callable[[ActionT], None] | None = None
    blocks: Callable[[ActionT], bool] | None = None
    actions: list[ActionT] = field(default_factory=list)
    blocked: bool = False

    def record(self, action: ActionT) -> None:
        if self.blocks is not None and self.blocks(action):
            self.blocked = True
        self.actions.append(action)
        if self.on_action is not None:
            self.on_action(action)

    def as_tuple(self) -> tuple[ActionT, ...]:
        return tuple(self.actions)
