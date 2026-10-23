"""Status sensor per Tonie."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATE_PENDING, STATES
from .entity import TonieSyncEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    manager = entry.runtime_data
    async_add_entities(StatusSensor(manager, tid, "status") for tid in manager.tonies)


class StatusSensor(TonieSyncEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_icon = "mdi:sync"

    @property
    def _st(self) -> dict[str, Any]:
        return self.manager.status.get(self.tonie_id) or {}

    @property
    def native_value(self) -> str:
        return self._st.get("state") or STATE_PENDING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self._st.get("attrs") or {})
        attrs.pop("playlist_id", None)
        return attrs
