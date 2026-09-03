# Access Control System

A two-factor physical access control system for the Raspberry Pi. A user scans an RFID card, then verifies their identity with a fingerprint sensor. Access is granted only if both factors match. As of v1.5, **all four peripherals — RFID reader, fingerprint sensor, buzzer, and LCD — are mandatory**: the system will not start the menu until every one of them initializes successfully. Includes a setup script that checks and configures the Pi automatically.

## How it works

1. On startup, the system checks all four required peripherals (RFID, fingerprint sensor, buzzer, LCD). If any is missing, it retries every 5 seconds, logs what's missing, shows it on the LCD if the LCD itself is up, and sounds a distinct buzzer alert pattern — the app will not proceed to the menu until all four are online.
2. Scan an RFID card on the reader.
3. If the card matches the stored master UID (the system's sole admin), the fingerprint sensor activates.
4. If the fingerprint matches a registered user, and that user is within their allowed access hours (if any are set), access is granted.
5. Every attempt (granted, denied, error) is logged with a timestamp and a session ID that ties together every event from a single scan-to-result cycle.
6. After repeated failed attempts, the system locks out scanning. The lockout cooldown escalates (roughly doubling) with each new lockout and persists across restarts, then slowly decays back to normal after enough clean time has passed.

## Hardware

| Component | Model used | Connection |
|---|---|---|
| Raspberry Pi | Any model with SPI + UART (tested on Pi 3/4) | — |
| RFID reader | MFRC522 | SPI |
| Fingerprint sensor | R307 / R307s | UART (`/dev/serial0`) |
| Buzzer | Active or passive | GPIO (BOARD pin 12 by default) |
| LCD | 16x2 character LCD — either an I2C backpack (PCF8574) or direct-wired GPIO | I2C (pins 3/5) or GPIO (BOARD pins 32/26/13/15/18/16 by default) — see "16x2 LCD" below |

**All four components above are required as of v1.5.** The system will not enter the menu or scanner mode unless every one initializes successfully at startup — see "Mandatory hardware check" below.

## Wiring (Raspberry Pi 3B+)

All pin numbers below are **physical (BOARD) pin numbers** on the 40-pin header, matching what the code and `setup.sh` expect.

### MFRC522 (RFID reader) — SPI

| MFRC522 pin | Pi physical pin | Pi function |
|---|---|---|
| 3.3V | 1 (or 17) | 3.3V power — **do not use 5V** |
| RST | 22 | Any free GPIO |
| GND | 6 (or any GND) | Ground |
| IRQ | not connected | — |
| MISO | 21 | SPI0 MISO |
| MOSI | 19 | SPI0 MOSI |
| SCK | 23 | SPI0 SCLK |
| SDA (SS/CS) | 24 | SPI0 CE0 |

### R307 / R307s (fingerprint sensor) — UART

| R307 wire | Pi physical pin | Pi function |
|---|---|---|
| VCC | 2 or 4 | 5V power |
| GND | 6 (or any GND) | Ground |
| TX | 10 | GPIO15 / RXD (Pi RX ← sensor TX) |
| RX | 8 | GPIO14 / TXD (Pi TX → sensor RX) |

The sensor runs its logic at 3.3V-compatible UART levels but is typically powered from 5V — check your specific module's datasheet before wiring. TX/RX must be crossed (sensor TX → Pi RX, sensor RX → Pi TX), which the table above already reflects.

### Buzzer — GPIO

| Buzzer pin | Pi physical pin | Pi function |
|---|---|---|
| Signal / + | 12 | GPIO18 (PWM-capable) |
| GND / − | 6 (or any GND) | Ground |

Works with either active or passive buzzers — the code drives it with software PWM either way. If you wire it to a different physical pin, update `BUZZER_PIN` in `run.py` to match. **This component is mandatory as of v1.5** — see "Mandatory hardware check" below.

### 16x2 LCD — I2C or GPIO direct-wired

Two wiring styles are supported, controlled by `LCD_INTERFACE` in `run.py` (default `"auto"`, which tries I2C first and falls back to GPIO — so either display type works without editing any settings). **This component is mandatory as of v1.5** regardless of which interface is used.

#### Option A: I2C backpack (PCF8574) — 4 wires, recommended

| LCD backpack pin | Pi physical pin | Pi function |
|---|---|---|
| VCC | 2 or 4 | 5V power |
| GND | 6 (or any GND) | Ground |
| SDA | 3 | I2C1 SDA |
| SCL | 5 | I2C1 SCL |

Enable I2C first (`sudo raspi-config` → Interface Options → I2C), then confirm the backpack's address with `i2cdetect -y 1` (usually `0x27` or `0x3F`). By default `LCD_I2C_ADDRESS = None` in `run.py`, which auto-scans both common addresses; set it explicitly if your backpack uses a different one. Requires the `smbus2` package (installed by `setup.sh`).

#### Option B: direct-wired GPIO, no backpack — 6 signal wires

All numbers are **physical (BOARD) pin numbers**, to match the RFID reader and buzzer above — RPi.GPIO only allows one numbering mode per process, so every piece of hardware in this project has to agree on BOARD.

| LCD pin | LCD pin name | Pi physical pin | Pi function |
|---|---|---|---|
| 1 | VSS (GND) | 6 (or any GND) | Ground |
| 2 | VDD (+5V) | 2 or 4 | 5V power |
| 3 | V0 (contrast) | — | Via resistor to GND rail — contrast adjustment |
| 4 | RS | 32 | Any free GPIO |
| 5 | R/W | — | Tie to GND rail (always write mode) |
| 6 | E | 26 | Any free GPIO |
| 11 | D4 | 13 | Any free GPIO — **moved**, see note below |
| 12 | D5 | 15 | Any free GPIO — **moved**, see note below |
| 13 | D6 | 18 | Any free GPIO |
| 14 | D7 | 16 | Any free GPIO |
| 15 | A (backlight +) | — | Via resistor to +5V rail |
| 16 | K (backlight −) | — | GND rail |

**Note on D4/D5:** if you're following an existing wiring plan for this LCD, you may have seen D4 → pin 24 and D5 → pin 22 elsewhere. Those two specific pins are already used by the RFID reader in this project (SDA/CS and RST respectively), so D4 and D5 have been moved here to pins 13 and 15 instead — physically adjacent to D6/D7 (18/16), so the wiring stays easy to lay out on a breadboard. RS, E, D6, and D7 are unchanged from a typical wiring plan. If you rewire the LCD to different physical pins, update `LCD_PIN_RS`, `LCD_PIN_E`, and `LCD_PINS_DATA` in `run.py` to match.

A GPIO-wired display is write-only (R/W is tied to GND), so the app can confirm the pins toggled correctly but can't confirm the screen actually displayed anything — a totally blank screen (no backlight) with GPIO wiring is almost always a power/contrast/backlight wiring issue, not a software one. An I2C display can be confirmed present with `i2cdetect` before you even run the app, which is easier to debug.

## OS setup (what actually needs to be configured, and why)

The sensor needs a clean, dedicated UART — by default the Pi's serial port
is either occupied by a login shell, or (on boards with onboard Bluetooth)
shared with Bluetooth on an unstable clock. Both have to be dealt with.

**1. Enable the UART and turn off the serial login shell.**

Either via `sudo raspi-config` → Interface Options → Serial Port →
"login shell over serial" = **No**, "serial hardware enabled" = **Yes** —
or by editing `/boot/firmware/config.txt` directly and confirming
`cmdline.txt` has no `console=serial0,...` entry. Verified working values:

`/boot/firmware/config.txt`, in the `[all]` section:
```
enable_uart=1
dtoverlay=disable-bt
```

`/boot/firmware/cmdline.txt` — should **not** contain a `console=serial0...`
or `console=ttyAMA0...` entry. Just `console=tty1` (plus your normal root/
boot parameters) is correct.

**2. Free the full UART from Bluetooth.**

`dtoverlay=disable-bt` above does the actual work — without it,
`/dev/serial0` maps to the mini-UART, which Bluetooth also uses and whose
clock isn't stable enough for reliable 57600 baud, causing intermittent
read errors. As a belt-and-suspenders step (not strictly required once the
overlay is set, but prevents Bluetooth from ever re-claiming the interface
after an OS update), also disable the service:

```bash
sudo systemctl disable bluetooth.service
sudo systemctl mask bluetooth.service
```

**3. Reboot, then verify.**

```bash
sudo reboot
ls -l /dev/serial0
```

You want to see:
```
serial0 -> ttyAMA0
```

If it instead points to `ttyS0`, `disable-bt` didn't take effect — check
`config.txt` and reboot again.

> **Note on Raspberry Pi 5:** the UART is routed through the RP1 I/O chip
> rather than directly off the SoC, so `/dev/serial0` behaves slightly
> differently under the hood than on Pi 3/4/Zero. The same two settings
> (`enable_uart=1` + `dtoverlay=disable-bt`) are still correct and
> sufficient — RP1 doesn't change what you need to set, just how it's
> implemented internally.

## Software Setup

Clone the repository onto your Pi, then run the setup script to check and configure everything needed:

```bash
chmod +x setup.sh
sudo ./setup.sh --auto
```

If SPI or UART was enabled for the first time, reboot before continuing:

```bash
sudo reboot
```

## Running

```bash
python3 run.py
```

You'll see a menu:

```
1. Start Scanner Mode
2. Enroll New Fingerprint
3. Delete Fingerprint
4. Change Master RFID Card (also lets you set a display name for the admin)
5. Show System Status
6. View Recent Security Logs
7. Edit User Access Schedule
8. Exit
```

On first run, set a master RFID card (option 4) before starting the scanner.

## Roles

There are no configurable roles in this build. **The master RFID card holder is the sole admin** — enrolling/deleting fingerprints, changing the master card, and editing schedules are all menu actions available to whoever is running `run.py` at the terminal (there's no separate login for the app itself; physical/terminal access to the Pi is the actual admin boundary). Every enrolled fingerprint is a regular user, subject to whatever access schedule (if any) is set for their slot.

The master card can optionally be given a display name (set via **Change Master RFID Card**, option 4) — if set, it's shown when the card is scanned and in **Show System Status**; if left blank, the admin is just labeled "admin".

## Enrolling a user

1. Choose **Enroll New Fingerprint** from the menu.
2. Enter the user's name.
3. Place the same finger on the sensor twice, as prompted.
4. Optionally set an access-hours restriction (e.g. `09:00-18:00`) — leave blank for unrestricted access. Overnight windows like `22:00-06:00` are supported.
5. The fingerprint is stored on the sensor itself; only the name, slot number, and schedule are saved locally.

You can change a user's schedule later without re-enrolling via **Edit User Access Schedule** (option 7).

## Lockout behavior

After 3 consecutive denied attempts, scanning locks out for a cooldown period. Unlike a simple fixed cooldown, this build:

- **Escalates**: each new lockout roughly doubles the previous cooldown (10s → 20s → 40s → ...), capped at 300 seconds, so repeated failed attempts take progressively longer.
- **Persists across restarts**: the lockout and escalation state live in `fingerprint_database.json`, so restarting the app (or the Pi) does not reset an active lockout or the escalation level.
- **Decays over time**: once a lockout period has fully expired without triggering again, the escalation level resets back down, so a single lockout long ago doesn't permanently saddle a legitimate user with a longer cooldown.

Current lockout state (remaining cooldown, consecutive failures, and escalation count) is visible any time via **Show System Status** (option 5).

## Files created at runtime

| File | Purpose |
|---|---|
| `fingerprint_database.json` | Master RFID UID, slot-to-name mappings, per-user schedules, and persistent lockout state |
| `fingerprint_database.json.bak` | Automatic backup, written before each save |
| `access_log.jsonl` | Line-delimited log of every access attempt and admin action (mirrors the latest daily log, kept for tools that expect a fixed filename) |
| `logs/access_log-YYYY-MM-DD.jsonl` | One log file per calendar day, so logs don't grow without bound; each line includes a `session` ID correlating every event from a single RFID-scan-to-result cycle |

## LCD status messages

When idle it shows `Access Control / Ready`. During a scan it mirrors the flow: `Card detected / Checking...`, then `RFID OK / Scan finger...` while waiting on the fingerprint sensor, then a two-line `ACCESS GRANTED` (with the user's name) or `ACCESS DENIED` (with the reason — unknown card, finger mismatch, timeout, or outside allowed hours) result. During a lockout it shows `LOCKED OUT / Wait Ns` with the live remaining cooldown. At startup, if any required peripheral is missing, it shows `HARDWARE ERROR` with the missing component name(s) (when the LCD itself is one of the ones that's up). You can test it independently any time from the menu: **Show System Status** (option 5) → LCD section → "Test LCD now?".
