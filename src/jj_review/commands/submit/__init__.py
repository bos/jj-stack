"""Create or update GitHub pull requests for the selected stack of changes."""

from __future__ import annotations

from . import command as _command
from .command import (
    HELP as HELP,
    submit as submit,
)
from .models import (
    GeneratedDescription as GeneratedDescription,
    PreparedSubmitRevision as PreparedSubmitRevision,
    PullRequestSyncResult as PullRequestSyncResult,
    SubmitResult as SubmitResult,
    SubmittedRevision as SubmittedRevision,
)

__doc__ = _command.__doc__
