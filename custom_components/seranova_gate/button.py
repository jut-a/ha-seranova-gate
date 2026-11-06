"""Buttons that pulse the PROGATE outputs."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SeranovaConfigEntry
from .entity import SeranovaEntity
from .hub import OutputConfig, SeranovaHub, outputs_from_options


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeranovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One button per enabled output; drop buttons of outputs disabled since."""
    hub = entry.runtime_data
    outputs = outputs_from_options(dict(entry.options))
    wanted = {f"{hub.imei}_out{output.number}" for output in outputs}
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "button" and reg_entry.unique_id not in wanted:
            registry.async_remove(reg_entry.entity_id)
    async_add_entities(SeranovaOutputButton(hub, output) for output in outputs)


class SeranovaOutputButton(SeranovaEntity, ButtonEntity):
    """Pulse one output (e.g. open the gate)."""

    _attr_icon = "mdi:gate-open"

    def __init__(self, hub: SeranovaHub, output: OutputConfig) -> None:
        """Name and pulse length come from the options."""
        super().__init__(hub, f"out{output.number}")
        self._output = output
        self._attr_name = output.name

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Which output and how long."""
        return {"output": self._output.number, "pulse_seconds": self._output.seconds}

    async def async_press(self) -> None:
        """Send the pulse; raise if the cloud could not confirm it."""
        result = await self.hub.async_pulse(self._output.number, self._output.seconds)
        if not result.success:
            raise HomeAssistantError(
                f"{self._output.name}: command failed at '{result.stage}': {result.error}"
            )
