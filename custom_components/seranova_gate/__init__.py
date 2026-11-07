"""Seranova Gate: the Topkodas PROGATE gate controller via the SERANOVA cloud."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

import aiohttp
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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .api import (
    CannotConnect,
    CaptchaRequired,
    InvalidAuth,
    SeranovaWebApi,
    TwoFactorRequired,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_EVENT_TYPE,
    ATTR_LIMIT,
    ATTR_OUTPUT,
    ATTR_SECONDS,
    ATTR_SINCE,
    ATTR_UNTIL,
    CONF_POLL_MINUTES,
    DEFAULT_EVENTS_LIMIT,
    DEFAULT_POLL_MINUTES,
    DEFAULT_PULSE_SECONDS,
    DOMAIN,
    EVENT_TYPES,
    MAX_EVENTS_LIMIT,
    MAX_OUTPUTS,
    SEQ_STORAGE_VERSION,
    SERVICE_GET_EVENTS,
    SERVICE_OPEN,
)
from .hub import (
    DeviceUnavailable,
    LoginRejected,
    SeranovaHub,
    outputs_from_options,
    seq_storage_key,
)
from .session import async_get_session

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.EVENT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Setup waits this long for the gate controller to answer a state request: socket
# connect (up to 20 s) + CMD 1 (3 x 5 s) + the state request (7 x 3 s) = 56 s worst case.
SETUP_READY_TIMEOUT = 75
# One signed-in SERANOVA web session per account, kept across setup retries and
# reloads: a fresh REST login on every retry (every ~80 s while the gate controller
# is offline) would add up to hundreds of logins a night and trigger the captcha.
DATA_WEB_SESSIONS = f"{DOMAIN}_web_sessions"


def _web_api(
    hass: HomeAssistant, session: aiohttp.ClientSession, email: str, password: str
) -> SeranovaWebApi:
    """The kept web session for this account (a new one if the password changed)."""
    sessions: dict[str, SeranovaWebApi] = hass.data.setdefault(DATA_WEB_SESSIONS, {})
    web = sessions.get(email)
    if web is None or not web.signs_in_as(email, password):
        web = sessions[email] = SeranovaWebApi(session, email, password)
    return web

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


GET_EVENTS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_SINCE): cv.datetime,
        vol.Optional(ATTR_UNTIL): cv.datetime,
        vol.Optional(ATTR_EVENT_TYPE): vol.In(EVENT_TYPES),
        vol.Optional(ATTR_LIMIT, default=DEFAULT_EVENTS_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_EVENTS_LIMIT)
        ),
    }
)


def _loaded_entry(hass: HomeAssistant, call: ServiceCall) -> SeranovaConfigEntry:
    """The one loaded entry the call is for."""
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
        raise ServiceValidationError("Several Seranova systems are set up; pass config_entry_id")
    return loaded[0]


def _as_utc(value: datetime | None) -> datetime | None:
    """A datetime from the action; one without a time zone is local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(value)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the seranova_gate.open and seranova_gate.get_events actions."""

    async def _async_open(call: ServiceCall) -> ServiceResponse:
        entry = _loaded_entry(hass, call)
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

    async def _async_get_events(call: ServiceCall) -> ServiceResponse:
        hub = _loaded_entry(hass, call).runtime_data
        events = hub.get_events(
            since=_as_utc(call.data.get(ATTR_SINCE)),
            until=_as_utc(call.data.get(ATTR_UNTIL)),
            event_type=call.data.get(ATTR_EVENT_TYPE),
            limit=call.data[ATTR_LIMIT],
        )
        return {"events": events, "count": len(events), "logged": len(hub.event_log)}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_EVENTS,
        _async_get_events,
        schema=GET_EVENTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SeranovaConfigEntry) -> bool:
    """Log in, open the persistent cloud connection and add the entities."""
    session = async_get_session(hass)
    web = _web_api(hass, session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    try:
        # Reuse a kept token; the connection refreshes it or signs in again only
        # when the cloud rejects it.
        if web.token is None:
            await web.login()
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed("SERANOVA rejected the e-mail or password") from err
    except TwoFactorRequired as err:
        raise ConfigEntryError("The SERANOVA account requires 2FA, which is not supported") from err
    except (CannotConnect, CaptchaRequired) as err:
        raise ConfigEntryNotReady(f"SERANOVA login failed: {err}") from err

    hub = SeranovaHub(hass, entry, session, web)
    await hub.async_load_seq()
    await hub.async_load_event_log()
    entry.async_create_background_task(
        hass, hub.async_run(), f"{DOMAIN} cloud connection {hub.imei[-4:]}"
    )
    # Only a gate controller that answers a state request counts as set up; until
    # then Home Assistant shows "Retrying setup" with the reason and retries itself.
    try:
        await hub.async_wait_ready(SETUP_READY_TIMEOUT)
    except LoginRejected as err:
        await hub.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except DeviceUnavailable as err:
        await hub.async_close()
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    poll_minutes = int(entry.options.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES))
    if poll_minutes > 0:
        entry.async_on_unload(
            async_track_time_interval(hass, hub.async_poll, timedelta(minutes=poll_minutes))
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SeranovaConfigEntry) -> bool:
    """Unload the entities and close the connection."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: SeranovaConfigEntry) -> None:
    """Delete the stored frame counter with the entry."""
    await Store(hass, SEQ_STORAGE_VERSION, seq_storage_key(entry.entry_id)).async_remove()
