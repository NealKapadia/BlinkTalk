"""
BlinkTalk - laptop side of the board link: user picker, start/stop, sensitivity
and the guided calibration screen.

Everything goes over the SK-AM62B's serial console (the same cable PuTTY uses),
so no network is needed. The speller types shell commands into the console
(logging in as root if needed) and reads the "BLINKTALK ..." lines the board prints.

Board side: edge/blinktalk_live.py (live) and edge/calib_session.py (calibration).
"""
import queue
import re
import threading
import time
import tkinter as tk

BG, PANEL, INK, QUIET = "#0F1B24", "#182A36", "#F4F1EA", "#8FA3AE"
TEAL, AMBER, RED, GREEN = "#4FB3BF", "#F2A541", "#C0392B", "#5FBF77"

ACTIONS = ["blink", "left", "right", "rest"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r")
TITLES = {"blink": "BLINK", "left": "LOOK LEFT", "right": "LOOK RIGHT", "rest": "REST"}
HOW = {
    "blink": "Each time you see NOW, blink once, firmly. Keep your eyes looking ahead.",
    "left": "Each time you see NOW, look LEFT, hold for half a second, then back to the centre.",
    "right": "Each time you see NOW, look RIGHT, hold for half a second, then back to the centre.",
    "rest": "Just relax for a minute: read something on the screen and blink normally.",
}


def beep(freq=880, ms=150):
    def run():
        try:
            import winsound
            winsound.Beep(freq, ms)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


class BoardLink:
    """Serial console of the board: send shell commands, receive lines."""

    def __init__(self, port_name):
        import serial                      # pip install pyserial
        self.port = serial.Serial(port_name, 115200, timeout=0.2)
        self.lines = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        self.send("\n")                    # wake the console / show the prompt

    def _reader(self):
        buf = b""
        while True:
            try:
                buf += self.port.read(256)
            except Exception as e:
                self.lines.put(f"BLINKTALK LINK lost {e}")
                return
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = ANSI.sub("", raw.decode(errors="replace")).strip()
                if "BLINKTALK " in text:            # drop prompt / terminal codes in front
                    text = text[text.index("BLINKTALK "):]
                if text:
                    self.lines.put(text)
            if buf.rstrip().endswith(b"login:"):          # board just booted
                buf = b""
                self.send("root\n")
                self.lines.put("BLINKTALK LINK logged in")

    def send(self, text):
        with self._lock:
            self.port.write(text.encode())

    def run(self, command):
        """Stop whatever runs on the console, then start `command` in /root/edge."""
        def go():
            self.send("\x03")
            time.sleep(0.6)
            self.send("\n")
            time.sleep(0.2)
            self.send(f"cd /root/edge && {command}\n")
        threading.Thread(target=go, daemon=True).start()

    def stop(self):
        self.send("\x03")


class BoardPanel:
    """Control bar + calibration overlay, added to the speller window."""

    def __init__(self, app, link, led=12, default_new="alex"):
        self.app, self.link, self.led = app, link, led
        root = app.root
        self.running_user = None
        self.calibrating = False
        self.seen_live = False
        self.cal_user = None
        self.users = []
        self.last_recall = {}

        bar = tk.Frame(root, bg=PANEL)
        bar.pack(side="bottom", fill="x", padx=40, pady=(0, 6))
        lab = dict(bg=PANEL, fg=QUIET, font=("Segoe UI", 12))
        btn = dict(font=("Segoe UI", 12, "bold"), relief="flat", takefocus=0, padx=10, pady=4)

        tk.Label(bar, text="User", **lab).pack(side="left", padx=(12, 4), pady=8)
        self.user_var = tk.StringVar(value="")
        self.user_menu = tk.OptionMenu(bar, self.user_var, "")
        self.user_menu.configure(font=("Segoe UI", 12), bg=BG, fg=INK, activebackground=TEAL,
                                 highlightthickness=0, relief="flat", takefocus=0, width=10)
        self.user_menu.pack(side="left")
        tk.Button(bar, text="Start", bg=TEAL, fg=BG, command=self.start, **btn).pack(side="left", padx=4)
        tk.Button(bar, text="Stop", bg=BG, fg=INK, command=self.stop, **btn).pack(side="left", padx=4)

        tk.Label(bar, text="   Sensitivity", **lab).pack(side="left", padx=(12, 4))
        self.sens = tk.Scale(bar, from_=1, to=10, orient="horizontal", length=160, showvalue=True,
                             bg=PANEL, fg=INK, troughcolor=BG, highlightthickness=0, takefocus=0,
                             command=self._sens_changed)
        self.sens.set(5)
        self.sens.pack(side="left")
        self._sens_job = None

        tk.Label(bar, text="   New user", **lab).pack(side="left", padx=(12, 4))
        self.new_name = tk.Entry(bar, width=10, font=("Segoe UI", 12), bg=BG, fg=INK,
                                 insertbackground=INK, relief="flat")
        self.new_name.insert(0, default_new)
        self.new_name.pack(side="left")
        tk.Button(bar, text="Calibrate", bg=AMBER, fg=BG, command=self.calibrate,
                  **btn).pack(side="left", padx=6)

        self.status = tk.Label(bar, text="board: connecting...", **lab)
        self.status.pack(side="right", padx=12)

        self._build_overlay()
        root.after(2500, self._first_contact)

    # ------------------------------------------------------------ controls
    def _first_contact(self):
        if self.seen_live:
            self.status.config(text="board: BlinkTalk already running")
        else:
            self.refresh_users()

    def refresh_users(self):
        self.link.run("python3 calib_session.py --list-users")

    def _set_users(self, names, select=None):
        self.users = [n for n in names if n]
        menu = self.user_menu["menu"]
        menu.delete(0, "end")
        for n in self.users:
            menu.add_command(label=n, command=lambda v=n: self.user_var.set(v))
        if select and select in self.users:
            self.user_var.set(select)
        elif self.user_var.get() not in self.users and self.users:
            self.user_var.set(self.users[0])
        self.status.config(text=f"board: ready ({len(self.users)} users)")

    def start(self, user=None):
        user = user or self.user_var.get()
        if not user:
            self.status.config(text="board: pick a user (or calibrate one) first")
            return
        self.running_user = user
        self.status.config(text=f"board: starting {user}...")
        self.link.run(f"python3 blinktalk_live.py --model models/{user}.joblib --led {self.led} "
                      f"--sensitivity {int(self.sens.get())}")
        self.app.root.focus_set()

    def stop(self):
        self.link.stop()
        self.running_user = None
        self.status.config(text="board: stopped")
        self.app.root.focus_set()

    def _sens_changed(self, _value):
        # send once the slider settles; the live program reads "sens N" from its input
        if self._sens_job:
            self.app.root.after_cancel(self._sens_job)
        self._sens_job = self.app.root.after(400, self._send_sens)

    def _send_sens(self):
        self._sens_job = None
        if self.running_user:
            self.link.send(f"sens {int(self.sens.get())}\n")
        self.app.root.focus_set()

    def calibrate(self):
        name = "".join(c for c in self.new_name.get().strip().lower() if c.isalnum() or c in "_-")
        if not name:
            self.status.config(text="board: type a name for the new user")
            return
        self.cal_user = name
        self.calibrating = True
        self.running_user = None
        self._show_overlay(name)
        self.link.run(f"python3 calib_session.py --user {name} --led {self.led}")
        self.app.root.focus_set()

    # ------------------------------------------------------------ overlay
    def _build_overlay(self):
        o = self.overlay = tk.Frame(self.app.root, bg=BG)
        self.ov_title = tk.Label(o, font=("Segoe UI", 26, "bold"), fg=AMBER, bg=BG)
        self.ov_title.pack(pady=(40, 6))
        self.ov_how = tk.Label(o, font=("Segoe UI", 18), fg=INK, bg=BG, wraplength=1000)
        self.ov_how.pack(pady=(0, 10))
        self.ov_big = tk.Label(o, font=("Segoe UI", 120, "bold"), fg=INK, bg=PANEL, width=8)
        self.ov_big.pack(pady=10)
        self.ov_sub = tk.Label(o, font=("Segoe UI", 18), fg=QUIET, bg=BG)
        self.ov_sub.pack()
        self.ov_log = tk.Label(o, font=("Consolas", 14), fg=INK, bg=BG, justify="left", anchor="w")
        self.ov_log.pack(pady=(14, 6))
        row = tk.Frame(o, bg=BG)
        row.pack(pady=10)
        btn = dict(font=("Segoe UI", 14, "bold"), relief="flat", takefocus=0, padx=16, pady=6)
        self.ov_use = tk.Button(row, text="Start BlinkTalk", bg=TEAL, fg=BG, command=self._use_new, **btn)
        self.ov_close = tk.Button(row, text="Cancel", bg=PANEL, fg=INK, command=self._cancel, **btn)
        self.ov_close.pack(side="left", padx=8)
        self.log_lines = []

    def _show_overlay(self, name):
        self.log_lines = []
        self.ov_title.config(text=f"Calibrating {name.upper()}")
        self.ov_how.config(text="Put the electrodes on. Getting the board ready...")
        self.ov_big.config(text="...", bg=PANEL, fg=INK)
        self.ov_sub.config(text="")
        self.ov_log.config(text="")
        self.ov_use.pack_forget()
        self.ov_close.config(text="Cancel")
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.overlay.lift()

    def _log(self, text):
        self.log_lines = (self.log_lines + [text])[-7:]
        self.ov_log.config(text="\n".join(self.log_lines))

    def _cancel(self):
        if self.calibrating:
            self.link.stop()
        self.calibrating = False
        self.overlay.place_forget()
        self.status.config(text="board: calibration closed")
        self.refresh_users()

    def _use_new(self):
        self.overlay.place_forget()
        self.calibrating = False
        if self.cal_user:
            self.user_var.set(self.cal_user)
            self.start(self.cal_user)

    def _flash_now(self, action):
        self.ov_big.config(text="NOW", bg=AMBER, fg=BG)
        beep()
        self.app.root.after(700, lambda: self.ov_big.config(bg=PANEL, fg=INK)
                            if self.ov_big.cget("text") == "NOW" else None)

    # ------------------------------------------------------------ board lines
    def handle(self, parts):
        """Handle CAL / CUE / USERS / LINK / READY / ERROR lines. Returns True if used."""
        kind = parts[1]
        if kind == "USERS":
            self._set_users(parts[2].split(",") if len(parts) > 2 else [], select=self.cal_user)
            return True
        if kind == "LINK":
            self.status.config(text="board: " + " ".join(parts[2:]))
            return True
        if kind in ("READY", "HR", "CMD"):
            self.seen_live = True
            if kind == "READY":
                user = next((p.split("=", 1)[1] for p in parts if p.startswith("user=")), "?")
                self.running_user = self.running_user or user
                self.status.config(text=f"board: running ({self.running_user})")
            return False                    # the speller still handles these
        if kind == "ERROR":
            self.status.config(text="board: " + " ".join(parts[2:])[:80])
            self.running_user = None
            return True
        if kind == "CUE" and len(parts) >= 5:
            action, sec, phase = parts[2], int(parts[3]), parts[4]
            if phase == "now":
                self._flash_now(action)
            elif phase == "wait":
                self.ov_big.config(text=str(5 - sec % 5), bg=PANEL, fg=INK)
                self.ov_sub.config(text=f"get ready... {sec} / 60 s" if sec < 5 else f"{sec} / 60 s")
            elif phase == "fast":
                self.ov_big.config(text=f"FAST {60 - sec}", bg=PANEL, fg=AMBER)
                self.ov_how.config(text=f"Now {TITLES[action].lower()} quickly, again and again, "
                                        "until the time runs out.")
                self.ov_sub.config(text=f"{sec} / 60 s")
            elif phase == "rest":
                self.ov_big.config(text=str(60 - sec), bg=PANEL, fg=INK)
                self.ov_sub.config(text=f"{sec} / 60 s")
            elif phase == "done":
                self.ov_big.config(text="OK", bg=PANEL, fg=GREEN)
                self.ov_sub.config(text="checking the recording...")
            return True
        if kind != "CAL" or len(parts) < 3:
            return False
        what = parts[2]
        if what == "START":
            self._log("Board ready. Follow the prompts.")
        elif what == "STEP":
            action, attempt = parts[3], int(parts[4])
            step = ACTIONS.index(action) + 1
            again = "" if attempt == 1 else f"  (try {attempt})"
            self.ov_title.config(text=f"Calibrating {self.cal_user.upper()} - {TITLES[action]}{again}"
                                      + (f"   step {step} of 4" if attempt == 1 else ""))
            self.ov_how.config(text=HOW[action])
            self.ov_big.config(text="5", bg=PANEL, fg=INK)
            self.ov_sub.config(text="get ready...")
        elif what == "CHECK":
            action, hits, verdict = parts[3], parts[4].split("=")[1], parts[5]
            name = TITLES[action].capitalize()
            if action == "rest":
                self._log("Rest: recorded.")
            elif verdict == "ok":
                self._log(f"{name}: found at {hits} prompts - good.")
            elif verdict == "wrong_direction":
                self._log(f"{name}: the eyes moved the other way - let's do {name.lower()} again.")
            elif verdict == "redo":
                self._log(f"{name}: only found at {hits} prompts - let's record it again.")
            else:
                self._log(f"{name}: {hits} prompts (keeping the best try).")
        elif what == "TRAIN":
            self.ov_title.config(text=f"Training {self.cal_user.upper()}'s model on the board")
            self.ov_how.config(text="The SK-AM62B is training a personal AI model from your recordings "
                                    "and testing it on data it has not seen.")
            self.ov_big.config(text="AI", bg=PANEL, fg=TEAL)
            self.ov_sub.config(text="a few seconds...")
        elif what == "RECALL":
            vals = dict(p.split("=") for p in parts[3:] if "=" in p)
            self.last_recall = {k: float(v) for k, v in vals.items()}
            pct = lambda k: f"{float(vals.get(k, 0)) * 100:.0f}%"
            self._log(f"Test: blink {pct('blink')}  left {pct('left')}  right {pct('right')}  "
                      f"| overall {pct('acc')}  spaced {pct('spaced')}")
        elif what == "REDO":
            action = parts[3]
            rec = parts[4].split("=")[1] if len(parts) > 4 else "?"
            self._log(f"{TITLES[action].capitalize()} was only {float(rec) * 100:.0f}% - "
                      f"recording it once more to improve it.")
        elif what == "DONE":
            vals = dict(p.split("=", 1) for p in parts[3:] if "=" in p)
            self.calibrating = False
            self.ov_title.config(text=f"{self.cal_user.upper()} is calibrated")
            self.ov_how.config(text="Personal model trained and saved on the board.")
            self.ov_big.config(text=f"{float(vals.get('spaced', 0)) * 100:.0f}%", bg=PANEL, fg=GREEN)
            self.ov_sub.config(text=f"accuracy on spaced gestures   (overall incl. fast repeats "
                                    f"{float(vals.get('acc', 0)) * 100:.0f}%)")
            weak = [k for k in ("blink", "left", "right") if self.last_recall.get(k, 1) < 0.5]
            if weak:
                self._log("Still weak: " + ", ".join(weak) + " - try sensitivity 7-8, or check the "
                          "electrodes and calibrate again.")
            self.ov_use.config(text=f"Start BlinkTalk as {self.cal_user}")
            self.ov_use.pack(side="left", padx=8)
            self.ov_close.config(text="Close")
        elif what == "ERROR":
            self.calibrating = False
            self.ov_title.config(text="Calibration stopped")
            self.ov_how.config(text=" ".join(parts[3:]))
            self.ov_big.config(text="!", bg=PANEL, fg=RED)
            self.ov_close.config(text="Close")
        elif what == "LOG":
            pass                               # full report stays in the console window
        return True
