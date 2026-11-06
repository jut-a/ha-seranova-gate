"""SERANOVA cloud client: web login (seranova.eu) and the socket.io channel.

The socket.io (Engine.IO v4) handshake is implemented directly on aiohttp so
the integration has no extra requirements:

    server -> "0{...pingInterval, pingTimeout...}"   open
    client -> '40{"token": "..."}'                    connect with auth
    server -> "40{...}" ok  |  "44{...}" refused
    server -> "2"  client -> "3"                      ping / pong
    both   -> '42["message", <payload>]'              events

Server payloads are strings that can hold several JSON objects back to back;
they are split the same way the web app does before parsing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import json
import logging
import re
import time
from typing import Any

import aiohttp

from .const import CMD_INFO, USER_AGENT, WEB_BASE, WS_URL

_LOGGER = logging.getLogger(__name__)

_CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]*)"')
_JSON_SEPARATORS = (",", ":")


class SeranovaError(Exception):
    """Base error for the SERANOVA client."""


class CannotConnect(SeranovaError):
    """The cloud could not be reached or answered unexpectedly."""


class InvalidAuth(SeranovaError):
    """Wrong e-mail or password."""


class TwoFactorRequired(SeranovaError):
    """The account needs a 2FA code, which this client cannot supply."""


class CaptchaRequired(SeranovaError):
    """The login was refused with a captcha challenge."""


class SocketAuthError(SeranovaError):
    """The socket server refused the session token."""


class SocketClosed(SeranovaError):
    """The socket connection ended."""


class NotSent(SocketClosed):
    """A frame was not written because the socket was already closed."""


def split_json_objects(text: str) -> list[str]:
    """Return the top-level {...} objects found in text (port of the web app's splitter)."""
    found: list[str] = []
    depth = 0
    state = 0  # 0 outside, 1 inside object, 2 inside string, 3 after backslash
    start = 0
    for index, char in enumerate(text):
        if state == 0:
            if char == "{":
                depth, state, start = 1, 1, index
        elif state == 1:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    state = 0
                    found.append(text[start : index + 1])
            elif char == '"':
                state = 2
        elif state == 2:
            if char == '"':
                state = 1
            elif char == "\\":
                state = 3
        else:
            state = 2
    return found


def decode_payload(data: Any) -> list[dict[str, Any]]:
    """Turn one socket.io event argument into a list of frame dicts."""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        frames: list[dict[str, Any]] = []
        for item in data:
            frames.extend(decode_payload(item))
        return frames
    if isinstance(data, bytes | bytearray):
        data = data.decode(errors="replace")
    if isinstance(data, str):
        frames = []
        for chunk in split_json_objects(data):
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(obj, dict):
                frames.append(obj)
        return frames
    return []


def build_frame(
    imei: str, app_key: str, seq: int, cmd: int, val: Any, code: str | None = None
) -> dict[str, Any]:
    """Build a command frame exactly like the web app (CODE omitted when None)."""
    msg: dict[str, Any] = {"SEQ": seq, "CMD": cmd}
    if code is not None:
        msg["CODE"] = code
    msg["VAL"] = val
    return {"IMEI": imei, "KEY": app_key, "MSG": msg}


def as_int(value: Any) -> int | None:
    """Coerce a frame field to int, or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SeranovaWebApi:
    """REST side of seranova.eu: login, token refresh, account queries."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        """Any aiohttp session works; the (cookie based) REST cookies are kept here."""
        self._session = session
        self._email = email
        self._password = password
        self.token: str | None = None
        # Cookies are kept per account; the shared session has no cookie jar.
        self._cookies: dict[str, str] = {}

    def _headers(self, path: str = "/login") -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": WEB_BASE,
            "Referer": f"{WEB_BASE}{path}",
            "Accept": "application/json, text/plain, */*",
        }
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return headers

    def _remember_cookies(self, resp: aiohttp.ClientResponse) -> None:
        for name, morsel in resp.cookies.items():
            self._cookies[name] = morsel.value

    async def login(self) -> str:
        """Log in and return the socket session token."""
        try:
            async with asyncio.timeout(20):
                async with self._session.get(
                    f"{WEB_BASE}/login", headers=self._headers()
                ) as resp:
                    self._remember_cookies(resp)
                    html = await resp.text()
                body: dict[str, Any] = {"email": self._email, "password": self._password}
                if match := _CSRF_RE.search(html):
                    body["csrf"] = match.group(1)
                async with self._session.post(
                    f"{WEB_BASE}/login/auth", json=body, headers=self._headers()
                ) as resp:
                    self._remember_cookies(resp)
                    if resp.status >= 500:
                        raise CannotConnect(f"login failed: HTTP {resp.status}")
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError as err:
                        raise CannotConnect(
                            f"login returned non-JSON (HTTP {resp.status})"
                        ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(f"login failed: {err!r}") from err
        return self._handle_login(data)

    def _handle_login(self, data: Any) -> str:
        message = data.get("message") if isinstance(data, dict) else None
        token = None
        if isinstance(data, dict):
            token = data.get("socketSessionToken") or data.get("accessToken")
        if token and message in (None, "Login Success"):
            self.token = token
            return token
        if message == "requires2FA":
            raise TwoFactorRequired
        if message in ("IncorrectPassword", "AccountNotFound"):
            raise InvalidAuth(message)
        if message == "Captcha failed":
            raise CaptchaRequired
        raise CannotConnect(f"unexpected login response: {message!r}")

    async def refresh(self) -> str | None:
        """Refresh the session token from the cookie; None when that fails."""
        try:
            async with asyncio.timeout(15):
                async with self._session.post(
                    f"{WEB_BASE}/api/auth/refresh", headers=self._headers("/dashboard")
                ) as resp:
                    self._remember_cookies(resp)
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        token = data.get("socketSessionToken") or data.get("accessToken")
        if token:
            self.token = token
        return token

    async def systems(self) -> list[dict[str, Any]]:
        """Return the account's systems (web app command "11"); [] on failure."""
        try:
            async with asyncio.timeout(15):
                async with self._session.post(
                    f"{WEB_BASE}/command",
                    json={"uid": None, "cmd": "11", "val": None},
                    headers=self._headers("/dashboard"),
                ) as resp:
                    self._remember_cookies(resp)
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return []
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]


