"""Seranova Gate: the Topkodas PROGATE gate controller via the SERANOVA cloud."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .api import (
    CannotConnect,
    CaptchaRequired,
    InvalidAuth,
    SeranovaWebApi,
    TwoFactorRequired,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_OUTPUT,
    ATTR_SECONDS,
    CONF_POLL_MINUTES,
    DEFAULT_POLL_MINUTES,
    DEFAULT_PULSE_SECONDS,
    DOMAIN,
    MAX_OUTPUTS,
    SERVICE_OPEN,
)
from .hub import SeranovaHub, outputs_from_options
from .session import async_get_session

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.EVENT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SeranovaConfigEntry = ConfigEntry[SeranovaHub]

OPEN_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_OUTPUT, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_OUTPUTS)
        ),
        vol.Optional(ATTR_SECONDS): vol.All(vol.Coerce(int), vol.Range(min=1, max=600)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the seranova_gate.open action."""

    async def _async_open(call: ServiceCall) -> ServiceResponse:
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
            loaded = [entry for entry in loaded if entry.entry_id == entry_id]
        if not loaded:
            raise ServiceValidationError("No loaded Seranova Gate entry")
        if len(loaded) > 1:
            raise ServiceValidationError(
                "Several Seranova systems are set up; pass config_entry_id"
            )
        entry: SeranovaConfigEntry = loaded[0]
        output = call.data[ATTR_OUTPUT]
        seconds = call.data.get(ATTR_SECONDS)
        if seconds is None:
            configured = {o.number: o for o in outputs_from_options(dict(entry.options))}
            seconds = (
                configured[output].seconds if output in configured else DEFAULT_PULSE_SECONDS
            )
        result = await entry.runtime_data.async_pulse(output, seconds)
        if call.return_response:
            return result.as_dict()
        if not result.success:
            raise HomeAssistantError(f"Gate command failed at '{result.stage}': {result.error}")
        return None

    hass.services.async_register(
        DOMAIN,
        SERVICE_OPEN,
        _async_open,
        schema=OPEN_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SeranovaConfigEntry) -> bool:
    """Log in, open the persistent cloud connection and add the entities."""
    session = async_get_session(hass)
    web = SeranovaWebApi(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    try:
        await web.login()
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed("SERANOVA rejected the e-mail or password") from err
    except TwoFactorRequired as err:
        raise ConfigEntryError("The SERANOVA account requires 2FA, which is not supported") from err
    except (CannotConnect, CaptchaRequired) as err:
        raise ConfigEntryNotReady(f"SERANOVA login failed: {err}") from err

    hub = SeranovaHub(hass, entry, session, web)
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_create_background_task(
        hass, hub.async_run(), f"{DOMAIN} cloud connection {hub.imei[-4:]}"
    )
    poll_minutes = int(entry.options.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES))
    if poll_minutes > 0:
        entry.async_on_unload(
            async_track_time_interval(hass, hub.async_poll, timedelta(minutes=poll_minutes))
        )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SeranovaConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SeranovaConfigEntry) -> bool:
    """Unload the entities and close the connection."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok
