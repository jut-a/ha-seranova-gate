"""Persistent SERANOVA cloud connection for one PROGATE system."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import contextlib
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
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
    CONF_PULSE_LOCKOUT,
    CONF_SYSTEM_NAME,
    CONF_USER_CODE,
    CONFIRMED_PULSE_GUARD,
    DEFAULT_PULSE_LOCKOUT,
    DEFAULT_PULSE_SECONDS,
    DOMAIN,
    ERR_DEVICE_OFFLINE,
    EVENT_DEVICE,
    MANUFACTURER,
    MAX_OUTPUTS,
    MODEL,
    PULSE_CONFIRM_TIMEOUT,
    RECONNECT_MIN,
    REFUSAL_ERRORS,
    SEND_INTERVAL,
    SEND_RETRIES,
    SEQ_MAX,
    SEQ_MIN,
    SEQ_RESTART_GAP,
    SEQ_SAVE_DELAY,
    SEQ_STORAGE_VERSION,
    STATE_MAX_AGE,
    WAKE_INTERVAL,
    WAKE_RETRIES,
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


def _frame_kind(frame: dict[str, Any]) -> str:
    """Short, payload-free description of a frame for debug logs."""
    parts = []
    if (cmd := as_int(frame.get("CMD"))) is not None:
        parts.append(f"CMD {cmd}")
    if isinstance(ack := frame.get("ack"), dict):
        parts.append(f"ack cmd {ack.get('cmd')}")
    if (error := as_int(frame.get("ERROR"))) is not None:
        parts.append(f"ERROR {error}")
    return ", ".join(parts) or "other"


def seq_storage_key(entry_id: str) -> str:
    """Storage key of one entry's frame counter."""
    return f"{DOMAIN}.{entry_id}.seq"


def pulse_confirmation(frame: dict[str, Any]) -> str | None:
    """What proves a pulse reached the device: its own access-granted event.

    The cloud acks CMD 6 within a fraction of a second whether or not the device
    ever gets it, and CMD 10 status frames also arrive while an app polls, so
    neither counts.
    """
    if as_int(frame.get("CMD")) != CMD_EVENT:
        return None
    event = frame.get("EVENT")
    if isinstance(event, dict) and parse_event(event)[0] == "access_granted":
        return "access_event"
    return None


def state_age(frame: dict[str, Any], now: float) -> float | None:
    """Seconds since the device stamped a CMD 10 frame (None: not CMD 10 or no TIME)."""
    if as_int(frame.get("CMD")) != CMD_SYSTEM:
        return None
    stamp = as_int(frame.get("TIME"))
    return None if stamp is None else now - stamp


def is_fresh_state(frame: dict[str, Any], now: float) -> bool:
    """A CMD 10 frame the device stamped within STATE_MAX_AGE (either way)."""
    age = state_age(frame, now)
    return age is not None and abs(age) <= STATE_MAX_AGE


class StateCheck:
    """Confirms a CMD 10 request: only the device's own, freshly stamped state frame.

    The cloud acks the request at once and may hand out a stored copy, so neither
    the ack nor an old frame proves the device answered.
    """

    def __init__(self) -> None:
        """Nothing seen yet."""
        self.stale = 0
        self.lag: float | None = None

    def __call__(self, frame: dict[str, Any]) -> str | None:
        """Label for a fresh state frame, else None."""
        age = state_age(frame, time.time())
        if age is None:
            return None
        if abs(age) > STATE_MAX_AGE:
            self.stale += 1
            return None
        self.lag = round(age, 1)
        return "device_state"


