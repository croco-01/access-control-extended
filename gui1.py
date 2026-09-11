#!/usr/bin/env python3
"""
Touchscreen GUI for the RFID + fingerprint access control system.

This is a thin UI layer on top of run.py: every piece of hardware logic
(RFID reads, fingerprint capture/match, buzzer, LCD, database, lockout,
logging) lives in run.py and is reused here unchanged. This file only
adds a Tkinter front end, meant for a monitor/touchscreen plugged
directly into the Pi (HDMI + touch, or HDMI + mouse/keyboard).

Threading model (required because Tkinter is not thread-safe and several
run.py calls block while waiting on hardware):
  - All blocking hardware work (scanning, enrolling, waiting for an RFID
    card) happens on a background thread.
  - Background threads never touch Tkinter widgets directly. They push
    small (event_type, payload) tuples onto a queue.Queue.
  - The Tkinter main loop polls that queue every ~100ms via
    root.after(...) and updates widgets there -- the only place widgets
    are touched.
  - Anything with a Cancel/Stop button passes a threading.Event down
    into run.py (wait_for_finger, wait_for_no_finger,
    authenticate_fingerprint all accept cancel_event=... for this).

Run with:  python3 gui.py
Requires:  python3-tk  (installed by setup.sh)
"""

import queue
import signal
import threading
import time
import tkinter as tk

import run


# --------------------------------------------------------------- theme ----
# Large fonts and big touch targets throughout -- this is meant to be
# poked with a finger on a small screen, not clicked with a precise mouse.

BG = "#1e1f26"
PANEL_BG = "#2a2c38"
FG = "#f2f2f2"
MUTED = "#9a9cae"
ACCENT = "#4f8cff"
GREEN = "#3cb371"
RED = "#e0554f"
YELLOW = "#e0b34f"
ORANGE = "#e0824f"

FONT_TITLE = ("DejaVu Sans", 26, "bold")
FONT_BIG = ("DejaVu Sans", 22, "bold")
FONT_NORMAL = ("DejaVu Sans", 15)
FONT_SMALL = ("DejaVu Sans", 12)
FONT_MONO = ("DejaVu Sans Mono", 12)

BTN_OPTS = dict(font=FONT_NORMAL, bg=ACCENT, fg="white",
                activebackground="#3a6fd0", relief="flat",
                padx=16, pady=14, bd=0, cursor="hand2")
BTN_DANGER = dict(BTN_OPTS, bg=RED, activebackground="#b8433e")
BTN_MUTED = dict(BTN_OPTS, bg="#454858", activebackground="#565968")


def big_button(parent, text, command, danger=False, muted=False, **kw):
    opts = dict(BTN_DANGER if danger else BTN_MUTED if muted else BTN_OPTS)
    opts.update(kw)
    return tk.Button(parent, text=text, command=command, **opts)


# ------------------------------------------------------------- app root ----

class AccessControlGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Access Control v{run.VERSION}")
        self.configure(bg=BG)
        try:
            self.attributes("-fullscreen", True)
        except tk.TclError:
            self.geometry("800x480")
        self.bind("<Escape>", lambda e: self.confirm_quit())
        self.protocol("WM_DELETE_WINDOW", self.confirm_quit)

        self.event_queue = queue.Queue()
        self.startup_cancel_event = threading.Event()
        self._startup_cancel_pending = False
        self.startup_active = True
        self.container = tk.Frame(self, bg=BG)
        self.container.pack(fill="both", expand=True)
        self.current_frame = None

        self.show_frame(SplashScreen)
        self.after(100, self._poll_queue)
        signal.signal(signal.SIGINT, self._signal_shutdown)
        signal.signal(signal.SIGTERM, self._signal_shutdown)

    def _signal_shutdown(self, signum, frame):
        """Signals run on Tk's main thread; schedule the UI work safely."""
        self.after_idle(self.cancel_startup)

    # ---- frame navigation ----
    def show_frame(self, frame_cls, **kwargs):
        if self.current_frame is not None:
            # Give a screen a chance to stop work before its widgets go
            # away.  Workers are daemon threads and may take a moment to
            # notice cancellation, so queue delivery is also scoped to the
            # originating frame below.
            on_hide = getattr(self.current_frame, "on_hide", None)
            if on_hide is not None:
                on_hide()
            self.current_frame.destroy()
        self.current_frame = frame_cls(self.container, self, **kwargs)
        self.current_frame.pack(fill="both", expand=True)

    # ---- queue plumbing shared by every worker thread ----
    def _poll_queue(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                origin, event = event
                if (isinstance(self.current_frame, QueueListener)
                        and (origin is None or origin is self.current_frame)):
                    self.current_frame.on_event(event)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def post(self, event_type, *, origin=None, **payload):
        """Queue a worker event, optionally bound to one screen instance."""
        self.event_queue.put((origin, (event_type, payload)))

    def confirm_quit(self):
        self.show_frame(ConfirmQuitScreen)

    def cancel_startup(self):
        """Request a clean exit while the splash worker is retrying hardware."""
        if not self.startup_active:
            self.destroy()
            return
        if self._startup_cancel_pending:
            return
        self._startup_cancel_pending = True
        self.startup_cancel_event.set()
        if isinstance(self.current_frame, SplashScreen):
            self.current_frame.status_label.config(text="Canceling startup...")


class QueueListener:
    """Mixin marker: frames that want queue events implement on_event()."""
    def post(self, event_type, **payload):
        """Post an event that only this exact frame instance can receive."""
        self.app.post(event_type, origin=self, **payload)

    def on_event(self, event):
        pass


# ------------------------------------------------------------ splash ------

class SplashScreen(tk.Frame, QueueListener):
    """Shown at startup. Brings up hardware on a background thread by
    calling run.initialize_hardware() -- the same function main() uses,
    so this screen has no opinion of its own about which peripherals are
    mandatory. If REQUIRE_ALL_HARDWARE is True, that call blocks/retries
    until everything is online (Dashboard only shows once it is); if
    False, it returns after one pass and the Dashboard opens regardless
    of what's missing."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app

        tk.Label(self, text="Access Control", font=FONT_TITLE, bg=BG, fg=FG).pack(pady=(60, 10))
        tk.Label(self, text=f"v{run.VERSION}", font=FONT_SMALL, bg=BG, fg=MUTED).pack()

        self.status_label = tk.Label(self, text="Starting up...", font=FONT_NORMAL,
                                      bg=BG, fg=MUTED, wraplength=600, justify="center")
        self.status_label.pack(pady=40)

        self.dots = tk.Frame(self, bg=BG)
        self.dots.pack(pady=10)
        self.dot_labels = {}
        for key, label in (("rfid", "RFID"), ("fingerprint", "Fingerprint"),
                            ("buzzer", "Buzzer"), ("lcd", "LCD")):
            row = tk.Frame(self.dots, bg=BG)
            row.pack(pady=4)
            dot = tk.Label(row, text="\u25cf", font=("DejaVu Sans", 16), bg=BG, fg=MUTED)
            dot.pack(side="left", padx=(0, 8))
            tk.Label(row, text=label, font=FONT_NORMAL, bg=BG, fg=FG).pack(side="left")
            self.dot_labels[key] = dot

        big_button(self, "Exit", self.app.cancel_startup, danger=True).pack(
            fill="x", padx=20, pady=(20, 10), ipady=8)

        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def _run(self):
        try:
            run.load_database()
            run.decay_lockout_backoff()

            def progress(attempt, state, missing):
                self.post("hw_progress", attempt=attempt, state=state, missing=missing)

            # Delegates entirely to run.py's shared initialize_hardware(): if
            # REQUIRE_ALL_HARDWARE is True this blocks/retries until every
            # peripheral is online (same as the CLI's boot gate); if False it
            # initializes once and returns right away regardless of what's
            # missing. Either way, this is the ONLY place that decision is
            # made -- the GUI never hardcodes its own opinion about which
            # hardware is mandatory, so it can't drift out of sync with the
            # CLI's behavior.
            started = run.initialize_hardware(
                progress_cb=progress, cancel_event=self.app.startup_cancel_event)
            self.post("hw_ready" if started else "hw_canceled")
        except Exception as exc:
            run.log(f"Hardware startup failed unexpectedly: {exc}", "ERROR")
            self.post("hw_failed", error=str(exc))

    def on_event(self, event):
        etype, payload = event
        if etype == "hw_progress":
            for key, dot in self.dot_labels.items():
                dot.config(fg=GREEN if payload["state"][key] else RED)
            if payload["missing"]:
                self.status_label.config(
                    text=f"Attempt {payload['attempt']}: waiting on "
                         f"{', '.join(payload['missing'])}...")
            else:
                self.status_label.config(text="All hardware online.")
        elif etype == "hw_ready":
            self.app.startup_active = False
            self.app.show_frame(Dashboard)
        elif etype == "hw_canceled":
            self.app.destroy()
        elif etype == "hw_failed":
            self.app.startup_active = False
            self.status_label.config(text=f"Startup failed: {payload['error']}")


# --------------------------------------------------------- confirm quit ---

class ConfirmQuitScreen(tk.Frame, QueueListener):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        tk.Label(self, text="Exit Access Control?", font=FONT_BIG, bg=BG, fg=FG).pack(pady=(150, 30))
        row = tk.Frame(self, bg=BG)
        row.pack()
        big_button(row, "Cancel", lambda: app.show_frame(Dashboard), muted=True).pack(side="left", padx=10)
        big_button(row, "Exit", self._quit, danger=True).pack(side="left", padx=10)
        self.app = app

    def _quit(self):
        if self.app.startup_active:
            self.app.cancel_startup()
        else:
            self.app.destroy()


# ------------------------------------------------------------- dashboard --

class Dashboard(tk.Frame, QueueListener):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=20, pady=(20, 10))
        tk.Label(header, text="Access Control", font=FONT_TITLE, bg=BG, fg=FG).pack(side="left")
        admin = run.config.get("admin_name") or "admin"
        tk.Label(header, text=f"Signed in as: {admin}", font=FONT_SMALL,
                 bg=BG, fg=MUTED).pack(side="right")

        status_row = tk.Frame(self, bg=BG)
        status_row.pack(fill="x", padx=20, pady=(0, 20))
        for key, label, online in (
            ("rfid", "RFID", run.rfid_online),
            ("fp", "Fingerprint", run.finger_sensor_online),
            ("buzz", "Buzzer", run.buzzer_online),
            ("lcd", "LCD", run.lcd_online),
        ):
            chip = tk.Label(status_row, text=f"\u25cf {label}", font=FONT_SMALL,
                             bg=PANEL_BG, fg=GREEN if online else RED, padx=10, pady=6)
            chip.pack(side="left", padx=(0, 8))

        remaining = run.current_lockout_remaining()
        if remaining > 0:
            tk.Label(self, text=f"LOCKED OUT \u2014 {int(remaining)}s remaining",
                     font=FONT_NORMAL, bg=ORANGE, fg="white", pady=8).pack(fill="x", padx=20, pady=(0, 10))

        grid = tk.Frame(self, bg=BG)
        grid.pack(fill="both", expand=True, padx=20, pady=10)
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        buttons = [
            ("Start Scanner", lambda: app.show_frame(ScannerScreen)),
            ("Enroll User", lambda: app.show_frame(EnrollScreen)),
            ("Manage Users", lambda: app.show_frame(ManageUsersScreen)),
            ("Master Card", lambda: app.show_frame(MasterCardScreen)),
            ("System Status", lambda: app.show_frame(StatusScreen)),
            ("Security Log", lambda: app.show_frame(LogsScreen)),
        ]
        for i, (text, cmd) in enumerate(buttons):
            b = big_button(grid, text, cmd)
            b.grid(row=i // 2, column=i % 2, sticky="nsew", padx=10, pady=10, ipady=20)
            grid.rowconfigure(i // 2, weight=1)

        big_button(self, "Exit", app.confirm_quit, danger=True).pack(
            side="bottom", fill="x", padx=20, pady=20, ipady=6)


# -------------------------------------------------------- back button bar -

def back_bar(parent, app, title):
    bar = tk.Frame(parent, bg=BG)
    bar.pack(fill="x", padx=20, pady=(20, 10))
    big_button(bar, "\u2190 Back", lambda: app.show_frame(Dashboard), muted=True).pack(side="left")
    tk.Label(bar, text=title, font=FONT_BIG, bg=BG, fg=FG).pack(side="left", padx=20)
    return bar


# ------------------------------------------------------------ scanner -----

class ScannerScreen(tk.Frame, QueueListener):
    """Mirrors run.scanner_mode()'s logic, but driven by a background
    thread that posts events instead of printing, so the touchscreen
    stays responsive and shows the same information the LCD does."""

    STATE_COLORS = {
        "ready": (ACCENT, "Ready"),
        "checking": (YELLOW, "Checking..."),
        "granted": (GREEN, "ACCESS GRANTED"),
        "denied": (RED, "ACCESS DENIED"),
        "locked": (ORANGE, "LOCKED OUT"),
    }

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.stop_event = threading.Event()
        self.worker = None

        back_bar(self, app, "Scanner")

        self.status_panel = tk.Frame(self, bg=ACCENT, height=160)
        self.status_panel.pack(fill="x", padx=20, pady=10)
        self.status_title = tk.Label(self.status_panel, text="Ready", font=FONT_TITLE,
                                      bg=ACCENT, fg="white")
        self.status_title.pack(pady=(20, 4))
        self.status_detail = tk.Label(self.status_panel, text="Scan an RFID card to begin",
                                       font=FONT_NORMAL, bg=ACCENT, fg="white")
        self.status_detail.pack(pady=(0, 20))

        self.feed = tk.Text(self, font=FONT_MONO, bg=PANEL_BG, fg=FG, height=10,
                             state="disabled", wrap="word", bd=0, padx=12, pady=12)
        self.feed.pack(fill="both", expand=True, padx=20, pady=10)

        self.toggle_btn = big_button(self, "Start Scanning", self.toggle)
        self.toggle_btn.pack(fill="x", padx=20, pady=(0, 20), ipady=10)

        self.set_state("ready", "Press Start Scanning to begin")

    def set_state(self, state, detail=""):
        color, title = self.STATE_COLORS.get(state, (ACCENT, state))
        self.status_panel.config(bg=color)
        self.status_title.config(bg=color, text=title)
        self.status_detail.config(bg=color, text=detail)

    def log_feed(self, text):
        self.feed.config(state="normal")
        self.feed.insert("end", text + "\n")
        self.feed.see("end")
        self.feed.config(state="disabled")

    def toggle(self):
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self.toggle_btn.config(text="Stopping...", state="disabled")
        else:
            self.stop_event = threading.Event()
            self.worker = threading.Thread(target=self._scan_loop, args=(self.stop_event,), daemon=True)
            self.worker.start()
            self.toggle_btn.config(text="Stop Scanning", bg=RED, activebackground="#b8433e")
            self.set_state("ready", "Scan an RFID card")

    def on_hide(self):
        # Leaving the screen stops the background scan loop too.
        self.stop_event.set()

    def _scan_loop(self, stop_event):
        if not run.rfid_online:
            self.post("scan_error", text="RFID reader is offline. Connect it, then restart the application.")
            self.post("scan_stopped")
            return
        if run.config["AUTHORIZED_UID"] is None:
            self.post("scan_error", text="No master RFID card is set. Set one from Master Card first.")
            self.post("scan_stopped")
            return

        run.decay_lockout_backoff()
        run.lcd_show("Access Control", "Ready")
        run.led_blink()

        while not stop_event.is_set():
            remaining = run.current_lockout_remaining()
            if remaining > 0:
                self.post("scan_locked", remaining=remaining)
                run.lcd_show("LOCKED OUT", f"Wait {int(remaining)}s")
                time.sleep(min(1.0, remaining))
                continue

            uid = run.read_rfid_nonblocking()
            if uid is None:
                time.sleep(run.RFID_SCAN_DELAY)
                continue

            run.new_scan_session()
            self.post("scan_card_detected", uid=uid)
            run.lcd_show("Card detected", "Checking...")
            granted = False
            hardware_error = False

            if uid == run.config["AUTHORIZED_UID"]:
                admin_name = run.config.get("admin_name") or "admin"
                self.post("scan_rfid_ok", admin_name=admin_name)
                run.security_event("rfid_authorized", uid=uid, role="admin", admin_name=admin_name)
                run.lcd_show("RFID OK", "Scan finger...")
                time.sleep(run.FINGERPRINT_SETTLE_TIME)

                if not run.finger_sensor_online:
                    hardware_error = True
                    self.post("scan_error", text="Fingerprint sensor is offline; access cannot be verified.")
                    run.lcd_show("SENSOR OFFLINE", "Access unavailable")
                    status = "HARDWARE_OFFLINE"
                    slot = name = None
                else:
                    status, slot, name, _confidence = run.authenticate_fingerprint(cancel_event=stop_event)

                if status == "SUCCESS":
                    schedule = run.config["user_schedules"].get(str(slot))
                    if not run.is_within_schedule(schedule):
                        self.post("scan_denied", reason=f"{name} is outside allowed hours "
                                       f"({schedule['start']}-{schedule['end']})")
                        run.security_event("access_denied", reason="outside_schedule",
                                            user=name, slot=slot, schedule=schedule)
                        run.buzz_denied()
                        run.lcd_show("ACCESS DENIED", "Outside hours")
                    else:
                        self.post("scan_granted", name=name, slot=slot)
                        granted = True
                        run.security_event("access_granted", method="RFID + fingerprint",
                                           fingerprint_slot=slot, user=name)
                        run.buzz_granted()
                        run.led_solid_on()
                        run.lcd_show("ACCESS GRANTED", name[:run.LCD_COLS])
                elif status == "NO_MATCH":
                    self.post("scan_denied", reason="Fingerprint verification failed.")
                    run.security_event("access_denied", reason="fingerprint_not_recognized")
                    run.buzz_denied()
                    run.lcd_show("ACCESS DENIED", "Finger no match")
                elif status == "COMMUNICATION_ERROR":
                    hardware_error = True
                    self.post("scan_error", text="Fingerprint sensor comms failed.")
                    run.buzz_denied()
                    run.lcd_show("SENSOR ERROR", "Try again later")
                elif status == "HARDWARE_OFFLINE":
                    pass
                elif status == "SHUTDOWN":
                    break
                else:
                    self.post("scan_denied", reason="Fingerprint timeout.")
                    run.buzz_denied()
                    run.lcd_show("ACCESS DENIED", "Finger timeout")
            else:
                self.post("scan_denied", reason="Unknown RFID card.")
                run.security_event("access_denied", reason="unknown_rfid", uid=uid)
                run.buzz_denied()
                run.lcd_show("ACCESS DENIED", "Unknown card")

            if granted:
                run.register_success()
            elif not hardware_error:
                cooldown = run.register_denial()
                if cooldown is not None:
                    self.post("scan_lockout_triggered", cooldown=cooldown,
                                  count=run.config["lockout_state"]["lockout_count"])
                    run.security_event("lockout_triggered", cooldown_seconds=cooldown,
                                        lockout_count=run.config["lockout_state"]["lockout_count"])
                    run.lcd_show("LOCKED OUT", f"Wait {int(cooldown)}s")

            time.sleep(1.5)
            if run.current_lockout_remaining() == 0 and not stop_event.is_set():
                run.lcd_show("Access Control", "Ready")
                run.led_blink()

        run.led_off()
        self.post("scan_stopped")

    def on_event(self, event):
        etype, payload = event
        if etype == "scan_card_detected":
            self.set_state("checking", "Card detected \u2014 checking...")
            self.log_feed(f"Card detected: {payload['uid']}")
        elif etype == "scan_rfid_ok":
            self.set_state("checking", "RFID OK \u2014 scan finger...")
            self.log_feed(f"RFID authorized ({payload['admin_name']}) \u2014 waiting for fingerprint")
        elif etype == "scan_granted":
            self.set_state("granted", f"{payload['name']} (slot #{payload['slot']})")
            self.log_feed(f"GRANTED: {payload['name']} (slot #{payload['slot']})")
        elif etype == "scan_denied":
            self.set_state("denied", payload["reason"])
            self.log_feed(f"DENIED: {payload['reason']}")
        elif etype == "scan_error":
            self.set_state("denied", payload["text"])
            self.log_feed(f"ERROR: {payload['text']}")
        elif etype == "scan_locked":
            self.set_state("locked", f"Wait {int(payload['remaining'])}s")
        elif etype == "scan_lockout_triggered":
            self.log_feed(f"Locked out for {int(payload['cooldown'])}s "
                          f"(lockout #{payload['count']})")
        elif etype == "scan_stopped":
            self.toggle_btn.config(text="Start Scanning", bg=ACCENT,
                                    activebackground="#3a6fd0", state="normal")
            self.set_state("ready", "Stopped")
            self.log_feed("Scanner stopped.")


# ------------------------------------------------------------- enroll -----

class EnrollScreen(tk.Frame, QueueListener):
    """Reimplements run.enroll_fingerprint()'s capture sequence directly
    against the sensor primitives (wait_for_finger / image_2_tz /
    finger_fast_search / create_model / store_model), because the
    original is built around input() and can't be reused verbatim in a
    GUI. Name and schedule are collected up front via the form, then the
    capture sequence runs on a background thread with a Cancel option."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.cancel_event = threading.Event()
        self.worker = None

        back_bar(self, app, "Enroll User")

        form = tk.Frame(self, bg=BG)
        form.pack(fill="x", padx=20, pady=10)

        tk.Label(form, text="Name", font=FONT_NORMAL, bg=BG, fg=FG).grid(row=0, column=0, sticky="w", pady=6)
        self.name_entry = tk.Entry(form, font=FONT_NORMAL)
        self.name_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=10)

        tk.Label(form, text="Access hours (optional)", font=FONT_NORMAL, bg=BG, fg=FG).grid(
            row=1, column=0, sticky="w", pady=6)
        tk.Label(form, text="Start", font=FONT_SMALL, bg=BG, fg=MUTED).grid(row=1, column=1)
        self.start_entry = tk.Entry(form, font=FONT_NORMAL, width=8)
        self.start_entry.grid(row=1, column=1, padx=(60, 4))
        self.start_entry.insert(0, "")
        tk.Label(form, text="End", font=FONT_SMALL, bg=BG, fg=MUTED).grid(row=1, column=2)
        self.end_entry = tk.Entry(form, font=FONT_NORMAL, width=8)
        self.end_entry.grid(row=1, column=2, padx=(50, 4))
        tk.Label(form, text="HH:MM-HH:MM, leave blank for unrestricted", font=FONT_SMALL,
                 bg=BG, fg=MUTED).grid(row=2, column=0, columnspan=4, sticky="w")

        form.columnconfigure(1, weight=1)

        self.status_label = tk.Label(self, text="Enter a name, then press Start.", font=FONT_NORMAL,
                                      bg=PANEL_BG, fg=FG, wraplength=700, justify="center", pady=30)
        self.status_label.pack(fill="both", expand=True, padx=20, pady=10)

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(0, 20))
        self.start_btn = big_button(btn_row, "Start Enrollment", self.start)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 10), ipady=10)
        self.cancel_btn = big_button(btn_row, "Cancel", self.cancel, danger=True, state="disabled")
        self.cancel_btn.pack(side="left", expand=True, fill="x", ipady=10)

    def start(self):
        if not run.finger_sensor_online:
            self.status_label.config(
                text="Fingerprint sensor is offline. Connect it, then restart the application.",
                bg=RED, fg="white")
            return
        name = self.name_entry.get().strip()
        if not name:
            self.status_label.config(text="Name cannot be empty.")
            return
        start = self.start_entry.get().strip()
        end = self.end_entry.get().strip()
        schedule = None
        if start or end:
            schedule = {"start": start, "end": end}
            valid, err = run.validate_schedule(schedule)
            if not valid:
                self.status_label.config(text=f"Schedule error: {err}")
                return

        self.cancel_event = threading.Event()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal", bg=RED, activebackground="#b8433e")
        self.worker = threading.Thread(target=self._enroll, args=(name, schedule, self.cancel_event), daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_event.set()
        self.cancel_btn.config(state="disabled")

    def on_hide(self):
        """Navigation has the same cancellation semantics as Cancel."""
        self.cancel_event.set()

    def _enroll(self, name, schedule, cancel_event):
        def status(text):
            self.post("enroll_status", text=text)

        if cancel_event.is_set():
            self.post("enroll_done", ok=False, text="Canceled.")
            return

        slot = run.find_next_free_slot()
        if slot is None:
            self.post("enroll_done", ok=False, text="Fingerprint database is full.")
            return

        status(f"Assigning '{name}' to slot #{slot}.\nStep 1/2 - place your finger...")
        result = run.wait_for_finger(timeout=15, cancel_event=cancel_event)
        if result is None:
            self.post("enroll_done", ok=False, text="Fingerprint communication failure.")
            return
        if result is False:
            self.post("enroll_done", ok=False, text="No fingerprint captured (timed out).")
            return

        if cancel_event.is_set():
            self.post("enroll_done", ok=False, text="Canceled.")
            return

        try:
            if cancel_event.is_set():
                self.post("enroll_done", ok=False, text="Canceled.")
                return
            if run.finger.image_2_tz(1) != run.adafruit_fingerprint.OK:
                self.post("enroll_done", ok=False, text="Could not process fingerprint.")
                return

            status("Checking for existing registration...")
            if run.finger.finger_fast_search() == run.adafruit_fingerprint.OK:
                existing_slot = run.finger.finger_id
                existing_name = run.config["user_mappings"].get(
                    str(existing_slot), f"Unknown User (Slot #{existing_slot})")
                run.security_event("duplicate_fingerprint_enrollment",
                                    existing_slot=existing_slot, existing_user=existing_name)
                run.wait_for_no_finger(cancel_event=cancel_event)
                self.post("enroll_done", ok=False,
                              text=f"Fingerprint already registered: slot #{existing_slot} ({existing_name}).")
                return
        except (OSError, RuntimeError) as e:
            self.post("enroll_done", ok=False, text=f"Duplicate check failed: {e}")
            return

        status("Remove your finger.")
        if not run.wait_for_no_finger(timeout=10, cancel_event=cancel_event):
            self.post("enroll_done", ok=False,
                          text="Could not confirm finger removal. Try again.")
            return
        time.sleep(0.5)

        if cancel_event.is_set():
            self.post("enroll_done", ok=False, text="Canceled.")
            return

        status("Step 2/2 - place the SAME finger again...")
        result = run.wait_for_finger(timeout=15, cancel_event=cancel_event)
        if result is None:
            self.post("enroll_done", ok=False, text="Fingerprint communication failure.")
            return
        if result is False:
            self.post("enroll_done", ok=False, text="Second capture failed (timed out).")
            return

        try:
            if cancel_event.is_set():
                self.post("enroll_done", ok=False, text="Canceled.")
                return
            if run.finger.image_2_tz(2) != run.adafruit_fingerprint.OK:
                self.post("enroll_done", ok=False, text="Could not process second fingerprint.")
                return

            model_result = run.finger.create_model()
            if model_result == run.adafruit_fingerprint.ENROLLMISMATCH:
                run.security_event("enrollment_mismatch", user=name)
                self.post("enroll_done", ok=False, text="The two fingerprints did not match.")
                return
            if model_result != run.adafruit_fingerprint.OK:
                self.post("enroll_done", ok=False,
                              text=f"Could not create model: {run.result_name(model_result)}")
                return

            if cancel_event.is_set():
                self.post("enroll_done", ok=False, text="Canceled.")
                return
            store_result = run.finger.store_model(slot)
            if store_result != run.adafruit_fingerprint.OK:
                self.post("enroll_done", ok=False,
                              text=f"Could not store fingerprint: {run.result_name(store_result)}")
                return

            if cancel_event.is_set():
                self.post("enroll_done", ok=False, text="Canceled.")
                return
            run.config["user_mappings"][str(slot)] = name
            if schedule is not None:
                run.config["user_schedules"][str(slot)] = schedule
            else:
                run.config["user_schedules"].pop(str(slot), None)

            if not run.save_database():
                self.post("enroll_done", ok=False,
                              text=f"Stored in sensor slot #{slot}, but database save failed.")
                return

            run.security_event("fingerprint_enrolled", user=name, slot=slot, schedule=schedule)
            detail = f"User: {name}  Slot: #{slot}"
            if schedule:
                detail += f"\nAccess restricted to {schedule['start']}-{schedule['end']} daily."
            self.post("enroll_done", ok=True, text=detail)

        except (OSError, RuntimeError) as e:
            self.post("enroll_done", ok=False, text=f"Enrollment error: {e}")

    def on_event(self, event):
        etype, payload = event
        if etype == "enroll_status":
            self.status_label.config(text=payload["text"], bg=PANEL_BG, fg=FG)
        elif etype == "enroll_done":
            ok = payload["ok"]
            self.status_label.config(
                text=("ENROLLMENT SUCCESSFUL\n" if ok else "ENROLLMENT FAILED\n") + payload["text"],
                bg=GREEN if ok else RED, fg="white")
            self.start_btn.config(state="normal")
            self.cancel_btn.config(state="disabled", bg="#454858", activebackground="#565968")


# -------------------------------------------------------- manage users ----

class ManageUsersScreen(tk.Frame, QueueListener):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        back_bar(self, app, "Manage Users")

        self.listbox = tk.Listbox(self, font=FONT_NORMAL, bg=PANEL_BG, fg=FG,
                                   selectbackground=ACCENT, bd=0, highlightthickness=0)
        self.listbox.pack(fill="both", expand=True, padx=20, pady=10)
        self._refresh_list()

        form = tk.Frame(self, bg=BG)
        form.pack(fill="x", padx=20, pady=6)
        tk.Label(form, text="Schedule:", font=FONT_NORMAL, bg=BG, fg=FG).pack(side="left")
        self.start_entry = tk.Entry(form, font=FONT_NORMAL, width=8)
        self.start_entry.pack(side="left", padx=6)
        tk.Label(form, text="-", font=FONT_NORMAL, bg=BG, fg=FG).pack(side="left")
        self.end_entry = tk.Entry(form, font=FONT_NORMAL, width=8)
        self.end_entry.pack(side="left", padx=6)
        big_button(form, "Save Schedule", self.save_schedule, muted=True).pack(side="left", padx=10)
        big_button(form, "Clear Schedule", self.clear_schedule, muted=True).pack(side="left")

        self.msg = tk.Label(self, text="", font=FONT_SMALL, bg=BG, fg=MUTED)
        self.msg.pack(fill="x", padx=20)

        big_button(self, "Delete Selected User", self.delete_selected, danger=True).pack(
            fill="x", padx=20, pady=(10, 20), ipady=8)

    def _refresh_list(self):
        self.listbox.delete(0, "end")
        self._slots = []
        for slot, name in sorted(run.config["user_mappings"].items(), key=lambda x: int(x[0])):
            schedule = run.config["user_schedules"].get(slot)
            sched_str = f"  [{schedule['start']}-{schedule['end']}]" if schedule else ""
            self.listbox.insert("end", f"#{slot}  {name}{sched_str}")
            self._slots.append(slot)
        if not self._slots:
            self.listbox.insert("end", "(no registered users)")

    def _selected_slot(self):
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self._slots):
            return None
        return self._slots[sel[0]]

    def save_schedule(self):
        slot = self._selected_slot()
        if slot is None:
            self.msg.config(text="Select a user first.")
            return
        schedule = {"start": self.start_entry.get().strip(), "end": self.end_entry.get().strip()}
        valid, err = run.validate_schedule(schedule)
        if not valid:
            self.msg.config(text=f"Schedule error: {err}")
            return
        previous = run.config["user_schedules"].get(slot)
        run.config["user_schedules"][slot] = schedule
        if run.save_database():
            name = run.config["user_mappings"][slot]
            run.security_event("schedule_updated", user=name, slot=int(slot), schedule=schedule)
            self.msg.config(text="Schedule saved.")
            self._refresh_list()
        else:
            if previous is None:
                run.config["user_schedules"].pop(slot, None)
            else:
                run.config["user_schedules"][slot] = previous
            self.msg.config(text="Save failed.")

    def clear_schedule(self):
        slot = self._selected_slot()
        if slot is None:
            self.msg.config(text="Select a user first.")
            return
        previous = run.config["user_schedules"].pop(slot, None)
        if run.save_database():
            name = run.config["user_mappings"][slot]
            run.security_event("schedule_updated", user=name, slot=int(slot), schedule=None)
            self.msg.config(text="Schedule cleared (unrestricted access).")
            self._refresh_list()
        else:
            if previous is not None:
                run.config["user_schedules"][slot] = previous
            self.msg.config(text="Save failed.")

    def delete_selected(self):
        slot = self._selected_slot()
        if slot is None:
            self.msg.config(text="Select a user first.")
            return
        name = run.config["user_mappings"][slot]
        self.app.show_frame(ConfirmDeleteScreen, slot=slot, name=name)


