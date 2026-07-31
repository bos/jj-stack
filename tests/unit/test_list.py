from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.commands.list_ import (
    OrphanRow,
    _emit_orphan_hint,
)


def test_orphan_hint_is_emitted_once_for_all_rows(monkeypatch) -> None:
    row = OrphanRow(
        branch="jj-stack/orphan-aaaaaaaa",
        change_id="a" * 32,
        pull_request={"number": 1},
        review="orphan",
        state="orphan",
        subject="orphan",
    )
    notes: list[ui.Message] = []
    monkeypatch.setattr("jj_stack.commands.list_.console.note", notes.append)

    _emit_orphan_hint((row, row))

    assert len(notes) == 1
    assert "unstack --cleanup --pull-request orphans" in ui.plain_text(notes[0])
