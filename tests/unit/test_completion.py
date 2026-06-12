from __future__ import annotations

import pytest

from jj_stack.cli import build_parser
from jj_stack.completion import emit_shell_completion


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("bash", "complete -F _jj_stack jj-stack"),
        ("zsh", "#compdef jj-stack"),
        ("fish", "complete -c jj-stack -f"),
    ],
)
def test_emit_shell_completion_smoke(shell: str, marker: str) -> None:
    script = emit_shell_completion(build_parser(), shell)

    assert marker in script
    assert "jj-stack" in script
