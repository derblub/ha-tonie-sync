"""Common base: one device per Tonie, linked via via_device_id to the Creative Tonie device of the toniebox integration.

Attaching directly to the toniebox device no longer works cleanly since HA 2026.x: device identifiers
are unique per config entry, so identical identifiers would create a second device.
"""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import bridge
from .const import DOMAIN, SIGNAL_UPDATE
from .manager import TonieSyncManager


class TonieSyncEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: TonieSyncManager, tonie_id: str, kind: str) -> None:
        self.manager = manager
        self.tonie_id = tonie_id
        self._attr_unique_id = f"{tonie_id}_{kind}"
        self._attr_translation_key = kind
        cfg = manager.tonies.get(tonie_id, {})
        label = cfg.get("name") or tonie_id
        info = DeviceInfo(
            identifiers={(DOMAIN, tonie_id)},
            name=f"Tonie Sync {bridge.short_name(label) or label}",
            manufacturer="tonie_sync",
            model="Navidrome sync",
        )
        if via := bridge.toniebox_device_id(manager.hass, tonie_id):
            info["via_device_id"] = via
        self._attr_device_info = info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(entry_id=self.manager.entry.entry_id, tonie_id=self.tonie_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