@dataclass(slots=True)
class CommandResult:
    """Outcome of an output command.

    success is True only when the device itself reported the access.
    fallback_safe is True only when the output command itself was never
    delivered, so another channel (SMS, call) cannot cause a second pulse.
    """

    success: bool
    stage: str | None = None  # "locked", "connect", "wake" or "open"
    error: str | None = None
    fallback_safe: bool = False
    acked: bool = False  # the cloud acknowledged CMD 6 (not proof of delivery)
    confirmed_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Service response."""
        return asdict(self)


class _Waiter:
    """Collects the frames that answer one command."""

    def __init__(
        self,
        ack_cmd: int | None,
        reply_cmd: int | None = None,
        confirm: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> None:
        """Done on the ack, on a reply_cmd frame, or (when given) only on confirm."""
        self.future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.ack_cmd = ack_cmd
        self.reply_cmd = reply_cmd
        self.confirm = confirm
        self.confirmed_by: str | None = None
        self.acked = False
        # True once a write was attempted past the "not connected" check.
        self.sent = False
        # For the diagnostics history (monotonic clock).
        self.seq: int | None = None
        self.attempts = 0
        self.sent_at: float | None = None
        self.acked_at: float | None = None
        self.done_at: float | None = None

    def _finish(self, frame: dict[str, Any]) -> None:
        self.done_at = time.monotonic()
        self.future.set_result(frame)

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
            if self.acked_at is None:
                self.acked_at = time.monotonic()
            if self.reply_cmd is None and self.confirm is None:
                self._finish(frame)
                return
        if self.confirm is not None:
            if (label := self.confirm(frame)) is not None:
                self.confirmed_by = label
                self._finish(frame)
            return
        if self.reply_cmd is not None and as_int(frame.get("CMD")) == self.reply_cmd:
            self._finish(frame)

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
        self._seq = SEQ_MIN
        self._seq_store: Store[dict[str, int]] = Store(
            hass, SEQ_STORAGE_VERSION, seq_storage_key(entry.entry_id)
        )
        self._lockout = int(entry.options.get(CONF_PULSE_LOCKOUT, DEFAULT_PULSE_LOCKOUT))
        self._last_pulse_at: float | None = None
        # When the device's access event confirmed that pulse (None: not confirmed).
        self._confirmed_at: float | None = None
        self._last_access_at: float | None = None
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
        # Diagnostics: what happened recently (no names, codes or identifiers).
        self.recent_sessions: deque[dict[str, Any]] = deque(maxlen=20)
        self.recent_commands: deque[dict[str, Any]] = deque(maxlen=20)
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=20)
        self.recent_state_checks: deque[dict[str, Any]] = deque(maxlen=20)
        self._device_checked = False
        self.stats: dict[str, int] = dict.fromkeys(
            (
                "pulses_requested",
                "pulses_confirmed",
                "pulses_unconfirmed",
                "pulses_refused",
                "pulses_not_sent",
                "state_checks",
                "state_checks_unanswered",
                "events_received",
                "sessions_ended",
                "connect_failures",
            ),
            0,
        )

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
                    self.stats["connect_failures"] += 1
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
        self.stats["sessions_ended"] += 1
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
        _LOGGER.debug(
            "Cloud session established (ping every %s ms)", conn.greeting.get("pingInterval")
        )
        self._notify()
        self.entry.async_create_background_task(
            self.hass, self._async_after_connect(), f"{DOMAIN} after connect"
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
        await self._seq_store.async_save(self._seq_data())

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
            _LOGGER.debug("Frame received: %s", _frame_kind(frame))
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

            if cmd is not None and error != ERR_DEVICE_OFFLINE:
                changed = True
                if cmd == CMD_INFO:
                    # Answered within seconds even while the device runs no commands:
                    # most likely the cloud's copy of the device's connect message, so
                    # it does not prove the device is reachable.
                    self.sysinfo = frame.get("VAL")
                elif cmd != CMD_SYSTEM or is_fresh_state(frame, time.time()):
                    # A device frame: an event, or state stamped by the device's clock.
                    self.last_seen = dt_util.utcnow()
                    self.device_online = True
                    if cmd == CMD_EVENT:
                        self._dispatch_event(frame)

            for waiter in list(self._waiters):
                waiter.feed(frame)
        if changed:
            self._notify()

    @callback
    def _dispatch_event(self, frame: dict[str, Any]) -> None:
        event = frame.get("EVENT")
        event_type, data = parse_event(event if isinstance(event, dict) else {})
        if event_type == "access_granted":
            self._last_access_at = time.monotonic()
        self.stats["events_received"] += 1
        self.recent_events.append(
            {
                "received": dt_util.utcnow().isoformat(),
                "event_type": event_type,
                **{
                    key: data.get(key)
                    for key in ("code", "partition", "user_or_zone", "device_time")
                },
            }
        )
        for handler in list(self._event_listeners):
            handler(event_type, data)
        self.hass.bus.async_fire(
            EVENT_DEVICE,
            {"config_entry_id": self.entry.entry_id, "type": event_type, **data},
        )

    # ---- commands --------------------------------------------------------

    async def async_load_seq(self) -> None:
        """Continue the frame counter where the last run stopped; never reuse a SEQ."""
        data = await self._seq_store.async_load()
        stored = data.get("seq") if isinstance(data, dict) else None
        if isinstance(stored, int):
            start = stored + SEQ_RESTART_GAP
        else:  # first run: a time-based start no earlier run can have used
            start = SEQ_MIN + int(time.time()) % (SEQ_MAX - SEQ_MIN - SEQ_RESTART_GAP)
        self._seq = start if SEQ_MIN <= start < SEQ_MAX else SEQ_MIN
        await self._seq_store.async_save(self._seq_data())

    def _seq_data(self) -> dict[str, int]:
        return {"seq": self._seq}

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = seq + 1 if seq + 1 < SEQ_MAX else SEQ_MIN
        self._seq_store.async_delay_save(self._seq_data, SEQ_SAVE_DELAY)
        return seq

    def _lockout_remaining(self, requested_at: float) -> float:
        """Seconds until a pulse requested at `requested_at` is allowed (0 = allowed).

        The minimum time runs from our last pulse and from any access event, with one
        exception: once the device confirmed our pulse with its access event, a press
        made after that event waits only CONFIRMED_PULSE_GUARD seconds from it. A
        press made before it (a double tap, a queued call) waits the full time, and
        any other access event (another user, or the device repeating a command)
        restarts the full time, because the two cannot be told apart.
        """
        if self._lockout <= 0:
            return 0
        now = time.monotonic()
        waits = [0.0]
        confirmed = self._confirmed_at
        if self._last_pulse_at is not None:
            if confirmed is not None and requested_at > confirmed:
                guard = min(CONFIRMED_PULSE_GUARD, self._lockout)
                waits.append(guard - (now - confirmed))
            else:
                waits.append(self._lockout - (now - self._last_pulse_at))
        access = self._last_access_at
        if access is not None and (confirmed is None or access > confirmed):
            waits.append(self._lockout - (now - access))
        return max(waits)

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
        retries: int | None = None,
        interval: float | None = None,
    ) -> dict[str, Any]:
        """Send a frame, re-sending the same frame (same SEQ) until it is answered."""
        # Defaults resolved at call time (tests shorten them on the module).
        retries = SEND_RETRIES if retries is None else retries
        interval = SEND_INTERVAL if interval is None else interval
        conn = await self._ensure_connected()
        frame = build_frame(self.imei, self._app_key, self._next_seq(), cmd, val, code)
        waiter.seq = frame["MSG"]["SEQ"]
        self._waiters.add(waiter)
        try:
            for attempt in range(1, retries + 2):
                waiter.attempts = attempt
                _LOGGER.debug(
                    "Sending CMD %s seq %s (attempt %s/%s)", cmd, waiter.seq, attempt, retries + 1
                )
                try:
                    await conn.emit(frame)
                except NotSent:
                    raise
                except SeranovaError:
                    waiter.sent = True  # the write may still have reached the server
                    raise
                waiter.sent = True
                if waiter.sent_at is None:
                    waiter.sent_at = time.monotonic()
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
        """Pulse an output: wake (CMD 10 until the device answers), CMD 2, CMD 6 once.

        CMD 10 and CMD 2 may be re-sent; CMD 6 never is. Nothing that can switch an
        output is sent before the device itself has answered the state request.
        Success needs the device's own access event; a pulse within the lockout
        of the previous one (or of any access the device reported) is refused.
        Every attempt is recorded for the diagnostics.
        """
        # Timed from the request: a press queued behind another pulse counts its wait.
        started = time.monotonic()
        started_wall = dt_util.utcnow()
        trace: dict[str, _Waiter] = {}
        result = await self._pulse(output, seconds, trace, started)
        self._record_pulse(output, seconds, result, trace, started, started_wall)
        return result

    @callback
    def _record_pulse(
        self,
        output: int,
        seconds: int,
        result: CommandResult,
        trace: dict[str, _Waiter],
        started: float,
        started_wall: datetime,
    ) -> None:
        def after(mark: float | None) -> float | None:
            return None if mark is None else round(mark - started, 2)

        wake, prepare, pulse = trace.get("wake"), trace.get("prepare"), trace.get("pulse")
        check = wake.confirm if wake and isinstance(wake.confirm, StateCheck) else None
        entry = {
            "time": started_wall.isoformat(),
            "output": output,
            "seconds": seconds,
            "success": result.success,
            "stage": result.stage,
            "fallback_safe": result.fallback_safe,
            "confirmed_by": result.confirmed_by,
            "error": result.error,
            "wake_sends": wake.attempts if wake else 0,
            "wake_answered_after_s": after(wake.done_at) if wake else None,
            "device_lag_s": check.lag if check else None,
            "stale_state_replies": check.stale if check else 0,
            "prepare_sends": prepare.attempts if prepare else 0,
            "prepare_acked_after_s": after(prepare.done_at) if prepare else None,
            "pulse_seq": pulse.seq if pulse and pulse.sent else None,
            "pulse_sent_after_s": after(pulse.sent_at) if pulse else None,
            "cloud_ack_after_s": after(pulse.acked_at) if pulse else None,
            "confirmed_after_s": after(pulse.done_at) if pulse and result.success else None,
        }
        self.recent_commands.append(entry)
        self.stats["pulses_requested"] += 1
        if result.success:
            self.stats["pulses_confirmed"] += 1
        elif result.stage == "locked":
            self.stats["pulses_refused"] += 1
        elif result.fallback_safe:  # the output command was never delivered
            self.stats["pulses_not_sent"] += 1
        else:
            self.stats["pulses_unconfirmed"] += 1
        _LOGGER.debug("Pulse result: %s", entry)

    async def _pulse(
        self, output: int, seconds: int, trace: dict[str, _Waiter], requested_at: float
    ) -> CommandResult:
        async with self._cmd_lock:
            if (wait := self._lockout_remaining(requested_at)) > 0:
                return CommandResult(
                    False,
                    "locked",
                    f"the gate was operated moments ago; next pulse allowed in {wait:.0f} s",
                )
            try:
                await self._ensure_connected()
            except SeranovaError as err:
                return CommandResult(False, "connect", str(err), fallback_safe=True)

            # Wake: the device's own answer to a state request (CMD 10) before anything
            # that can switch an output, as an open web page has before its button is
            # pressed. Without it the cloud acks CMD 6 but the device often never gets it.
            trace["wake"] = _Waiter(None, confirm=StateCheck())
            try:
                await self._check_device(trace["wake"], "pulse")
            except SeranovaError as err:
                return CommandResult(False, "wake", str(err), fallback_safe=True)

            # CMD 2 is answered by the cloud itself (in well under a second), so a
            # failure here is the cloud not responding: stage "connect".
            trace["prepare"] = _Waiter(CMD_PREPARE)
            try:
                await self._send(CMD_PREPARE, None, None, trace["prepare"])
            except DeviceRefused as err:
                return CommandResult(False, "connect", str(err), fallback_safe=True)
            except CommandTimeout:
                return CommandResult(
                    False, "connect", "the cloud did not acknowledge CMD 2", fallback_safe=True
                )
            except SeranovaError as err:
                return CommandResult(False, "connect", str(err), fallback_safe=True)

            # CMD 6 is sent ONCE. The device does not de-duplicate a re-sent frame
            # with the same SEQ: in testing, re-sending every second (waiting for a
            # CMD 10 that never came) opened the gate twice, 4 s apart. The web app
            # re-sends too but gets CMD 10 quickly because its open page has already
            # woken the device. Success needs the device's access event, not the ack.
            waiter = trace["pulse"] = _Waiter(CMD_OUTPUT_PULSE, confirm=pulse_confirmation)
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
            except CommandTimeout:
                reason = (
                    "the cloud accepted the command but the gate did not confirm it; "
                    "it may still open late, or not at all"
                    if waiter.acked
                    else "no answer from the cloud or the gate"
                )
                return CommandResult(False, "open", reason, acked=waiter.acked)
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
            finally:
                if waiter.sent:
                    self._last_pulse_at = time.monotonic()
                    self._confirmed_at = None
            # The access event that confirmed it (_last_access_at was set by the same
            # frame just before, so it is not counted as a separate access).
            self._confirmed_at = waiter.done_at
            return CommandResult(
                True, "open", acked=waiter.acked, confirmed_by=waiter.confirmed_by
            )

    async def _check_device(self, waiter: _Waiter, reason: str) -> None:
        """Ask the device for its state (CMD 10) until it answers with a fresh frame.

        The caller holds the command lock; waiter.confirm must be a StateCheck.
        Re-sent every WAKE_INTERVAL s (a read, safe to repeat). Raises CommandTimeout
        when no fresh answer comes, DeviceRefused on ERROR 2/4/5, other SeranovaErrors
        on socket trouble. Every check is recorded for the diagnostics.
        """
        started = time.monotonic()
        started_wall = dt_util.utcnow()
        try:
            await self._send(
                CMD_SYSTEM,
                "",
                self._user_code,
                waiter,
                retries=WAKE_RETRIES,
                interval=WAKE_INTERVAL,
            )
        except CommandTimeout as err:
            self.device_online = False
            self._notify()
            check = waiter.confirm
            stale = check.stale if isinstance(check, StateCheck) else 0
            detail = f" (only {stale} out-of-date state replies)" if stale else ""
            raise CommandTimeout(
                "the gate controller did not answer a state request in "
                f"{time.monotonic() - started:.0f} s{detail}; nothing was sent to the gate"
            ) from err
        finally:
            self._record_state_check(reason, waiter, started, started_wall)

    @callback
    def _record_state_check(
        self, reason: str, waiter: _Waiter, started: float, started_wall: datetime
    ) -> None:
        check = waiter.confirm if isinstance(waiter.confirm, StateCheck) else None
        answered = waiter.done_at is not None
        entry = {
            "time": started_wall.isoformat(),
            "reason": reason,
            "answered": answered,
            "sends": waiter.attempts,
            "answered_after_s": round(waiter.done_at - started, 2) if answered else None,
            "device_lag_s": check.lag if check else None,
            "stale_replies": check.stale if check else 0,
        }
        self.recent_state_checks.append(entry)
        self.stats["state_checks"] += 1
        if not answered:
            self.stats["state_checks_unanswered"] += 1
        self._device_checked = True
        _LOGGER.debug("State check: %s", entry)

    async def async_poll(self, _now: datetime | None = None) -> None:
        """Ask the device itself for its state, so the Online sensor tells the truth."""
        if self._cmd_lock.locked() or self._conn is None:
            return
        async with self._cmd_lock:
            try:
                await self._check_device(_Waiter(None, confirm=StateCheck()), "poll")
            except DeviceRefused as err:
                self.last_error = str(err)
            except SeranovaError:
                pass  # recorded; a timeout has already marked the device offline
            finally:
                with contextlib.suppress(Exception):
                    self._notify()

    async def _async_after_connect(self) -> None:
        """Open each session with CMD 1 as before, then check the device once per run.

        The CMD 1 reply (probably from the cloud) keeps the system info for the
        diagnostics. It is sent on every reconnect, as earlier versions did, because
        pushed events have always arrived on such sessions.
        """
        if self._cmd_lock.locked() or self._conn is None:
            return
        async with self._cmd_lock:
            try:
                await self._send(
                    CMD_INFO, "", self._user_code, _Waiter(None, CMD_INFO), retries=2, interval=5
                )
            except DeviceRefused as err:
                self.last_error = str(err)
                self._notify()
                return
            except SeranovaError:
                return
        if not self._device_checked:
            await self.async_poll()
