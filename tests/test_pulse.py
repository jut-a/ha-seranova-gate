"""The gate command path.

Hard rules checked here:
- CMD 6 is written exactly once, never retried;
- nothing that can switch an output is sent before the device itself answered a
  state request (CMD 10 with a fresh device TIME);
- success needs the device's own access event (the cloud's ack is not enough);
- a SEQ is never reused, also across a restart;
- a second pulse within the lockout is refused (open/stop/close gates), except a
  new press made after the device confirmed the previous pulse, after a short guard.

Also: the reconnect backoff, setup readiness ("Retrying setup" until the device
answers), and the event audit log with its action.

A fake socket records every frame the hub writes and answers with scripted
frames. Needs Home Assistant's test kit (pytest-homeassistant-custom-component).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
import time
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.seranova_gate import (  # noqa: E402
    SETUP_READY_TIMEOUT,
    _web_api,
    async_setup,
    diagnostics,
)
from custom_components.seranova_gate import hub as hub_module  # noqa: E402
from custom_components.seranova_gate.api import (  # noqa: E402
    CannotConnect,
    NotSent,
    SocketClosed,
)
from custom_components.seranova_gate.const import (  # noqa: E402
    CONF_APP_KEY,
    CONF_EVENT_LOG_DAYS,
    CONF_IMEI,
    CONF_PULSE_LOCKOUT,
    CONF_SYSTEM_NAME,
    CONF_USER_CODE,
    DOMAIN,
    SEQ_MAX,
    SEQ_MIN,
)

IMEI = "864000000000000"
USER_CODE = "1234"

Script = Callable[[dict[str, Any]], list[dict[str, Any]] | None]


class FakeConnection:
    """Stands in for SocketIoConnection: records writes, plays scripted replies."""

    def __init__(self, hub: hub_module.SeranovaHub, script: Script) -> None:
        self.hub = hub
        self.script = script
        self.connected = True
        self.fail_write_for: int | None = None  # CMD whose write raises mid-send
        self.written: list[dict[str, Any]] = []

    async def emit(self, payload: dict[str, Any]) -> None:
        if not self.connected:
            raise NotSent("not connected")
        if payload["MSG"]["CMD"] == self.fail_write_for:
            raise SocketClosed("write failed")
        self.written.append(payload)
        if replies := self.script(payload):
            asyncio.get_running_loop().call_soon(self.hub._handle_frames, replies)

    async def close(self) -> None:
        self.connected = False

    def count(self, cmd: int) -> int:
        return sum(1 for frame in self.written if frame["MSG"]["CMD"] == cmd)

    def seqs(self) -> list[int]:
        return [frame["MSG"]["SEQ"] for frame in self.written]


def ack(cmd: int) -> dict[str, Any]:
    return {"ack": {"cmd": cmd}, "UID": IMEI}


ACCESS_EVENT = {
    "CMD": 14,
    "UID": IMEI,
    "EVENT": {"CID": "142200001", "TXT": "Object Name\nAccess Gained by\nJohn\nUser 1\n"},
}
STATUS = {"CMD": 10, "UID": IMEI, "OUT": "000,010,020,030"}


def state(age: float = 1) -> dict[str, Any]:
    """A CMD 10 frame stamped by the device's clock `age` seconds ago."""
    return {**STATUS, "TIME": int(time.time() - age)}


def stored_copy_script(age: float = 3600) -> Script:
    """The cloud answers every state request with the same stored frame.

    The frame is stamped once: two calls to state() can fall on either side of a
    second boundary, and a later TIME is (correctly) taken as the device answering
    with a wrong clock.
    """
    old = state(age=age)

    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        return [ack(10), dict(old)] if frame["MSG"]["CMD"] == 10 else gate_ok(frame)

    return script


