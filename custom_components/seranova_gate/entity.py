"""Base entity for Seranova Gate."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .hub import SeranovaHub


class SeranovaEntity(Entity):
    """Entity attached to one PROGATE device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: SeranovaHub, key: str) -> None:
        """Bind to the hub."""
        self.hub = hub
        self._attr_unique_id = f"{hub.imei}_{key}"
        self._attr_device_info = hub.device_info
