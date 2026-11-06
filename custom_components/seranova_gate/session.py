"""One shared HTTP session for the integration.

Created through Home Assistant (closed by HA at shutdown, never by us) and
without a cookie jar: SeranovaWebApi keeps the seranova.eu cookies per account,
so several accounts and the config flow never mix them.
"""

from __future__ import annotations

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN

_DATA_SESSION = f"{DOMAIN}_session"


@callback
def async_get_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return the integration's shared session, creating it once."""
    session: aiohttp.ClientSession | None = hass.data.get(_DATA_SESSION)
    if session is None or session.closed:
        session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
        hass.data[_DATA_SESSION] = session
    return session