def awake(script: Script) -> Script:
    """The device answers state requests (CMD 10) itself; other CMDs go to `script`."""

    def wrapped(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        if frame["MSG"]["CMD"] == 10:
            return [ack(10), state()]
        return script(frame)

    return wrapped


def gate_ok(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A device that works: CMD 10 answered, CMD 2 acked, CMD 6 acked + access event."""
    cmd = frame["MSG"]["CMD"]
    if cmd == 10:
        return [ack(10), state()]
    if cmd == 2:
        return [ack(2)]
    if cmd == 6:
        return [ack(6), ACCESS_EVENT]
    return None


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the real 1 s / 10 s waits; the retry COUNTS stay the production values."""
    monkeypatch.setattr(hub_module, "SEND_INTERVAL", 0.01)
    monkeypatch.setattr(hub_module, "WAKE_INTERVAL", 0.01)
    monkeypatch.setattr(hub_module, "PULSE_CONFIRM_TIMEOUT", 0.05)


_HUBS: list[hub_module.SeranovaHub] = []


@pytest.fixture(autouse=True)
async def close_hubs() -> AsyncIterator[None]:
    """Close every hub (flushes its delayed SEQ save, so no timer lingers)."""
    yield
    while _HUBS:
        await _HUBS.pop().async_close()


def make_entry(
    lockout: int | None = None, event_log_days: int | None = None, **extra: str
) -> MockConfigEntry:
    options: dict[str, Any] = {}
    if lockout is not None:
        options[CONF_PULSE_LOCKOUT] = lockout
    if event_log_days is not None:
        options[CONF_EVENT_LOG_DAYS] = event_log_days
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMEI: IMEI,
            CONF_APP_KEY: "123456",
            CONF_USER_CODE: USER_CODE,
            CONF_SYSTEM_NAME: "Gate",
            **extra,
        },
        options=options,
    )


def make_hub(
    hass: HomeAssistant, script: Script, entry: MockConfigEntry | None = None
) -> tuple[hub_module.SeranovaHub, FakeConnection]:
    hub = hub_module.SeranovaHub(hass, entry or make_entry(), session=None, web=None)  # type: ignore[arg-type]
    conn = FakeConnection(hub, script)
    hub._conn = conn  # type: ignore[assignment]
    hub.cloud_connected = True
    hub._connected.set()
    _HUBS.append(hub)
    return hub, conn


# ---- single write of CMD 6 -------------------------------------------------


async def test_success_needs_the_access_event(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok)
    result = await hub.async_pulse(1, 2)

    assert result.success
    assert result.confirmed_by == "access_event"
    assert conn.count(10) == 1
    assert conn.count(2) == 1
    assert conn.count(6) == 1
    wake, prepare, pulse = conn.written  # in this order
    assert wake["MSG"]["CMD"] == 10
    assert wake["MSG"]["CODE"] == USER_CODE
    assert wake["MSG"]["VAL"] == ""
    assert "CODE" not in prepare["MSG"]
    assert pulse["MSG"]["CODE"] == USER_CODE
    assert pulse["MSG"]["VAL"] == "1#2#"


async def test_cloud_ack_alone_is_not_success(hass: HomeAssistant) -> None:
    """The 2026-11-06 21:38 case: cloud acked in 0.2 s, the gate never moved."""
    hub, conn = make_hub(hass, awake(lambda f: [ack(f["MSG"]["CMD"])]))
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.acked
    assert result.stage == "open"
    assert result.fallback_safe is False  # it may still open late
    assert "did not confirm" in (result.error or "")
    assert conn.count(6) == 1


async def test_status_frame_is_not_confirmation(hass: HomeAssistant) -> None:
    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        return [ack(2)] if frame["MSG"]["CMD"] == 2 else [ack(6), state()]

    hub, conn = make_hub(hass, awake(script))
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert conn.count(6) == 1


async def test_unanswered_cmd6_is_not_retried(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, awake(lambda f: [ack(2)] if f["MSG"]["CMD"] == 2 else None))
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "open"
    assert result.fallback_safe is False  # it may have switched: no SMS on top
    assert conn.count(6) == 1


async def test_failed_cmd6_write_is_unsafe(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, awake(lambda f: [ack(2)] if f["MSG"]["CMD"] == 2 else None))
    conn.fail_write_for = 6
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "open"
    assert result.fallback_safe is False  # the bytes may have left


async def test_socket_closed_before_cmd6_is_fallback_safe(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, lambda f: None)

    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        if frame["MSG"]["CMD"] == 2:
            conn.connected = False  # the socket dies right after CMD 2 is answered
            return [ack(2)]
        return None

    conn.script = awake(script)
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "open"
    assert result.fallback_safe is True
    assert conn.count(6) == 0


