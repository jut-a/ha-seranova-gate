# Changelog

## Unreleased

### Changed
- Manifest `categories`: `blinds_curtains` (gates and garage doors) and
  `security_safety.locks_access`, for browsing by category in the Marketplace.

### Documentation
- README leads with the product name (Topkodas PROGATE GSM gate controller), names the
  device as it appears in the app, SERA2 and on its label, adds a Lithuanian summary and an FAQ.

## 0.2.5

### Fixed
- An unexpected error while handling a cloud frame no longer ends the connection for good
  (the entry stayed "loaded" without a socket until a restart). Frame handling and the
  connection loop now log such an error and carry on; an event whose `TIME` is out of range
  is kept without a device time instead of raising.
- Retrying setup reuses the entry's signed-in SERANOVA session instead of logging in again on
  every retry (every ~80 s while the gate controller is offline), which could trigger the
  captcha. The session is kept per config entry (entries on one account never reset each
  other's token) and replaced when the login or Home Assistant's HTTP session changes.
- Only a state request that really timed out counts as "did not answer"; a dropped socket no
  longer does, and a refused App Key / user code is reported as such.
- Setup now waits up to 75 s for the gate controller (the worst case is 56 s; it was 45 s).
- A gate controller whose clock is off by more than 90 s no longer looks unreachable: a state
  reply stamped later than any seen before also proves it answered, and the error names the
  clock offset when every reply looked out of date.

### Changed
- `pyproject.toml` version follows the manifest; icons for `get_events` and Last event time.
- README: how fast the event log grows.

## 0.2.4

### Added
- **Event log** kept outside the recorder, so it is never purged: every event the gate
  controller reports (time, type, user or zone, text), stored per device in Home Assistant's
  storage and kept when the integration is removed and added again.
- Option **Keep events in the event log (days)**; `0` (the default) keeps them forever.
- Action **`seranova_gate.get_events`**: logged events, newest first, filtered by `since`,
  `until`, `event_type` and `limit`.
- Sensor **Last event time** (timestamp, by the gate controller's own clock), restored after a
  restart.

### Changed
- **Setup completes only when the gate controller answers a state request.** Otherwise the
  integration shows *Retrying setup* with the reason (controller offline, cloud keeps closing
  the session, no answer, cloud unreachable, App Key / user code rejected) and Home Assistant
  retries on its own; a rejected login asks to re-authenticate. A running integration that loses
  the controller the same way logs a warning and goes back to *Retrying setup*.
- **Reconnect backoff:** a cloud session that ends within 30 s counts as a failed connect, so
  the wait grows exponentially (2 s → 5 min) instead of reconnecting every 2 s. A gate press
  cuts the wait short.
- The options form reloads the integration through Home Assistant's own mechanism; the update
  listener is gone. This removes the warning *"has an update listener and should use it for
  scheduling a reload"* after Reconfigure / re-authentication, which would stop working in
  Home Assistant 2026.12.
- **Minimum Home Assistant version: 2025.8** (was 2025.3).

### Documentation
- README: the setup's *user code* is the user access code (SERA2 → Users), not the SMS
  password; changing the App Key took a device offline in testing; event log and
  `get_events`; *Retrying setup* in Troubleshooting.
- `docs/SMS.md`: App Key caution on command `009`. `docs/PROTOCOL.md`: how the cloud behaves
  while the device is offline (`ERROR 3`, sessions closed within 0.3 s).

## 0.2.3

- Before every pulse, ask the gate controller for its state (`CMD 10`) and send the output
  command only after its own answer; without this most commands were acknowledged by the
  cloud and lost. No answer: stage `wake`, nothing sent, `fallback_safe: true`.
- A failed `CMD 2` reports stage `connect`. **Online** follows the device's own frames.
- After the device confirmed a pulse, a new press is allowed 5 s later (stop / close from
  Home Assistant); double taps, unconfirmed pulses and other accesses keep the full lockout.
- Diagnostics: state checks and per-press wake timings. README: arrival-by-car examples,
  *Tested with*, picture. `docs/SMS.md`: the PROGATE SMS commands.

Earlier versions: see the [GitHub releases](https://github.com/jut-a/ha-seranova-gate/releases).
