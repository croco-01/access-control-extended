#!/usr/bin/env python3

import os
import json
import time
import signal
import threading
import uuid
import copy
from datetime import datetime, time as dtime

import serial
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522
import adafruit_fingerprint
# RPLCD's GPIO and I2C drivers are imported lazily inside initialize_lcd(),
# not here -- so a system with only one of the two RPLCD extras installed
# (or only smbus2, or neither) doesn't crash at startup before we even
# know which interface this run needs.

VERSION = "1.5"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "fingerprint_database.json")
DB_BACKUP = DB_FILE + ".bak"
SECURITY_LOG = os.path.join(BASE_DIR, "access_log.jsonl")

# Peripherals are optional by default so the UI can be tested on a machine
# without the complete RFID/fingerprint/buzzer/LCD setup.  Individual
# features safely remain unavailable while their device is offline.
REQUIRE_ALL_HARDWARE = False
HARDWARE_RETRY_SECONDS = 5

# ---------------------------------------------------------- access rules --
#
# Regular (non-admin) users can optionally be restricted to specific
# hours of the day. Format: {"start": "HH:MM", "end": "HH:MM"}, 24h clock.
# An entry of None (default) means "no restriction, any time." Admins
# (the master RFID holder) are never subject to a schedule.
DEFAULT_SCHEDULE = None  # e.g. {"start": "09:00", "end": "18:00"}

# Persistent lockout: failed-attempt state survives a restart, and the
# cooldown grows with repeated lockouts instead of resetting every time
# (simple exponential backoff), which makes brute-forcing much less
# practical than a flat, always-the-same cooldown.
LOCKOUT_BACKOFF_MULTIPLIER = 2
LOCKOUT_MAX_SECONDS = 300  # cap the backoff so a legitimate user is never
                            # locked out for an unreasonable length of time


FINGERPRINT_UART = "/dev/serial0"
FINGERPRINT_BAUD = 57600
FINGER_WAIT_TIMEOUT = 12
FINGER_RELEASE_TIMEOUT = 5
FINGERPRINT_SETTLE_TIME = 0.5
COMMUNICATION_ERROR_THRESHOLD = 2
MAX_FINGERPRINT_SLOTS = 162  # R307/R307s hardware template capacity

RFID_SCAN_DELAY = 0.15
MAX_FINGERPRINT_ATTEMPTS = 3
LOCKOUT_THRESHOLD = 3       # consecutive denied attempts before lockout
LOCKOUT_SECONDS = 10

BUZZER_PIN = 12                 # BOARD (physical) pin numbering, to match
                                 # the mfrc522 library, which forces
                                 # GPIO.setmode(GPIO.BOARD) internally.
                                 # Change to match your wiring.
BUZZER_ACTIVE_HIGH = True       # False if using an active-low buzzer/relay module
BUZZER_TONE_HZ = 2000           # PWM frequency driving the buzzer. Works for
                                 # both types: a passive buzzer needs this
                                 # tone to make sound at all; an active
                                 # buzzer has its own oscillator and just
                                 # buzzes on any signal, ignoring the tone.
GRANTED_BUZZ_SECONDS = 0.3
DENIED_BUZZ_SECONDS = 5.0
# Distinct SOS-like pattern (3 short, 3 long, 3 short) used ONLY to signal
# a mandatory-hardware failure at boot, so a failure is audible even when
# the LCD itself is the thing that's down and can't show an error.
HARDWARE_FAILURE_PATTERN = (
    [(0.15, 0.15)] * 3 + [(0.5, 0.15)] * 3 + [(0.15, 0.15)] * 3
)

# Status LED. Entirely optional/best-effort, same as the buzzer -- NOT
# part of REQUIRE_ALL_HARDWARE's mandatory set, and never blocks startup
# or counts toward "missing hardware" even when REQUIRE_ALL_HARDWARE is
# True. A plain LED (with a current-limiting resistor) to GND is enough;
# no PWM/driver IC needed. Change LED_PIN if it conflicts with your wiring.
LED_PIN = 11                    # BOARD 11 (BCM17) -- free on this project's
                                 # pin map (checked against RFID/buzzer/LCD/UART).
LED_ACTIVE_HIGH = True          # False if wired so GPIO LOW lights the LED
LED_BLINK_INTERVAL = 0.5        # seconds on, seconds off, while blinking

# 16x2 character LCD. Two wiring styles are supported:
#   - "gpio": direct-wired, 6 GPIO lines (RS, E, D4-D7), no backpack.
#   - "i2c":  4-wire I2C backpack (PCF8574/PCF8574A), typical address
#             0x27 or 0x3F.
# LCD_INTERFACE = "auto" tries I2C first (a quick, non-destructive bus
# probe) and falls back to GPIO if nothing answers. If this installation
# only ever uses a direct-wired display, set it to "gpio" to skip the
# needless I2C probe and make that choice explicit. A GPIO LCD cannot be
# electrically detected while its R/W pin is tied to ground; successful
# initialization only proves the GPIO driver could be configured.
LCD_ENABLED = True
LCD_INTERFACE = "auto"   # "auto" | "gpio" | "i2c"

# --- GPIO (direct-wired) settings ---
# Pin numbers below are BOARD (physical) numbers, to match every other
# piece of hardware in this project -- mfrc522 forces GPIO.setmode
# (GPIO.BOARD) internally, and the buzzer uses BOARD too. RPi.GPIO only
# allows ONE numbering mode per process, so everything has to agree.
# RS, D6, D7 match the original wiring plan (BCM12/BOARD32, BCM24/BOARD18,
# BCM23/BOARD16). D4, D5, and E were all MOVED from their original pins:
#   - D4/D5: from BOARD24/BOARD22 to BOARD13/BOARD15, because 24 and 22
#     are already used by the RFID reader (SDA/CS and RST).
#   - E: from BOARD26 to BOARD29, because BOARD26 is BCM7 -- SPI0's CE1
#     line. Enabling SPI (needed for the RFID reader) via the standard
#     2-chip-select overlay reserves BOTH CE0 (BOARD24) and CE1 (BOARD26)
#     for the SPI peripheral's pinmux function, even though the RFID
#     reader only ever opens CE0. BOARD26 is therefore not reliably
#     usable as a plain GPIO once SPI is on, regardless of whether
#     anything actually talks to CE1.
# If you rewire the LCD to different physical pins, update the map below
# to match.
LCD_PIN_RS = 32                   # BOARD 32 (BCM12)
LCD_PIN_E = 29                    # BOARD 29 (BCM5) -- moved off BOARD26/CE1, see above
LCD_PINS_DATA = [13, 15, 18, 16]  # D4, D5, D6, D7 (D4/D5 moved off RFID pins)

# --- I2C (backpack) settings ---
LCD_I2C_ADDRESS = None    # e.g. 0x27 or 0x3F; None = auto-scan common addresses
LCD_I2C_PORT = 1          # I2C bus number; 1 on every Pi with a 40-pin header
LCD_I2C_EXPANDER = "PCF8574"  # PCF8574 covers the vast majority of backpacks
LCD_I2C_COMMON_ADDRESSES = (0x27, 0x3F)

LCD_COLS = 16
LCD_ROWS = 2

RESULT_NAMES = {
    adafruit_fingerprint.OK: "OK",
    adafruit_fingerprint.NOFINGER: "NO FINGER",
    adafruit_fingerprint.IMAGEFAIL: "IMAGE FAILURE",
    adafruit_fingerprint.IMAGEMESS: "IMAGE TOO MESSY",
    adafruit_fingerprint.FEATUREFAIL: "FEATURE FAILURE",
    adafruit_fingerprint.INVALIDIMAGE: "INVALID IMAGE",
    adafruit_fingerprint.NOMATCH: "NO MATCH",
    adafruit_fingerprint.NOTFOUND: "NOT FOUND",
    adafruit_fingerprint.ENROLLMISMATCH: "ENROLLMENT MISMATCH",
    adafruit_fingerprint.BADLOCATION: "BAD SLOT",
    adafruit_fingerprint.DBRANGEFAIL: "DATABASE RANGE ERROR",
    adafruit_fingerprint.FLASHERR: "FLASH ERROR",
}


def result_name(code):
    return RESULT_NAMES.get(code, f"UNKNOWN CODE {code}")


# ---------------------------------------------------------------- logging --

# Log rotation: one file per calendar day, named access_log-YYYY-MM-DD.jsonl,
# so the log doesn't grow without bound and old days are easy to archive
# or delete independently. SECURITY_LOG (the original fixed filename) is
# kept too, as a symlink-free "latest" pointer some tooling may expect --
# it's just today's file re-copied on rollover.
LOG_DIR = os.path.join(BASE_DIR, "logs")