async def test_asleep_device_never_gets_cmd2_or_cmd6(hass: HomeAssistant) -> None:
    """The 2026-11-07 case: everything acked by the cloud, the device silent."""
    hub, conn = make_hub(hass, lambda f: [ack(f["MSG"]["CMD"])])
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "wake"
    assert result.fallback_safe is True
    assert "did not answer a state request" in (result.error or "")
    assert conn.count(10) == hub_module.WAKE_RETRIES + 1  # a read may be re-sent
    assert conn.count(2) == 0
    assert conn.count(6) == 0
    assert hub.device_online is False


async def test_stale_state_is_not_an_answer(hass: HomeAssistant) -> None:
    """A CMD 10 with an old device TIME is a stored copy, not the device answering."""
    hub, conn = make_hub(hass, stored_copy_script())
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "wake"
    assert result.fallback_safe is True
    assert "out-of-date" in (result.error or "")
    assert conn.count(2) == 0
    assert conn.count(6) == 0


async def test_wake_waits_for_a_late_answer(hass: HomeAssistant) -> None:
    """The device answers only the third state request: then the pulse goes out once."""
    sends = {"n": 0}

    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        if frame["MSG"]["CMD"] == 10:
            sends["n"] += 1
            return [ack(10), state()] if sends["n"] >= 3 else [ack(10)]
        return gate_ok(frame)

    hub, conn = make_hub(hass, script)
    result = await hub.async_pulse(1, 2)

    assert result.success
    assert conn.count(10) == 3
    assert conn.count(6) == 1
    assert hub.recent_commands[-1]["wake_sends"] == 3
    assert hub.recent_state_checks[-1]["answered"] is True


async def test_wake_refused_sends_nothing_else(hass: HomeAssistant) -> None:
    hub, conn = make_hub(
        hass, lambda f: [{"ERROR": 5, "UID": IMEI}] if f["MSG"]["CMD"] == 10 else None
    )
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "wake"
    assert result.fallback_safe is True
    assert "user code" in (result.error or "")
    assert conn.count(10) == 1  # a refusal is final
    assert conn.count(2) == 0
    assert conn.count(6) == 0


async def test_unacked_prepare_never_gets_cmd6(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, awake(lambda f: None))
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "connect"  # CMD 2 is answered by the cloud itself
    assert result.fallback_safe is True
    assert conn.count(2) == hub_module.SEND_RETRIES + 1  # CMD 2 may be re-sent
    assert conn.count(6) == 0


async def test_wrong_user_code_is_not_retried(hass: HomeAssistant) -> None:
    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        cmd = frame["MSG"]["CMD"]
        if cmd == 2:
            return [ack(2)]
        if cmd == 6:
            return [{"ERROR": 5, "UID": IMEI}]
        return None

    hub, conn = make_hub(hass, awake(script))
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.fallback_safe is True
    assert "user code" in (result.error or "")
    assert conn.count(6) == 1


# ---- lockout (open/stop/close gates) -----------------------------------------


async def test_second_pulse_within_lockout_is_refused(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=30))
    assert (await hub.async_pulse(1, 2)).success

    second = await hub.async_pulse(1, 2)
    assert not second.success
    assert second.stage == "locked"
    assert second.fallback_safe is False  # an SMS now would stop the moving gate
    assert conn.count(6) == 1


async def test_lockout_also_follows_an_unconfirmed_pulse(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, awake(lambda f: [ack(f["MSG"]["CMD"])]), make_entry(lockout=30))
    assert not (await hub.async_pulse(1, 2)).success  # sent, may still open late

    assert (await hub.async_pulse(1, 2)).stage == "locked"
    assert conn.count(6) == 1


async def test_lockout_after_an_access_from_elsewhere(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=30))
    hub._handle_frames([ACCESS_EVENT])  # opened by the app / SMS / a key fob

    result = await hub.async_pulse(1, 2)
    assert result.stage == "locked"
    assert conn.written == []  # not even CMD 2


