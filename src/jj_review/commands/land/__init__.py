"""Land the consecutive changes above `trunk()` that are ready to land now."""

from __future__ import annotations

from . import command as _command
from .command import (
    HELP as HELP,
    land as land,
)
from .models import (
    LandAction as LandAction,
    LandPlan as LandPlan,
    LandResult as LandResult,
    LandRevision as LandRevision,
    PreparedLand as PreparedLand,
)

__doc__ = _command.__doc__
