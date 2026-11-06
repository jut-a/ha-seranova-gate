# Seranova Gate — Home Assistant integration for the Topkodas PROGATE

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=jut-a&repository=ha-seranova-gate&category=integration)
[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=seranova_gate)

Control a **Topkodas PROGATE** cellular gate controller from Home Assistant through the
**SERANOVA** cloud (the same cloud the SERANOVA app and [seranova.eu](https://seranova.eu) use).

> **Unofficial.** Not affiliated with or endorsed by UAB Topkodas. The cloud protocol was
> decoded from the public SERANOVA web app and can change without notice. You use this
> to control a physical gate — test it while watching the gate.

## Features

- UI setup: sign in with your SERANOVA e-mail and password, pick your system, enter the
  App Key and user code (checked against the device before the entry is created).
- **Persistent cloud connection** with automatic reconnect and token refresh.
- **Button per output** (up to 4) that pulses the output for a configurable number of seconds.
- **Online** connectivity sensor (cloud connected and the device answering).
- **Event entity** for everything the device reports (access granted / denied, alarms,
  troubles, restores, tests), decoded into readable fields such as
  `message: "Access Gained by John (User 1)"`. Also fired as the `seranova_gate_event` bus event.
- `seranova_gate.open` action with an optional response, so automations can fall back to
  SMS or a phone call **only when it is safe** (see below).
- No extra Python requirements.

## Requirements

- A PROGATE with **SERA Cloud Service enabled** and a SIM with mobile data (the device must
  show as online in the SERANOVA app).
- A SERANOVA account that signs in with **e-mail and password**. Google / Apple sign-in and
  two-factor authentication are not supported.
- Home Assistant 2025.3 or newer (tested on 2026.11).

## Installation

### Marketplace (Home Assistant 2026.11 and newer)

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=jut-a&repository=ha-seranova-gate&category=integration)

Or by hand: **Settings → Marketplace → Add from link** →
`https://github.com/jut-a/ha-seranova-gate`, then install **Seranova Gate (Topkodas PROGATE)**.
The setup starts right after the install; updates later need a restart.

### HACS (Home Assistant before 2026.11)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jut-a&repository=ha-seranova-gate&category=integration)

Or by hand: HACS → ⋮ → **Custom repositories** → add
`https://github.com/jut-a/ha-seranova-gate`, category **Integration**, install
**Seranova Gate (Topkodas PROGATE)** and restart Home Assistant.

### Manual

Copy `custom_components/seranova_gate` into your Home Assistant `config/custom_components/`
folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=seranova_gate)

Or: Settings → Devices & services → **Add integration** → *Seranova Gate*.

| Step | What to enter |
| --- | --- |
| Sign in | SERANOVA e-mail and password |
| System | Pick the system from your account (or type the IMEI) |
| Access codes | **App Key**: the device's SERA Cloud Service key (factory default `123456` — change it). **User code**: your code in the device's user table, the one the SERANOVA app uses. |

**Options** (⚙ on the integration): enable/name up to four outputs and set each pulse length
(default: output 1 "Gate", 2 s), and the status poll interval (default 10 min, `0` = off).
**Reconfigure** changes the login, the codes or the name; if the password stops working,
Home Assistant asks you to sign in again.

## Entities

| Entity | Description |
| --- | --- |
| `button.<system>_<output>` | Pulses the output. Raises an error if the device did not confirm it. |
| `binary_sensor.<system>_online` | Connectivity (holds its state through a short reconnect). Attributes: `last_seen`, `last_error`. |
| `sensor.<system>_last_event` | Text of the last device event, e.g. `Access Gained by John (User 1)`; shows in the logbook and history, restored after a restart. Attributes: `event_type`, `subject`, `user_or_zone`, `code`, `device_time`. |
| `event.<system>_event` | Last device event. Types: `access_granted`, `access_denied`, `alarm`, `trouble`, `restore`, `test`, `event`. Attributes: `message`, `subject`, `subject_detail`, `user_or_zone`, `partition`, `description`, `object`, `code`, `cid`, `device_time`, `text`. |

## The `seranova_gate.open` action

```yaml
action: seranova_gate.open
data:
  output: 1      # optional, default 1
  seconds: 2     # optional, default = the output's configured pulse length
response_variable: gate
```

The response is `{success, stage, error, fallback_safe, acked}`. `stage` is `connect`,
`wake` or `open`. **`fallback_safe` is `true` only when the output command never reached the
device.** If the command was sent but not confirmed, the gate may already be moving — sending
another pulse (by SMS, call or a retry) can stop or reverse it.

Example: open from the cloud, fall back to an SMS only when that cannot double-trigger:

```yaml
- action: seranova_gate.open
  response_variable: gate
- if: "{{ not gate.success and gate.fallback_safe }}"
  then:
    - action: notify.my_sms_gateway   # your SMS service; message format: USER<code> 022 1#2#
      data:
        message: "USER123456 022 1#2#"
```

Example: notify on every access:

```yaml
triggers:
  - trigger: state
    entity_id: event.my_gate_event
    attribute: event_type
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'access_granted' }}"
actions:
  - action: notify.notify
    data:
      message: "{{ trigger.to_state.attributes.message }}"
```

## Safety: why the output command is never retried

The device does **not** de-duplicate a re-sent output command. The official web app re-sends
every second until it sees a state update; when that update is slow, the gate receives two
pulses. This integration sends the output command **exactly once** and only retries the
harmless "prepare" step before it. Details in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Troubleshooting

- **Online is off**: check the device in the SERANOVA app; the SIM needs mobile data and SERA
  Cloud Service must be on. The device keeps its cloud link alive with a ping every
  `PING` seconds (default 600).
- **Wrong App Key / user code**: use *Reconfigure*.
- **Diagnostics**: device page → *Download diagnostics* shows the connection state and the last
  frame of each kind (identifiers redacted). Attach it to issues.

## License

[MIT](LICENSE)
