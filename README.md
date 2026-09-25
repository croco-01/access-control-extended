# Access Control

A beginner-friendly, GUI-only two-factor access-control app for a Raspberry Pi. Access is granted only after the configured **master RFID card** and a registered **fingerprint** are both verified. `gui.py` is the application entry point; `run.py` provides the hardware and access-control backend.

The buzzer and LCD are optional feedback devices. The GUI can start without them, but their features remain unavailable until connected.

## Before you begin

This guide uses **physical pin numbers** on the Raspberry Pi's 40-pin header, also called `BOARD` numbering. These are not BCM/GPIO numbers used by many online tutorials.

Your direct-wired 1602A/16x2 LCD uses GPIO mode by default:

```python
LCD_INTERFACE = "gpio"
```

Do not copy a standalone LCD wiring example unchanged into this full project. Several common LCD pins clash with the MFRC522 RFID reader's SPI pins.

## Parts

| Part | Needed? | Notes |
|---|---|---|
| Raspberry Pi with 40-pin header | Yes | Pi 3, 4, or similar with SPI and UART |
| MFRC522 RFID reader | Yes for scanning | **3.3 V only** |
| R307/R307s fingerprint sensor | Yes for scanning | Usually powered from 5 V |
| 16x2 HD44780/1602A LCD | Optional | Direct GPIO wiring is below |
| Active or passive buzzer | Optional | Audio feedback |
| Full-size breadboard and jumpers | Recommended | Safely distributes power and signals |
| 10 kΩ potentiometer | Recommended | LCD contrast control |
| 220–1 kΩ resistor | Maybe needed | LCD backlight, unless built into your module |

## Breadboard safety

1. Shut down and unplug the Pi before changing wires.
2. Every module must share the Pi's ground.
3. Keep separate breadboard rails for 5 V and 3.3 V.
4. Connect the MFRC522 only to **3.3 V**. Never connect it to 5 V.
5. Use one power source only. Do not back-feed the Pi through its 5 V pin.
6. Some breadboards split their power rails halfway along; bridge the halves or use one side only.

```text
Pi physical pin 2 or 4 (5 V)   -> breadboard +5 V rail
Pi physical pin 1 or 17 (3.3 V)-> breadboard +3.3 V rail
Pi physical pin 6 (GND)        -> breadboard GND rail

LCD and fingerprint sensor     -> use +5 V where stated below
MFRC522                         -> use +3.3 V only
All module grounds              -> common GND rail
```

## Complete wiring map

All Pi pins in this guide are **physical pin numbers**.

### MFRC522 RFID reader (SPI)

| MFRC522 pin | Pi pin | Purpose |
|---|---:|---|
| 3.3V | 1 or 17 | 3.3 V power only |
| GND | 6 | Ground |
| RST | 22 | Reset |
| SDA / SS | 24 | SPI CE0 chip-select |
| SCK | 23 | SPI clock |
| MOSI | 19 | SPI MOSI |
| MISO | 21 | SPI MISO |
| IRQ | Leave unconnected | Not used |

### Fingerprint sensor (UART)

| Sensor wire | Pi pin | Purpose |
|---|---:|---|
| VCC | 2 or 4 | 5 V power, if specified by your sensor |
| GND | 6 | Ground |
| TX | 10 | Pi RXD—sensor sends data to Pi |
| RX | 8 | Pi TXD—Pi sends data to sensor |

TX and RX are crossed. Check your sensor's voltage specification. If its RX input is not explicitly 3.3 V tolerant, use a logic-level converter between Pi pin 8 and sensor RX.

### Buzzer (optional)

| Buzzer pin | Pi pin |
|---|---:|
| Signal / `+` | 12 |
| GND / `−` | 6 |

For a bare buzzer that needs more current than a GPIO can supply, use a transistor driver rather than connecting it directly.

### Direct-wired 16x2 LCD (default GPIO mode)

Only LCD data pins D4–D7 are used. D0–D3 stay unconnected.

| LCD pin | Label | Connect to | Notes |
|---:|---|---:|---|
| 1 | VSS | GND rail | Ground |
| 2 | VDD | 5 V rail | Power |
| 3 | V0 | 10 kΩ potentiometer wiper | Pot outer legs go to 5 V and GND |
| 4 | RS | Pi 32 | GPIO signal |
| 5 | R/W | GND rail | Write-only mode |
| 6 | E | Pi 29 | GPIO signal |
| 11 | D4 | Pi 13 | GPIO signal |
| 12 | D5 | Pi 15 | GPIO signal |
| 13 | D6 | Pi 18 | GPIO signal |
| 14 | D7 | Pi 16 | GPIO signal |
| 15 | A / LED+ | 5 V through resistor if needed | Backlight positive |
| 16 | K / LED− | GND rail | Backlight ground |

### Critical LCD pin-conflict warning

Many LCD-only tutorials use physical pins **24**, **22**, and **26**. Do not use those pins for this project:

