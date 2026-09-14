"""Convert released tracking files to the current representation when loading them."""

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
            f"Tracking data uses format version {version}, which is newer than supported "
            f"version {current_version}.",
            hint="Upgrade jj-stack to read this tracking data.",
        )
    if version == current_version:
        return raw
    if version not in {5, 6, 7}:
        raise ValueError(f"unsupported tracking schema version {version}")
    migrated = deepcopy(raw)
    identities = migrated.pop("pr_identities", {} if version == 7 else None)
    baselines = migrated.pop("submitted_baselines", {} if version > 5 else None)
    if not isinstance(identities, dict) or not isinstance(baselines, dict):
        raise ValueError("persisted tracking records must be an object")
    if identities.keys() != baselines.keys():
        raise ValueError("Pull request identities and baselines must have identical keys.")
    if "prs" in migrated:
        raise ValueError(f"unexpected tracking records in schema version {version}")
    if version < 7:
        for change_id, identity in identities.items():
            _remove_legacy_metadata(identity, baselines[change_id], version=version)
    migrated["prs"] = {
        change_id: {"pr_identity": identity, "submitted_baseline": baselines[change_id]}
        for change_id, identity in identities.items()
    }
    migrated["version"] = current_version
    return migrated


def _remove_legacy_metadata(identity: object, baseline: object, *, version: int) -> None:
    if not isinstance(identity, dict):
        raise ValueError("persisted tracking record must be an object")
    if version == 5:
        for record, expected in ((identity, 3), (baseline, 1)):
            if not isinstance(record, dict):
                raise ValueError("persisted tracking record must be an object")
            record_version = record.pop("version", None)
            if type(record_version) is not int or record_version != expected:
                raise ValueError("unsupported persisted tracking record version")
    for field in ("repo_owner", "repo_name", "head_owner"):
        identity.pop(field, None)
