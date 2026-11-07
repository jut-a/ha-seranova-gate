"""Diagnostics: connection state and the last frame of each kind (redacted)."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import SeranovaConfigEntry
from .const import CONF_APP_KEY, CONF_IMEI, CONF_SYSTEM_NAME, CONF_USER_CODE

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_APP_KEY,
    CONF_USER_CODE,
    CONF_IMEI,
    CONF_SYSTEM_NAME,
    "IMEI",
    "KEY",
    "CODE",
    "UID",
    "TEL",
    "email",
    "token",
    # Event text and notes carry user and object names.
    "TXT",
    "NOTE",
    "SysName",
    "address",
}

# The CMD 1 VAL string carries identifiers inline: "...,IMEI:…,ICCID:…,APPKEY:…,NAME:…"
_INLINE_SECRETS = re.compile(r"\b(IMEI|ICCID|APPKEY|SN|TEL|OBJ|NAME):[^,#]*")


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _INLINE_SECRETS.sub(r"\1:**REDACTED**", value)
    if isinstance(value, dict):
        return {key: _redact_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_strings(item) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SeranovaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one entry."""
    hub = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "state": {
            "cloud_connected": hub.cloud_connected,
            "device_online": hub.device_online,
            "last_seen": hub.last_seen.isoformat() if hub.last_seen else None,
            "last_error": hub.last_error,
            "pulse_lockout_s": hub._lockout,
            "next_seq": hub._seq,
        },
        "stats": dict(hub.stats),
        "recent_commands": list(hub.recent_commands),
        "recent_state_checks": list(hub.recent_state_checks),
        "recent_events": list(hub.recent_events),
        "recent_sessions": list(hub.recent_sessions),
        "sysinfo": _redact_strings(hub.sysinfo),
        "last_frames": _redact_strings(async_redact_data(hub.last_frames, TO_REDACT)),
    }