async def test_new_press_after_confirmation_waits_only_the_guard(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop or close the gate from HA without waiting out the full lockout."""
    monkeypatch.setattr(hub_module, "CONFIRMED_PULSE_GUARD", 0.05)
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=30))
    assert (await hub.async_pulse(1, 2)).success

    assert (await hub.async_pulse(1, 2)).stage == "locked"  # inside the guard
    await asyncio.sleep(0.1)
    assert (await hub.async_pulse(1, 2)).success
    assert conn.count(6) == 2


async def test_double_tap_before_confirmation_is_refused(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two presses queued together: the second was made before the first was confirmed."""
    monkeypatch.setattr(hub_module, "CONFIRMED_PULSE_GUARD", 0)
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=30))
    first, second = await asyncio.gather(hub.async_pulse(1, 2), hub.async_pulse(1, 2))

    assert first.success
    assert second.stage == "locked"
    assert conn.count(6) == 1


async def test_unconfirmed_pulse_keeps_the_full_lockout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "CONFIRMED_PULSE_GUARD", 0)
    hub, conn = make_hub(hass, awake(lambda f: [ack(f["MSG"]["CMD"])]), make_entry(lockout=30))
    assert not (await hub.async_pulse(1, 2)).success  # sent, may still open late
    await asyncio.sleep(0.05)

    assert (await hub.async_pulse(1, 2)).stage == "locked"
    assert conn.count(6) == 1


async def test_another_access_after_confirmation_restarts_the_full_lockout(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeat run by the device or someone else's access: cannot be told apart."""
    monkeypatch.setattr(hub_module, "CONFIRMED_PULSE_GUARD", 0.05)
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=30))
    assert (await hub.async_pulse(1, 2)).success
    hub._handle_frames([ACCESS_EVENT])  # a second "Access Gained"
    await asyncio.sleep(0.1)

    assert (await hub.async_pulse(1, 2)).stage == "locked"
    assert conn.count(6) == 1


async def test_lockout_zero_disables_it(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=0))
    assert (await hub.async_pulse(1, 2)).success
    assert (await hub.async_pulse(1, 2)).success
    assert conn.count(6) == 2


# ---- SEQ is never reused ------------------------------------------------------


async def test_first_run_seq_is_time_seeded(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok)
    await hub.async_load_seq()
    await hub.async_pulse(1, 2)

    assert all(SEQ_MIN <= seq < SEQ_MAX for seq in conn.seqs())
    assert len(set(conn.seqs())) == len(conn.seqs())


async def test_seq_continues_across_a_restart(hass: HomeAssistant) -> None:
    """The 2026-11-06 failures: after each restart CMD 6 went out with SEQ 2."""
    entry = make_entry(lockout=0)
    first, conn1 = make_hub(hass, gate_ok, entry)
    await first.async_load_seq()
    await first.async_pulse(1, 2)
    await first.async_close()  # HA stops; the counter is saved
    _HUBS.remove(first)

    second, conn2 = make_hub(hass, gate_ok, entry)  # HA starts again
    await second.async_load_seq()
    await second.async_pulse(1, 2)

    assert min(conn2.seqs()) > max(conn1.seqs())


async def test_seq_wraps_inside_the_range(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok)
    hub._seq = SEQ_MAX - 1
    await hub.async_pulse(1, 2)

    assert conn.seqs() == [SEQ_MAX - 1, SEQ_MIN, SEQ_MIN + 1]


# ---- events ---------------------------------------------------------------------


async def test_access_event_reaches_listeners(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: None)
    seen: list[tuple[str, dict[str, Any]]] = []
    hub.async_add_event_listener(lambda event_type, data: seen.append((event_type, data)))

    hub._handle_frames([ACCESS_EVENT, {"CMD": 14, "UID": "999", "EVENT": {"CID": "142200001"}}])

    assert len(seen) == 1  # the frame for another system is ignored
    event_type, data = seen[0]
    assert event_type == "access_granted"
    assert data["message"] == "Access Gained by John (User 1)"


# ---- Online: only the device's own frames count ---------------------------------


async def test_cmd1_reply_does_not_prove_the_device_is_online(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: None)
    hub._handle_frames([{"CMD": 1, "UID": IMEI, "VAL": "CON:1,RSSI:31"}])
    assert hub.device_online is None
    assert hub.sysinfo == "CON:1,RSSI:31"

    hub._handle_frames([state(age=3600)])  # a stored copy
    assert hub.device_online is None

    hub._handle_frames([state()])
    assert hub.device_online is True
    assert hub.last_seen is not None


