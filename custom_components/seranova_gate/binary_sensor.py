"""Online (connectivity) sensor for the PROGATE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SeranovaConfigEntry
from .entity import SeranovaEntity
from .hub import SeranovaHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeranovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Online sensor."""
    async_add_entities([SeranovaOnlineSensor(entry.runtime_data)])


class SeranovaOnlineSensor(SeranovaEntity, BinarySensorEntity):
    """On while the cloud is connected and the device answers."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "online"

    def __init__(self, hub: SeranovaHub) -> None:
        """Bind to the hub."""
        super().__init__(hub, "online")

    async def async_added_to_hass(self) -> None:
        """Follow hub state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool | None:
        """Device reachable."""
        return self.hub.online

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Connection details."""
        return {
            "last_seen": self.hub.last_seen.isoformat() if self.hub.last_seen else None,
            "last_error": self.hub.last_error,
        }
