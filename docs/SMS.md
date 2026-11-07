# PROGATE SMS command reference

A summary of the SMS commands in the Topkodas **PROGATE user manual, version V-2026-10-15,
section 10** ([PDF](https://topkodas.com/Downloads/media/Manuals/PROGATE_UM_EN.pdf)), arranged
by task. The manual is the authoritative source; check it if something here does not work on
your firmware. SMS is the PROGATE's own channel and does not go through the SERANOVA cloud, so it
keeps working when the cloud path does not (see the SMS fallback example in the README).

## Format

```
INST<installer code> <command> <parameters>
USER<SMS user code> <command> <parameters>
```

- **`INST…` commands** work from **any** phone number that knows the installer code
  (default `000000`).
- **`USER…` commands** work only from phone numbers in the device's user list (SERA2 → Users /
  Access control); SMS from other numbers are ignored. Default SMS user code: `123456`.
- **Change both default codes** before the device is put to use (commands `090` and `091`).
- Exactly **one space** between the parts (the manual writes it as `˽`), nothing before or after
  the text. **Latin characters only**, no Unicode.
- Parameters inside one group are separated by `#`; most groups also end with `#`.
- Several commands can follow each other in one SMS (the manual's example:
  `INST<code> 011 1 004 9##1#10#Guest#`), except after `094`, whose text runs to the end.
- Phone numbers are written without `+`, with the country and operator code, up to 16 digits.

## Outputs — the gate (USER and INST)

| Command | What it does |
| --- | --- |
| `020 N` | Invert the state of output N (each SMS toggles it). |
| `021 N#ST` | Output N on (`ST` = `1`) or off (`0`). |
| `022 N#SECONDS#` | **Pulse** output N for SECONDS (0–999999). |

`N` is the logical output 1–4: **OUT1 is the RELAY output** (NC / C / NO terminals), OUT2… are
the programmable I/O terminals, depending on the configuration.

Example — pulse the gate relay for 2 s, the same as this integration's button (which sends the
equivalent `CMD 6` with `VAL "1#2#"` through the cloud):

```
USER<SMS user code> 022 1#2#
```

## Status requests (USER and INST)

`100 N` — the device answers by SMS to the sender:

| N | Answer |
| --- | --- |
| `1` | System test: module information (IMEI, firmware, signal level …). |
| `2` | Values of the active sensors. |
| `3` | States of the active zones. |
| `4` | Output states. |
| `5` | Full system state: inputs, outputs and the arm state. |

## Alarm system (USER and INST)

| Command | What it does |
| --- | --- |
| `030 ST` | System mode: `0` disarm, `1` arm, `2` stay, `3` sleep. |
| `031 ZN#BYP` | Zone ZN (1–5): `1` bypass, `0` active. |
| `094 TEL#TEXT` | Send TEXT from the module to TEL (local or `+` international format). Nothing after it is read as a command. |

## Installer only (INST)

### Users who control the gate by a call

| Command | What it does |
| --- | --- |
| `004 ID#TEL#OUT#OPT#NAME#` | Put a caller at index ID (1–800). `OUT`: output the call controls (`0` none, `1` = OUT1/RELAY, `2` = OUT2 …). `OPT`: two flags, left to right — user enabled, may arm/disarm by call (e.g. `10`). `NAME`: up to 31 Latin characters. |
| `012 TEL#OUT#OPT#NAME#` | The same, in the first free index. |
| `013 TEL#NAME#` | Add an enabled caller in the first free index, controlling OUT1 (RELAY). |
| `005 TEL#` | Delete the caller with this number. |
| `006 ID` | Delete the caller at index ID. |
| `018` | List the callers by SMS: enabled, index, number, output. |
| `011 E` | Guest mode on (`1`) / off (`0`): every unknown caller is handled with the settings of user 9 (set them with `004 9#…`). |

### Notification numbers (admins)

| Command | What it does |
| --- | --- |
| `001 ID#TEL#SMS#DIAL#` | Admin ID (1–8) gets event SMS and/or calls. `SMS` and `DIAL` are strings of `1`/`0` flags, one per event group in this order: 1 alarm/restore (CID 100 group), 2 open/close (CID 400), 3 troubles (CID 300), 4 sensor 1–32 alarm/restore, 5 test events (CID 600), 6 other events, 7 input/zone 1, 8 input/zone 2, and so on. |
| `002 ID` | Delete admin ID. |
| `003` | Delete all users. |

### Network and the SERANOVA cloud

| Command | What it does |
| --- | --- |
| `008 APN#LOGIN#PSW#` | Mobile data settings, up to 31 characters each, e.g. `008 internet###`. |
| `009 ADDR#PORT#PING#KEY#` | Cloud connection. Default `cloud.topkodas.lt#10000#600#123456#`: server, TCP port, keep-alive in seconds, **App Key** (the key this integration asks for). Topkodas recommends keeping the defaults. |
| `010 E` | SERA Cloud Service on (`1`) / off (`0`). Needed for the app and this integration; turn it off when the SIM has no data plan. |
| `007 P#PER#HH:mm#` | Periodic test report: `P` = `0` off, `1` every PER days, `2` every PER hours (1–99999), at HH:mm. |

### Output configuration

| Command | What it does |
| --- | --- |
| `019 N#P` | Operating mode of output N, P = 0–24: 0 disabled, 1 bell, 2 buzzer, 3 flash, 4 system state, 5 arm status, 6 remote control & automation, 7 AC OK, 8 battery OK, 9 system armed status, 10 alarm indication, 11 lost primary channel, 12 lost secondary channel, 13 fire sensor, 14 RH sensor trouble, 15 access gained, 16 stay armed status, 17 sleep armed status, 18 pulse on arm/disarm, 19 output state, 20 zone OK, 21 activate by arm/disarm command, 22 by sleep/disarm command, 23 by stay/disarm command, 24 access control. |

### Sensors and keys

| Command | What it does |
| --- | --- |
| `070 N#VALUE#` | Sensor N high alarm level (SMS "High Alarm"), format `0000.00`. |
| `071 N#VALUE#` | Sensor N low alarm level (SMS "Low Alarm"). |
| `072 N#VALUE#` | Sensor N level above which the assigned output switches on (e.g. cooling). |
| `073 N#VALUE#` | Sensor N level below which the assigned output switches on (e.g. heating). |
| `063 S` | iButton / RFID keys: `1` learn mode on, `0` off, `2` delete the keys that are touched to the reader. |

### System

| Command | What it does |
| --- | --- |
| `090 NEWCODE` | New installer code. |
| `091 NEWCODE` | New SMS user code. |
| `092` | Restart the module. |
| `093 yyyy/MM/dd#HH:mm#` | Set the clock (normally synchronised from the network or the cloud). |
| `095 E` | Zone walk test on (`1`) / off (`0`); arming or disarming ends it. |
| `096` | Reset the fire sensors. |
