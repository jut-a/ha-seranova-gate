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
CONF_PULSE_LOCKOUT: Final = "pulse_lockout"
CONF_EVENT_LOG_DAYS: Final = "event_log_days"

DEFAULT_APP_KEY: Final = "123456"
DEFAULT_POLL_MINUTES: Final = 10
DEFAULT_PULSE_SECONDS: Final = 2
# Minimum seconds between two pulses (also after any access the device reports):
# on an open/stop/close gate input a second pulse stops or reverses the gate.
DEFAULT_PULSE_LOCKOUT: Final = 30
# Exception: once the device confirmed our pulse with its access event, a NEW press
# (made after that event) is allowed this many seconds after it, so the gate can be
# stopped or closed without waiting out the minimum time. A press made before the
# event (a double tap, a queued call) still waits the full minimum time.
CONFIRMED_PULSE_GUARD: Final = 5
MAX_OUTPUTS: Final = 4
# Days the event audit log keeps; 0 keeps every event forever.
DEFAULT_EVENT_LOG_DAYS: Final = 0

# Frame CMD numbers (MSG.CMD when sending, CMD when receiving).
CMD_INFO: Final = 1  # request / reply: system info (VAL)
CMD_PREPARE: Final = 2  # sent before every output command, without CODE
CMD_OUTPUT_PULSE: Final = 6  # VAL "<n>#<seconds>#"
CMD_SYSTEM: Final = 10  # request (CODE, VAL "") / reply + pushed: outputs / system state
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
# CMD 6 is sent once and waits this long for the device's access event; the
# cloud's ack alone does not prove the device got the command.
PULSE_CONFIRM_TIMEOUT: Final = 10.0

# Wake: before every pulse (and on each poll) ask the device for its state with
# CMD 10, as an open web page does, and wait for the device's own answer. The
# cloud acks every command at once, but in field tests a CMD 6 sent without this
# reached the device about one time in three. CMD 10 is a read, safe to re-send.
WAKE_RETRIES: Final = 6  # extra sends: 7 in all
WAKE_INTERVAL: Final = 3.0  # seconds between sends, so about 21 s before giving up
# A CMD 10 frame proves the device answered only when its own clock (TIME, UTC
# epoch) is this recent; an older one is a stored copy, not an answer.
STATE_MAX_AGE: Final = 90

# Frame counter (SEQ): stored across restarts and never reused. Reusing a small
# SEQ after a restart made the device skip a CMD 6, or run it several times.
SEQ_STORAGE_VERSION: Final = 1
SEQ_MIN: Final = 1000
SEQ_MAX: Final = 60000
SEQ_RESTART_GAP: Final = 100  # skip values a crash may have used before saving
SEQ_SAVE_DELAY: Final = 10

# Reconnect this soon after a working session ends. Failed connects, and sessions the
# server closes within SHORT_SESSION_SECONDS, back off exponentially up to RECONNECT_MAX:
# with the device offline the cloud closed every session within 0.3 s, and a fixed
# 2 s retry opened 300+ sessions in 13 minutes. A command that needs the connection
# cuts the wait short.
RECONNECT_MIN: Final = 2
RECONNECT_MAX: Final = 300
SHORT_SESSION_SECONDS: Final = 30
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

# Event audit log: every device event, kept in HA storage (outside the recorder, so
# never purged) under the device's IMEI, so it survives removing and re-adding the entry.
EVENT_LOG_STORAGE_VERSION: Final = 1
EVENT_LOG_SAVE_DELAY: Final = 2

SERVICE_OPEN: Final = "open"
SERVICE_GET_EVENTS: Final = "get_events"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_OUTPUT: Final = "output"
ATTR_SECONDS: Final = "seconds"
ATTR_SINCE: Final = "since"
ATTR_UNTIL: Final = "until"
ATTR_EVENT_TYPE: Final = "event_type"
ATTR_LIMIT: Final = "limit"
DEFAULT_EVENTS_LIMIT: Final = 100
MAX_EVENTS_LIMIT: Final = 10000


def output_key(number: int, field: str) -> str:
    """Option key for one output field, e.g. out1_name."""
    return f"out{number}_{field}"


DEFAULT_OPTIONS: Final[dict] = {
    CONF_POLL_MINUTES: DEFAULT_POLL_MINUTES,
    CONF_PULSE_LOCKOUT: DEFAULT_PULSE_LOCKOUT,
    CONF_EVENT_LOG_DAYS: DEFAULT_EVENT_LOG_DAYS,
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
