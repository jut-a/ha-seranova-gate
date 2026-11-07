# SERANOVA / PROGATE protocol notes

Decoded from the public SERANOVA web app (`seranova.eu/dist/js/app.*.js`), the PROGATE
installation manual and live observation. The SERANOVA phone apps are WebView wrappers of the
same web app (`window.webkit.messageHandlers.*`, `window.Android.*`), so the bundle is the
reference — no need to intercept the phone apps.

## Topology

```
PROGATE ──(cellular, raw TCP)──> cloud.topkodas.lt:10000
                                      │
browser / app / this integration <──(socket.io over wss)── cloud.topkodas.lt:10004
                                      │
                      seranova.eu (REST: login, account, settings)
```

- The device keeps an outbound TCP session to the address set by SMS
  `INST<installer pw> 009 ADDR#PORT#PING#KEY#` (default `cloud.topkodas.lt#10000#600#123456#`),
  sending a keep-alive every `PING` seconds. Port 10000 is plain TCP with a proprietary
  protocol: it closes the connection on HTTP, MQTT CONNECT and JSON probes. `INST<pw> 010 1|0`
  turns the cloud link on/off.
- Clients never talk to the device directly; the cloud relays frames.

## REST (seranova.eu)

| Request | Purpose |
| --- | --- |
| `GET /login` | Sets cookies; the page has `<meta name="csrf-token" content="…">`. |
| `POST /login/auth` `{email, password, csrf}` | Reply `message`: `Login Success` (+ `socketSessionToken`, `accessToken`), `requires2FA` (+ `tempToken`), `IncorrectPassword`, `AccountNotFound`, `Captcha failed`. |
| `POST /api/auth/refresh` | Cookie-based; returns new `accessToken` / `socketSessionToken`. |
| `POST /command {uid, cmd:"11", val:null}` | List of the account's systems (`uid` = IMEI, `SysName`, `address`, …). |
| `POST /command {uid, cmd:"7", val:null}` | User data incl. the current system. |

The REST API is cookie based: keep the cookies per account. The login sets cookies even on
failure (`PHPSESSID`, `accessToken`, `refreshToken`, `expire`, `userid`, …).

## Socket (wss://cloud.topkodas.lt:10004)

Socket.IO v4 / Engine.IO v4, websocket transport only, path `/socket.io/`:

```
S: 0{"sid":…,"upgrades":[],"pingInterval":10000,"pingTimeout":5000,"maxPayload":1000000}
C: 40{"token":"<socketSessionToken>"}
S: 40{"sid":…}                         ok
S: 44{"message":"Authentication error"} bad/expired token → refresh or log in again
S: 2   C: 3                             ping every 10 s, answer within 5 s
C: 42["message",{…frame…}]
S: 42["message","<one or more JSON objects back to back>"]
```

While the device is not connected to the cloud, the server answers a command for it with
`{"ERROR":3}` and then closes the client's session within 0.1–0.3 s — every session, for as
long as the device stays away. A client that reconnects at a fixed short interval hammers the
server; the integration backs off exponentially (2 s → 300 s) for sessions shorter than 30 s
and, after three of them, goes back to Home Assistant's "Retrying setup".

