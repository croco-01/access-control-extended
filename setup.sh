#!/usr/bin/env bash
#
# Usage:
#   chmod +x setup.sh        
#   sudo ./setup.sh

set +e  # keep checking even after individual failures

# ---- output helpers -----------------------------------------------------

PASS=0
WARN=0
FAIL=0

GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
BOLD="\033[1m"
RESET="\033[0m"

ok()   { printf "  ${GREEN}[ OK ]${RESET} %s\n" "$1"; PASS=$((PASS+1)); }
warn() { printf "  ${YELLOW}[WARN]${RESET} %s\n" "$1"; WARN=$((WARN+1)); }
fail() { printf "  ${RED}[FAIL]${RESET} %s\n" "$1"; FAIL=$((FAIL+1)); }
info() { printf "         %s\n" "$1"; }
section() { printf "\n${BOLD}%s${RESET}\n" "$1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_FILE="$SCRIPT_DIR/run.py"

# Setup applies available fixes by default. No command-line flag is needed.

APT_PACKAGES="python3-pip python3-dev python3-setuptools python3-venv i2c-tools python3-tk"
REQUIRED_GROUPS="gpio spi i2c dialout"

# ---- figure out who the "real" (non-root) user is ------------------------
#
# This matters a lot when run via `sudo ./setup.sh`: $USER and
# `whoami` would report "root" in that case, which is NOT who should be
# added to the gpio/spi/dialout groups. SUDO_USER holds the original login
# user when invoked through sudo, so we prefer that whenever it's set and
# isn't itself "root".

if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != "root" ]; then
    REAL_USER="$SUDO_USER"
else
    REAL_USER="${USER:-$(whoami 2>/dev/null)}"
fi
if [ -z "$REAL_USER" ]; then
    REAL_USER="$(id -un 2>/dev/null)"
fi

RUNNING_AS_ROOT=0
if [ "$(id -u)" -eq 0 ]; then
    RUNNING_AS_ROOT=1
fi