async def test_poll_follows_the_device(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, lambda f: [ack(f["MSG"]["CMD"])])  # the cloud acks only
    await hub.async_poll()
    assert hub.device_online is False
    assert hub.recent_state_checks[-1]["answered"] is False
    assert hub.recent_state_checks[-1]["reason"] == "poll"
    assert hub.stats["state_checks_unanswered"] == 1

    conn.script = gate_ok
    await hub.async_poll()
    assert hub.device_online is True
    assert hub.recent_state_checks[-1]["answered"] is True
    assert conn.count(6) == 0  # a poll never switches anything


# ---- diagnostics history ------------------------------------------------------


async def test_history_records_a_confirmed_pulse(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok)
    await hub.async_pulse(1, 2)

    entry = hub.recent_commands[-1]
    assert entry["success"] is True
    assert entry["confirmed_by"] == "access_event"
    assert entry["wake_sends"] == 1
    assert entry["wake_answered_after_s"] is not None
    assert entry["device_lag_s"] is not None
    assert entry["stale_state_replies"] == 0
    assert entry["prepare_sends"] == 1
    assert entry["pulse_seq"] == conn.written[2]["MSG"]["SEQ"]
    assert entry["cloud_ack_after_s"] is not None
    assert entry["confirmed_after_s"] is not None
    assert hub.stats["pulses_confirmed"] == 1
    assert hub.recent_events[-1]["event_type"] == "access_granted"


async def test_history_counts_every_outcome(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, awake(lambda f: [ack(f["MSG"]["CMD"])]), make_entry(lockout=30))
    await hub.async_pulse(1, 2)  # cloud ack only: unconfirmed
    await hub.async_pulse(1, 2)  # within the lockout: refused

    assert [c["stage"] for c in hub.recent_commands] == ["open", "locked"]
    assert hub.recent_commands[0]["cloud_ack_after_s"] is not None
    assert hub.recent_commands[0]["confirmed_after_s"] is None
    assert hub.stats["pulses_unconfirmed"] == 1
    assert hub.stats["pulses_refused"] == 1
    assert hub.stats["pulses_requested"] == 2


async def test_diagnostics_carry_no_personal_data(hass: HomeAssistant) -> None:
    entry = make_entry(
        lockout=0, system_name="Secret Lane Gate", email="someone@example.com", password="hunter2"
    )
    hub, _ = make_hub(hass, gate_ok, entry)
    hub._handle_frames(
        [
            {
                "CMD": 1,
                "UID": IMEI,
                "VAL": f"CON:1,IMEI:{IMEI},ICCID:8999000000000000000,APPKEY:123456,NAME:Lane Ten",
            },
            ACCESS_EVENT,
        ]
    )
    assert (await hub.async_pulse(1, 2)).success  # the dump must hold a sent command
    entry.runtime_data = hub

    dump = str(await diagnostics.async_get_config_entry_diagnostics(hass, entry))
    for secret in (
        IMEI,
        USER_CODE,
        "8999000000000000000",
        "Lane Ten",
        "John",
        "Secret Lane Gate",
        "someone@example.com",
        "hunter2",
    ):
        assert secret not in dump, secret
    assert "recent_commands" in dump
    assert "event_log_size" in dump  # the log itself (names, numbers) stays out


# ---- reconnect backoff ----------------------------------------------------------


def test_reconnect_backs_off_while_sessions_are_cut_short() -> None:
    """The 2026-11-07 storm: the cloud closed every session within 0.3 s."""
    backoff: float = hub_module.RECONNECT_MIN
    delays = []
    for _ in range(10):
        delay, backoff = hub_module.reconnect_delay(backoff, 0.2)
        delays.append(delay)
    assert delays[:5] == [2, 4, 8, 16, 32]
    assert delays[-1] == hub_module.RECONNECT_MAX
    assert hub_module.reconnect_delay(4, None) == (4, 8)  # a failed connect, too
    # A working session ends routinely: back quickly, backoff restarted.
    assert hub_module.reconnect_delay(300, 120) == (2, 2)


async def test_a_press_cuts_the_reconnect_wait_short(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, gate_ok)
    hub._conn = None
    hub._connected.clear()
    with pytest.raises(CannotConnect):
        await hub._ensure_connected(timeout=0.01)
    async with asyncio.timeout(1):
        await hub._wait_before_reconnect(300)  # returns at once: a command is waiting
    assert not hub._reconnect_now.is_set()


