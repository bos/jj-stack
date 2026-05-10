"""Intent file persistence and process-liveness helpers."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from jj_review.models.intent import IntentFile, LoadedIntent
from jj_review.system import pid_is_alive

logger = logging.getLogger(__name__)
_INTENT_ADAPTER = TypeAdapter(IntentFile)


def _intent_filename(state_dir: Path, now: datetime) -> Path:
    base = now.strftime("%Y-%m-%d-%H-%M")
    for nn in range(1, 100):
        candidate = state_dir / f"incomplete-{base}.{nn:02d}.json"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate intent file name (100 collisions).")


def write_new_intent(state_dir: Path, intent: IntentFile) -> Path:
    """Write an intent file atomically. Returns the path of the created file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    dest = _intent_filename(state_dir, datetime.now(UTC))
    save_intent(dest, intent)
    logger.debug("Wrote intent file %s", dest.name)
    return dest


def save_intent(path: Path, intent: IntentFile) -> None:
    """Persist an intent atomically at a known path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent_file(path, intent)


def _write_intent_file(path: Path, intent: IntentFile) -> None:
    rendered = intent.model_dump_json(exclude_none=True, indent=2) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        Path(tmp_path_str).replace(path)
    except Exception:
        try:
            Path(tmp_path_str).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning(
                "Could not remove temporary intent file %s: %s",
                tmp_path_str,
                error,
            )
        raise


def scan_intents(state_dir: Path) -> list[LoadedIntent]:
    results = []
    for p in sorted(state_dir.glob("incomplete-*.json")):
        try:
            loaded = LoadedIntent(
                path=p,
                intent=_INTENT_ADAPTER.validate_json(p.read_text(encoding="utf-8")),
            )
        except OSError as error:
            logger.error("Could not read intent file %s: %s", p, error)
            continue
        except ValidationError as error:
            logger.error("Could not parse intent file %s: %s", p, error)
            continue
        results.append(loaded)
    return results


def check_same_kind_intent(
    state_dir: Path,
    new_intent: IntentFile,
    *,
    print_fn: Callable[[str], None] = print,
) -> list[LoadedIntent]:
    """Return stale same-kind intents and report live legacy intents without waiting."""

    existing = [
        loaded for loaded in scan_intents(state_dir) if loaded.intent.kind == new_intent.kind
    ]
    stale: list[LoadedIntent] = []
    for loaded in existing:
        if pid_is_alive(loaded.intent.pid):
            print_fn(
                f"Another {loaded.intent.label} is in progress "
                f"(PID {loaded.intent.pid})."
            )
        else:
            stale.append(loaded)
    return stale
