"""Diagnostics: raw chapters per assigned Tonie and the stored mapping.

Password, URL, username and household IDs are redacted, Tonie IDs are replaced by tonie_1, tonie_2, …
"""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant

from . import bridge
from .const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, OPT_TONIES
from .privacy import redact_keys, replace_ids
from .subsonic import describe_error

TO_REDACT = {CONF_PASSWORD, CONF_URL, CONF_USERNAME, "household_id", "householdId"}

_CHAPTER_KEYS = ("id", "file", "title", "seconds", "transcoding")
_TONIE_KEYS = ("name", "secondsPresent", "secondsRemaining", "chaptersPresent", "chaptersRemaining", "transcoding")


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    manager = entry.runtime_data
    tonies: dict[str, Any] = {}
    stored = manager.stored_data()
    ids = set(manager.tonies) | set(stored.get("tonies", {}))
    for tid in sorted(ids):
        info: dict[str, Any] = {
            "config": manager.tonies.get(tid),
            "status": manager.status.get(tid),
            "store": stored.get("tonies", {}).get(tid),
        }
        try:
            ref = bridge.find_tonie(hass, tid)
            async with asyncio.timeout(60):
                raw = await ref.client.get_creative_tonie(ref.household_id, tid)
            raw = raw if isinstance(raw, dict) else {}
            info["cloud"] = {k: raw.get(k) for k in _TONIE_KEYS}
            chapters = raw.get("chapters")
            info["chapters"] = (
                [{k: c.get(k) for k in _CHAPTER_KEYS} for c in chapters if isinstance(c, dict)]
                if isinstance(chapters, list)
                else repr(chapters)
            )
        except Exception as err:  # noqa: BLE001, diagnostics should still return something
            info["cloud_error"] = describe_error(err)
        tonies[tid] = info
    result = {"entry": {"data": dict(entry.data), "options": dict(entry.options)}, "tonies": tonies}
    known = ids | set(entry.options.get(OPT_TONIES) or {})
    aliases = {tid: f"tonie_{i}" for i, tid in enumerate(sorted(known), 1)}
    return replace_ids(redact_keys(result, TO_REDACT), aliases)
