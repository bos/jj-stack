"""Forward migrations for persisted tracking data."""

from __future__ import annotations

from copy import deepcopy

from jj_stack.errors import TrackingStateError
from jj_stack.models.tracking import TrackingState


def migrate_tracking_state(raw: dict[str, object]) -> dict[str, object]:
    """Return tracking data expressed in the current schema."""

    version = raw.get("version")
    if type(version) is not int:
        raise ValueError("tracking schema version must be an integer")
    if version > (current_version := TrackingState().version):
        raise TrackingStateError(
            f"schema version {version} is newer than supported version {current_version}",
            hint="Upgrade jj-stack to read this tracking data.",
        )
    while version < current_version:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f"unsupported tracking schema version {version}")
        raw = migration(raw)
        if raw.get("version") != version + 1:
            raise ValueError(f"migration from tracking schema version {version} is invalid")
        version += 1
    return raw


def _migrate_v5_to_v6(raw: dict[str, object]) -> dict[str, object]:
    migrated = deepcopy(raw)
    for field, expected in (("pr_identities", 3), ("submitted_baselines", 1)):
        records = migrated.get(field)
        if not isinstance(records, dict):
            raise ValueError("versioned tracking records must be an object")
        for record in records.values():
            if not isinstance(record, dict):
                raise ValueError("persisted tracking record must be an object")
            record_version = record.pop("version", None)
            if type(record_version) is not int or record_version != expected:
                raise ValueError("unsupported persisted tracking record version")
    migrated["version"] = 6
    return migrated


_MIGRATIONS = {5: _migrate_v5_to_v6}
