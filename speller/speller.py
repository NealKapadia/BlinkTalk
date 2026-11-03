"""
BlinkTalk speller - prototype.

Keyboard stands in for the eyes until the live signal is connected:
    Left / Right arrow   glance left / right (move the highlight)
    Space or Enter       double blink (select)
    S                    triple blink (SOS alert)
    A                    toggle auto-scan (blink-only mode)
    Esc                  quit

Live from the SK-AM62B (close PuTTY first; COM12 = the board's console port):
    pip install pyserial
    python speller.py --serial COM12
    The bottom bar controls the board: pick a user and Start / Stop, set the
    sensitivity, or type a new name and Calibrate (guided on-screen prompts; the
    board records, checks each gesture, re-records weak ones, trains and saves
    that person's model). The board prints lines like "BLINKTALK CMD left",
    "BLINKTALK HR 74 good q=0.86"; the speller acts on them.

Replay a recorded eye signal through the old rule-based detector (baseline):
    python speller.py --csv recording.csv --fs 250 --col 1
    python speller.py --demo          (synthetic signal, no hardware)
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import threading
import tkinter as tk

# ---------------------------------------------------------------- content
GROUPS = ["ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXYZ"]

QUICK_PHRASES = [
    "Yes.", "No.", "I'm in pain.", "Please call the nurse.",
    "I'm thirsty.", "Thank you.",
]

# (keyword, phrase): typing the start of the keyword suggests the phrase.
PHRASE_BANK = [
    ("water", "I'd like some water, please."),
    ("water", "Can I have more water?"),
    ("want", "I want to sit up."),
    ("wait", "Wait a moment, please."),
    ("what", "What time is it?"),
    ("pain", "I'm in pain."),
    ("nurse", "Please call the nurse."),
    ("help", "Please help me."),
    ("hot", "I'm too hot."),
    ("cold", "I'm cold."),
    ("bathroom", "I need the bathroom."),
    ("tired", "I'm tired."),
    ("thank", "Thank you."),
    ("love", "I love you."),
    ("hungry", "I'm hungry."),
    ("turn", "Please turn me over."),
    ("light", "Please turn off the light."),
    ("tv", "Please turn on the TV."),
    ("family", "Please call my family."),
    ("doctor", "I'd like to see the doctor."),
    ("medicine", "Is it time for my medicine?"),
    ("music", "Please play some music."),
    ("blanket", "Can I have a blanket?"),
    ("sleep", "I want to sleep."),
]

WORDS = """i you yes no please thank thanks water want wait what when where who why how
help hurt pain nurse doctor family mom dad home hot cold tired hungry thirsty food
drink bathroom bed sit up down turn light tv music phone call more less now later
today tomorrow good bad okay love need like feel sleep medicine blanket pillow
window door open close stop go come here there time morning night""".split()

USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phrase_usage.json")


# ---------------------------------------------------------------- speech
def speak(text):
    """Offline text-to-speech in a background thread."""
    def run():
        system = platform.system()
        try:
            if system == "Windows":
                safe = text.replace("'", "''")
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Add-Type -AssemblyName System.Speech; "
                     f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{safe}')"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
                return
            if system == "Darwin":
                subprocess.run(["say", text], check=False)
                return
            for exe in ("espeak-ng", "espeak"):
                if shutil.which(exe):
                    subprocess.run([exe, text], check=False)
                    return
            import pyttsx3  # optional fallback
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
        except Exception as exc:  # never crash the speller over audio
            print(f"[speech unavailable: {exc}] {text}")
    print(f"SPEAK: {text}")
    threading.Thread(target=run, daemon=True).start()


def alarm():
    def run():
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 300)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- logic
class Speller:
    """All speller behaviour, no GUI (so it can be tested on its own)."""

    def __init__(self, say=speak):
        self.say = say
        self.text = ""
        self.mode = "main"          # main | group | quick
        self.group = None
        self.items = []
        self.idx = 0
        self.last_spoken = ""
        self.usage = self._load_usage()
        self._rebuild()
        self.idx = self._index_of("group", GROUPS[0])

    # --- phrase learning
    def _load_usage(self):
        try:
            with open(USAGE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _count_use(self, phrase):
        self.usage[phrase] = self.usage.get(phrase, 0) + 1
        try:
            with open(USAGE_FILE, "w") as f:
                json.dump(self.usage, f, indent=1)
        except Exception:
            pass

    # --- suggestions
    def current_word(self):
        if not self.text or self.text.endswith(" "):
            return ""
        return self.text.split(" ")[-1]

    def suggestions(self):
        w = self.current_word().lower()
        if len(w) < 2:
            return []
        phrases = []
        for key, phrase in PHRASE_BANK:
            if key.startswith(w) and phrase not in phrases:
                phrases.append(phrase)
        phrases.sort(key=lambda p: -self.usage.get(p, 0))   # most used first
        words = [x for x in WORDS if x.startswith(w) and x != w]
        out = [("phrase", p) for p in phrases[:3]]
        out += [("word", x) for x in words[:4 - len(out)]]
        return out

    # --- item list for the current screen
    def _rebuild(self):
        if self.mode == "group":
            items = [("letter", c) for c in self.group] + [("back", "Back")]
        elif self.mode == "quick":
            items = [("phrase", p) for p in QUICK_PHRASES] + [("back", "Back")]
        else:
            sug = self.suggestions()
            items = list(sug)
            if sug:
                items.append(("keep", "Keep typing..."))
            items.append(("quick", "Quick phrases"))
            items += [("group", g) for g in GROUPS]
            items += [("space", "Space"), ("delete", "Delete"), ("speak", "Speak")]
        self.items = items
        self.idx = max(0, min(self.idx, len(items) - 1))

    def _index_of(self, kind, value=None):
        for i, (k, v) in enumerate(self.items):
            if k == kind and (value is None or v == value):
                return i
        return 0

    # --- events
    def move(self, step):
        self.idx = (self.idx + step) % len(self.items)

    def select(self):
        kind, val = self.items[self.idx]
        back_to_group = None
        if kind == "phrase":
            self._speak(val)
            self.text, self.mode = "", "main"
        elif kind == "word":
            cw = self.current_word()
            self.text = self.text[:len(self.text) - len(cw)] + val + " "
        elif kind == "keep":
            self.idx = self._index_of("group")
            return
        elif kind == "quick":
            self.mode, self.idx = "quick", 0
        elif kind == "group":
            self.mode, self.group, self.idx = "group", val, 0
        elif kind == "letter":
            self.text += val
            self.mode, back_to_group = "main", self.group
        elif kind == "back":
            back_to_group = self.group if self.mode == "group" else None
            self.mode = "main"
        elif kind == "space":
            if self.text and not self.text.endswith(" "):
                self.text += " "
        elif kind == "delete":
            self.text = self.text[:-1]
        elif kind == "speak":
            if self.text.strip():
                self._speak(self.text.strip())
                self.text = ""
        self._rebuild()
        # Where the highlight lands next
        if self.mode == "main" and kind in ("letter", "back", "phrase", "word"):
            if kind == "letter" and self.suggestions():
                self.idx = 0                                   # first suggestion
            elif back_to_group:
                self.idx = self._index_of("group", back_to_group)
            else:
                self.idx = self._index_of("group", GROUPS[0])

    def _speak(self, text):
        self.last_spoken = text
        self._count_use(text)
        self.say(text)


# ---------------------------------------------------------------- GUI
BG, PANEL, INK, QUIET = "#0F1B24", "#182A36", "#F4F1EA", "#8FA3AE"
TEAL, AMBER, RED = "#4FB3BF", "#F2A541", "#C0392B"


class App:
    def __init__(self, root, scan_ms=1200):
        self.root = root
        self.sp = Speller()
        self.scan_ms = scan_ms
        self.auto = False
        self.sos_on = False
        self.panel = None               # board controls (only with --serial)

        root.title("BlinkTalk")
        root.configure(bg=BG)
        root.geometry("1280x720")

        tk.Label(root, text="BlinkTalk", font=("Segoe UI", 28, "bold"),
                 fg=AMBER, bg=BG).pack(anchor="w", padx=40, pady=(24, 0))
        self.mode_lbl = tk.Label(root, font=("Segoe UI", 14), fg=QUIET, bg=BG)
        self.mode_lbl.pack(anchor="w", padx=40)

        self.text_lbl = tk.Label(root, font=("Segoe UI", 44, "bold"), fg=INK, bg=PANEL,
                                 anchor="w", padx=24, pady=16)
        self.text_lbl.pack(fill="x", padx=40, pady=20)

        self.sug_row = tk.Frame(root, bg=BG)
        self.sug_row.pack(fill="x", padx=40, pady=(0, 12))
        self.key_row = tk.Frame(root, bg=BG)
        self.key_row.pack(fill="x", padx=40)
        self.act_row = tk.Frame(root, bg=BG)
        self.act_row.pack(fill="x", padx=40, pady=(12, 0))

        self.spoken_lbl = tk.Label(root, font=("Segoe UI", 18), fg=TEAL, bg=BG)
        self.spoken_lbl.pack(anchor="w", padx=40, pady=(24, 0))
        self.event_lbl = tk.Label(root, font=("Segoe UI", 14), fg=QUIET, bg=BG)
        self.event_lbl.pack(anchor="w", padx=40)
        self.hr_lbl = tk.Label(root, font=("Segoe UI", 18, "bold"), fg=AMBER, bg=BG)
        self.hr_lbl.place(relx=1.0, x=-40, y=32, anchor="ne")
        tk.Label(root, text="Left/Right = glance   Space = double blink   "
                            "S = SOS   A = auto-scan   Esc = quit",
                 font=("Segoe UI", 12), fg=QUIET, bg=BG).pack(side="bottom", pady=12)

        self.sos = tk.Label(root, text="SOS\nCaregiver alert\n\n(double blink to clear)",
                            font=("Segoe UI", 54, "bold"), fg="white", bg=RED)

        for key in ("<Left>", "<Right>", "<space>", "<Return>", "s", "S", "a", "A"):
            root.bind(key, self._on_key)
        root.bind("<Escape>", lambda e: root.destroy())
        self.render()

    # --- events from keyboard or detector
    def _on_key(self, e):
        if isinstance(self.root.focus_get(), tk.Entry):
            return                      # typing a user name, not controlling the speller
        k = e.keysym.lower()
        self.event({"left": "left", "right": "right", "space": "select",
                    "return": "select", "s": "sos", "a": "auto"}[k])

    def event(self, name):
        self.event_lbl.config(text=f"last input: {name}")
        if name == "auto":
            self.auto = not self.auto
            if self.auto:
                self.root.after(self.scan_ms, self._scan)
        elif name == "sos":
            self.sos_on = True
            self.sos.place(relx=0, rely=0, relwidth=1, relheight=1)
            alarm()
            speak("Help, please. I need assistance.")
        elif self.sos_on:
            if name == "select":
                self.sos_on = False
                self.sos.place_forget()
        elif name == "left":
            self.sp.move(-1)
        elif name == "right":
            self.sp.move(1)
        elif name == "select":
            self.sp.select()
        self.render()

    def _scan(self):
        if not self.auto:
            return
        if not self.sos_on:
            self.sp.move(1)
            self.render()
        self.root.after(self.scan_ms, self._scan)

    # --- drawing
    def render(self):
        sp = self.sp
        mode = {"main": "Choose a group, suggestion or action",
                "group": f"Pick a letter from {sp.group}",
                "quick": "Quick phrases"}[sp.mode]
        if self.auto:
            mode += "   (auto-scan on)"
        self.mode_lbl.config(text=mode)
        self.text_lbl.config(text=(sp.text or " ") + "_")
        self.spoken_lbl.config(text=f'Spoken: "{sp.last_spoken}"' if sp.last_spoken else "")

        for row in (self.sug_row, self.key_row, self.act_row):
            for w in row.winfo_children():
                w.destroy()
        for i, (kind, val) in enumerate(sp.items):
            on = i == sp.idx
            if kind in ("word", "keep") or (kind == "phrase" and (sp.mode == "main" or i < 3)):
                row = self.sug_row
            elif kind in ("group", "letter", "phrase"):
                row = self.key_row
            else:                       # quick, space, delete, speak, back
                row = self.act_row
            if kind == "keep":
                fg, bg = (BG, AMBER) if on else (AMBER, BG)
            else:
                fg, bg = (BG, TEAL) if on else (INK, PANEL)
            size = 26 if kind in ("group", "letter") else 18
            tk.Label(row, text=val, font=("Segoe UI", size, "bold"), fg=fg, bg=bg,
                     padx=18, pady=12, relief="flat",
                     highlightthickness=2,
                     highlightbackground=AMBER if kind == "keep" else bg
                     ).pack(side="left", padx=6, pady=6)

    # --- lines from the SK-AM62B
    def board_line(self, line):
        parts = line.split()
        if len(parts) < 2 or parts[0] != "BLINKTALK":
            return
        if self.panel and self.panel.handle(parts):
            return
        if self.panel and self.panel.calibrating:
            return                      # no speller actions while calibrating
        kind = parts[1]
        if kind == "CMD" and len(parts) >= 3 and parts[2] in ("left", "right", "select", "sos"):
            self.event(parts[2])
        elif kind == "HR":
            txt = "heart rate --" if parts[2] == "--" else f"heart rate {parts[2]} bpm"
            status = parts[3] if len(parts) > 3 else ""
            self.hr_lbl.config(text=f"{txt} ({status})", fg=AMBER if status == "good" else QUIET)
        elif kind == "ALERT":
            self.event("sos")
            self.event_lbl.config(text=f"ALERT from board: {' '.join(parts[2:])}")
        elif kind in ("READY", "INFO"):
            self.event_lbl.config(text="board: " + " ".join(parts[1:])[:120])

    # --- replay recorded signal
    def replay(self, events, speed=1.0):
        for t, name in events:
            self.root.after(int(t * 1000 / speed), self.event, name)
        self.event_lbl.config(text=f"replaying {len(events)} detected events...")


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="BlinkTalk speller prototype")
    p.add_argument("--csv", help="replay a recorded eye signal (CSV)")
    p.add_argument("--demo", action="store_true", help="replay a synthetic signal")
    p.add_argument("--fs", type=float, default=250)
    p.add_argument("--col", type=int, default=0)
    p.add_argument("--invert", action="store_true")
    p.add_argument("--swap-lr", action="store_true")
    p.add_argument("--blink-k", type=float, default=6.0)
    p.add_argument("--glance-k", type=float, default=5.0)
    p.add_argument("--speed", type=float, default=1.0, help="replay speed")
    p.add_argument("--scan-ms", type=int, default=1200, help="auto-scan step time")
    p.add_argument("--serial", help="board console port, e.g. COM12 (needs: pip install pyserial)")
    p.add_argument("--led", type=int, default=12, help="PPG LED current code for the board")
    p.add_argument("--new-user", default="alex", help="name suggested for a new calibration")
    args = p.parse_args()

    root = tk.Tk()
    app = App(root, args.scan_ms)

    if args.csv or args.demo:
        import eog_detect
        x = eog_detect.synthetic_signal(int(args.fs)) if args.demo \
            else eog_detect.load_csv(args.csv, args.col)
        events, _ = eog_detect.detect(x, args.fs, args.invert, args.swap_lr,
                                      args.blink_k, args.glance_k)
        print("Detected:", ", ".join(f"{n}@{t:.1f}s" for t, n in events))
        root.after(1500, app.replay, events, args.speed)

    if args.serial:
        from board_panel import BoardLink, BoardPanel
        try:
            link = BoardLink(args.serial)
        except Exception as e:
            raise SystemExit(f"Could not open {args.serial}: {e}\n"
                             "Close PuTTY (only one program can use the port) and check the COM number.")
        app.panel = BoardPanel(app, link, led=args.led, default_new=args.new_user)

        def pump():
            while not link.lines.empty():
                line = link.lines.get()
                if line.startswith("BLINKTALK") and " CUE " not in line:
                    print(line)
                app.board_line(line)
            root.after(30, pump)

        app.event_lbl.config(text=f"connected to the board on {args.serial}")
        root.after(30, pump)

    root.mainloop()


if __name__ == "__main__":
    main()
