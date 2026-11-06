"""Event entity fed by the device's pushed CMD 14 events."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SeranovaConfigEntry
from .const import EVENT_TYPES
from .entity import SeranovaEntity
from .hub import SeranovaHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeranovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the event entity."""
    async_add_entities([SeranovaDeviceEvent(entry.runtime_data)])


class SeranovaDeviceEvent(SeranovaEntity, EventEntity):
    """Last device event (Contact ID code + text)."""

    _attr_translation_key = "device_event"
    _attr_event_types = EVENT_TYPES

    def __init__(self, hub: SeranovaHub) -> None:
        """Bind to the hub."""
        super().__init__(hub, "event")

    async def async_added_to_hass(self) -> None:
        """Listen for pushed events."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        self._trigger_event(event_type, data)
        self.async_write_ha_state()
