"""The gate command path.

Hard rules checked here:
- CMD 6 is written exactly once, never retried;
- success needs the device's own access event (the cloud's ack is not enough);
- a SEQ is never reused, also across a restart;
- a second pulse within the lockout is refused (open/stop/close gates).

A fake socket records every frame the hub writes and answers with scripted
frames. Needs Home Assistant's test kit (pytest-homeassistant-custom-component).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.seranova_gate import diagnostics  # noqa: E402
from custom_components.seranova_gate import hub as hub_module  # noqa: E402
from custom_components.seranova_gate.api import NotSent, SocketClosed  # noqa: E402
from custom_components.seranova_gate.const import (  # noqa: E402
    CONF_APP_KEY,
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


def gate_ok(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A device that works: CMD 2 acked, CMD 6 acked by the cloud + access event."""
    cmd = frame["MSG"]["CMD"]
    if cmd == 2:
        return [ack(2)]
    if cmd == 6:
        return [ack(6), ACCESS_EVENT]
    return None


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the real 1 s / 10 s waits; the retry COUNTS stay the production values."""
    monkeypatch.setattr(hub_module, "SEND_INTERVAL", 0.01)
    monkeypatch.setattr(hub_module, "PULSE_CONFIRM_TIMEOUT", 0.05)


_HUBS: list[hub_module.SeranovaHub] = []


@pytest.fixture(autouse=True)
async def close_hubs() -> AsyncIterator[None]:
    """Close every hub (flushes its delayed SEQ save, so no timer lingers)."""
    yield
    while _HUBS:
        await _HUBS.pop().async_close()


def make_entry(lockout: int | None = None, **extra: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMEI: IMEI,
            CONF_APP_KEY: "123456",
            CONF_USER_CODE: USER_CODE,
            CONF_SYSTEM_NAME: "Gate",
            **extra,
        },
        options={} if lockout is None else {CONF_PULSE_LOCKOUT: lockout},
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
    assert conn.count(2) == 1
    assert conn.count(6) == 1
    prepare, pulse = conn.written
    assert "CODE" not in prepare["MSG"]
    assert pulse["MSG"]["CODE"] == USER_CODE
    assert pulse["MSG"]["VAL"] == "1#2#"


async def test_cloud_ack_alone_is_not_success(hass: HomeAssistant) -> None:
    """The 2026-11-06 21:38 case: cloud acked in 0.2 s, the gate never moved."""
    hub, conn = make_hub(hass, lambda f: [ack(f["MSG"]["CMD"])])
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.acked
    assert result.stage == "open"
    assert result.fallback_safe is False  # it may still open late
    assert "did not confirm" in (result.error or "")
    assert conn.count(6) == 1


async def test_status_frame_is_not_confirmation(hass: HomeAssistant) -> None:
    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        return [ack(2)] if frame["MSG"]["CMD"] == 2 else [ack(6), STATUS]

    hub, conn = make_hub(hass, script)
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert conn.count(6) == 1


async def test_unanswered_cmd6_is_not_retried(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, lambda f: [ack(2)] if f["MSG"]["CMD"] == 2 else None)
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "open"
    assert result.fallback_safe is False  # it may have switched: no SMS on top
    assert conn.count(6) == 1


async def test_failed_cmd6_write_is_unsafe(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, lambda f: [ack(2)] if f["MSG"]["CMD"] == 2 else None)
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

    conn.script = script
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "open"
    assert result.fallback_safe is True
    assert conn.count(6) == 0


async def test_asleep_device_never_gets_cmd6(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, lambda f: None)
    result = await hub.async_pulse(1, 2)

    assert not result.success
    assert result.stage == "wake"
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

    hub, conn = make_hub(hass, script)
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
    hub, conn = make_hub(hass, lambda f: [ack(f["MSG"]["CMD"])], make_entry(lockout=30))
    assert not (await hub.async_pulse(1, 2)).success  # sent, may still open late

    assert (await hub.async_pulse(1, 2)).stage == "locked"
    assert conn.count(6) == 1


async def test_lockout_after_an_access_from_elsewhere(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok, make_entry(lockout=30))
    hub._handle_frames([ACCESS_EVENT])  # opened by the app / SMS / a key fob

    result = await hub.async_pulse(1, 2)
    assert result.stage == "locked"
    assert conn.written == []  # not even CMD 2


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

    assert conn.seqs() == [SEQ_MAX - 1, SEQ_MIN]


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


# ---- diagnostics history ------------------------------------------------------


async def test_history_records_a_confirmed_pulse(hass: HomeAssistant) -> None:
    hub, conn = make_hub(hass, gate_ok)
    await hub.async_pulse(1, 2)

    entry = hub.recent_commands[-1]
    assert entry["success"] is True
    assert entry["confirmed_by"] == "access_event"
    assert entry["prepare_sends"] == 1
    assert entry["pulse_seq"] == conn.written[1]["MSG"]["SEQ"]
    assert entry["cloud_ack_after_s"] is not None
    assert entry["confirmed_after_s"] is not None
    assert hub.stats["pulses_confirmed"] == 1
    assert hub.recent_events[-1]["event_type"] == "access_granted"


async def test_history_counts_every_outcome(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: [ack(f["MSG"]["CMD"])], make_entry(lockout=30))
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