class ConfirmDeleteScreen(tk.Frame, QueueListener):
    def __init__(self, parent, app, slot, name):
        super().__init__(parent, bg=BG)
        self.app = app
        self.slot = slot
        self.name = name
        tk.Label(self, text=f"Delete '{name}' (slot #{slot})?", font=FONT_BIG,
                 bg=BG, fg=FG, wraplength=700).pack(pady=(150, 10))
        tk.Label(self, text="This removes the fingerprint from the sensor and the database. "
                             "This cannot be undone.", font=FONT_NORMAL, bg=BG, fg=MUTED,
                 wraplength=700, justify="center").pack(pady=(0, 30))
        row = tk.Frame(self, bg=BG)
        row.pack()
        big_button(row, "Cancel", lambda: app.show_frame(ManageUsersScreen), muted=True).pack(side="left", padx=10)
        big_button(row, "Delete", self._delete, danger=True).pack(side="left", padx=10)

    def _delete(self):
        if not run.finger_sensor_online:
            self.app.show_frame(ManageUsersScreen)
            return
        try:
            result = run.finger.delete_model(int(self.slot))
            if result != run.adafruit_fingerprint.OK:
                self.app.show_frame(ManageUsersScreen)
                return
            del run.config["user_mappings"][self.slot]
            run.config["user_schedules"].pop(self.slot, None)
            run.save_database()
            run.security_event("fingerprint_deleted", user=self.name, slot=int(self.slot))
        except (OSError, RuntimeError):
            pass
        self.app.show_frame(ManageUsersScreen)


