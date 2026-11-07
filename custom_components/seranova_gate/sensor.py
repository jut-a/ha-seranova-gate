"""Last device event: its text, e.g. "Access Gained by John (User 1)", and its time."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import RestoreSensor, SensorDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SeranovaConfigEntry
from .entity import SeranovaEntity
from .hub import SeranovaHub, event_time, logged_time

# Attributes carried from the decoded event (the full set stays on the event entity).
_ATTRS = ("event_type", "subject", "user_or_zone", "code", "device_time")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeranovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Last event and Last event time sensors."""
    hub = entry.runtime_data
    async_add_entities([SeranovaLastEventSensor(hub), SeranovaLastEventTimeSensor(hub)])


class SeranovaLastEventSensor(SeranovaEntity, RestoreSensor):
    """Text of the last device event; restored across restarts."""

    _attr_translation_key = "last_event"
    # Record every event, also when the same text repeats (same user opens twice).
    _attr_force_update = True

    def __init__(self, hub: SeranovaHub) -> None:
        """Bind to the hub."""
        super().__init__(hub, "last_event")
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the last text, then follow new events."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = data.native_value
        if (state := await self.async_get_last_state()) is not None:
            self._attr_extra_state_attributes = {
                key: state.attributes[key] for key in _ATTRS if key in state.attributes
            }
        self.async_on_remove(self.hub.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        self._attr_native_value = str(data.get("message") or "")[:255]
        self._attr_extra_state_attributes = {
            "event_type": event_type,
            **{key: data.get(key) for key in _ATTRS if key != "event_type"},
        }
        self.async_write_ha_state()


class SeranovaLastEventTimeSensor(SeranovaEntity, RestoreSensor):
    """When the last device event happened, by the gate controller's own clock.

    Restored across restarts; on its first start it takes the newest entry of the
    event log.
    """

    _attr_translation_key = "last_event_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub: SeranovaHub) -> None:
        """Bind to the hub."""
        super().__init__(hub, "last_event_time")

    async def async_added_to_hass(self) -> None:
        """Restore the last time (or take it from the event log), then follow events."""
        await super().async_added_to_hass()
        data = await self.async_get_last_sensor_data()
        if data is not None and isinstance(data.native_value, datetime):
            self._attr_native_value = data.native_value
        else:
            stamps = [t for e in self.hub.event_log if (t := logged_time(e)) is not None]
            self._attr_native_value = max(stamps) if stamps else None
        self.async_on_remove(self.hub.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event_type: str, data: dict[str, Any]) -> None:
        self._attr_native_value = event_time(data, dt_util.utcnow())
        self.async_write_ha_state()
