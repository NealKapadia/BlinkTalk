"""
BlinkTalk - the program that runs on the SK-AM62B.

    AFE4950EVM (USB) -> afe4950.py -> C eye-command recognizer (eog/) -> commands
                                   -> heart rate from the PPG channel  -> heart rate + alert

There is one model: the C random forest in eog/ (eog_model.h), compiled for the board's Cortex-A53.
The laptop hub starts this program automatically over the board's serial console and reads what it
prints. Every result is one line starting with "BLINKTALK":

    BLINKTALK READY model=eog_c features=72 sensitivity=5
    BLINKTALK CMD left t=12.40 delay_ms=900
    BLINKTALK CMD select t=20.85 delay_ms=2010      (2 quick blinks)
    BLINKTALK CMD toggle t=31.02 delay_ms=1980      (3 quick blinks: switch BlinkTalk on/off)
    BLINKTALK CMD sos t=40.11 delay_ms=2050         (5 or more quick blinks)
    BLINKTALK HR 74 good q=0.86
    BLINKTALK ALERT high_heart_rate
    BLINKTALK INFO ...

While it runs, typing "sens 7" + Enter changes the sensitivity (1 strict .. 10 sensitive) and "quit" stops it.

Usage on the board:
    python3 blinktalk_board.py                          # live from the EVM
    python3 blinktalk_board.py --led 12 --sensitivity 6
    python3 blinktalk_board.py --replay ../data/recordings/look_left_every_3s.csv   # no electrodes needed
"""
import argparse
import os
import select
import sys
import time

import numpy as np

from afe4950 import load_csv
from heart_rate import HeartMonitor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eog"))
import recognizer  # noqa: E402

# recognizer command names -> the words the hub and SpacetimeDB use
COMMANDS = {"left": "left", "right": "right", "select": "select", "power": "toggle", "sos": "sos"}
SENS_DEFAULT = 5
PPG_RATE = 50.0


class ReplaySource:
    """Plays a recorded CSV as if it were arriving live (testing without electrodes)."""

    def __init__(self, ecg_csv, ppg_csv=None, speed=1.0):
        _, self.ecg, self.fs = load_csv(ecg_csv)
        self.ppg = load_csv(ppg_csv)[1] if ppg_csv else None
        self.speed, self.i, self.t0 = speed, 0, None
        self.rates = {"ECG": float(self.fs), "TIA1-3": PPG_RATE}

    def start(self):
        self.t0 = time.time()

    def read(self, wait=0.05):
        if self.speed > 0:
            time.sleep(wait)
            target = int((time.time() - self.t0) * self.speed * self.fs)
        else:
            target = self.i + int(0.2 * self.fs)          # as fast as possible
        target = min(target, len(self.ecg))
        out = {"ECG": self.ecg[self.i:target]}
        if self.ppg is not None:
            j0, j1 = int(self.i * PPG_RATE / self.fs), int(target * PPG_RATE / self.fs)
            out["TIA1-3"] = self.ppg[j0:j1]
        self.i = target
        return out

    def done(self):
        return self.i >= len(self.ecg)

    def stop(self):
        pass

    def close(self):
        pass


def say(line, log=None):
    print("BLINKTALK " + line, flush=True)
    if log:
        log.write(f"{time.time():.3f},{line}\n")
        log.flush()


