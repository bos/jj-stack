from __future__ import annotations

import io

import pytest

from jj_stack.commands.checkout import CheckoutPickerChoice, _prompt_picker_choice
from jj_stack.errors import CliError, UsageError


def test_prompt_picker_choice_reports_missing_or_invalid_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    with pytest.raises(CliError, match="No active local or GitHub stacks"):
        _prompt_picker_choice(())

    monkeypatch.setattr("sys.stdin", io.StringIO("9\n"))
    with pytest.raises(UsageError, match="not a valid stack number"):
        _prompt_picker_choice(
            (CheckoutPickerChoice(details=(), heading="feature 1", revset="change-1"),)
        )