# ---- readiness: "Retrying setup" until the device answers ---------------------


async def test_setup_is_ready_once_the_device_answers(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, gate_ok)
    await hub.async_poll()
    await hub.async_wait_ready(0.1)


async def test_offline_device_fails_the_setup(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: None)
    hub._handle_frames([{"ERROR": 3, "UID": IMEI}])
    with pytest.raises(hub_module.DeviceUnavailable, match="offline"):
        await hub.async_wait_ready(0.1)


async def test_unanswered_state_check_fails_the_setup(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: [ack(f["MSG"]["CMD"])])
    await hub.async_poll()
    with pytest.raises(hub_module.DeviceUnavailable, match="did not answer"):
        await hub.async_wait_ready(0.1)


async def test_sessions_cut_short_fail_the_setup(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, gate_ok)
    for _ in range(3):
        hub._note_session_length(0.2, 2)
    with pytest.raises(hub_module.DeviceUnavailable, match="keeps closing"):
        await hub.async_wait_ready(0.1)


async def test_failures_after_setup_set_the_entry_up_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, conn = make_hub(hass, gate_ok)
    await hub.async_poll()  # set up
    reloads: list[str] = []
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reloads.append)

    conn.script = lambda f: [ack(f["MSG"]["CMD"])]  # the device stops answering
    await hub.async_poll()
    assert reloads == []  # one miss is tolerated
    await hub.async_poll()
    assert reloads == [hub.entry.entry_id]
    await hub.async_poll()
    assert len(reloads) == 1  # asked once


# ---- event audit log ------------------------------------------------------------


def access_event(epoch: int | None = None) -> dict[str, Any]:
    event = dict(ACCESS_EVENT["EVENT"])
    if epoch is not None:
        event["TIME"] = epoch
    return {**ACCESS_EVENT, "EVENT": event}


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_event_time_prefers_the_gate_clock() -> None:
    received = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    stamped = {"device_time": "2026-11-07T09:59:58+00:00"}
    assert hub_module.event_time(stamped, received) == datetime(2026, 11, 7, 9, 59, 58, tzinfo=UTC)
    assert hub_module.event_time({}, received) == received


async def test_events_are_logged_and_survive_a_new_entry(hass: HomeAssistant) -> None:
    first, _ = make_hub(hass, gate_ok)
    await first.async_load_event_log()
    first._handle_frames([access_event(1_794_000_000)])
    logged = first.event_log[-1]
    assert logged["type"] == "access_granted"
    assert logged["message"] == "Access Gained by John (User 1)"
    assert logged["time"] == iso(1_794_000_000)
    await first.async_close()
    _HUBS.remove(first)

    second, _ = make_hub(hass, gate_ok, make_entry())  # removed and added again
    await second.async_load_event_log()
    assert [e["time"] for e in second.event_log] == [iso(1_794_000_000)]


async def test_kept_days_drop_older_events(hass: HomeAssistant) -> None:
    now = int(time.time())
    hub, _ = make_hub(hass, gate_ok, make_entry(event_log_days=30))
    hub._handle_frames([access_event(now - 40 * 86400), access_event(now - 86400)])
    assert [e["time"] for e in hub.event_log] == [iso(now - 86400)]

    forever, _ = make_hub(hass, gate_ok, make_entry(event_log_days=0))
    forever._handle_frames([access_event(now - 4000 * 86400)])
    assert len(forever.event_log) == 1


async def test_get_events_filters_newest_first(hass: HomeAssistant) -> None:
    base = 1_794_000_000
    hub, _ = make_hub(hass, gate_ok)
    alarm = {
        "CMD": 14,
        "UID": IMEI,
        "EVENT": {"CID": "113000001", "TIME": base + 120, "TXT": "Object Name\nAlarm\n"},
    }
    hub._handle_frames([access_event(base + 60)])
    hub._handle_frames([access_event(base)])  # delivered late: older than the one before
    hub._handle_frames([alarm])

    assert [e["time"] for e in hub.get_events()] == [iso(base + 120), iso(base + 60), iso(base)]
    assert [e["type"] for e in hub.get_events(event_type="access_granted")] == [
        "access_granted",
        "access_granted",
    ]
    since = datetime.fromtimestamp(base + 30, UTC)
    assert len(hub.get_events(since=since)) == 2
    assert len(hub.get_events(until=since)) == 1
    assert len(hub.get_events(limit=1)) == 1


