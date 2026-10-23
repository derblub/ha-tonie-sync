"""Migrations of the stored data (.storage/tonie_sync.<entry_id>). No HA imports, so it is testable."""
from __future__ import annotations

from typing import Any

try:
    from .const import LEGACY_STATES
except ImportError:  # tests: modules are on sys.path directly
    from const import LEGACY_STATES


def migrate_store(old_major: int, data: Any) -> Any:
    """Bring stored data from an older major version to the current one."""
    if old_major < 2 and isinstance(data, dict) and isinstance(data.get("tonies"), dict):
        # v1 → v2: German state keys → English state keys
        for ts in data["tonies"].values():
            last = ts.get("last") if isinstance(ts, dict) else None
            if isinstance(last, dict) and last.get("state") in LEGACY_STATES:
                last["state"] = LEGACY_STATES[last["state"]]
    return data