A websocket that does not send the `40` connect within 45 s is closed by the server
(socket.io's default `connectTimeout`). Expect server-initiated closes (`41` / `1`) of
authenticated sessions too — for a while they came every ~100 s, cause unknown — and
reconnect within a few seconds; pushed events sent while disconnected are not replayed.

**Server messages can carry several JSON objects concatenated in one string**, e.g.
`{"ack":{"cmd":2}}{"UID":"…","CMD":10,…}`; split them with a brace/string-aware scanner before
parsing (the web app does the same).

### Command frame

```json
{"IMEI": "<uid>", "KEY": "<App Key>", "MSG": {"SEQ": 0, "CMD": 6, "CODE": "<user code>", "VAL": "1#2#"}}
```

`SEQ` is a client counter starting at 0 per session. `CODE` is omitted for CMD 2.

| CMD | Direction | Meaning |
| --- | --- | --- |
| 1 | → / ← | System info request (`CODE`, `VAL ""`) / reply `VAL` string (see below). The reply comes within seconds even while the device runs no commands — most likely the cloud's copy of the device's connect message, so it is no proof the device is reachable. |
| 2 | → | "Prepare" — the web app sends it before every output command, **without CODE**, `VAL null`. Acked with `{"ack":{"cmd":2}}`. Safe to re-send. |
| 5 | → | Output level: `VAL "<n>#<state>"` (preceded by CMD 2). |
| 6 | → | **Output pulse**: `VAL "<n>#<seconds>#"` (same as SMS `USER<code> 022 n#seconds#`). Acked with `{"ack":{"cmd":6}}`. |
| 10 | → / ← | State request (`CODE`, `VAL ""`) / reply, also pushed after a change — system/outputs: `{"OUT":"000,010,020,030","ZN":"0000,0100,0200,0300","SYS":"010D,0210,0300,0400","SV":23.3,"RSSI":31,"TIME":…}` (`SV` = supply voltage). |
| 11 | ← | Sensors: `{"SEN":"000000000000","TIME":…}`. |
| 14 | ← | Event: `{"EVENT":{"CID":"142200001","TXT":"…","TIME":…,"COPT":"…","NOTE":""}}`. |

Every frame from the device carries `UID` (the IMEI). `TIME` is a UTC Unix timestamp.

### ERROR field

| Value | Meaning |
| --- | --- |
| 0, 1, 6 | OK |
| 2 | UID not found |
| 3 | Device offline |
| 4 | Wrong App Key |
| 5 | Wrong user code |

### CMD 1 VAL

Comma-separated `KEY:value` pairs, e.g.
`CON:1,PING:600,OBJ:1234,IMEI:…,V:<fw>,BT:PROGATEv2_boot_v2,SN:…,HW:PROGATE,HWID:2,PCBID:3,OEMID:1,ICCID:…,MODID:1,MODFW:V01,RSSI:31,APPKEY:…,NAME:Object Name`.
It contains identifiers and the App Key — redact before sharing.

### Events (CMD 14)

`CID` = qualifier (`1` new / `3` restore) + 3-digit **Contact ID** code + 2-digit partition +
3-digit user/zone number. `142200001` = new, 422 *access granted*, partition 00, user 001.
`TXT` is what the web app shows, one item per line: object name (the device `NAME`), event
description, then the user or zone name and its label:

```
Object Name
Access Gained by
John
User 1
```

## Wake the device before an output command

The cloud acknowledges every command at once whether or not the device is listening. In
field tests a CMD 2 → CMD 6 sequence sent on its own reached the device about one time in
three (the rest were acked and lost), while the web app opened the gate every time. The
difference is what an open page sends **before** the button is pressed:

1. The system page shows a grey dot, then sends **CMD 10** with `CODE` = user code, `VAL ""`
   (a state request).
2. Every widget on the page (system panel, each output, sensors) runs the same small state
   machine: no info yet → CMD 1 (`CODE`, `VAL ""`); once a CMD 1 frame arrives → CMD 10; then
   idle, asking for CMD 10 again every 60 s (panel) or 120 s (outputs) while the page is open.
3. An unanswered request is re-sent after 4 s (the re-sends omit `CODE`); after 2 failures the
   machine starts over, so an open page keeps asking until the device answers.
4. A request counts as answered only by the **frame with that CMD**, never by `{ack}`. The dot
   turns green on a CMD 1 frame; the output states appear with the CMD 10 frame (usually 1–2 s
   after login).

By the time the user presses a button the device has just answered, and the CMD 6 lands. Only
`message` frames are sent over the socket; there is no subscribe event and no MQTT. The REST
call `POST /command {uid, cmd:"12"}` is made only when a system is picked in the list.

A CMD 10 frame carries the device's own `TIME` (UTC epoch), so a reply whose `TIME` is within
a minute or so of now proves a device round trip; an old `TIME` is a stored copy. The
integration (0.2.3+) sends CMD 10 before every pulse and on each poll, re-sending every 3 s
for about 21 s, and sends CMD 2 and CMD 6 only after a fresh CMD 10 frame. If none comes, the
pulse fails with stage `wake` and nothing that can switch an output was sent.

## Output commands must not be retried

The web app re-sends a command frame every second (up to 10 extra times) until it sees the
expected reply — for CMD 6 an ack **and** a CMD 10 frame. A page that is open gets CMD 10
updates quickly. A client that does not get CMD 10 keeps re-sending, and **the device runs the
re-sent CMD 6 again**: one button press produced two "Access Gained" events 4 s apart. The
integration therefore re-sends only CMD 2 and sends CMD 6 exactly once.

## The cloud's ack is not delivery

`{"ack":{"cmd":6}}` arrives 0.2–0.5 s after the command, far too fast for a round trip to a
cellular device: it is the **cloud's** acknowledgement. In one test the ack came and the
device never ran the command (nothing in its event log). Only the device's own CMD 14 event
with Contact ID 422 ("Access Gained") proves the output switched; CMD 10 frames are no proof
either, they also arrive whenever an app polls the device (they prove the device is reachable,
not that it ran the command).

## Never reuse a SEQ

Observed with a client whose SEQ restarted at 0 on every start (so CMD 6 always went out with
the same small SEQ): one press ran **three** times (2 s apart), the next one **not at all**
although the cloud acked it — while presses with fresh SEQ values ran once. The web app starts
at 0 per page load too, but polls constantly, so its SEQ at the moment of a press is large and
different every time. Keep a counter that is stored across restarts and never repeats.

## SMS control

Summary of all SMS commands from the PROGATE manual: [SMS.md](SMS.md). The ones that matter
here: `USER<code> 022 N#SECONDS#` pulses output N (the SMS twin of CMD 6), `INST<pw> 009 …` sets
the cloud server, port, keep-alive and App Key, `INST<pw> 010 1|0` turns the cloud link on/off.
