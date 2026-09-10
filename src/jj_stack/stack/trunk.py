"""Shared validation for observed trunk commits."""

from __future__ import annotations

from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import CliError, UnsupportedStackError
from jj_stack.models.stack import LocalCommit


def require_usable_trunk(trunks: Sequence[LocalCommit]) -> LocalCommit:
    if len(trunks) != 1:
        raise CliError(t"Could not resolve {ui.revset('trunk()')} to one commit.")
    trunk = trunks[0]
    if not trunk.parents:
        raise UnsupportedStackError(
            t"{ui.revset('trunk()')} resolves to the root commit, so this repo has no trunk.",
            hint=t"This usually means the repo has no Git remote, or its trunk branch has not "
            t"been fetched. Run {ui.cmd('jj-stack doctor')} to check the remote and trunk "
            t"branch.",
            reason="trunk_resolved_to_root",
        )
    return trunk
