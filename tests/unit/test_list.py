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
        pr={"number": 1},
        pr_label="orphan",
        state="orphan",
        subject="orphan",
    )
    notes: list[ui.Message] = []
    monkeypatch.setattr("jj_stack.commands.list_.console.note", notes.append)

    _emit_orphan_hint((row, row))

    assert len(notes) == 1
    assert "cleanup --pull-request orphans --close" in ui.plain_text(notes[0])