# ------------------------------------------------------------ master card -

class MasterCardScreen(tk.Frame, QueueListener):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.cancel_event = threading.Event()
        self.worker = None

        back_bar(self, app, "Master Card")

        form = tk.Frame(self, bg=BG)
        form.pack(fill="x", padx=20, pady=10)
        tk.Label(form, text="Admin display name", font=FONT_NORMAL, bg=BG, fg=FG).pack(anchor="w")
        self.name_entry = tk.Entry(form, font=FONT_NORMAL)
        self.name_entry.pack(fill="x", pady=6)
        self.name_entry.insert(0, run.config.get("admin_name") or "")

        current = run.config.get("AUTHORIZED_UID")
        tk.Label(self, text=f"Current master UID: {current or '(none set)'}",
                 font=FONT_SMALL, bg=BG, fg=MUTED).pack(padx=20, pady=(0, 10), anchor="w")

        self.status_label = tk.Label(self, text="Enter a name (optional), then press Scan New Card.",
                                      font=FONT_NORMAL, bg=PANEL_BG, fg=FG, wraplength=700,
                                      justify="center", pady=30)
        self.status_label.pack(fill="both", expand=True, padx=20, pady=10)

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(0, 20))
        self.start_btn = big_button(btn_row, "Scan New Card", self.start)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 10), ipady=10)
        self.cancel_btn = big_button(btn_row, "Cancel", self.cancel, danger=True, state="disabled")
        self.cancel_btn.pack(side="left", expand=True, fill="x", ipady=10)

    def start(self):
        if not run.rfid_online:
            self.status_label.config(
                text="RFID reader is offline. Connect it, then restart the application.",
                bg=RED, fg="white")
            return
        admin_name = self.name_entry.get().strip() or None
        self.cancel_event = threading.Event()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal", bg=RED, activebackground="#b8433e")
        self.status_label.config(text="Scan the new master RFID card now...", bg=PANEL_BG, fg=FG)
        self.worker = threading.Thread(target=self._scan, args=(admin_name, self.cancel_event), daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_event.set()
        self.cancel_btn.config(state="disabled")

    def on_hide(self):
        """Do not leave an RFID scan running after this screen is closed."""
        self.cancel_event.set()

    def _scan(self, admin_name, cancel_event):
        start = time.monotonic()
        while time.monotonic() - start < 15:
            if cancel_event.is_set():
                self.post("master_done", ok=False, text="Canceled.")
                return
            uid = run.read_rfid_nonblocking()
            if uid is None:
                time.sleep(0.1)
                continue
            if cancel_event.is_set():
                self.post("master_done", ok=False, text="Canceled.")
                return
            if uid == run.config["AUTHORIZED_UID"]:
                self.post("master_done", ok=False, text="This is already the authorized master card.")
                return
            old_uid = run.config["AUTHORIZED_UID"]
            old_name = run.config["admin_name"]
            run.config["AUTHORIZED_UID"] = uid
            run.config["admin_name"] = admin_name
            if run.save_database():
                run.security_event("master_rfid_changed", uid=uid, admin_name=admin_name)
                text = f"Master RFID changed. New UID: {uid}"
                if admin_name:
                    text += f"\nAdmin: {admin_name}"
                self.post("master_done", ok=True, text=text)
            else:
                run.config["AUTHORIZED_UID"] = old_uid
                run.config["admin_name"] = old_name
                self.post("master_done", ok=False,
                              text="Database save failed; the existing master card was kept.")
            return
        self.post("master_done", ok=False, text="Timed out waiting for a card.")

    def on_event(self, event):
        etype, payload = event
        if etype == "master_done":
            ok = payload["ok"]
            self.status_label.config(text=payload["text"], bg=GREEN if ok else RED, fg="white")
            self.start_btn.config(state="normal")
            self.cancel_btn.config(state="disabled", bg="#454858", activebackground="#565968")


# ----------------------------------------------------------------- status -

class StatusScreen(tk.Frame, QueueListener):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        back_bar(self, app, "System Status")

        self.text = tk.Text(self, font=FONT_MONO, bg=PANEL_BG, fg=FG,
                             state="disabled", wrap="word", bd=0, padx=14, pady=14)
        self.text.pack(fill="both", expand=True, padx=20, pady=10)

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=20, pady=(0, 20))
        big_button(btn_row, "Refresh", self.refresh, muted=True).pack(side="left", expand=True, fill="x", padx=(0, 6))
        big_button(btn_row, "Test Buzzer", self.test_buzzer, muted=True).pack(side="left", expand=True, fill="x", padx=6)
        big_button(btn_row, "Test LED", self.test_led, muted=True).pack(side="left", expand=True, fill="x", padx=6)
        big_button(btn_row, "Test LCD", self.test_lcd, muted=True).pack(side="left", expand=True, fill="x", padx=(6, 0))

        self.refresh()

    def refresh(self):
        s = run.get_status_dict()
        lines = []
        lines.append(f"RFID: master UID {s['rfid']['master_uid']}  "
                      f"admin: {s['rfid']['admin_name'] or '(not set)'}  "
                      f"[{'ONLINE' if s['rfid']['online'] else 'OFFLINE'}]")
        lines.append(f"Fingerprint: {'ONLINE' if s['fingerprint_sensor']['online'] else 'OFFLINE'}  "
                      f"usage {s['fingerprint_sensor']['local_usage']}/{s['fingerprint_sensor']['capacity']}")
        lines.append(f"Buzzer: {'ONLINE' if s['buzzer']['online'] else 'OFFLINE'}  (pin {s['buzzer']['pin']})")
        lines.append(f"LED: {'ONLINE' if s['led']['online'] else 'OFFLINE'}  (pin {s['led']['pin']})")
        lcd = s["lcd"]
        active = f", active: {lcd['active']['interface']}" if lcd["active"] else ""
        lines.append(f"LCD: {'ONLINE' if lcd['online'] else 'OFFLINE'}  "
                      f"(configured: {lcd['configured_interface']}{active})")
        if lcd["active"]:
            lines.append(f"  {lcd['active']['detection']}")
        if lcd["last_error"]:
            lines.append(f"  Diagnostic: {lcd['last_error']}")
        lo = s["lockout"]
        lines.append("")
        if lo["locked_out"]:
            lines.append(f"LOCKED OUT for {int(lo['remaining_seconds'])}s more")
        else:
            lines.append("Not currently locked out")
        lines.append(f"Consecutive failures: {lo['consecutive_failures']}/{lo['threshold']}  "
                      f"|  Lockout count: {lo['lockout_count']}")
        lines.append("")
        lines.append(f"Registered users ({len(s['users'])}):")
        for u in s["users"]:
            sched = f"  [{u['schedule']['start']}-{u['schedule']['end']}]" if u["schedule"] else ""
            lines.append(f"  #{u['slot']}  {u['name']}{sched}")

        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "\n".join(lines))
        self.text.config(state="disabled")

    def test_buzzer(self):
        run.test_buzzer()

    def test_led(self):
        run.test_led()

    def test_lcd(self):
        if not run.test_lcd():
            self.refresh()


# ------------------------------------------------------------------- logs -

class LogsScreen(tk.Frame, QueueListener):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        back_bar(self, app, "Security Log")

        self.text = tk.Text(self, font=FONT_MONO, bg=PANEL_BG, fg=FG,
                             state="disabled", wrap="word", bd=0, padx=14, pady=14)
        self.text.pack(fill="both", expand=True, padx=20, pady=10)

        big_button(self, "Refresh", self.refresh, muted=True).pack(fill="x", padx=20, pady=(0, 20), ipady=8)
        self.refresh()

    def refresh(self):
        records = run.get_recent_logs(50)
        lines = []
        for r in records:
            if r.get("_parse_error"):
                lines.append(f"(invalid entry) {r['_raw']}")
                continue
            details = {k: v for k, v in r.items() if k not in ("timestamp", "event", "session")}
            detail_str = f"  {details}" if details else ""
            lines.append(f"{r.get('timestamp')}  {r.get('event')}{detail_str}")
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "\n".join(lines) if lines else "No security events logged yet.")
        self.text.config(state="disabled")


# --------------------------------------------------------------------------

if __name__ == "__main__":
    app = AccessControlGUI()
    try:
        app.mainloop()
    finally:
        run.cleanup()