| LCD signal | Do not use | Conflict | Use instead |
|---|---:|---|---:|
| D4 | 24 | MFRC522 SPI CE0 | 13 |
| D5 | 22 | MFRC522 reset | 15 |
| E | 26 | SPI CE1 | 29 |

Keep RS → 32, D6 → 18, and D7 → 16. If an LCD-only test works but the complete project fails, these conflicting pins are the likely cause.

> A blue backlight with no text usually means the LCD has power but contrast is wrong. Turn the 10 kΩ potentiometer slowly before changing code.

### Optional I2C LCD backpack

The software also supports an I2C backpack. Set `LCD_INTERFACE = "i2c"` in `run.py`, then wire VCC → 5 V, GND → ground, SDA → physical pin 3, and SCL → physical pin 5. Setup enables I2C when available; reboot if setup says one is needed.

## Software setup

1. Copy or clone this project to your Pi.
2. Run setup:

   ```bash
   chmod +x setup.sh
   sudo ./setup.sh
   ```

   No extra flag is needed: `setup.sh` automatically installs missing packages, adds the login user to required groups, enables SPI/UART/I2C when needed through `raspi-config`, and disables the serial login shell. It also checks GPIO, LCD pins, and optional hardware. If it changes a Pi interface, reboot when it tells you to.

3. If asked, reboot:

   ```bash
   sudo reboot
   ```

4. Start the GUI from the Pi desktop terminal:

   ```bash
   python3 gui.py
   ```

The touchscreen GUI requires a desktop display session. `run.py` is the shared backend and is not the app to launch.

### Manual Pi interface settings (fallback only)

Normally you do not need this section because `setup.sh` performs these actions automatically. Use it only if setup reports that `raspi-config` is unavailable or a configuration step failed.

Open:

```bash
sudo raspi-config
```

Under **Interface Options**:

- Enable **SPI** for the MFRC522.
- Enable **Serial Port hardware** for the fingerprint sensor.
- Disable the **serial login shell** so it does not occupy the fingerprint UART.
- Enable **I2C** only when using an I2C LCD backpack.

## First run

1. Launch `python3 gui.py` and wait for startup to finish.
2. Open **Master Card**, then scan the RFID card that will be the administrator card.
3. Open **Enroll User**, enter a unique user name, and follow the two fingerprint prompts.
4. Open **Start Scanner** and press **Start Scanning** to test access.

The scanner needs the master RFID card first, then a fingerprint registered in the local database.

## GUI screens

| Screen | Action |
|---|---|
| Start Scanner | Scan cards and verify fingerprints |
| Enroll User | Register a fingerprint and optional access hours |
| Manage Users | Change schedules or delete a user |
| Master Card | Set or replace the administrator RFID card |
| System Status | Review hardware/database status and test buzzer/LCD |
| Security Log | Review recent security events |

## Troubleshooting

### LCD is backlit but no text appears

1. Adjust the 10 kΩ contrast potentiometer on LCD pin 3 (V0).
2. Confirm LCD pin 5 (R/W) is connected to ground.
3. Confirm `LCD_INTERFACE = "gpio"` in `run.py`.
4. Recheck the six signal pins: RS 32, E 29, D4 13, D5 15, D6 18, D7 16.
5. Ensure D4/D5/E are not connected to 24/22/26 from an LCD-only tutorial.
6. Open **System Status**, run **Test LCD**, and read the diagnostic there.

### LCD works alone but fails with RFID connected

There is a pin conflict. Rewire LCD E, D4, and D5 to physical pins 29, 13, and 15. Keep the MFRC522 on its SPI pins.

### RFID reader is offline

- Confirm it is powered from 3.3 V, never 5 V.
- Enable SPI, then reboot.
- Recheck SDA/SS → 24, SCK → 23, MOSI → 19, MISO → 21, and RST → 22.

### Fingerprint sensor is offline

- Confirm TX/RX are crossed: sensor TX → Pi 10; sensor RX → Pi 8.
- Enable serial hardware and disable the serial login shell.
- Confirm the sensor has power and shares ground with the Pi.

### A device says OFFLINE but the GUI starts

This is intentional. Hardware is optional at startup. Use **System Status** to see the failing component and LCD error details.

## Data files

| File | Purpose |
|---|---|
| `fingerprint_database.json` | Master RFID UID, users, schedules, and lockout state |
| `fingerprint_database.json.bak` | Backup made before database saves |
| `access_log.jsonl` | Latest security-event log |
| `logs/access_log-YYYY-MM-DD.jsonl` | Daily rotated security logs |

Do not edit `fingerprint_database.json` while the application is running. Back it up before making manual changes.

## Security notes

- The master RFID card is the first factor; a registered fingerprint is the second.
- Anyone with physical access to the Pi or its logged-in desktop can use the administration screens. Secure the Pi and its user account.
- Repeated denials trigger a persistent, escalating lockout.
- Do not publish database or log files: they contain RFID identifiers and user names.
