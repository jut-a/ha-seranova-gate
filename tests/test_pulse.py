"""The gate command path: CMD 6 must be written exactly once, never retried.

A fake socket records every frame the hub writes and answers with scripted
frames. Needs Home Assistant's test kit (pytest-homeassistant-custom-component).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.seranova_gate import hub as hub_module  # noqa: E402
from custom_components.seranova_gate.api import NotSent, SocketClosed  # noqa: E402
from custom_components.seranova_gate.const import (  # noqa: E402
    CONF_APP_KEY,
    CONF_IMEI,
    CONF_SYSTEM_NAME,
    CONF_USER_CODE,
    DOMAIN,
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


def ack(cmd: int) -> dict[str, Any]:
    return {"ack": {"cmd": cmd}, "UID": IMEI}


ACCESS_EVENT = {
    "CMD": 14,
    "UID": IMEI,
    "EVENT": {"CID": "142200001", "TXT": "Object Name\nAccess Gained by\nJohn\nUser 1\n"},
}


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the real 1 s / 10 s waits; the retry COUNTS stay the production values."""
    monkeypatch.setattr(hub_module, "SEND_INTERVAL", 0.01)
    monkeypatch.setattr(hub_module, "PULSE_CONFIRM_TIMEOUT", 0.05)


def make_hub(hass: HomeAssistant, script: Script) -> tuple[hub_module.SeranovaHub, FakeConnection]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_IMEI: IMEI,
            CONF_APP_KEY: "123456",
            CONF_USER_CODE: USER_CODE,
            CONF_SYSTEM_NAME: "Gate",
        },
    )
    hub = hub_module.SeranovaHub(hass, entry, session=None, web=None)  # type: ignore[arg-type]
    conn = FakeConnection(hub, script)
    hub._conn = conn  # type: ignore[assignment]
    hub.cloud_connected = True
    hub._connected.set()
    return hub, conn


async def test_success_writes_cmd6_once(hass: HomeAssistant) -> None:
    def script(frame: dict[str, Any]) -> list[dict[str, Any]] | None:
        cmd = frame["MSG"]["CMD"]
        if cmd == 2:
            return [ack(2)]
        if cmd == 6:
            return [ack(6), ACCESS_EVENT]
        return None

    hub, conn = make_hub(hass, script)
    result = await hub.async_pulse(1, 2)

    assert result.success
    assert result.acked
    assert conn.count(2) == 1
    assert conn.count(6) == 1
    prepare, pulse = conn.written
    assert "CODE" not in prepare["MSG"]
    assert pulse["MSG"]["CODE"] == USER_CODE
    assert pulse["MSG"]["VAL"] == "1#2#"


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


async def test_access_event_reaches_listeners(hass: HomeAssistant) -> None:
    hub, _ = make_hub(hass, lambda f: None)
    seen: list[tuple[str, dict[str, Any]]] = []
    hub.async_add_event_listener(lambda event_type, data: seen.append((event_type, data)))

    hub._handle_frames([ACCESS_EVENT, {"CMD": 14, "UID": "999", "EVENT": {"CID": "142200001"}}])

    assert len(seen) == 1  # the frame for another system is ignored
    event_type, data = seen[0]
    assert event_type == "access_granted"
    assert data["message"] == "Access Gained by John (User 1)"
