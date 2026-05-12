from __future__ import annotations

import pytest

from jj_review.cli import build_parser
from jj_review.completion import emit_shell_completion


@pytest.mark.parametrize(
    ("shell", "marker"),
    [
        ("bash", "complete -F _jj_review jj-review"),
        ("zsh", "#compdef jj-review"),
        ("fish", "complete -c jj-review -f"),
    ],
)
def test_emit_shell_completion_smoke(shell: str, marker: str) -> None:
    script = emit_shell_completion(build_parser(), shell)

    assert marker in script
    assert "jj-review" in script
