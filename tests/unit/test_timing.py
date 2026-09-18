from __future__ import annotations

import logging

import jj_stack.timing as timing


def test_timed_calls_are_logged_and_totalled_by_kind(monkeypatch, caplog) -> None:
    monkeypatch.setattr(timing, "_totals", {})
    with caplog.at_level(logging.DEBUG, logger="jj_stack.timing"):
        with timing.timed("jj", "jj log --no-graph -r\n  @ -T description"):
            pass
        for _ in range(2):
            with timing.timed("github", "POST /graphql PullRequestsByNumber"):
                pass

    assert [record.getMessage().split()[0] for record in caplog.records] == [
        "jj",
        "github",
        "github",
    ]
    assert "\n" not in caplog.records[0].getMessage()
    summary = timing.summary(imports_seconds=0.25)
    assert summary.startswith("wall ")
    assert "imports 0.25s" in summary
    assert "github 2\u00d7 " in summary
    assert "jj 1\u00d7 " in summary
