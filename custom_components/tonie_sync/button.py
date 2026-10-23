"""Sync button per Tonie."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import TonieSyncEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    manager = entry.runtime_data
    async_add_entities(SyncButton(manager, tid, "sync") for tid in manager.tonies)


class SyncButton(TonieSyncEntity, ButtonEntity):
    _attr_icon = "mdi:playlist-music"

    @property
    def available(self) -> bool:
        return not self.manager.is_running(self.tonie_id)

    async def async_press(self) -> None:
        # Runs in the background so the button press does not hang for minutes
        self.hass.async_create_background_task(
            self.manager.async_sync(self.tonie_id), f"tonie_sync {self.tonie_id}"
        )
