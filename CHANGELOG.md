# Changelog

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
