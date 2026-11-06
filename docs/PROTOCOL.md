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
| 1 | → / ← | System info request (`CODE`, `VAL ""`) / reply `VAL` string (see below). |
| 2 | → | "Prepare" — the web app sends it before every output command, **without CODE**, `VAL null`. Acked with `{"ack":{"cmd":2}}`. Safe to re-send. |
| 5 | → | Output level: `VAL "<n>#<state>"` (preceded by CMD 2). |
| 6 | → | **Output pulse**: `VAL "<n>#<seconds>#"` (same as SMS `USER<code> 022 n#seconds#`). Acked with `{"ack":{"cmd":6}}`. |
| 10 | ← | System/outputs: `{"OUT":"000,010,020,030","ZN":"0000,0100,0200,0300","SYS":"010D,0210,0300,0400","SV":23.3,"RSSI":31,"TIME":…}` (`SV` = supply voltage). |
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

## Output commands must not be retried

The web app re-sends a command frame every second (up to 10 extra times) until it sees the
expected reply — for CMD 6 an ack **and** a CMD 10 frame. A page that is open gets CMD 10
updates quickly. A client that does not get CMD 10 keeps re-sending, and **the device runs the
re-sent CMD 6 again**: one button press produced two "Access Gained" events 4 s apart. The
integration therefore re-sends only CMD 2 and sends CMD 6 exactly once, treating its ack, a
CMD 10 or the CMD 14 access event as confirmation (10 s timeout).

## SMS control (from the manual)

- `USER<SMS user password> 021 N#1|0` — latch output N on/off.
- `USER<SMS user password> 022 N#SECONDS#` — pulse output N.
- `USER…` commands are accepted only from phone numbers registered in the device's user table;
  `INST<installer password> …` commands from any number.
- `USER<pw> 100 4` / `100 5` — output states / full system state, replied by SMS.