class SocketIoConnection:
    """One socket.io session to the SERANOVA cloud."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        on_frames: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        """Frames received are handed to on_frames (called in the event loop)."""
        self._session = session
        self._token = token
        self._on_frames = on_frames
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ping_interval = 25.0
        self._ping_timeout = 20.0
        # Observability (diagnostics): server greeting, pings answered, last send.
        self.greeting: dict[str, Any] = {}
        self.pings = 0
        self.last_sent = 0.0

    @property
    def connected(self) -> bool:
        """True while the websocket is open."""
        return self._ws is not None and not self._ws.closed

    async def connect(self, timeout: float = 20) -> None:
        """Open the websocket and complete the socket.io connect with the token."""
        try:
            async with asyncio.timeout(timeout):
                self._ws = await self._session.ws_connect(
                    WS_URL,
                    headers={"Origin": WEB_BASE, "User-Agent": USER_AGENT},
                    autoping=True,
                )
                opening = await self._receive_text()
                if not opening.startswith("0"):
                    raise CannotConnect(f"unexpected socket greeting {opening[:40]!r}")
                info = _json_or_empty(opening[1:])
                self.greeting = info
                self._ping_interval = info.get("pingInterval", 25000) / 1000
                self._ping_timeout = info.get("pingTimeout", 20000) / 1000
                await self._ws.send_str(
                    "40" + json.dumps({"token": self._token}, separators=_JSON_SEPARATORS)
                )
                self.last_sent = time.monotonic()
                while True:
                    packet = await self._receive_text()
                    if packet == "2":
                        await self._ws.send_str("3")
                    elif packet.startswith("40"):
                        return
                    elif packet.startswith("44"):
                        detail = _json_or_empty(packet[2:])
                        message = str(detail.get("message", packet[2:]))
                        if "auth" in message.lower():
                            raise SocketAuthError(message)
                        raise CannotConnect(f"socket connect refused: {message}")
                    elif packet.startswith("42"):
                        self._dispatch(packet)
        except SeranovaError:
            await self.close()
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            await self.close()
            raise CannotConnect(f"socket connect failed: {err!r}") from err

    async def run(self) -> None:
        """Read packets until the connection ends; always raises a SeranovaError."""
        idle_limit = self._ping_interval + self._ping_timeout + 5
        while True:
            try:
                packet = await self._receive_text(timeout=idle_limit)
            except TimeoutError as err:
                raise SocketClosed("no ping from the server") from err
            except aiohttp.ClientError as err:
                raise SocketClosed(repr(err)) from err
            if not packet:
                continue
            if packet == "2":
                self.pings += 1
                await self._ws.send_str("3")
            elif packet.startswith("42"):
                self._dispatch(packet)
            elif packet.startswith("44"):
                raise SocketAuthError(packet[2:])
            elif packet.startswith(("41", "1")):
                raise SocketClosed(f"the server closed the session ({packet[:80]!r})")

    async def emit(self, payload: dict[str, Any]) -> None:
        """Send one 'message' event (NotSent when nothing was written)."""
        if not self.connected:
            raise NotSent("not connected")
        try:
            await self._ws.send_str(
                "42" + json.dumps(["message", payload], separators=_JSON_SEPARATORS)
            )
            self.last_sent = time.monotonic()
        except (aiohttp.ClientError, ConnectionError) as err:
            raise SocketClosed(repr(err)) from err

    async def close(self) -> None:
        """Close the websocket if open."""
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def _receive_text(self, timeout: float | None = None) -> str:
        msg = await self._ws.receive(timeout=timeout)
        if msg.type is aiohttp.WSMsgType.TEXT:
            return msg.data
        if msg.type is aiohttp.WSMsgType.BINARY:
            return msg.data.decode(errors="replace")
        if msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        ):
            raise SocketClosed(f"websocket {msg.type.name.lower()}")
        return ""

    def _dispatch(self, packet: str) -> None:
        body = packet[2:]
        start = body.find("[")
        if start < 0:
            return
        try:
            args = json.loads(body[start:])
        except ValueError:
            _LOGGER.debug("Unparsable event packet: %.200s", packet)
            return
        if not isinstance(args, list) or not args:
            return
        if args[0] != "message":
            _LOGGER.debug("Ignoring socket event %r", args[0])
            return
        frames: list[dict[str, Any]] = []
        for arg in args[1:]:
            frames.extend(decode_payload(arg))
        if frames:
            self._on_frames(frames)


def _json_or_empty(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def async_probe_system(
    session: aiohttp.ClientSession,
    token: str,
    imei: str,
    app_key: str,
    user_code: str,
    timeout: float = 12,
) -> str:
    """Ask the device for its info once and classify the answer.

    Returns "ok", "offline", "no_reply", "unknown_uid", "bad_app_key" or "bad_user_code".
    """
    result = "no_reply"
    done = asyncio.Event()

    def on_frames(frames: list[dict[str, Any]]) -> None:
        nonlocal result
        for frame in frames:
            uid = frame.get("UID")
            if uid is not None and str(uid) != str(imei):
                continue
            error = as_int(frame.get("ERROR"))
            if error in (2, 4, 5):
                result = {2: "unknown_uid", 4: "bad_app_key", 5: "bad_user_code"}[error]
                done.set()
            elif error == 3:
                result = "offline"
            if as_int(frame.get("CMD")) == CMD_INFO:
                result = "ok"
                done.set()

    conn = SocketIoConnection(session, token, on_frames)
    await conn.connect()
    reader = asyncio.get_running_loop().create_task(conn.run())
    try:
        frame = build_frame(imei, app_key, 0, CMD_INFO, "", user_code)
        for _ in range(3):
            await conn.emit(frame)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout / 3):
                    await done.wait()
            if done.is_set():
                break
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, SeranovaError):
            await reader
        await conn.close()
    return result
