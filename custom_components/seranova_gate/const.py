"""Constants for the Seranova Gate (Topkodas PROGATE) integration.

The cloud protocol here was decoded from the SERANOVA web app bundle
(seranova.eu/dist/js/app.*.js); see docs/PROTOCOL.md in the repository.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "seranova_gate"
MANUFACTURER: Final = "Topkodas"
MODEL: Final = "PROGATE"

WEB_BASE: Final = "https://seranova.eu"
WS_URL: Final = "wss://cloud.topkodas.lt:10004/socket.io/?EIO=4&transport=websocket"
USER_AGENT: Final = "Mozilla/5.0 (compatible; HomeAssistant seranova_gate)"

CONF_IMEI: Final = "imei"
CONF_APP_KEY: Final = "app_key"
CONF_USER_CODE: Final = "user_code"
CONF_SYSTEM_NAME: Final = "system_name"
CONF_POLL_MINUTES: Final = "poll_minutes"

DEFAULT_APP_KEY: Final = "123456"
DEFAULT_POLL_MINUTES: Final = 10
DEFAULT_PULSE_SECONDS: Final = 2
MAX_OUTPUTS: Final = 4

# Frame CMD numbers (MSG.CMD when sending, CMD when receiving).
CMD_INFO: Final = 1  # request / reply: system info (VAL)
CMD_PREPARE: Final = 2  # sent before every output command, without CODE
CMD_OUTPUT_PULSE: Final = 6  # VAL "<n>#<seconds>#"
CMD_SYSTEM: Final = 10  # pushed: outputs / system state
CMD_EVENT: Final = 14  # pushed: EVENT {CID, TXT}
# More commands (5 = output level, 11 = sensors) are listed in docs/PROTOCOL.md.

# Cloud ERROR field values.
ERR_UID_NOT_FOUND: Final = 2
ERR_DEVICE_OFFLINE: Final = 3
ERR_WRONG_APP_KEY: Final = 4
ERR_WRONG_USER_CODE: Final = 5
OK_ERRORS: Final = frozenset({0, 1, 6})
REFUSAL_ERRORS: Final = {
    ERR_UID_NOT_FOUND: "the cloud does not know this IMEI",
    ERR_WRONG_APP_KEY: "the device rejected the App Key",
    ERR_WRONG_USER_CODE: "the device rejected the user code",
}

# The web app re-sends the same frame every second, up to 10 extra times.
# Used for CMD 2 (prepare) only: re-sending CMD 6 makes the device pulse again.
SEND_RETRIES: Final = 10
SEND_INTERVAL: Final = 1.0
# CMD 6 is sent once and waits this long for its ack / the access event.
PULSE_CONFIRM_TIMEOUT: Final = 10.0

# Reconnect this soon after a working session ends; failed attempts back off to 300 s.
RECONNECT_MIN: Final = 2
# The Online sensor keeps its state this long while the socket reconnects.
CLOUD_GRACE_SECONDS: Final = 60

EVENT_DEVICE: Final = f"{DOMAIN}_event"
EVENT_TYPES: Final = [
    "access_granted",
    "access_denied",
    "alarm",
    "trouble",
    "restore",
    "test",
    "event",
]

SERVICE_OPEN: Final = "open"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_OUTPUT: Final = "output"
ATTR_SECONDS: Final = "seconds"


def output_key(number: int, field: str) -> str:
    """Option key for one output field, e.g. out1_name."""
    return f"out{number}_{field}"


DEFAULT_OPTIONS: Final[dict] = {
    CONF_POLL_MINUTES: DEFAULT_POLL_MINUTES,
    **{
        key: value
        for n in range(1, MAX_OUTPUTS + 1)
        for key, value in (
            (output_key(n, "enabled"), n == 1),
            (output_key(n, "name"), "Gate" if n == 1 else f"Output {n}"),
            (output_key(n, "seconds"), DEFAULT_PULSE_SECONDS),
        )
    },
}