def read_console(level, rec):
    """Handle "sens N" / "quit" typed on the console. Returns (level, quit, stdin_still_open)."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (ValueError, OSError):
        return level, False, False
    if not ready:
        return level, False, True
    line = sys.stdin.readline()
    if not line:
        return level, False, False                         # stdin closed (started in the background)
    words = line.strip().lower().split()
    if words[:1] == ["sens"] and len(words) > 1:
        try:
            level = max(1.0, min(10.0, float(words[1])))
            rec.set_sensitivity(level)
            say(f"INFO sensitivity={level:g}")
        except ValueError:
            pass
    return level, words[:1] in (["quit"], ["stop"]), True


def main():
    ap = argparse.ArgumentParser(description="BlinkTalk on the SK-AM62B: eye commands + heart rate")
    ap.add_argument("--sensitivity", type=float, default=SENS_DEFAULT,
                    help="1 (strict, fewer false commands) .. 10 (catches weaker movements); 5 = tested default")
    ap.add_argument("--swap-lr", action="store_true",
                    help="electrodes mirrored (red beside the LEFT eye): swap left and right")
    ap.add_argument("--led", type=int, help="PPG LED current code (afe4950.py --led-sweep picks one)")
    ap.add_argument("--replay", help="recording (CSV) to play instead of the live EVM")
    ap.add_argument("--replay-ppg", help="PPG recording to play alongside --replay")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed (0 = as fast as possible)")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this long (0 = until Ctrl+C)")
    ap.add_argument("--log", default="blinktalk_log.csv", help="CSV log of every printed line ('' = none)")
    ap.add_argument("--ports", nargs="*", help="EVM serial ports (default: all /dev/ttyACM*)")
    ap.add_argument("--config", help="AFE register settings (default: afe_config/AFE4950_Settings.cfg)")
    a = ap.parse_args()

    if not recognizer.available():
        say(f"ERROR no eye-command library at {recognizer.library_path()}")
        return 1
    level = max(1.0, min(10.0, a.sensitivity))
    rec = recognizer.Recognizer(sensitivity=level, swap_lr=a.swap_lr)
    say(f"READY model=eog_c features=72 sensitivity={level:g}{' swap_lr=1' if a.swap_lr else ''}")

    if a.replay:
        src = ReplaySource(a.replay, a.replay_ppg, a.speed)
    else:
        from afe4950 import AFE4950EVM
        src = AFE4950EVM(a.ports, log=lambda m: say("INFO " + m))
        say(f"INFO device={src.device()} firmware={src.firmware()}")
        src.configure(a.config) if a.config else src.configure()
        if a.led is not None:
            src.set_led(a.led)
            say(f"INFO led={a.led}")
    fs = src.rates.get("ECG", 500.0)
    heart = HeartMonitor(src.rates.get("TIA1-3", PPG_RATE))

    total = 0                      # ECG samples received so far
    last_hr = 0.0
    latencies = []
    stdin_open = True
    log = open(a.log, "a") if a.log else None

    def emit(events, t_now_s, t_proc):
        for name, onset in events:
            cmd, t_cmd = COMMANDS.get(name), onset / fs
            if cmd is None:                                # a single blink: ignored on purpose
                say(f"INFO {name} t={t_cmd:.2f}", log)
                continue
            delay = (t_now_s - t_cmd) * 1e3 + (time.time() - t_proc) * 1e3
            latencies.append(delay)
            say(f"CMD {cmd} t={t_cmd:.2f} delay_ms={delay:.0f}", log)

    src.start()
    t_start = time.time()
    try:
        while True:
            new = src.read(0.05)
            ecg = new.get("ECG")
            if ecg is not None and len(ecg):
                t_proc = time.time()
                total += len(ecg)
                emit(rec.push(ecg), total / fs, t_proc)
            ppg = new.get("TIA1-3")
            if ppg is not None and len(ppg):
                heart.add(ppg)

            now = total / fs
            if now - last_hr >= 2.0:
                last_hr = now
                bpm, q, status, alert = heart.update()
                if bpm is not None or status != "no signal":
                    say(f"HR {bpm:.0f} {status} q={q:.2f}" if bpm else f"HR -- {status}", log)
                if alert:
                    say(f"ALERT {alert}", log)

            if stdin_open:
                level, quit_now, stdin_open = read_console(level, rec)
                if quit_now:
                    break
            if a.minutes and time.time() - t_start > a.minutes * 60:
                break
            if a.replay and src.done():
                hold = float(src.ecg[-1]) if len(src.ecg) else 0.0     # flush the recognizer's look-ahead
                emit(rec.push([hold] * int(1.6 * fs)), (total + int(1.6 * fs)) / fs, time.time())
                break
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
        src.close()
        if log:
            log.close()
        if latencies:
            say(f"SUMMARY commands={len(latencies)} median_delay_ms={np.median(latencies):.0f} "
                f"max_delay_ms={max(latencies):.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