# Helper: run a command with sudo only if we're not already root.
as_root() {
    if [ "$RUNNING_AS_ROOT" -eq 1 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Helper: run python3 as the real login user, not root. This matters
# because `sudo ./setup.sh` installs pip packages into the
# real user's home directory (~/.local/lib/...), which root's own python3
# cannot see. Every import check must run as that same user or it will
# report a false FAIL right after a successful install.
run_as_real_user() {
    if [ "$RUNNING_AS_ROOT" -eq 1 ] && [ "$REAL_USER" != "root" ] && command -v runuser >/dev/null 2>&1; then
        runuser -u "$REAL_USER" -- "$@"
    else
        "$@"
    fi
}

# Setup always applies available fixes.
should_fix() {
    return 0
}

printf "${BOLD}=========================================================\n"
printf " ACCESS CONTROL SYSTEM - SETUP CHECK\n"
printf "=========================================================${RESET}\n"
info "Missing packages and enabled interfaces will be fixed automatically."
info "RFID, fingerprint, buzzer, and LCD hardware are optional: the app starts"
info "without them, but the features that depend on an offline device are unavailable."
if [ "$RUNNING_AS_ROOT" -eq 0 ]; then
    warn "Not running as root. Run: sudo ./setup.sh"
fi
info "Detected login user for group membership / permissions: $REAL_USER"

# ---- 1. Platform sanity --------------------------------------------------

section "Platform"

if [ -f /proc/device-tree/model ]; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model)
    ok "Running on: $MODEL"
else
    warn "Could not detect Raspberry Pi model (not a Pi, or old OS). Continuing anyway."
fi

if command -v vcgencmd >/dev/null 2>&1; then
    ok "vcgencmd available"
else
    warn "vcgencmd not found (fine on Pi OS Lite / some 64-bit images, just fewer diagnostics)"
fi

# ---- 2. Interfaces: SPI (RFID) and Serial (fingerprint) ------------------

section "Interfaces (SPI / UART / I2C)"

SPI_OK=0
if lsmod 2>/dev/null | grep -q "spi_bcm2835\|spi_bcm2708"; then
    ok "SPI kernel module loaded (needed for RFID/MFRC522)"
    SPI_OK=1
else
    fail "SPI kernel module NOT loaded."
fi

if ls /dev/spidev* >/dev/null 2>&1; then
    ok "SPI device nodes present: $(ls /dev/spidev* | tr '\n' ' ')"
else
    fail "No /dev/spidev* device found."
    SPI_OK=0
fi

UART_OK=1
if [ -e /dev/serial0 ]; then
    TARGET=$(readlink -f /dev/serial0 2>/dev/null)
    ok "/dev/serial0 exists (-> $TARGET)"
else
    fail "/dev/serial0 not found."
    UART_OK=0
fi

if [ -f /boot/cmdline.txt ]; then
    CMDLINE_FILE=/boot/cmdline.txt
elif [ -f /boot/firmware/cmdline.txt ]; then
    CMDLINE_FILE=/boot/firmware/cmdline.txt
else
    CMDLINE_FILE=""
fi

SERIAL_CONSOLE_ON=0
if [ -n "$CMDLINE_FILE" ]; then
    if grep -q "console=serial0\|console=ttyAMA0\|console=ttyS0" "$CMDLINE_FILE" 2>/dev/null; then
        fail "Serial console is still enabled in $CMDLINE_FILE (conflicts with the fingerprint sensor's UART)."
        SERIAL_CONSOLE_ON=1
        UART_OK=0
    else
        ok "Serial console is not competing for the UART ($CMDLINE_FILE clean)"
    fi
else
    warn "Could not locate cmdline.txt to verify serial console state."
fi

if systemctl is-active --quiet serial-getty@ttyAMA0.service 2>/dev/null || \
   systemctl is-active --quiet serial-getty@serial0.service 2>/dev/null; then
    fail "A serial-getty service is actively running on the UART."
    SERIAL_CONSOLE_ON=1
    UART_OK=0
else
    ok "No serial-getty login shell competing for the UART"
fi

I2C_OK=0
if lsmod 2>/dev/null | grep -q "i2c_bcm2835\|i2c_bcm2708\|i2c_dev"; then
    ok "I2C kernel module loaded (needed for an I2C-wired LCD)"
    I2C_OK=1
else
    warn "I2C kernel module not loaded (only needed if your LCD uses an I2C backpack; a direct-wired LCD doesn't need this)."
fi
if ls /dev/i2c-* >/dev/null 2>&1; then
    ok "I2C device node(s) present: $(ls /dev/i2c-* | tr '\n' ' ')"
else
    warn "No /dev/i2c-* device found (only needed for an I2C-wired LCD)."
    I2C_OK=0
fi

# Auto-fix: enable SPI / UART / I2C hardware via raspi-config non-interactively,
# and disable the serial console the same way. Requires root and
# raspi-config (i.e. an actual Raspberry Pi OS install).
NEEDS_REBOOT=0
if [ "$SPI_OK" -eq 0 ] || [ "$UART_OK" -eq 0 ] || [ "$I2C_OK" -eq 0 ]; then
    if command -v raspi-config >/dev/null 2>&1; then
        if should_fix; then
            if [ "$SPI_OK" -eq 0 ]; then
                as_root raspi-config nonint do_spi 0    # 0 = enable
                if [ $? -eq 0 ]; then
                    ok "SPI enabled via raspi-config (takes effect after reboot)"
                    NEEDS_REBOOT=1
                else
                    fail "raspi-config failed to enable SPI. Enable manually: sudo raspi-config -> Interface Options -> SPI."
                fi
            fi
            if [ "$UART_OK" -eq 0 ]; then
                as_root raspi-config nonint do_serial_hw 0   # enable UART hardware
                as_root raspi-config nonint do_serial_cons 1 # disable login shell over serial
                if [ $? -eq 0 ]; then
                    ok "UART hardware enabled and serial console disabled via raspi-config (takes effect after reboot)"
                    NEEDS_REBOOT=1
                else
                    fail "raspi-config failed to configure UART. Configure manually: sudo raspi-config -> Interface Options -> Serial Port."
                fi
            fi
            if [ "$I2C_OK" -eq 0 ]; then
                as_root raspi-config nonint do_i2c 0    # 0 = enable
                if [ $? -eq 0 ]; then
                    ok "I2C enabled via raspi-config (takes effect after reboot). Only needed for an I2C-wired LCD; harmless if you're using the direct-wired GPIO LCD."
                    NEEDS_REBOOT=1
                else
                    warn "raspi-config failed to enable I2C. Only matters if you're using an I2C LCD - enable manually: sudo raspi-config -> Interface Options -> I2C."
                fi
            fi
        else
            info "Skipped. To fix manually: sudo raspi-config -> Interface Options -> SPI / Serial Port / I2C, then reboot."
        fi
    else
        warn "raspi-config not found; cannot auto-enable SPI/UART/I2C. Not on Raspberry Pi OS, or it's missing. Configure manually via /boot/config.txt if needed."
    fi
fi

# ---- 3. APT / system packages --------------------------------------------

section "System (apt) Packages"

if command -v apt-get >/dev/null 2>&1; then
    ok "apt-get available (Debian-based OS, expected on Raspberry Pi OS)"

    MISSING_APT=()
    for pkg in $APT_PACKAGES; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            ok "apt package '$pkg' installed"
        else
            fail "apt package '$pkg' NOT installed."
            MISSING_APT+=("$pkg")
        fi
    done

    if [ "${#MISSING_APT[@]}" -gt 0 ]; then
        if should_fix; then
            as_root apt-get update && as_root apt-get install -y "${MISSING_APT[@]}"
            if [ $? -eq 0 ]; then
                ok "apt packages installed: ${MISSING_APT[*]}"
            else
                fail "apt-get install failed. Check the output above and try running it manually."
            fi
        else
            info "Skipped. Install manually: sudo apt-get install -y ${MISSING_APT[*]}"
        fi
    fi
else
    warn "apt-get not found. This project targets Raspberry Pi OS (Debian-based); if you're on a different distro, install equivalent packages manually: $APT_PACKAGES"
fi

# ---- 3b. Remove PEP 668 EXTERNALLY-MANAGED marker -------------------------
#
# Recent Debian/Pi OS ship python3-pip with an EXTERNALLY-MANAGED marker
# file that blocks plain `pip install` system-wide (PEP 668). We already
# pass --break-system-packages to pip below, which is normally enough on
# its own; removing the marker here too is a belt-and-suspenders step some
# environments still need. This script applies the change by default, and it
# change, and made safe to re-run (glob may match nothing / multiple
# python3.X dirs).

section "Python Package Policy (PEP 668)"

EXTERNALLY_MANAGED_FILES=(/usr/lib/python3*/EXTERNALLY-MANAGED)
FOUND_MARKER=0
for f in "${EXTERNALLY_MANAGED_FILES[@]}"; do
    [ -e "$f" ] || continue
    FOUND_MARKER=1
    warn "Found PEP 668 marker: $f"
    if should_fix; then
        as_root rm -f "$f"
        if [ $? -eq 0 ]; then
            ok "Removed $f"
        else
            fail "Could not remove $f. Try manually: sudo rm $f"
        fi
    else
        info "Skipped. Remove manually: sudo rm $f"
    fi
done
if [ "$FOUND_MARKER" -eq 0 ]; then
    ok "No EXTERNALLY-MANAGED marker found (pip installs unrestricted, or already removed)"
fi

# ---- 4. GPIO permissions --------------------------------------------------

section "GPIO / Hardware Permissions"

MISSING_GROUPS=()
for grp in $REQUIRED_GROUPS; do
    if ! getent group "$grp" >/dev/null 2>&1; then
        warn "Group '$grp' does not exist on this system (unusual on Pi OS; skipping)."
        continue
    fi
    if id -nG "$REAL_USER" 2>/dev/null | grep -qw "$grp"; then
        ok "User '$REAL_USER' is in the '$grp' group"
    else
        warn "User '$REAL_USER' is NOT in the '$grp' group."
        MISSING_GROUPS+=("$grp")
    fi
done

if [ "${#MISSING_GROUPS[@]}" -gt 0 ]; then
    GROUP_LIST=$(IFS=,; echo "${MISSING_GROUPS[*]}")
    if should_fix; then
        as_root usermod -aG "$GROUP_LIST" "$REAL_USER"
        if [ $? -eq 0 ]; then
            ok "Added '$REAL_USER' to: $GROUP_LIST"
            info "NOTE: log out and back in (or reboot) for this to take effect."
        else
            fail "usermod failed. Check the output above and try running it manually."
        fi
    else
        info "Skipped. Fix manually: sudo usermod -aG $GROUP_LIST $REAL_USER"
    fi
fi

if [ -e /dev/gpiomem ]; then
    if [ -r /dev/gpiomem ] && [ -w /dev/gpiomem ]; then
        ok "/dev/gpiomem is readable/writable by current session"
    else
        warn "/dev/gpiomem exists but may not be accessible without sudo or group membership."
    fi
else
    warn "/dev/gpiomem not found (unusual on Pi OS)."
fi

# ---- 5. Python and dependencies -------------------------------------------

section "Python Environment"

if command -v python3 >/dev/null 2>&1; then
    PYVER=$(python3 --version 2>&1)
    ok "python3 found: $PYVER"
else
    fail "python3 not found on PATH. Install it before continuing (e.g. sudo apt-get install -y python3)."
fi

if command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
    ok "pip available"
else
    warn "pip not found."
    if should_fix; then
        as_root apt-get update && as_root apt-get install -y python3-pip
        if command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
            ok "pip installed successfully"
        else
            fail "pip still not found after install attempt."
        fi
    else
        info "Skipped. Install manually: sudo apt-get install -y python3-pip"
    fi
fi

MISSING_PIP=()
check_python_module() {
    local module="$1"
    local pip_name="${2:-$1}"
    if run_as_real_user python3 -c "import $module" >/dev/null 2>&1; then
        ok "Python module '$module' importable"
    else
        fail "Python module '$module' NOT importable."
        MISSING_PIP+=("$pip_name")
    fi
}

check_python_module "serial" "pyserial"
check_python_module "RPi.GPIO" "RPi.GPIO"
check_python_module "mfrc522" "mfrc522"
check_python_module "adafruit_fingerprint" "adafruit-circuitpython-fingerprint"
check_python_module "RPLCD" "RPLCD"
check_python_module "smbus2" "smbus2"

if [ "${#MISSING_PIP[@]}" -gt 0 ]; then
    if should_fix; then
        # Always install as the real login user, never as root, even when
        # this script itself is run via sudo. This keeps packages in that
        # user's own site-packages (~/.local/...), which is where their
        # `python3 run.py` will actually look.
        run_as_real_user pip install "${MISSING_PIP[@]}" --break-system-packages
        if [ $? -eq 0 ]; then
            ok "pip install command completed: ${MISSING_PIP[*]}"
            # Re-verify each module individually, as the same real user,
            # so the pass/warn/fail counts reflect what's actually
            # importable now (e.g. RPi.GPIO installs fine on non-Pi
            # hardware but still refuses to import there).
            for m in serial RPi.GPIO mfrc522 adafruit_fingerprint RPLCD smbus2; do
                if run_as_real_user python3 -c "import $m" >/dev/null 2>&1; then
                    ok "Re-checked '$m': now importable"
                else
                    fail "Re-checked '$m': still not importable after install (may be normal on non-Pi hardware for RPi.GPIO/mfrc522)."
                fi
            done
        else
            fail "pip install failed. Check the output above and try running it manually."
        fi
    else
        info "Skipped. Install manually: pip install ${MISSING_PIP[*]} --break-system-packages"
    fi
fi

# ---- 6. Project files ------------------------------------------------------

section "Project Files"

if [ -f "$PROJECT_FILE" ]; then
    ok "Main script found: $PROJECT_FILE"
    if python3 -m py_compile "$PROJECT_FILE" 2>/tmp/pycompile_err.txt; then
        ok "Main script compiles without syntax errors"
    else
        fail "Main script has a syntax error:"
        sed 's/^/         /' /tmp/pycompile_err.txt
    fi
    rm -f /tmp/pycompile_err.txt
    rm -rf "$SCRIPT_DIR/__pycache__" 2>/dev/null
else
    fail "Main script not found at $PROJECT_FILE (expected run.py alongside setup.sh)."
fi

PROJECT_DIR="$(dirname "$PROJECT_FILE")"
if [ -w "$PROJECT_DIR" ]; then
    ok "Project directory is writable ($PROJECT_DIR) - database and logs can be saved here"
else
    fail "Project directory is NOT writable ($PROJECT_DIR). The app needs to write fingerprint_database.json and access_log.jsonl here."
fi

LOGS_DIR="$PROJECT_DIR/logs"
if [ -d "$LOGS_DIR" ]; then
    if [ -w "$LOGS_DIR" ]; then
        ok "Daily log directory exists and is writable ($LOGS_DIR)"
    else
        fail "Daily log directory exists but is NOT writable ($LOGS_DIR)."
    fi
else
    info "Daily log directory ($LOGS_DIR) doesn't exist yet - run.py creates it automatically on first run."
fi

# ---- 7. RFID reader probe (best-effort, non-invasive) ----------------------

section "RFID Reader (best-effort probe)"

if ls /dev/spidev* >/dev/null 2>&1 && run_as_real_user python3 -c "import spidev" >/dev/null 2>&1; then
    run_as_real_user python3 - <<'PYEOF' 2>/dev/null
import sys
try:
    import spidev
    spi = spidev.SpiDev()
    spi.open(0, 0)
    spi.max_speed_hz = 1000000
    spi.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
    if [ $? -eq 0 ]; then
        ok "SPI bus 0 opened successfully (device present and responsive at the bus level)"
    else
        warn "Could not open SPI bus 0. Check wiring (SDA/SCK/MOSI/MISO/RST/3.3V/GND) or that nothing else is holding it open."
    fi
else
    warn "Skipping SPI probe (spidev module or /dev/spidev not available)."
fi

# ---- 8. Fingerprint sensor probe (best-effort, non-invasive) ---------------

section "Fingerprint Sensor (best-effort probe)"

if [ -e /dev/serial0 ] && run_as_real_user python3 -c "import serial" >/dev/null 2>&1; then
    run_as_real_user python3 - <<'PYEOF' 2>/dev/null
import sys
try:
    import serial
    ser = serial.Serial('/dev/serial0', baudrate=57600, timeout=1)
    ser.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
    if [ $? -eq 0 ]; then
        ok "UART /dev/serial0 opened successfully at 57600 baud (port is free and accessible)"
    else
        warn "Could not open /dev/serial0. Check wiring (TX/RX crossed, 3.3V/5V per sensor spec, GND) or that another process/getty is holding the port."
    fi
else
    warn "Skipping UART probe (/dev/serial0 or pyserial not available)."
fi

# ---- 9. Buzzer (best-effort probe - optional hardware) ----------------------

section "Buzzer (best-effort probe)"

BUZZER_PIN_BOARD=12   # must match BUZZER_PIN in run.py (BOARD numbering)

if run_as_real_user python3 -c "import RPi.GPIO" >/dev/null 2>&1; then
    run_as_real_user python3 - <<PYEOF 2>/dev/null
import sys
try:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup($BUZZER_PIN_BOARD, GPIO.OUT)
    GPIO.output($BUZZER_PIN_BOARD, GPIO.LOW)
    GPIO.cleanup($BUZZER_PIN_BOARD)
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
    if [ $? -eq 0 ]; then
        ok "GPIO pin $BUZZER_PIN_BOARD (BOARD) is free and claimable for the buzzer"
        info "This confirms the pin is usable, not that a buzzer is physically connected."
    else
        warn "Could not claim GPIO pin $BUZZER_PIN_BOARD (BOARD) for the buzzer. It may be in use by another process/overlay, or wiring may be off."
    fi
else
    warn "RPi.GPIO not importable; cannot probe the buzzer pin."
fi
info "The buzzer is optional; audio feedback is unavailable while it is offline."
info "Check it live via the app's menu: option 5 (System Status) -> Buzzer section -> Test buzzer now?"

# ---- 9b. Touchscreen GUI (optional - only needed for gui.py) --------------

section "Touchscreen GUI (gui.py, optional)"

if run_as_real_user python3 -c "import tkinter" >/dev/null 2>&1; then
    ok "tkinter importable (needed only if you run gui.py, not run.py)"
else
    warn "tkinter not importable (should have been installed via python3-tk in step 5 above). Only needed for the optional touchscreen GUI (gui.py); the terminal menu (run.py) doesn't need it. Install manually with: sudo apt-get install -y python3-tk"
fi
if [ -n "${DISPLAY:-}" ] || [ -e /dev/fb0 ] || [ -e /dev/dri/card0 ]; then
    ok "A display environment appears to be present (DISPLAY set, or a framebuffer/DRM device found)"
else
    warn "No display environment detected (no \$DISPLAY, no /dev/fb0, no /dev/dri/card0). gui.py needs a monitor (and normally a desktop session) attached to the Pi -- it will not run over a plain SSH terminal. run.py's text menu works either way."
fi

# ---- 10. LCD (best-effort probe - optional hardware) ------------------------
#
# run.py supports two LCD wiring styles (LCD_INTERFACE = "auto" by default,
# which tries I2C first, then falls back to GPIO). This probe checks
# BOTH paths and reports on whichever one(s) look usable, since we don't
# know ahead of time which hardware is actually connected.

section "16x2 LCD (best-effort probe)"

# Must match LCD_PIN_RS / LCD_PIN_E / LCD_PINS_DATA in run.py (BOARD numbering).
LCD_PIN_RS_BOARD=32
LCD_PIN_E_BOARD=29
LCD_PINS_DATA_BOARD="13 15 18 16"
# Must match LCD_I2C_COMMON_ADDRESSES in run.py.
LCD_I2C_COMMON_ADDRESSES="0x27 0x3f"

LCD_I2C_FOUND=0
if command -v i2cdetect >/dev/null 2>&1 && ls /dev/i2c-* >/dev/null 2>&1; then
    for addr in $LCD_I2C_COMMON_ADDRESSES; do
        addr_dec=$((addr))
        if as_root i2cdetect -y 1 2>/dev/null | grep -qiE "(^|[[:space:]])$(printf '%02x' "$addr_dec")([[:space:]]|$)"; then
            ok "I2C device responding at address $addr on bus 1 (likely the LCD backpack)"
            LCD_I2C_FOUND=1
        fi
    done
    if [ "$LCD_I2C_FOUND" -eq 0 ]; then
        info "No I2C device found at common LCD addresses ($LCD_I2C_COMMON_ADDRESSES) on bus 1."
        info "If you have an I2C LCD, check wiring (SDA/SCL/5V/GND) and that I2C is enabled (sudo raspi-config -> Interface Options -> I2C)."
    fi
else
    warn "i2cdetect not available or no /dev/i2c-* device found; skipping I2C LCD probe. Install with: sudo apt-get install -y i2c-tools, and enable I2C via raspi-config."
fi

GPIO_PINS_OK=0
if run_as_real_user python3 -c "import RPi.GPIO" >/dev/null 2>&1; then
    run_as_real_user python3 - <<PYEOF 2>/dev/null
import sys
try:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    pins = [$LCD_PIN_RS_BOARD, $LCD_PIN_E_BOARD] + [$(echo $LCD_PINS_DATA_BOARD | tr ' ' ',')]
    for p in pins:
        GPIO.setup(p, GPIO.OUT)
        GPIO.output(p, GPIO.LOW)
    for p in pins:
        GPIO.cleanup(p)
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
    if [ $? -eq 0 ]; then
        ok "GPIO pins RS=$LCD_PIN_RS_BOARD E=$LCD_PIN_E_BOARD D4-D7=($LCD_PINS_DATA_BOARD) (BOARD) are free and claimable for a direct-wired LCD"
        GPIO_PINS_OK=1
    else
        warn "Could not claim one or more LCD GPIO pins. They may be in use by another process/overlay, or wiring may be off (only relevant if you're using a direct-wired, non-I2C LCD)."
    fi
else
    warn "RPi.GPIO not importable; cannot probe the LCD GPIO pins."
fi

if run_as_real_user python3 -c "import RPLCD" >/dev/null 2>&1; then
    ok "RPLCD importable"
else
    fail "RPLCD not importable (should have been installed in step 5 above)."
fi
if run_as_real_user python3 -c "import smbus2" >/dev/null 2>&1; then
    ok "smbus2 importable (needed for I2C LCD auto-detection in run.py)"
else
    warn "smbus2 not importable (should have been installed in step 5 above). Needed only for the I2C LCD path (auto-detect / LCD_INTERFACE=i2c). Install manually with: pip install smbus2 --break-system-packages"
fi

if [ "$LCD_I2C_FOUND" -eq 0 ] && [ "$GPIO_PINS_OK" -eq 0 ]; then
    warn "Neither an I2C LCD was detected nor were the direct-wired GPIO pins claimable. The app still starts, but LCD output is unavailable."
fi
info "run.py's LCD_INTERFACE is set to 'auto' by default: it tries I2C first, then falls back to GPIO. See LCD_INTERFACE in run.py to force one or the other."
info "Check it live via the app's menu: option 5 (System Status) -> LCD section -> Test LCD now?"

# ---- Summary ----------------------------------------------------------------

section "Summary"

printf "  ${GREEN}%d passed${RESET}   ${YELLOW}%d warnings${RESET}   ${RED}%d failed${RESET}\n" "$PASS" "$WARN" "$FAIL"

if [ "$NEEDS_REBOOT" -eq 1 ]; then
    printf "\n${YELLOW}${BOLD}A reboot is required${RESET} for the SPI/UART hardware changes to take effect: sudo reboot\n"
fi

if [ "$FAIL" -gt 0 ]; then
    printf "\n${RED}${BOLD}Setup is NOT ready.${RESET} "
    printf "Some items above still need manual attention (see messages above).\n"
    exit 1
elif [ "$WARN" -gt 0 ]; then
    printf "\n${YELLOW}${BOLD}Setup looks mostly ready${RESET}, but review the [WARN] items above.\n"
    exit 0
else
    printf "\n${GREEN}${BOLD}Everything checks out. You're good to run: python3 %s${RESET}\n" "$PROJECT_FILE"
    exit 0
fi
