"""Access to the internals of the toniebox integration (git4sim/HA-Toniebox).

All coupling points live here. If something is missing, a clear error is raised instead of a crash.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

from .const import TONIEBOX_DOMAIN

_REQUIRED = ("upload_file", "get_creative_tonie", "patch_creative_tonie")


class BridgeError(Exception):
    """toniebox integration is missing or has an unexpected interface."""


@dataclass
class TonieRef:
    coordinator: Any
    client: Any
    household_id: str
    tonie_id: str
    name: str


def _coordinators(hass: HomeAssistant) -> list[Any]:
    data = hass.data.get(TONIEBOX_DOMAIN)
    if not isinstance(data, dict) or not data:
        return []
    return [c for c in data.values() if isinstance(getattr(c, "data", None), dict)]


def list_creative_tonies(hass: HomeAssistant) -> dict[str, dict[str, str]]:
    """tonie_id → {name, household_id} of all Creative Tonies."""
    out: dict[str, dict[str, str]] = {}
    for coord in _coordinators(hass):
        for hh_id, hh in (coord.data.get("households") or {}).items():
            for t_id, t in (hh.get("creativetonies") or {}).items():
                out[t_id] = {"name": tonie_label(hass, t_id, str(t.get("name") or t_id)), "household_id": hh_id}
    return out


def find_tonie(hass: HomeAssistant, tonie_id: str) -> TonieRef:
    coords = _coordinators(hass)
    if not coords:
        raise BridgeError("toniebox integration is not loaded")
    for coord in coords:
        for hh_id, hh in (coord.data.get("households") or {}).items():
            t = (hh.get("creativetonies") or {}).get(tonie_id)
            if t is None:
                continue
            client = getattr(coord, "client", None)
            missing = [m for m in _REQUIRED if not callable(getattr(client, m, None))]
            if missing:
                raise BridgeError(f"toniebox client lacks {', '.join(missing)} (incompatible version?)")
            return TonieRef(coord, client, hh_id, tonie_id, str(t.get("name") or tonie_id))
    raise BridgeError(f"Creative Tonie {tonie_id} not found in the toniebox integration")


async def refresh(ref: TonieRef) -> None:
    fn = getattr(ref.coordinator, "async_request_refresh", None)
    if callable(fn):
        await fn()


def toniebox_device_id(hass: HomeAssistant, tonie_id: str) -> str | None:
    """ID of the Creative Tonie device of the toniebox integration (identifiers are unique per config entry)."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    ident = (TONIEBOX_DOMAIN, f"ct_{tonie_id}")
    for coord in _coordinators(hass):
        entry = getattr(coord, "entry", None)
        if entry is None or not hasattr(reg, "async_get_device_by_identifier"):
            continue
        dev = reg.async_get_device_by_identifier(ident, entry.entry_id)
        if dev is not None:
            return dev.id
    return None


def tonie_label(hass: HomeAssistant, tonie_id: str, fallback: str, hint: str | None = None) -> str:
    """Display name: device name (the cloud name is often just "Kreativ-Tonie"/"Creative-Tonie")."""
    from homeassistant.helpers import device_registry as dr

    name = None
    if dev_id := toniebox_device_id(hass, tonie_id):
        dev = dr.async_get(hass).async_get(dev_id)
        name = (dev.name_by_user or dev.name) if dev else None
    for cand in (name, hint, fallback):
        if cand and short_name(cand):
            return cand
    return fallback


def short_name(label: str) -> str:
    """'Creative-Tonie Bedtime' → 'Bedtime'; just 'Creative-Tonie' → ''. Also handles the German 'Kreativ-Tonie'."""
    import re

    return re.sub(r"^\s*(kreativ|creative)-?\s*tonie\s*", "", label or "", flags=re.IGNORECASE).strip()
