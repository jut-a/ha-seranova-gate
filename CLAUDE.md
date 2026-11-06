# ha-seranova-gate — working notes

Home Assistant custom integration (domain `seranova_gate`) for the Topkodas PROGATE gate
controller via the SERANOVA cloud. Public repo: https://github.com/jut-a/ha-seranova-gate.

## Layout

- `custom_components/seranova_gate/` — the integration (HACS layout)
  - `api.py` — REST login/refresh/system list on seranova.eu + a hand-written socket.io
    (Engine.IO v4) client on aiohttp + `async_probe_system`. No Home Assistant imports.
  - `hub.py` — one persistent connection per config entry: reconnect loop, frame routing,
    command queue (`async_pulse`, `async_poll`), state + event listeners.
  - `events.py` — CMD 14 event decoding (Contact ID + TXT lines). No HA imports.
  - `config_flow.py` — user → pick → codes, reauth, reconfigure, options (outputs + poll).
  - `button.py`, `binary_sensor.py`, `event.py`, `entity.py`, `diagnostics.py`.
  - `translations/en.json` is the only strings file (custom integrations don't use strings.json).
- `docs/PROTOCOL.md` — everything known about the cloud and device protocol. Update it when
  you learn something new.
- `hacs.json`, `.github/workflows/validate.yaml` (hassfest + HACS action, brands ignored).

## Hard rules

1. **Never re-send CMD 6 (or CMD 5).** The device does not de-duplicate a re-sent frame with
   the same SEQ; a retry pulses the output again, which stops or reverses a gate. CMD 2
   ("prepare", no CODE) is the only command that may be retried. If you change the command
   path, keep `CommandResult.fallback_safe` meaning "the output command was never delivered".
   Also: success only on the device's CMD 14 access event (the cloud ack is not delivery),
   never reuse a SEQ (the counter is stored per entry and survives restarts), and keep the
   minimum time between pulses — open/stop/close gate inputs are common.
2. **No personal data in the repo**: no IMEI, ICCID, App Key, user codes, phone numbers,
   e-mail addresses, real names or captured diagnostics — in code, tests, docs, commits or
   issue text. Use placeholders (`864000000000000`, `John`).
3. Keep `requirements: []`. HA's aiohttp is enough; don't add python-socketio.
4. Everything runs in the event loop; no blocking I/O.
5. Never commit, tag or push without the owner's explicit approval; propose the commit
   message first.

## Testing

Automated (CI runs both on every push):
- `pytest` — `tests/test_events.py` and `tests/test_api.py` run without Home Assistant (they load
  the HA-free modules by file path); `tests/test_pulse.py` needs `pip install -r
  requirements_test.txt` (HA's test kit) and drives `SeranovaHub.async_pulse` through a fake
  socket. **Its assertions encode hard rule 1 — CMD 6 is written exactly once in every
  scenario; never weaken them to make a change pass.**
- `ruff check .` (config in `pyproject.toml`; HA-style import order, line length 100).
- Dependabot (`.github/dependabot.yml`) opens `chore:` PRs — GitHub Actions monthly, the
  pinned test kit in `requirements_test.txt` weekly. A red test-kit bump means a new Home
  Assistant release breaks the integration: fix the code, don't pin the kit back.

Live: there is no simulator; test against a real device while someone watches the gate.
- Install into a HA instance by symlinking `custom_components/seranova_gate` into its
  `config/custom_components/`, then **restart HA** (a config-entry reload does not re-import
  Python modules).
- After a change to the command path: one press must produce exactly one "Access Gained"
  event in the SERANOVA event log.
- `api.py` / `events.py` can be exercised without HA (aiohttp only); a bad token must end in
  `SocketAuthError("Authentication error")`, a fake e-mail in `InvalidAuth("AccountNotFound")`.
- Diagnostics (device page → Download diagnostics) show the last frame of each CMD — the way to
  learn new frame formats. Check the redaction before sharing one.

## Releasing

1. Bump `version` in `custom_components/seranova_gate/manifest.json` (semver; 0.x until the
   integration has been tested in the field — first public version 0.2.0).
2. Commit, tag **exactly the manifest version** (e.g. `0.2.1`, no `v` prefix — the Home Assistant
   Marketplace expects the tag and `version` to match), push, and publish a GitHub release with
   notes; the Marketplace / HACS offer releases as updates.
3. The Validate workflow must be green (hassfest + HACS).

## Home Assistant Marketplace (HA 2026.11+, replaced HACS)

- Same catalog, same `hacs.json`; users install from Settings → Marketplace, or "Add from
  link" with the repo URL (needs a **public** repo).
- The `brands` check passes with an icon shipped in the integration:
  `custom_components/seranova_gate/brand/icon.png` (no Home Assistant or Topkodas logos).
  Until that exists the workflow ignores `brands`; the default catalog does not accept ignored
  checks. The catalog also wants repo topics, a description and issues enabled (the HACS
  `images` check applies to plugins and themes only). The HACS action reads these through
  the GitHub API, so it fails while the repo is private or missing them.
- Docs: https://contributors.home-assistant.io/marketplace/guides/integrations/ and
  …/marketplace/reference/validation/.