async def test_get_events_action(hass: HomeAssistant) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    hub, _ = make_hub(hass, gate_ok, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = hub
    hub._handle_frames([access_event(1_794_000_000)])
    assert await async_setup(hass, {})

    result = await hass.services.async_call(
        DOMAIN, "get_events", {"limit": 5}, blocking=True, return_response=True
    )
    assert result["count"] == 1
    assert result["logged"] == 1
    assert result["events"][0]["type"] == "access_granted"


# ---- 0.2.5 fixes ----------------------------------------------------------------


async def test_out_of_range_event_time_keeps_the_connection_alive(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, gate_ok)
    hub._handle_frames([access_event(10**12)])  # must not raise
    assert hub.event_log[-1]["device_time"] is None
    assert hub.event_log[-1]["time"]  # falls back to the received time


async def test_setup_retries_reuse_the_signed_in_session(hass: HomeAssistant) -> None:
    http, other_http = object(), object()
    first = _web_api(hass, "entry1", http, "john@example.com", "pw")  # type: ignore[arg-type]
    first.token = "kept"
    assert _web_api(hass, "entry1", http, "john@example.com", "pw") is first  # type: ignore[arg-type]
    # Another entry on the same account gets its own: no shared token to reset.
    assert _web_api(hass, "entry2", http, "john@example.com", "pw") is not first  # type: ignore[arg-type]
    # A new password or a replaced (closed) HTTP session gives a fresh one.
    changed = _web_api(hass, "entry1", http, "john@example.com", "new")  # type: ignore[arg-type]
    assert changed is not first
    assert changed.token is None
    renewed = _web_api(hass, "entry1", other_http, "john@example.com", "new")  # type: ignore[arg-type]
    assert renewed is not changed


def test_setup_waits_long_enough_for_the_worst_case() -> None:
    # socket connect 20 s + CMD 1 3 x 5 s + state request (WAKE_RETRIES + 1) x WAKE_INTERVAL
    worst = 20 + 15 + (hub_module.WAKE_RETRIES + 1) * hub_module.WAKE_INTERVAL
    assert worst < SETUP_READY_TIMEOUT


async def test_a_dropped_socket_is_not_an_unanswered_check(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok)
    conn.fail_write_for = 10
    await hub.async_poll()
    assert hub.recent_state_checks[-1]["outcome"] == "error"
    assert hub.stats["state_checks_unanswered"] == 0
    assert not hub._ready.done()  # the setup still waits for a real answer

    conn.fail_write_for = None
    await hub.async_poll()
    await hub.async_wait_ready(0.1)


async def test_a_refused_check_says_so(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: [{"ERROR": 4, "UID": IMEI}])
    await hub.async_poll()
    assert hub.recent_state_checks[-1]["outcome"] == "refused"
    assert hub.stats["state_checks_unanswered"] == 0
    with pytest.raises(hub_module.DeviceUnavailable, match="App Key"):
        await hub.async_wait_ready(0.1)


async def test_a_controller_with_a_wrong_clock_still_counts_as_answering(
    hass: HomeAssistant,
) -> None:
    """Its replies are an hour off, but each is stamped later than the one before."""
    stamp = {"t": int(time.time()) - 3600}

    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        if frame["MSG"]["CMD"] == 10:
            stamp["t"] += 3
            return [ack(10), {**STATUS, "TIME": stamp["t"]}]
        return gate_ok(frame)

    hub, conn = make_hub(hass, script)
    result = await hub.async_pulse(1, 2)
    assert result.success
    assert conn.count(10) == 2  # the second reply proves the round trip
    assert conn.count(6) == 1
    assert hub.device_online is True


async def test_a_repeated_old_copy_never_counts_and_names_the_clock(
    hass: HomeAssistant,
) -> None:
    hub, conn = make_hub(hass, stored_copy_script())
    result = await hub.async_pulse(1, 2)
    assert result.stage == "wake"
    assert "clock may be off" in (result.error or "")
    assert conn.count(6) == 0
