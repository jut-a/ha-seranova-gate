"""Persistent SERANOVA cloud connection for one PROGATE system."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import (
    CannotConnect,
    CaptchaRequired,
    InvalidAuth,
    NotSent,
    SeranovaError,
    SeranovaWebApi,
    SocketAuthError,
    SocketIoConnection,
    TwoFactorRequired,
    as_int,
    build_frame,
)
from .const import (
    CLOUD_GRACE_SECONDS,
    CMD_EVENT,
    CMD_INFO,
    CMD_OUTPUT_PULSE,
    CMD_PREPARE,
    CMD_SYSTEM,
    CONF_APP_KEY,
    CONF_IMEI,
    CONF_SYSTEM_NAME,
    CONF_USER_CODE,
    DEFAULT_PULSE_SECONDS,
    DOMAIN,
    ERR_DEVICE_OFFLINE,
    EVENT_DEVICE,
    MANUFACTURER,
    MAX_OUTPUTS,
    MODEL,
    OK_ERRORS,
    PULSE_CONFIRM_TIMEOUT,
    RECONNECT_MIN,
    REFUSAL_ERRORS,
    SEND_INTERVAL,
    SEND_RETRIES,
    WEB_BASE,
    output_key,
)
from .events import parse_event

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


class DeviceRefused(SeranovaError):
    """The cloud/device refused the command (unknown IMEI, App Key, user code)."""

    def __init__(self, code: int) -> None:
        """Keep the ERROR code."""
        super().__init__(REFUSAL_ERRORS.get(code, f"cloud error {code}"))
        self.code = code


class CommandTimeout(SeranovaError):
    """No acknowledgement after all re-sends."""


@dataclass(slots=True)
class OutputConfig:
    """One enabled output from the options."""

    number: int
    name: str
    seconds: int


def outputs_from_options(options: dict[str, Any]) -> list[OutputConfig]:
    """Enabled outputs, in order."""
    outputs = []
    for number in range(1, MAX_OUTPUTS + 1):
        if not options.get(output_key(number, "enabled"), number == 1):
            continue
        name = str(options.get(output_key(number, "name")) or f"Output {number}")
        seconds = int(options.get(output_key(number, "seconds")) or DEFAULT_PULSE_SECONDS)
        outputs.append(OutputConfig(number, name, max(1, seconds)))
    return outputs


@dataclass(slots=True)
class CommandResult:
    """Outcome of an output command.

    fallback_safe is True only when the output command itself was never
    delivered, so another channel (SMS, call) cannot cause a second pulse.
    """

    success: bool
    stage: str | None = None  # "connect", "wake" or "open"
    error: str | None = None
    fallback_safe: bool = False
    acked: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Service response."""
        return asdict(self)