def _today_log_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, f"access_log-{datetime.now():%Y-%m-%d}.jsonl")


def log(message, level="INFO"):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level}: {message}", flush=True)


# Correlation ID for one RFID-scan-to-result cycle, so every event logged
# during a single attempt (rfid_authorized, fingerprint_no_match,
# access_granted, etc.) can be grouped together when reviewing the log.
_current_session_id = None


def new_scan_session():
    global _current_session_id
    _current_session_id = uuid.uuid4().hex[:12]
    return _current_session_id


def security_event(event, **data):
    record = {"timestamp": datetime.now().isoformat(timespec="seconds"),
              "event": event, "session": _current_session_id, **data}
    line = json.dumps(record) + "\n"
    try:
        with open(_today_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
        # Best-effort mirror to the stable SECURITY_LOG filename too, so
        # anything that watches a fixed path (e.g. `tail -f`) keeps working.
        with open(SECURITY_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        log(f"Could not write security log: {e}", "ERROR")


# ------------------------------------------------------------- shutdown ----

shutdown_requested = False


def _signal_handler(signum, frame):
    global shutdown_requested
    if not shutdown_requested:
        shutdown_requested = True
        print("\n\nCtrl+C received. Stopping safely...", flush=True)


def install_signal_handlers():
    """Install the CLI shutdown handler.

    This is deliberately opt-in: importing this module from the Tk GUI must
    not replace Tk/Python's process-wide signal handling.
    """
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


# --------------------------------------------------------------- database --

DEFAULT_CONFIG = {
    "AUTHORIZED_UID": None,
    "admin_name": None,       # display name for the master RFID card holder;
                               # None until set via change_master_rfid()
    "user_mappings": {},       # slot(str) -> name(str)
    "user_schedules": {},      # slot(str) -> {"start": "HH:MM", "end": "HH:MM"}
                                # Applies to fingerprint users only. The
                                # master RFID card is the sole admin and
                                # is never subject to a schedule.
    "lockout_state": {         # persists across restarts
        "consecutive_failures": 0,
        "lockout_count": 0,       # how many times lockout has triggered,
                                   # drives the exponential backoff
        "locked_until": 0,        # unix timestamp, 0 = not locked
    },
}
config = copy.deepcopy(DEFAULT_CONFIG)


def normalize_config():
    global config
    if not isinstance(config, dict):
        config = copy.deepcopy(DEFAULT_CONFIG)
    config.setdefault("AUTHORIZED_UID", None)
    config.setdefault("admin_name", None)
    if not isinstance(config.get("user_mappings"), dict):
        config["user_mappings"] = {}
    if not isinstance(config.get("user_schedules"), dict):
        config["user_schedules"] = {}
    if not isinstance(config.get("lockout_state"), dict):
        config["lockout_state"] = copy.deepcopy(DEFAULT_CONFIG["lockout_state"])
    else:
        config["lockout_state"].setdefault("consecutive_failures", 0)
        config["lockout_state"].setdefault("lockout_count", 0)
        config["lockout_state"].setdefault("locked_until", 0)


def load_database():
    global config
    if not os.path.exists(DB_FILE):
        log("No database found. Creating new one.")
        normalize_config()
        save_database()
        return

    for path, label in ((DB_FILE, "database"), (DB_BACKUP, "backup")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            normalize_config()
            if label == "backup":
                log("Recovered database from backup.", "WARNING")
            else:
                log(f"Database loaded. {len(config['user_mappings'])} mapping(s).")
            return
        except (json.JSONDecodeError, OSError) as e:
            log(f"Could not load {label}: {e}", "ERROR")

    config = copy.deepcopy(DEFAULT_CONFIG)


def save_database():
    normalize_config()
    temp_file = DB_FILE + ".tmp"
    try:
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r", encoding="utf-8") as src, \
                     open(DB_BACKUP, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
            except OSError as e:
                log(f"Could not write backup: {e}", "WARNING")

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, DB_FILE)
        return True
    except OSError as e:
        log(f"Database save failed: {e}", "ERROR")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass
        return False


# ---------------------------------------------------------------- global ---

uart = None
finger = None
reader = None
finger_sensor_online = False
rfid_online = False


# ---------------------------------------------------- fingerprint sensor ---

def initialize_fingerprint():
    """Open the UART and bring up the R307/R307s. Call once at startup,
    and again only via recover_fingerprint_sensor() after a comms error."""
    global uart, finger, finger_sensor_online

    log("Initializing fingerprint sensor...")
    try:
        if uart is not None:
            try:
                uart.close()
            except OSError:
                pass
        uart = serial.Serial(FINGERPRINT_UART, baudrate=FINGERPRINT_BAUD,
                              timeout=1, write_timeout=1)
        try:
            uart.reset_input_buffer()
            uart.reset_output_buffer()
        except OSError:
            pass
        time.sleep(0.25)

        finger = adafruit_fingerprint.Adafruit_Fingerprint(uart)
        finger_sensor_online = True
        log("Fingerprint sensor online.")
        return True
    except (OSError, serial.SerialException, RuntimeError) as e:
        finger_sensor_online = False
        log(f"Fingerprint init failed: {e}", "ERROR")
        return False


def recover_fingerprint_sensor():
    """Only called after an actual UART communication error, never after
    a normal NOFINGER/NOTFOUND result."""
    global finger_sensor_online
    log("Attempting fingerprint sensor recovery...", "WARNING")
    finger_sensor_online = False
    if uart is not None:
        try:
            uart.reset_input_buffer()
            uart.reset_output_buffer()
        except OSError:
            pass
    time.sleep(0.5)
    return initialize_fingerprint()


def wait_for_no_finger(timeout=FINGER_RELEASE_TIMEOUT, cancel_event=None):
    if not finger_sensor_online or finger is None:
        return False
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if shutdown_requested or (cancel_event is not None and cancel_event.is_set()):
            return False
        try:
            if finger.get_image() == adafruit_fingerprint.NOFINGER:
                return True
        except (OSError, RuntimeError) as e:
            log(f"Comms error while checking finger release: {e}", "ERROR")
            return False
        time.sleep(0.15)
    return False


def wait_for_finger(timeout=FINGER_WAIT_TIMEOUT, cancel_event=None):
    """NOFINGER is a normal waiting state, not a failure.
    Returns True (finger captured) / False (timeout, no usable image) /
    None (comms error / shutdown). cancel_event is an optional
    threading.Event a caller (e.g. a GUI Cancel button) can set to break
    out early -- same effect as a timeout, just caller-triggered."""
    if not finger_sensor_online or finger is None:
        log("Fingerprint sensor is not available.", "ERROR")
        return None

    start = time.monotonic()
    comm_errors = 0
    imaging_failures = {adafruit_fingerprint.IMAGEFAIL, adafruit_fingerprint.IMAGEMESS,
                         adafruit_fingerprint.FEATUREFAIL, adafruit_fingerprint.INVALIDIMAGE}

    while time.monotonic() - start < timeout:
        if shutdown_requested or (cancel_event is not None and cancel_event.is_set()):
            return None
        try:
            result = finger.get_image()
        except (OSError, RuntimeError) as e:
            comm_errors += 1
            log(f"UART error ({comm_errors}/{COMMUNICATION_ERROR_THRESHOLD}): {e}", "ERROR")
            if comm_errors >= COMMUNICATION_ERROR_THRESHOLD:
                return None
            time.sleep(0.25)
            continue

        if result == adafruit_fingerprint.NOFINGER:
            comm_errors = 0
            time.sleep(0.12)
            continue
        if result == adafruit_fingerprint.OK:
            return True
        if result in imaging_failures:
            log(f"Imaging problem: {result_name(result)}", "WARNING")
            return False

        comm_errors += 1
        log(f"Unexpected sensor response: {result_name(result)}", "WARNING")
        if comm_errors >= COMMUNICATION_ERROR_THRESHOLD:
            return None
        time.sleep(0.2)

    return False


def get_sensor_template_count():
    if not finger_sensor_online or finger is None:
        return None
    try:
        if finger.count_templates() == adafruit_fingerprint.OK:
            return finger.template_count
    except (OSError, RuntimeError) as e:
        log(f"Could not read template count: {e}", "ERROR")
    return None


# ------------------------------------------------------------- fp auth -----

def authenticate_fingerprint(cancel_event=None):
    """Returns (status, slot, name, confidence).
    status in SUCCESS / NO_MATCH / TIMEOUT / COMMUNICATION_ERROR / SHUTDOWN.
    cancel_event is an optional threading.Event a caller (e.g. a GUI Stop
    button) can set to abort promptly, same effect as shutdown_requested."""
    if not finger_sensor_online or finger is None:
        return "COMMUNICATION_ERROR", None, None, None

    def canceled():
        return shutdown_requested or (cancel_event is not None and cancel_event.is_set())

    print(f"\nFingerprint authentication (max {MAX_FINGERPRINT_ATTEMPTS} attempts)")
    time.sleep(FINGERPRINT_SETTLE_TIME)

    if not wait_for_no_finger(cancel_event=cancel_event):
        log("Could not confirm sensor is clear.", "WARNING")

    for attempt in range(1, MAX_FINGERPRINT_ATTEMPTS + 1):
        if canceled():
            return "SHUTDOWN", None, None, None

        print(f"\nWaiting for fingerprint ({attempt}/{MAX_FINGERPRINT_ATTEMPTS})...")
        result = wait_for_finger(cancel_event=cancel_event)

        if result is None:
            if canceled():
                return "SHUTDOWN", None, None, None
            log("Fingerprint comms failure; not counted as a bad attempt.", "ERROR")
            security_event("fingerprint_communication_error")
            if recover_fingerprint_sensor():
                log("Recovery successful.")
                time.sleep(0.5)
                continue
            return "COMMUNICATION_ERROR", None, None, None

        if result is False:
            print("No usable fingerprint captured.")
            continue

        print("Fingerprint image captured.")
        try:
            if finger.image_2_tz(1) != adafruit_fingerprint.OK:
                continue

            print("Searching database...")
            match = finger.finger_fast_search()

            if match == adafruit_fingerprint.OK:
                slot, confidence = finger.finger_id, finger.confidence
                name = config["user_mappings"].get(str(slot))
                if name is None:
                    log(f"Fingerprint matched unmapped sensor slot #{slot}; denying access.", "WARNING")
                    security_event("fingerprint_unmapped", fingerprint_slot=slot,
                                   confidence=confidence)
                    return "NO_MATCH", None, None, None
                print(f"\nFingerprint matched! User: {name}  Slot: #{slot}  Confidence: {confidence}")
                security_event("fingerprint_matched", method="RFID + fingerprint",
                                fingerprint_slot=slot, user=name, confidence=confidence)
                return "SUCCESS", slot, name, confidence

            if match == adafruit_fingerprint.NOTFOUND:
                print("Fingerprint not recognized.")
                security_event("fingerprint_no_match")
                continue

            log(f"Fingerprint search failed: {result_name(match)}", "WARNING")
            security_event("fingerprint_search_error", result=result_name(match))

        except (OSError, RuntimeError) as e:
            log(f"Fingerprint processing error: {e}", "ERROR")
            security_event("fingerprint_processing_exception", error=str(e))
            continue

    return "NO_MATCH", None, None, None


# ------------------------------------------------------------------ RFID ---

def initialize_rfid():
    global reader, rfid_online
    log("Initializing RFID reader...")
    try:
        GPIO.setwarnings(False)
        reader = SimpleMFRC522()  # internally calls GPIO.setmode(GPIO.BOARD);
                                   # harmless no-op if buzzer already set it
        rfid_online = True
        log("RFID reader online.")
        return True
    except Exception as e:
        rfid_online = False
        log(f"RFID init failed: {e}", "ERROR")
        return False


def read_rfid_nonblocking():
    global rfid_online
    if not rfid_online or reader is None:
        return None
    try:
        uid, _text = reader.read_no_block()
        return uid
    except Exception as e:
        rfid_online = False
        log(f"RFID read error: {e}", "ERROR")
        security_event("rfid_reader_error", error=str(e))
        time.sleep(0.5)
        return None


# ---------------------------------------------------------------- buzzer ---
#
# Drives the buzzer with software PWM instead of a plain on/off level.
# This makes the SAME code work correctly whether the buzzer turns out to
# be active (has its own oscillator; produces sound on any signal,
# including PWM) or passive (needs a driven tone to make sound at all).
# We never need to know in advance which kind is connected.

buzzer_online = False
_buzzer_lock = threading.Lock()
_pwm = None


def initialize_buzzer():
    """Best-effort setup. The system is designed to work identically with
    or without a buzzer attached, so any failure here just leaves
    buzzer_online False and nothing else changes."""
    global buzzer_online, _pwm
    try:
        # BOARD mode is shared with the RFID reader (mfrc522 sets this
        # internally); setting it here first means whichever hardware
        # initializes first "wins" the mode and the other reuses it.
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(BUZZER_PIN, GPIO.OUT)
        GPIO.output(BUZZER_PIN, GPIO.LOW if BUZZER_ACTIVE_HIGH else GPIO.HIGH)
        _pwm = GPIO.PWM(BUZZER_PIN, BUZZER_TONE_HZ)
        buzzer_online = True
        log(f"Buzzer initialized on pin {BUZZER_PIN} (PWM {BUZZER_TONE_HZ}Hz).")
    except Exception as e:
        buzzer_online = False
        _pwm = None
        log(f"Buzzer not available (will run without it): {e}", "WARNING")


def _buzzer_on():
    """Starts PWM. If PWM setup ever fails at runtime (not just at init),
    falls back to a plain digital HIGH so an active buzzer still works
    even if something's wrong with the PWM channel."""
    try:
        _pwm.start(50)  # 50% duty cycle square wave
        return True
    except Exception as e:
        log(f"Buzzer PWM start failed, falling back to plain output: {e}", "WARNING")
        try:
            GPIO.output(BUZZER_PIN, GPIO.HIGH if BUZZER_ACTIVE_HIGH else GPIO.LOW)
            return True
        except Exception as e2:
            log(f"Buzzer GPIO write failed: {e2}", "WARNING")
            return False


def _buzzer_off():
    try:
        _pwm.stop()
    except Exception:
        pass
    try:
        GPIO.output(BUZZER_PIN, GPIO.LOW if BUZZER_ACTIVE_HIGH else GPIO.HIGH)
    except Exception:
        pass


def _buzz(duration, pattern=None):
    """Runs in a background thread so it never blocks scanning.
    pattern, if given, is a list of (on_seconds, off_seconds) pairs and
    duration is ignored; otherwise buzzes solid for `duration` seconds.
    Silently does nothing if the buzzer isn't available."""
    if not buzzer_online:
        return

    def worker():
        with _buzzer_lock:
            try:
                if pattern:
                    for on_s, off_s in pattern:
                        if not _buzzer_on():
                            return
                        time.sleep(on_s)
                        _buzzer_off()
                        time.sleep(off_s)
                else:
                    if _buzzer_on():
                        time.sleep(duration)
            finally:
                _buzzer_off()

    threading.Thread(target=worker, daemon=True).start()


def buzz_granted():
    """Single short buzz on access granted."""
    _buzz(GRANTED_BUZZ_SECONDS)


def buzz_denied():
    """Long buzz (default 5s) on access denied."""
    _buzz(DENIED_BUZZ_SECONDS)


def test_buzzer():
    """Used by the status/menu check. Returns True if a buzz was attempted."""
    if not buzzer_online:
        return False
    _buzz(0.3)
    return True


def buzz_hardware_failure():
    """Distinct alert pattern used ONLY when mandatory hardware is missing
    at boot. Runs synchronously (not via the background-thread _buzz
    helper) since it happens before the rest of the app is up, and we
    want it to fully finish before deciding what to do next. Best-effort:
    does nothing if the buzzer itself is the thing that's offline."""
    if not buzzer_online:
        return
    with _buzzer_lock:
        for on_s, off_s in HARDWARE_FAILURE_PATTERN:
            if not _buzzer_on():
                return
            time.sleep(on_s)
            _buzzer_off()
            time.sleep(off_s)


# ------------------------------------------------------------------- LED ---
#
# Simple on/off status LED. Same "fully optional, best-effort" pattern as
# the buzzer: any failure here just leaves led_online False and nothing
# else changes. Three states only -- off, blinking, solid on -- driven by
# a background thread for blinking so it never blocks scanning. Used by
# scanner_mode() (and the GUI's ScannerScreen, mirroring it) to give a
# glanceable visual: blinking while waiting for a card or fingerprint,
# solid on when access is granted, back to blinking for everything else
# (denied, error, timeout) and for the next idle cycle.

led_online = False
_led_lock = threading.Lock()
_led_stop_event = threading.Event()
_led_thread = None


def initialize_led():
    """Best-effort setup. The system is designed to work identically with
    or without an LED attached, so any failure here just leaves
    led_online False and nothing else changes. Never part of
    REQUIRE_ALL_HARDWARE's mandatory set."""
    global led_online
    try:
        # BOARD mode is shared with the RFID reader/buzzer/LCD; whichever
        # initializes first "wins" the mode and the rest reuse it.
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(LED_PIN, GPIO.OUT)
        GPIO.output(LED_PIN, GPIO.LOW if LED_ACTIVE_HIGH else GPIO.HIGH)
        led_online = True
        log(f"LED initialized on pin {LED_PIN}.")
        return True
    except Exception as e:
        led_online = False
        log(f"LED not available (will run without it): {e}", "WARNING")
        return False


def _led_write(on):
    try:
        GPIO.output(LED_PIN, (GPIO.HIGH if on else GPIO.LOW) if LED_ACTIVE_HIGH
                    else (GPIO.LOW if on else GPIO.HIGH))
        return True
    except Exception as e:
        log(f"LED GPIO write failed: {e}", "WARNING")
        return False


def _led_stop_blinking():
    """Stops any in-progress blink thread and waits for it to actually
    exit before returning, so callers never race a leftover blink
    thread's next toggle against the state they're about to set."""
    global _led_thread
    _led_stop_event.set()
    if _led_thread is not None and _led_thread.is_alive():
        _led_thread.join(timeout=LED_BLINK_INTERVAL + 0.5)
    _led_thread = None


def led_off():
    """Stops blinking (if any) and turns the LED fully off."""
    if not led_online:
        return
    with _led_lock:
        _led_stop_blinking()
        _led_write(False)


def led_solid_on():
    """Stops blinking (if any) and turns the LED on continuously -- used
    for the ACCESS GRANTED state."""
    if not led_online:
        return
    with _led_lock:
        _led_stop_blinking()
        _led_write(True)


def led_blink(interval=LED_BLINK_INTERVAL):
    """Starts (or restarts) continuous on/off blinking at `interval`
    seconds per phase, until led_off()/led_solid_on()/another led_blink()
    call stops it. This is the default/idle state: waiting for a card,
    waiting for a fingerprint, and every non-granted outcome (denied,
    error, timeout) all just leave the LED blinking, uninterrupted."""
    global _led_thread
    if not led_online:
        return
    with _led_lock:
        _led_stop_blinking()
        _led_stop_event.clear()
        stop_event = _led_stop_event

        def worker():
            state = True
            while not stop_event.is_set():
                if not _led_write(state):
                    return
                state = not state
                stop_event.wait(interval)
            _led_write(False)

        _led_thread = threading.Thread(target=worker, daemon=True)
        _led_thread.start()


def test_led():
    """Used by the status/menu check. Blinks briefly, then turns off.
    Returns True if the LED is available and a test was attempted."""
    if not led_online:
        return False
    led_blink(0.2)
    time.sleep(1.6)
    led_off()
    return True


# ------------------------------------------------------------------ LCD ----
#
# 16x2 character LCD, supporting either a direct 6-wire GPIO connection
# or a 4-wire I2C backpack (see LCD_INTERFACE above). Same "fully
# optional, best-effort" pattern as the buzzer: any failure here just
# leaves lcd_online False and the rest of the app runs unchanged,
# printing to the terminal exactly as before. Everything past
# initialize_lcd() (lcd_show, test_lcd, and every call site in the rest
# of the app) is interface-agnostic -- it just holds an RPLCD CharLCD
# object in `lcd` and doesn't care which backend produced it.

lcd_online = False
lcd = None
lcd_interface_used = None  # "gpio" or "i2c", set once init succeeds -- purely
                            # informational, shown in status/logs
lcd_last_error = None       # latest initialization/write error for diagnostics
_lcd_lock = threading.Lock()


def _probe_i2c_address(address):
    """Non-destructive check that something ACKs at this I2C address.
    Uses smbus2 (a light dependency RPLCD's i2c backend already needs)
    rather than instantiating CharLCD itself, so a failed probe can't
    leave a half-initialized display object behind."""
    try:
        from smbus2 import SMBus
    except ImportError:
        return None  # can't probe; caller decides how to handle that
    try:
        with SMBus(LCD_I2C_PORT) as bus:
            bus.write_quick(address)
        return True
    except OSError:
        return False


def _init_lcd_i2c():
    global lcd, lcd_interface_used
    from RPLCD.i2c import CharLCD as I2CCharLCD

    address = LCD_I2C_ADDRESS
    if address is None:
        for candidate in LCD_I2C_COMMON_ADDRESSES:
            result = _probe_i2c_address(candidate)
            if result is True:
                address = candidate
                break
            if result is None:
                # smbus2 missing -- can't scan OR confirm; let CharLCD's
                # own attempt below surface the real error instead of
                # silently trying every address blind.
                break
        if address is None:
            raise RuntimeError(
                "No I2C LCD found at common addresses "
                f"{[hex(a) for a in LCD_I2C_COMMON_ADDRESSES]} on bus "
                f"{LCD_I2C_PORT} (or smbus2 not installed to scan with). "
                "Run 'i2cdetect -y 1' to find the real address and set "
                "LCD_I2C_ADDRESS explicitly."
            )

    lcd = I2CCharLCD(
        i2c_expander=LCD_I2C_EXPANDER,
        address=address,
        port=LCD_I2C_PORT,
        cols=LCD_COLS,
        rows=LCD_ROWS,
    )
    lcd.clear()
    lcd_interface_used = "i2c"
    log(f"LCD initialized over I2C ({LCD_COLS}x{LCD_ROWS}, "
        f"expander={LCD_I2C_EXPANDER}, address={hex(address)}, "
        f"bus={LCD_I2C_PORT}).")


def _init_lcd_gpio():
    global lcd, lcd_interface_used
    from RPLCD.gpio import CharLCD as GPIOCharLCD

    # BOARD mode is shared with the RFID reader and buzzer (mfrc522
    # and initialize_buzzer() both set it); whichever runs first
    # wins the mode and everyone else just reuses it.
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    lcd = GPIOCharLCD(
        pin_rs=LCD_PIN_RS,
        pin_e=LCD_PIN_E,
        pins_data=LCD_PINS_DATA,
        numbering_mode=GPIO.BOARD,
        cols=LCD_COLS,
        rows=LCD_ROWS,
    )
    lcd.clear()
    lcd_interface_used = "gpio"
    log(f"LCD initialized over GPIO ({LCD_COLS}x{LCD_ROWS}, BOARD pins "
        f"RS={LCD_PIN_RS} E={LCD_PIN_E} D4-D7={LCD_PINS_DATA}; "
        "write-only, confirm the test message visually).")


def initialize_lcd():
    """Brings up the LCD using whichever interface LCD_INTERFACE selects.
    "auto" tries I2C first (cheap to probe, and the far more common
    hobbyist wiring these days) then falls back to GPIO -- so the exact
    same call works unmodified whether an I2C backpack or a direct-wired
    display is actually attached."""
    global lcd_online, lcd, lcd_interface_used, lcd_last_error
    if not LCD_ENABLED:
        lcd_online = False
        lcd_last_error = "LCD is disabled in configuration."
        return False

    order = {
        "i2c": (_init_lcd_i2c,),
        "gpio": (_init_lcd_gpio,),
        "auto": (_init_lcd_i2c, _init_lcd_gpio),
    }.get(LCD_INTERFACE)

    if order is None:
        log(f"Invalid LCD_INTERFACE {LCD_INTERFACE!r} (expected auto/gpio/i2c).",
            "ERROR")
        lcd_online = False
        lcd_last_error = f"Invalid LCD_INTERFACE: {LCD_INTERFACE!r}"
        return False

    errors = []
    for attempt in order:
        try:
            attempt()
            lcd_online = True
            lcd_last_error = None
            lcd_show("Access Control", "Ready")
            return True
        except Exception as e:
            errors.append(f"{attempt.__name__}: {e}")
            lcd = None
            continue

    lcd_online = False
    lcd_interface_used = None
    lcd_last_error = " | ".join(errors)
    log("LCD not available (will run without it): " + lcd_last_error,
        "WARNING")
    return False


def lcd_show(line1="", line2=""):
    """Best-effort two-line write. Silently does nothing if the LCD isn't
    available, so call sites never need to check lcd_online themselves.
    Identical for both interfaces -- RPLCD's I2C and GPIO CharLCD classes
    share the same write_string/cursor_pos/clear API."""
    global lcd_online, lcd, lcd_last_error
    if not lcd_online or lcd is None:
        return False
    with _lcd_lock:
        try:
            lcd.clear()
            lcd.write_string(line1[:LCD_COLS])
            lcd.cursor_pos = (1, 0)
            lcd.write_string(line2[:LCD_COLS])
            return True
        except Exception as e:
            lcd_online = False
            lcd = None
            lcd_last_error = f"LCD write failed: {e}"
            log(lcd_last_error, "WARNING")
            return False


def test_lcd():
    """Used by the status/menu check. Returns True if a message was shown.
    Note this only confirms the write call completed without error, not
    that the physical screen displayed it (true for GPIO-wired displays,
    which are typically write-only with R/W tied to GND; I2C displays
    CAN be read back, but RPLCD's i2c backend doesn't verify writes
    either, so the caveat applies to both interfaces as implemented)."""
    return lcd_show("Hello!", "LCD Working :)")


# ---------------------------------------------------------- access rules --

def is_within_schedule(schedule):
    """schedule is {"start": "HH:MM", "end": "HH:MM"} or None (unrestricted).
    Supports overnight windows (e.g. start > end, like 22:00-06:00)."""
    if not schedule:
        return True
    try:
        now = datetime.now().time()
        start_h, start_m = (int(x) for x in schedule["start"].split(":"))
        end_h, end_m = (int(x) for x in schedule["end"].split(":"))
        if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and
                0 <= end_h <= 23 and 0 <= end_m <= 59):
            raise ValueError("hour/minute out of range")
        start = dtime(start_h, start_m)
        end = dtime(end_h, end_m)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # overnight window
    except (KeyError, ValueError, TypeError) as e:
        log(f"Malformed schedule denied ({schedule}): {e}", "WARNING")
        return False  # A malformed restriction must not grant access.


def current_lockout_remaining():
    """Seconds left on the persistent lockout, 0 if not locked."""
    remaining = config["lockout_state"]["locked_until"] - time.time()
    return max(0, remaining)


def register_denial():
    """Call after any denied attempt. Applies exponential backoff: each
    time the failure threshold is hit again, the cooldown roughly doubles
    (capped), so repeated brute-force attempts get progressively more
    expensive instead of a constant, predictable cooldown."""
    state = config["lockout_state"]
    state["consecutive_failures"] += 1
    if state["consecutive_failures"] >= LOCKOUT_THRESHOLD:
        state["lockout_count"] += 1
        cooldown = min(
            LOCKOUT_SECONDS * (LOCKOUT_BACKOFF_MULTIPLIER ** (state["lockout_count"] - 1)),
            LOCKOUT_MAX_SECONDS,
        )
        state["locked_until"] = time.time() + cooldown
        state["consecutive_failures"] = 0
        save_database()
        return cooldown
    save_database()
    return None


def register_success():
    """Resets the failure streak on a granted access, but deliberately
    does NOT reset lockout_count -- that only decays over time (see
    decay_lockout_backoff), so a single success mid-attack can't be used
    to reset an attacker's escalating cooldown back to the base value."""
    state = config["lockout_state"]
    state["consecutive_failures"] = 0
    save_database()


def decay_lockout_backoff():
    """Slowly forgives the backoff level after a long period with no new
    lockouts, so a legitimate user who was locked out once a long time ago
    isn't stuck with an ever-growing cooldown forever. Called once at
    startup and once when entering scanner mode."""
    state = config["lockout_state"]
    if state["lockout_count"] > 0 and current_lockout_remaining() == 0:
        state["lockout_count"] = 0
        save_database()


# --------------------------------------------------------------- scanner ---

def scanner_mode():
    global shutdown_requested
    shutdown_requested = False

    print("\n" + "=" * 50)
    print("Scanner active.")
    print("Ctrl+C to return to the main menu.")
    print("=" * 50)

    if not rfid_online:
        print("RFID reader is offline. Connect it, then restart the application.")
        return

    if config["AUTHORIZED_UID"] is None:
        print("No master RFID card is set. Use menu option 4 first.")
        return

    decay_lockout_backoff()
    lcd_show("Access Control", "Ready")
    led_blink()

    while not shutdown_requested:
        remaining = current_lockout_remaining()
        if remaining > 0:
            lcd_show("LOCKED OUT", f"Wait {int(remaining)}s")
            time.sleep(min(1.0, remaining))
            continue

        uid = read_rfid_nonblocking()
        if uid is None:
            time.sleep(RFID_SCAN_DELAY)
            continue

        print(f"\nRFID card detected: {uid}")
        new_scan_session()
        lcd_show("Card detected", "Checking...")
        granted = False
        hardware_error = False

        if uid == config["AUTHORIZED_UID"]:
            admin_name = config.get("admin_name")
            admin_label = admin_name if admin_name else "admin"
            print(f"RFID authorized ({admin_label}). Proceeding to fingerprint "
                  "verification...")
            security_event("rfid_authorized", uid=uid, role="admin", admin_name=admin_name)
            lcd_show("RFID OK", "Scan finger...")
            time.sleep(FINGERPRINT_SETTLE_TIME)

            if not finger_sensor_online:
                hardware_error = True
                print("Fingerprint sensor is offline; access cannot be verified.")
                lcd_show("SENSOR OFFLINE", "Access unavailable")
                status = "HARDWARE_OFFLINE"
                slot = name = None
            else:
                status, slot, name, _confidence = authenticate_fingerprint()

            if status == "SUCCESS":
                schedule = config["user_schedules"].get(str(slot))
                if not is_within_schedule(schedule):
                    print(f"\n{'='*50}\nACCESS DENIED\n{name} is outside their "
                          f"allowed access hours ({schedule['start']}-{schedule['end']}).\n{'='*50}")
                    security_event("access_denied", reason="outside_schedule",
                                    user=name, slot=slot, schedule=schedule)
                    buzz_denied()
                    lcd_show("ACCESS DENIED", "Outside hours")
                else:
                    print(f"\n{'='*50}\nAccess granted for {name} (slot #{slot}).\n{'='*50}")
                    granted = True
                    security_event("access_granted", method="RFID + fingerprint",
                                   fingerprint_slot=slot, user=name)
                    buzz_granted()
                    led_solid_on()
                    lcd_show("ACCESS GRANTED", name[:LCD_COLS])
            elif status == "NO_MATCH":
                print(f"\n{'='*50}\nACCESS DENIED\nFingerprint verification failed.\n{'='*50}")
                security_event("access_denied", reason="fingerprint_not_recognized")
                buzz_denied()
                lcd_show("ACCESS DENIED", "Finger no match")
            elif status == "COMMUNICATION_ERROR":
                hardware_error = True
                print("\nAUTHENTICATION UNAVAILABLE: fingerprint sensor comms failed.")
                buzz_denied()
                lcd_show("SENSOR ERROR", "Try again later")
            elif status == "HARDWARE_OFFLINE":
                pass
            elif status == "SHUTDOWN":
                break
            else:
                print("\nFingerprint authentication timed out.")
                buzz_denied()
                lcd_show("ACCESS DENIED", "Finger timeout")
        else:
            print("RFID DENIED: Unknown card.")
            security_event("access_denied", reason="unknown_rfid", uid=uid)
            buzz_denied()
            lcd_show("ACCESS DENIED", "Unknown card")

        if granted:
            register_success()
        elif not hardware_error:
            cooldown = register_denial()
            if cooldown is not None:
                print(f"\nToo many failed attempts. Locked out for {int(cooldown)}s "
                      f"(lockout #{config['lockout_state']['lockout_count']}).")
                security_event("lockout_triggered", cooldown_seconds=cooldown,
                                lockout_count=config["lockout_state"]["lockout_count"])
                lcd_show("LOCKED OUT", f"Wait {int(cooldown)}s")

        print("\nScan complete.")
        time.sleep(1.5)
        if current_lockout_remaining() == 0:
            lcd_show("Access Control", "Ready")
            led_blink()

    led_off()
    print("\n" + "=" * 50 + "\nScanner stopped.\n" + "=" * 50 + "\n")


# --------------------------------------------------------------- enroll ----

def find_next_free_slot():
    used = set()
    for slot in config["user_mappings"]:
        try:
            n = int(slot)
            if 1 <= n <= MAX_FINGERPRINT_SLOTS:
                used.add(n)
        except (ValueError, TypeError):
            continue
    for slot in range(1, MAX_FINGERPRINT_SLOTS + 1):
        if slot not in used:
            return slot
    return None


def prompt_for_schedule():
    """Returns a schedule dict, or None for unrestricted access. Never
    raises -- any invalid input is treated as "no restriction" rather
    than blocking enrollment over a formatting mistake."""
    print("\nRestrict this user to specific hours? (24h clock, e.g. 09:00-18:00)")
    raw = input("Enter as HH:MM-HH:MM, or leave blank for no restriction: ").strip()
    if not raw:
        return None
    try:
        start, end = raw.split("-")
        start, end = start.strip(), end.strip()
        for part in (start, end):
            h, m = part.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                raise ValueError("out of range")
        return {"start": start, "end": end}
    except (ValueError, AttributeError):
        print("Could not parse that -- no time restriction will be applied.")
        return None


def validate_schedule(schedule):
    """Validates a {"start": "HH:MM", "end": "HH:MM"} dict -- the shape a
    caller with separate start/end fields (e.g. a GUI form) already has,
    as opposed to prompt_for_schedule()'s single "HH:MM-HH:MM" free-form
    string. Returns (True, "") if valid, or (False, message) if not.
    Unlike prompt_for_schedule(), this doesn't silently fall back to
    "no restriction" on bad input -- it reports why, so a caller with a
    form can show the error and let the user fix it."""
    if not isinstance(schedule, dict):
        return False, "Schedule must have start and end times."
    start, end = schedule.get("start", ""), schedule.get("end", "")
    if not start or not end:
        return False, "Both start and end times are required."
    for label, part in (("start", start), ("end", end)):
        try:
            h, m = part.strip().split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                return False, f"{label} time out of range (00:00-23:59)."
        except (ValueError, AttributeError):
            return False, f"{label} time must be in HH:MM format."
    return True, ""


def enroll_fingerprint():
    print("\n" + "=" * 50 + "\nNEW FINGERPRINT ENROLLMENT\n" + "=" * 50)

    if not finger_sensor_online:
        print("Fingerprint sensor is offline.")
        return

    name = input("Enter user name: ").strip()
    if not name:
        print("Name cannot be empty.")
        return

    slot = find_next_free_slot()
    if slot is None:
        print("Fingerprint database is full.")
        return

    print(f"\nAssigning '{name}' to slot #{slot}.\nStep 1/2 - place your finger...")
    result = wait_for_finger(timeout=15)
    if result is None:
        print("Fingerprint communication failure.")
        return
    if result is False:
        print("No fingerprint captured.")
        return

    try:
        if finger.image_2_tz(1) != adafruit_fingerprint.OK:
            print("Could not process fingerprint.")
            return

        print("Checking for existing registration...")
        if finger.finger_fast_search() == adafruit_fingerprint.OK:
            existing_slot = finger.finger_id
            existing_name = config["user_mappings"].get(str(existing_slot), f"Unknown User (Slot #{existing_slot})")
            print(f"Fingerprint already registered: slot #{existing_slot} ({existing_name}).")
            security_event("duplicate_fingerprint_enrollment",
                            existing_slot=existing_slot, existing_user=existing_name)
            wait_for_no_finger()
            return
    except (OSError, RuntimeError) as e:
        log(f"Duplicate check failed: {e}", "ERROR")
        return

    print("\nRemove your finger.")
    if not wait_for_no_finger(timeout=10):
        print("Could not confirm finger removal. Enrollment canceled to avoid "
              "comparing the same image against itself — please try again.")
        return
    time.sleep(0.5)

    print("\nStep 2/2 - place the SAME finger again...")
    result = wait_for_finger(timeout=15)
    if result is None:
        print("Fingerprint communication failure.")
        return
    if result is False:
        print("Second capture failed.")
        return

    try:
        if finger.image_2_tz(2) != adafruit_fingerprint.OK:
            print("Could not process second fingerprint.")
            return

        model_result = finger.create_model()
        if model_result == adafruit_fingerprint.ENROLLMISMATCH:
            print("The two fingerprints did not match.")
            security_event("enrollment_mismatch", user=name)
            return
        if model_result != adafruit_fingerprint.OK:
            print(f"Could not create model: {result_name(model_result)}")
            return

        store_result = finger.store_model(slot)
        if store_result != adafruit_fingerprint.OK:
            print(f"Could not store fingerprint: {result_name(store_result)}")
            return

        config["user_mappings"][str(slot)] = name

        schedule = prompt_for_schedule()
        if schedule is not None:
            config["user_schedules"][str(slot)] = schedule
        else:
            config["user_schedules"].pop(str(slot), None)

        if not save_database():
            print(f"\nWARNING: fingerprint stored in sensor slot #{slot} ('{name}'), "
                  f"but database save failed.")
            return

        print(f"\n{'='*50}\nENROLLMENT SUCCESSFUL\nUser: {name}  Slot: #{slot}\n{'='*50}")
        if schedule:
            print(f"Access restricted to {schedule['start']}-{schedule['end']} daily.")
        security_event("fingerprint_enrolled", user=name, slot=slot, schedule=schedule)

    except (OSError, RuntimeError) as e:
        log(f"Enrollment error: {e}", "ERROR")


def delete_fingerprint():
    print("\n" + "=" * 50 + "\nREGISTERED FINGERPRINTS\n" + "=" * 50)

    if not config["user_mappings"]:
        print("No registered fingerprints.")
        return

    for slot, name in sorted(config["user_mappings"].items(), key=lambda x: int(x[0])):
        print(f"Slot #{slot}: {name}")

    target = input("\nEnter user name to delete: ").strip()
    matches = [int(s) for s, n in config["user_mappings"].items()
               if n.lower() == target.lower()]
    if not matches:
        print("User not found.")
        return
    if len(matches) > 1:
        print(f"Multiple users named '{target}' found in slots: "
              f"{', '.join('#' + str(s) for s in sorted(matches))}")
        try:
            target_slot = int(input("Enter the exact slot number to delete: ").strip())
        except ValueError:
            print("Invalid slot number.")
            return
        if target_slot not in matches:
            print("That slot doesn't match the selected name.")
            return
    else:
        target_slot = matches[0]

    print(f"\nSelected: {target}  Slot: #{target_slot}")
    if input("Type DELETE to confirm: ").strip() != "DELETE":
        print("Deletion canceled.")
        return

    if not finger_sensor_online:
        print("Fingerprint sensor is offline.")
        return

    try:
        result = finger.delete_model(target_slot)
        if result != adafruit_fingerprint.OK:
            print(f"Failed to delete sensor template: {result_name(result)}")
            return

        del config["user_mappings"][str(target_slot)]
        config["user_schedules"].pop(str(target_slot), None)
        if not save_database():
            print("WARNING: sensor template deleted, but database update failed.")
            return

        print(f"Successfully deleted '{target}'.")
        security_event("fingerprint_deleted", user=target, slot=target_slot)

    except (OSError, RuntimeError) as e:
        log(f"Deletion error: {e}", "ERROR")


# ------------------------------------------------------------ master RFID --

def change_master_rfid():
    print("\n" + "=" * 50 + "\nCHANGE MASTER RFID CARD\n" + "=" * 50)
    print("WARNING: this card becomes the master authentication card.")

    if input("Type CHANGE to continue: ").strip() != "CHANGE":
        print("Operation canceled.")
        return

    print("\nScan the NEW master RFID card...")
    start = time.monotonic()
    while time.monotonic() - start < 15:
        if shutdown_requested:
            return
        uid = read_rfid_nonblocking()
        if uid is None:
            time.sleep(0.1)
            continue

        if uid == config["AUTHORIZED_UID"]:
            print("This is already the authorized master card.")
            return

        name = input("Enter a name for this admin (blank to leave unset): ").strip()
        old_uid = config["AUTHORIZED_UID"]
        old_name = config["admin_name"]
        config["AUTHORIZED_UID"] = uid
        config["admin_name"] = name or None
        if save_database():
            print(f"\nMaster RFID changed successfully. New UID: {uid}"
                  + (f" (admin: {name})" if name else ""))
            security_event("master_rfid_changed", uid=uid, admin_name=config["admin_name"])
        else:
            config["AUTHORIZED_UID"] = old_uid
            config["admin_name"] = old_name
            print("Database save failed; the existing master card was kept.")
        return

    print("RFID registration timed out.")


# -------------------------------------------------------------- status ----

def get_status_dict():
    """Pure data snapshot of system status, no I/O side effects (doesn't
    print or prompt) -- used by show_status() below and by anything else
    that needs the same information programmatically (e.g. a GUI)."""
    remaining = current_lockout_remaining()
    state = config["lockout_state"]
    sensor_templates = get_sensor_template_count() if finger_sensor_online else None

    lcd_active = None
    if lcd_online and lcd_interface_used == "i2c":
        addr = LCD_I2C_ADDRESS if LCD_I2C_ADDRESS is not None else "auto-detected"
        lcd_active = {
            "interface": "i2c", "expander": LCD_I2C_EXPANDER,
            "address": addr if addr == "auto-detected" else hex(addr),
            "bus": LCD_I2C_PORT,
            "detection": "I2C address acknowledged",
        }
    elif lcd_online and lcd_interface_used == "gpio":
        lcd_active = {
            "interface": "gpio", "pin_rs": LCD_PIN_RS, "pin_e": LCD_PIN_E,
            "pins_data": LCD_PINS_DATA,
            "detection": "GPIO initialized; confirm visually",
        }

    return {
        "rfid": {
            "master_uid": config["AUTHORIZED_UID"],
            "admin_name": config.get("admin_name"),
            "online": rfid_online,
        },
        "users": [
            {
                "slot": slot,
                "name": name,
                "schedule": config["user_schedules"].get(slot),
            }
            for slot, name in sorted(config["user_mappings"].items(), key=lambda x: int(x[0]))
        ],
        "lockout": {
            "locked_out": remaining > 0,
            "remaining_seconds": remaining,
            "consecutive_failures": state["consecutive_failures"],
            "threshold": LOCKOUT_THRESHOLD,
            "lockout_count": state["lockout_count"],
        },
        "fingerprint_sensor": {
            "online": finger_sensor_online,
            "capacity": MAX_FINGERPRINT_SLOTS,
            "local_usage": len(config["user_mappings"]),
            "sensor_templates": sensor_templates,
        },
        "buzzer": {
            "online": buzzer_online,
            "pin": BUZZER_PIN,
            "tone_hz": BUZZER_TONE_HZ,
            "granted_seconds": GRANTED_BUZZ_SECONDS,
            "denied_seconds": DENIED_BUZZ_SECONDS,
        },
        "led": {
            "online": led_online,
            "pin": LED_PIN,
            "blink_interval": LED_BLINK_INTERVAL,
        },
        "lcd": {
            "online": lcd_online,
            "last_error": lcd_last_error,
            "cols": LCD_COLS,
            "rows": LCD_ROWS,
            "configured_interface": LCD_INTERFACE,
            "active": lcd_active,
        },
        "storage": {
            "db_file": DB_FILE,
            "db_exists": os.path.exists(DB_FILE),
            "backup_file": DB_BACKUP,
            "backup_exists": os.path.exists(DB_BACKUP),
            "security_log": SECURITY_LOG,
            "log_dir": LOG_DIR,
        },
    }


def show_status():
    s = get_status_dict()
    print("\n" + "=" * 50 + "\nSYSTEM STATUS\n" + "=" * 50)

    print("\nRFID:")
    print(f"  Master UID: {s['rfid']['master_uid']}")
    print(f"  Admin name: {s['rfid']['admin_name'] or '(not set)'}")
    print(f"  Reader: {'ONLINE' if s['rfid']['online'] else 'OFFLINE'}")

    print("\nFingerprint database:")
    print(f"  Registered mappings: {len(s['users'])}")
    for u in s["users"]:
        schedule = u["schedule"]
        sched_str = f" (restricted {schedule['start']}-{schedule['end']})" if schedule else ""
        print(f"  Slot #{u['slot']}: {u['name']}{sched_str}")

    print("\nLockout state:")
    lo = s["lockout"]
    if lo["locked_out"]:
        print(f"  Currently LOCKED OUT for {int(lo['remaining_seconds'])} more second(s)")
    else:
        print("  Not currently locked out")
    print(f"  Consecutive failures: {lo['consecutive_failures']}/{lo['threshold']}")
    print(f"  Lockout count (drives backoff): {lo['lockout_count']}")

    print("\nSensor:")
    fp = s["fingerprint_sensor"]
    print(f"  Status: {'ONLINE' if fp['online'] else 'OFFLINE'}")
    print(f"  Capacity: {fp['capacity']}")
    print(f"  Local usage: {fp['local_usage']}/{fp['capacity']}")
    if fp["online"] and fp["sensor_templates"] is not None:
        print(f"  Sensor templates: {fp['sensor_templates']}")

    print("\nBuzzer:")
    bz = s["buzzer"]
    print(f"  Status: {'ONLINE' if bz['online'] else 'OFFLINE (system runs fine without it)'}")
    print(f"  Pin (BOARD): {bz['pin']}  |  Drive: PWM square wave, {bz['tone_hz']}Hz")
    print(f"  Granted buzz: {bz['granted_seconds']}s | Denied buzz: {bz['denied_seconds']}s")
    if bz["online"]:
        if input("  Test buzzer now? (y/N): ").strip().lower() == "y":
            if test_buzzer():
                print("  Buzzing...")
                time.sleep(0.3)
            else:
                print("  Test failed.")

    print("\nLED:")
    led = s["led"]
    print(f"  Status: {'ONLINE' if led['online'] else 'OFFLINE (system runs fine without it)'}")
    print(f"  Pin (BOARD): {led['pin']}  |  Blink interval: {led['blink_interval']}s")
    if led["online"]:
        if input("  Test LED now? (y/N): ").strip().lower() == "y":
            if test_led():
                print("  Blinked.")
            else:
                print("  Test failed.")

    print("\nLCD:")
    lcd = s["lcd"]
    print(f"  Status: {'ONLINE' if lcd['online'] else 'OFFLINE (system runs fine without it)'}")
    print(f"  Size: {lcd['cols']}x{lcd['rows']}  |  Configured interface: {lcd['configured_interface']}")
    if lcd["last_error"]:
        print(f"  Diagnostic: {lcd['last_error']}")
    if lcd["active"] and lcd["active"]["interface"] == "i2c":
        a = lcd["active"]
        print(f"  Active: I2C  |  Expander: {a['expander']}  |  "
              f"Address: {a['address']}  |  Bus: {a['bus']}")
    elif lcd["active"] and lcd["active"]["interface"] == "gpio":
        a = lcd["active"]
        print(f"  Active: GPIO  |  Pins (BOARD): RS={a['pin_rs']} "
              f"E={a['pin_e']} D4-D7={a['pins_data']}")
        print(f"  Detection: {a['detection']}")
    if lcd["online"]:
        if input("  Test LCD now? (y/N): ").strip().lower() == "y":
            if test_lcd():
                print("  Message sent to LCD.")
                time.sleep(2)
                lcd_show("Access Control", "Ready")
            else:
                print("  Test failed.")

    print("\nStorage:")
    st = s["storage"]
    print(f"  Database: {st['db_file']} (exists: {st['db_exists']})")
    print(f"  Backup: {st['backup_file']} (exists: {st['backup_exists']})")
    print(f"  Security log (latest): {st['security_log']}")
    print(f"  Daily-rotated logs: {st['log_dir']}/access_log-YYYY-MM-DD.jsonl")
    print("=" * 50)


def get_recent_logs(limit=20):
    """Pure data read of the last `limit` security-log lines, parsed into
    dicts (or a raw-line/error marker for unparseable lines). No I/O side
    effects besides the read itself -- used by show_recent_logs() and by
    anything else that wants the same records programmatically."""
    if not os.path.exists(SECURITY_LOG):
        return []
    try:
        with open(SECURITY_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log(f"Could not read security log: {e}", "ERROR")
        return []

    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append({"_raw": line.strip(), "_parse_error": True})
    return records


def show_recent_logs(limit=20):
    print("\n" + "=" * 50 + f"\nRECENT SECURITY EVENTS (LAST {limit})\n" + "=" * 50)

    if not os.path.exists(SECURITY_LOG):
        print("No security log exists.")
        return

    records = get_recent_logs(limit)
    if not records:
        print("Security log is empty.")
        return

    for record in records:
        if record.get("_parse_error"):
            print(f"Invalid log entry: {record['_raw']}")
            continue
        session = record.get("session")
        session_str = f" [session {session}]" if session else ""
        print(f"{record.get('timestamp')} | {record.get('event')}{session_str}")
        details = {k: v for k, v in record.items()
                   if k not in ("timestamp", "event", "session")}
        if details:
            print("    " + json.dumps(details, ensure_ascii=False))


# ------------------------------------------------------------ schedules ----

def edit_user_schedule():
    print("\n" + "=" * 50 + "\nEDIT USER ACCESS SCHEDULE\n" + "=" * 50)

    if not config["user_mappings"]:
        print("No registered fingerprints.")
        return

    for slot, name in sorted(config["user_mappings"].items(), key=lambda x: int(x[0])):
        schedule = config["user_schedules"].get(slot)
        sched_str = f" (restricted {schedule['start']}-{schedule['end']})" if schedule else " (unrestricted)"
        print(f"Slot #{slot}: {name}{sched_str}")

    target = input("\nEnter user name to edit: ").strip()
    matches = [s for s, n in config["user_mappings"].items() if n.lower() == target.lower()]
    if not matches:
        print("User not found.")
        return
    if len(matches) > 1:
        print(f"Multiple users named '{target}' found in slots: "
              f"{', '.join('#' + s for s in sorted(matches, key=int))}")
        slot = input("Enter the exact slot number to edit: ").strip()
        if slot not in matches:
            print("That slot doesn't match the selected name.")
            return
    else:
        slot = matches[0]

    schedule = prompt_for_schedule()
    if schedule is not None:
        config["user_schedules"][slot] = schedule
    else:
        config["user_schedules"].pop(slot, None)

    if save_database():
        name = config["user_mappings"][slot]
        print(f"\nSchedule updated for {name} (slot #{slot}).")
        security_event("schedule_updated", user=name, slot=slot, schedule=schedule)
    else:
        print("Schedule changed in memory, but database save failed.")


# ---------------------------------------------------------------- menu ----

MENU_ACTIONS = {
    "1": scanner_mode,
    "2": enroll_fingerprint,
    "3": delete_fingerprint,
    "4": change_master_rfid,
    "5": show_status,
    "6": show_recent_logs,
    "7": edit_user_schedule,
}
# Buzzer status/testing lives inside show_status() (option 5), per design —
# there is no separate hardware-presence question asked anywhere.


def main_menu():
    global shutdown_requested
    while not shutdown_requested:
        print(f"\n{'='*50}\n RFID + FINGERPRINT ACCESS SYSTEM  (v{VERSION})\n{'='*50}")
        print("1. Start Scanner Mode\n2. Enroll New Fingerprint\n3. Delete Fingerprint\n"
              "4. Change Master RFID Card\n5. Show System Status\n6. View Recent Security Logs\n"
              "7. Edit User Access Schedule\n8. Exit")
        print("=" * 50)

        try:
            choice = input("Select option (1-8): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "8":
            break
        action = MENU_ACTIONS.get(choice)
        if action is None:
            print("Invalid selection. Choose 1-8.")
            continue

        try:
            action()
        except KeyboardInterrupt:
            print("\nCanceled.")
        # Any action (not just scanner mode) may have caught a Ctrl+C via
        # the SIGINT handler, which sets shutdown_requested globally. Reset
        # it here so a Ctrl+C during enroll/delete/status/etc. returns to
        # the menu instead of exiting the whole app on the next loop check.
        shutdown_requested = False


# --------------------------------------------------------------- startup --

def cleanup():
    global uart
    print()
    log("Shutting down hardware...")
    if buzzer_online:
        try:
            _buzzer_off()
        except Exception:
            pass
    if lcd_online and lcd is not None:
        try:
            lcd.clear()
            lcd.close(clear=True)
        except Exception:
            pass
    try:
        GPIO.cleanup()
    except Exception as e:
        log(f"GPIO cleanup error: {e}", "WARNING")
    if uart is not None:
        try:
            uart.close()
        except OSError as e:
            log(f"UART cleanup error: {e}", "WARNING")
        uart = None
    log("Shutdown complete.")


def initialize_hardware(progress_cb=None, cancel_event=None):
    """Brings up all four MANDATORY peripherals (RFID, fingerprint,
    buzzer, LCD), plus the LED (always optional -- see below), then
    behaves according to REQUIRE_ALL_HARDWARE -- the single place that
    controls whether the four mandatory ones are required. This is the
    ONLY function that should decide that; callers (main() below, and
    the GUI's splash screen) both call this instead of branching on
    REQUIRE_ALL_HARDWARE themselves, so the two interfaces can never
    disagree about what's required.

    If REQUIRE_ALL_HARDWARE is True: retries indefinitely (every
    HARDWARE_RETRY_SECONDS) until every one of RFID/fingerprint/buzzer/
    LCD initializes successfully -- the app will not proceed with any of
    them missing. If False: initializes once, logs a warning for
    anything offline, and returns immediately either way.

    The LED is never part of this mandatory set and never contributes to
    `missing` or blocks a retry, regardless of REQUIRE_ALL_HARDWARE --
    it's purely a status indicator (see led_blink/led_solid_on/led_off),
    same "fully optional" tier as the buzzer, just not gated at all.

    progress_cb, if given, is called after every initialization attempt
    as progress_cb(attempt, state, missing) -- state is a dict of
    component name -> bool online, missing is the list of offline
    component labels. Optional; a caller that doesn't need live progress
    (the CLI, which already logs as it goes) can omit it. A GUI can use
    it to show per-attempt status without duplicating this logic.
    cancel_event, if supplied, aborts a mandatory-hardware retry promptly
    and makes this function return False.  This lets GUI callers cancel
    startup without depending on signals reaching a worker thread.

    Init order matters: initialize_buzzer() must run before
    initialize_rfid(), because SimpleMFRC522 forces GPIO.setmode
    (GPIO.BOARD) internally, and the buzzer, LED, and LCD also need
    BOARD mode -- setting it via the buzzer first means every later GPIO
    user just reuses the same mode instead of conflicting with it.

    Special case (REQUIRE_ALL_HARDWARE only): if the LCD is the only
    thing missing, there's no display to show that on, so a distinct
    buzzer pattern (buzz_hardware_failure) doubles as the failure signal
    whenever the buzzer itself is up. If the buzzer is ALSO down, the
    terminal/log is the only channel left, which was an accepted
    tradeoff (see prior conversation) rather than an oversight.
    """
    attempt = 0
    while True:
        if shutdown_requested or (cancel_event is not None and cancel_event.is_set()):
            log("Hardware initialization canceled.", "INFO")
            return False
        attempt += 1
        initialize_fingerprint()
        initialize_buzzer()
        initialize_led()
        initialize_lcd()
        initialize_rfid()

        state = {
            "rfid": rfid_online,
            "fingerprint": finger_sensor_online,
            "buzzer": buzzer_online,
            "lcd": lcd_online,
        }
        labels = {"rfid": "RFID reader", "fingerprint": "Fingerprint sensor",
                  "buzzer": "Buzzer", "lcd": "LCD"}
        missing = [labels[k] for k, online in state.items() if not online]

        if progress_cb is not None:
            progress_cb(attempt, state, missing)

        if not REQUIRE_ALL_HARDWARE:
            if not rfid_online:
                log("RFID reader is offline.", "WARNING")
            if not finger_sensor_online:
                log("Fingerprint sensor is offline.", "WARNING")
            if not buzzer_online:
                log("Buzzer is offline (system will run without audio feedback).", "WARNING")
            if not lcd_online:
                log("LCD is offline (system will run without a display).", "WARNING")
            if not led_online:
                log("LED is offline (system will run without a status light).", "WARNING")
            return True

        if not missing:
            log("All mandatory hardware online (RFID, fingerprint, buzzer, LCD).")
            lcd_show("All Systems", "Online")
            return True

        log(f"Attempt {attempt}: missing mandatory hardware: {', '.join(missing)}. "
            f"Retrying in {HARDWARE_RETRY_SECONDS}s...", "ERROR")
        # Show on whichever display channels are actually up right now.
        lcd_show("HARDWARE ERROR", (", ".join(missing))[:LCD_COLS])
        buzz_hardware_failure()

        if cancel_event is not None:
            if cancel_event.wait(HARDWARE_RETRY_SECONDS):
                log("Hardware initialization canceled.", "INFO")
                return False
        else:
            time.sleep(HARDWARE_RETRY_SECONDS)


def main():
    install_signal_handlers()
    print(f"\n{'='*50}\n RFID + FINGERPRINT ACCESS SYSTEM  (v{VERSION})\n{'='*50}")
    try:
        load_database()
        decay_lockout_backoff()

        initialize_hardware()

        if shutdown_requested:
            return

        if config["AUTHORIZED_UID"] is None:
            log("No master RFID card configured yet. Set one via the menu.", "WARNING")

        main_menu()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:  # top-level safety net
        log(f"Fatal application error: {e}", "ERROR")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