class _Waiter:
    """Collects the frames that answer one command."""

    def __init__(
        self,
        ack_cmd: int | None,
        reply_cmd: int | None = None,
        also_done_on: tuple[int, ...] = (),
    ) -> None:
        self.future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.ack_cmd = ack_cmd
        self.reply_cmd = reply_cmd
        self.also_done_on = also_done_on
        self.acked = False
        # True once a write was attempted past the "not connected" check.
        self.sent = False

    def feed(self, frame: dict[str, Any]) -> None:
        if self.future.done():
            return
        error = as_int(frame.get("ERROR"))
        if error in REFUSAL_ERRORS:
            self.future.set_exception(DeviceRefused(error))
            return
        ack = frame.get("ack")
        if (
            self.ack_cmd is not None
            and isinstance(ack, dict)
            and as_int(ack.get("cmd")) == self.ack_cmd
        ):
            self.acked = True
            if self.reply_cmd is None:
                self.future.set_result(frame)
                return
        cmd = as_int(frame.get("CMD"))
        if (self.reply_cmd is not None and cmd == self.reply_cmd) or (
            cmd is not None and cmd in self.also_done_on
        ):
            self.future.set_result(frame)

    def fail(self, err: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(err)

    def discard(self) -> None:
        if not self.future.done():
            self.future.cancel()
        elif not self.future.cancelled():
            self.future.exception()  # mark retrieved


class SeranovaHub:
    """Owns the socket, the command queue and the device state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: aiohttp.ClientSession,
        web: SeranovaWebApi,
    ) -> None:
        """Set up from the config entry data."""
        self.hass = hass
        self.entry = entry
        self._session = session
        self.web = web
        self.imei = str(entry.data[CONF_IMEI])
        self._app_key = str(entry.data[CONF_APP_KEY])
        self._user_code = str(entry.data[CONF_USER_CODE])
        self.name = entry.data.get(CONF_SYSTEM_NAME) or f"PROGATE {self.imei[-4:]}"

        self.cloud_connected = False
        self.device_online: bool | None = None
        self.last_seen: datetime | None = None
        self.last_error: str | None = None
        self.sysinfo: Any = None
        self.last_frames: dict[str, dict[str, Any]] = {}

        self._conn: SocketIoConnection | None = None
        self._connected = asyncio.Event()
        self._seq = 0
        self._cmd_lock = asyncio.Lock()
        self._waiters: set[_Waiter] = set()
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._closing = False
        self._down_logged = False
        self._established = False
        self._session_started = 0.0
        self._lost_at: float | None = None
        self._grace_unsub: CALLBACK_TYPE | None = None
        # Last socket sessions that ended (for diagnostics).
        self.recent_sessions: deque[dict[str, Any]] = deque(maxlen=20)

    # ---- entity plumbing -------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        """Device registry entry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.imei)},
            name=self.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            serial_number=self.imei,
            configuration_url=f"{WEB_BASE}/dashboard",
        )

    @property
    def online(self) -> bool | None:
        """Device reachable through the cloud (None until first known).

        A socket reconnect within CLOUD_GRACE_SECONDS does not change it.
        """
        if self.cloud_connected:
            return self.device_online
        if self._lost_at is not None and time.monotonic() - self._lost_at < CLOUD_GRACE_SECONDS:
            return self.device_online
        return False if self.device_online is not None else None

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Call update on every state change."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def async_add_event_listener(
        self, handler: Callable[[str, dict[str, Any]], None]
    ) -> CALLBACK_TYPE:
        """Call handler(event_type, data) for every CMD 14 event."""
        self._event_listeners.append(handler)
        return lambda: self._event_listeners.remove(handler)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # ---- connection loop -------------------------------------------------

    async def async_run(self) -> None:
        """Keep a socket open until the entry unloads (runs as a background task)."""
        backoff = RECONNECT_MIN
        while not self._closing:
            self._established = False
            try:
                await self._connect_and_read()
            except (InvalidAuth, TwoFactorRequired) as err:
                self.last_error = f"login rejected: {err!r}"
                _LOGGER.warning("SERANOVA login rejected, asking to re-authenticate")
                self.entry.async_start_reauth(self.hass)
                return
            except (SeranovaError, aiohttp.ClientError, TimeoutError) as err:
                if self._closing:
                    return
                reason = str(err) or type(err).__name__
                if self._established:
                    self._record_session_end(reason)
                else:
                    self.last_error = reason
                    log = _LOGGER.debug if self._down_logged else _LOGGER.warning
                    log("Cannot reach the SERANOVA cloud (%s); retrying", reason)
                    self._down_logged = True
            finally:
                self._drop_connection()
            if self._established:
                # A working session ended (the server closes them routinely):
                # come back quickly and restart the failure backoff.
                backoff = RECONNECT_MIN
                delay = RECONNECT_MIN
            else:
                delay = backoff
                backoff = min(backoff * 2, 300)
            await asyncio.sleep(delay)

    @callback
    def _record_session_end(self, reason: str) -> None:
        now = time.monotonic()
        conn = self._conn
        entry = {
            "ended": dt_util.utcnow().isoformat(),
            "seconds": round(now - self._session_started, 1),
            "reason": reason,
            "pings": conn.pings if conn else None,
            "since_last_send": round(now - conn.last_sent, 1) if conn and conn.last_sent else None,
            "greeting": {
                k: v for k, v in (conn.greeting if conn else {}).items() if k != "sid"
            },
        }
        self.recent_sessions.append(entry)
        _LOGGER.debug("Cloud session ended: %s", entry)

    async def _connect_and_read(self) -> None:
        if self.web.token is None:
            await self.web.login()
        conn = SocketIoConnection(self._session, self.web.token, self._handle_frames)
        try:
            await conn.connect()
        except SocketAuthError:
            token = await self.web.refresh()
            if token is None:
                try:
                    token = await self.web.login()
                except CaptchaRequired as err:
                    raise CannotConnect("login asks for a captcha") from err
            conn = SocketIoConnection(self._session, token, self._handle_frames)
            try:
                await conn.connect()
            except SocketAuthError:
                self.web.token = None
                raise
        self._conn = conn
        self._established = True
        self._session_started = time.monotonic()
        self.cloud_connected = True
        self._lost_at = None
        if self._grace_unsub is not None:
            self._grace_unsub()
            self._grace_unsub = None
        self._connected.set()
        if self._down_logged:
            _LOGGER.info("SERANOVA cloud connection restored")
            self.last_error = None
        self._down_logged = False
        self._notify()
        self.entry.async_create_background_task(
            self.hass, self.async_poll(), f"{DOMAIN} poll after connect"
        )
        try:
            await conn.run()
        except SocketAuthError:
            self.web.token = None
            raise
        finally:
            await conn.close()

    @callback
    def _drop_connection(self) -> None:
        self._connected.clear()
        self._conn = None
        for waiter in list(self._waiters):
            waiter.fail(CannotConnect("cloud connection lost"))
        if self.cloud_connected:
            self.cloud_connected = False
            self._lost_at = time.monotonic()
            # Online keeps its state for the grace period; re-check after it.
            if self._grace_unsub is None and not self._closing:
                self._grace_unsub = async_call_later(
                    self.hass, CLOUD_GRACE_SECONDS + 1, self._async_grace_over
                )

    @callback
    def _async_grace_over(self, _now: datetime) -> None:
        self._grace_unsub = None
        if not self.cloud_connected:
            self._notify()

    async def async_close(self) -> None:
        """Stop reconnecting and close the socket (the HTTP session is shared)."""
        self._closing = True
        if self._grace_unsub is not None:
            self._grace_unsub()
            self._grace_unsub = None
        conn = self._conn
        if conn is not None:
            await conn.close()

    # ---- incoming frames -------------------------------------------------

    @callback
    def _handle_frames(self, frames: list[dict[str, Any]]) -> None:
        changed = False
        for frame in frames:
            uid = frame.get("UID")
            if uid is not None and str(uid) != self.imei:
                continue
            cmd = as_int(frame.get("CMD"))
            error = as_int(frame.get("ERROR"))
            if cmd is not None:
                key = f"CMD{cmd}"
            elif "ack" in frame:
                key = "ack"
            else:
                key = f"ERROR{error}"
            self.last_frames[key] = {"received": dt_util.utcnow().isoformat(), "frame": frame}

            if error == ERR_DEVICE_OFFLINE and self.device_online is not False:
                self.device_online = False
                changed = True
            elif error in REFUSAL_ERRORS:
                self.last_error = REFUSAL_ERRORS[error]
                changed = True
            elif error is not None and error not in OK_ERRORS:
                _LOGGER.debug("Cloud frame with ERROR %s: %s", error, frame)

            if cmd is not None and error != ERR_DEVICE_OFFLINE:
                self.last_seen = dt_util.utcnow()
                self.device_online = True
                changed = True
                if cmd == CMD_INFO:
                    self.sysinfo = frame.get("VAL")
                elif cmd == CMD_EVENT:
                    self._dispatch_event(frame)

            for waiter in list(self._waiters):
                waiter.feed(frame)
        if changed:
            self._notify()

    @callback
    def _dispatch_event(self, frame: dict[str, Any]) -> None:
        event = frame.get("EVENT")
        event_type, data = parse_event(event if isinstance(event, dict) else {})
        for handler in list(self._event_listeners):
            handler(event_type, data)
        self.hass.bus.async_fire(
            EVENT_DEVICE,
            {"config_entry_id": self.entry.entry_id, "type": event_type, **data},
        )

    # ---- commands --------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    async def _ensure_connected(self, timeout: float = 15) -> SocketIoConnection:
        if self._conn is None or not self._conn.connected:
            try:
                async with asyncio.timeout(timeout):
                    await self._connected.wait()
            except TimeoutError as err:
                raise CannotConnect("not connected to the SERANOVA cloud") from err
        assert self._conn is not None
        return self._conn

    async def _send(
        self,
        cmd: int,
        val: Any,
        code: str | None,
        waiter: _Waiter,
        retries: int = SEND_RETRIES,
        interval: float = SEND_INTERVAL,
    ) -> dict[str, Any]:
        """Send a frame, re-sending the same frame (same SEQ) until it is answered."""
        conn = await self._ensure_connected()
        frame = build_frame(self.imei, self._app_key, self._next_seq(), cmd, val, code)
        self._waiters.add(waiter)
        try:
            for _ in range(retries + 1):
                try:
                    await conn.emit(frame)
                except NotSent:
                    raise
                except SeranovaError:
                    waiter.sent = True  # the write may still have reached the server
                    raise
                waiter.sent = True
                try:
                    async with asyncio.timeout(interval):
                        return await asyncio.shield(waiter.future)
                except TimeoutError:
                    continue
            raise CommandTimeout(f"no answer to CMD {cmd} after {retries + 1} sends")
        finally:
            self._waiters.discard(waiter)
            waiter.discard()

    async def async_pulse(self, output: int, seconds: int) -> CommandResult:
        """Pulse an output: CMD 2 (re-sent until acked), then CMD 6 sent once."""
        async with self._cmd_lock:
            try:
                await self._ensure_connected()
            except SeranovaError as err:
                return CommandResult(False, "connect", str(err), fallback_safe=True)

            try:
                await self._send(CMD_PREPARE, None, None, _Waiter(CMD_PREPARE))
            except DeviceRefused as err:
                return CommandResult(False, "wake", str(err), fallback_safe=True)
            except CommandTimeout:
                reason = (
                    "device offline"
                    if self.device_online is False
                    else "device did not answer"
                )
                return CommandResult(False, "wake", reason, fallback_safe=True)
            except SeranovaError as err:
                return CommandResult(False, "wake", str(err), fallback_safe=True)

            # CMD 6 is sent ONCE. The device does not de-duplicate a re-sent frame
            # with the same SEQ: in testing, re-sending every second (waiting for a
            # CMD 10 that never came) opened the gate twice, 4 s apart. The web app
            # re-sends too but gets CMD 10 quickly because its page subscribes to
            # state; this client stops at the ack or the access event.
            waiter = _Waiter(CMD_OUTPUT_PULSE, also_done_on=(CMD_SYSTEM, CMD_EVENT))
            try:
                await self._send(
                    CMD_OUTPUT_PULSE,
                    f"{output}#{seconds}#",
                    self._user_code,
                    waiter,
                    retries=0,
                    interval=PULSE_CONFIRM_TIMEOUT,
                )
            except DeviceRefused as err:
                return CommandResult(False, "open", str(err), fallback_safe=True)
            except SeranovaError as err:
                if not waiter.sent:
                    # Never written to the socket, so it cannot have reached the device.
                    return CommandResult(False, "open", f"not sent ({err})", fallback_safe=True)
                return CommandResult(
                    False,
                    "open",
                    f"sent but not confirmed ({err}); the output may have switched",
                    acked=waiter.acked,
                )
            _LOGGER.debug("Output %s pulsed for %ss", output, seconds)
            return CommandResult(True, "open", acked=waiter.acked)

    async def async_poll(self, _now: datetime | None = None) -> None:
        """Ask the device for its info so the Online sensor stays current."""
        if self._cmd_lock.locked() or self._conn is None:
            return
        async with self._cmd_lock:
            try:
                await self._send(
                    CMD_INFO, "", self._user_code, _Waiter(None, CMD_INFO), retries=2, interval=5
                )
            except DeviceRefused as err:
                self.last_error = str(err)
            except CommandTimeout:
                self.device_online = False
            except SeranovaError:
                return
            finally:
                with contextlib.suppress(Exception):
                    self._notify()
