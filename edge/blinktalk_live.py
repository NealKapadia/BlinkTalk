"""
BlinkTalk - live edge pipeline on the SK-AM62B.

AFE4950EVM (USB) -> afe_stream -> rolling 12 s buffer -> filtering + detection
-> features -> personal model -> gesture rules -> commands
PPG channel -> heart rate + distress alert

Every result is printed as one line starting with "BLINKTALK", e.g.
  BLINKTALK CMD left t=12.40 delay_ms=1180
  BLINKTALK CMD select t=20.85 delay_ms=1210
  BLINKTALK HR 74 good q=0.86
  BLINKTALK ALERT high_heart_rate
The laptop speller reads these lines from the board's serial console
(python speller.py --serial COM12), so no network is needed.

Usage on the board:
  python3 blinktalk_live.py --model models/neal.joblib                    # live from the EVM
  python3 blinktalk_live.py --model models/neal.joblib --replay data/neal_left.csv   # test without electrodes
"""
import argparse
import os
import select
import sys
import time

import joblib
import numpy as np

import features as F
from heart import HeartMonitor

BUFFER_S = 12.0          # analysis window kept in memory
SENS_DEFAULT = 5         # 1 (strict) .. 10 (sensitive); 5 = plain classifier decision


def none_weight(level):
    """How much the model's 'no gesture' vote counts. Level 5 = 1 (unchanged);
    higher sensitivity shrinks it so weaker glances/blinks still count."""
    return 2.0 ** ((SENS_DEFAULT - float(level)) / 2.5)


def decide(model, fv, level):
    proba = model.predict_proba(fv)[0]
    classes = list(model.classes_)
    if "none" in classes:
        proba = proba.copy()
        proba[classes.index("none")] *= none_weight(level)
    return str(classes[int(np.argmax(proba))])
SETTLE_S = 0.4           # extra margin so filter edges never touch a processed event
STEP_S = 0.2             # how often the buffer is analysed


class GestureStream:
    """Streaming version of features.to_commands (same rules, one event at a time)."""

    def __init__(self):
        self.last_glance, self.last_glance_t = None, -1e9
        self.group, self.group_last_t, self.group_start = [], -1e9, None

    def _flush(self):
        out = []
        n = F.count_blinks(self.group)
        if n >= 3:
            out.append((self.group_start, "sos"))
        elif n == 2:
            out.append((self.group_start, "select"))
        self.group, self.group_start = [], None
        return out

    def push(self, t, pred, sign=0):
        out = []
        if self.group and (t - self.group_last_t > F.MULTI_BLINK_S or pred in ("left", "right")):
            out += self._flush()
        if pred in ("left", "right"):
            if self.last_glance and pred != self.last_glance and t - self.last_glance_t <= F.RETURN_S:
                self.last_glance = None                 # eyes returning to centre
            else:
                self.last_glance, self.last_glance_t = pred, t
                out.append((t, pred))
        elif pred == "blink":
            if not self.group:
                self.group_start = t
            self.group.append((pred, t, sign))
            self.group_last_t = t
        elif self.group and t - self.group_last_t <= F.MULTI_BLINK_S:
            self.group.append((pred, t, sign))          # bounce after a blink (does not extend the wait)
        return out

    def tick(self, now):
        """Close a blink group once no new movement can join it."""
        if self.group and now - self.group_last_t > F.MULTI_BLINK_S:
            return self._flush()
        return []


class ReplaySource:
    """Plays a recorded CSV as if it were arriving live (for testing / demo fallback)."""

    def __init__(self, ecg_csv, ppg_csv=None, speed=1.0):
        _, x, self.fs = F.load_afe_csv(ecg_csv)
        self.ecg = x / 1e3
        self.ppg = F.load_afe_csv(ppg_csv)[1] / 1e3 if ppg_csv else None
        self.speed, self.i, self.t0 = speed, 0, None
        self.rates = {"ECG": self.fs, "TIA1-3": 50.0}

    def start(self):
        self.t0 = time.time()

    def read(self, wait=0.05):
        if self.speed > 0:
            time.sleep(wait)
            target = int((time.time() - self.t0) * self.speed * self.fs)
        else:
            target = self.i + int(STEP_S * self.fs)     # as fast as possible
        target = min(target, len(self.ecg))
        out = {"ECG": self.ecg[self.i:target]}
        if self.ppg is not None:
            j0, j1 = int(self.i * 50 / self.fs), int(target * 50 / self.fs)
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


def main():
    ap = argparse.ArgumentParser(description="BlinkTalk live pipeline")
    ap.add_argument("--model", required=True, help="personal model from calibrate.py")
    ap.add_argument("--replay", help="ECG CSV to replay instead of the live EVM")
    ap.add_argument("--replay-ppg", help="PPG CSV to replay alongside")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed (0 = as fast as possible)")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this long (0 = until Ctrl+C)")
    ap.add_argument("--log", default="live_log.csv", help="CSV log of every printed line")
    ap.add_argument("--ports", nargs="*", help="EVM serial ports (default: all /dev/ttyACM*)")
    ap.add_argument("--config", help="AFE register settings file (default: afe_config/AFE4950_Settings.cfg)")
    ap.add_argument("--led", type=int, help="green LED current code from afe_stream.py --led-sweep")
    ap.add_argument("--sensitivity", type=float, default=SENS_DEFAULT,
                    help="1 (strict, fewer false commands) .. 10 (catches weaker movements); "
                         "can also be changed while running by typing: sens 7")
    a = ap.parse_args()

    if not os.path.exists(a.model):
        say(f"ERROR no model {a.model} - calibrate this user first")
        return 1
    bundle = joblib.load(a.model)
    model = bundle["model"]
    level = max(1.0, min(10.0, a.sensitivity))
    say(f"READY model={os.path.basename(a.model)} user={bundle.get('user', '?')} "
        f"classes={','.join(map(str, bundle['classes']))} sensitivity={level:g}")
    stdin_ok = True                         # the laptop GUI types "sens N" / "quit" here

    if a.replay:
        src = ReplaySource(a.replay, a.replay_ppg, a.speed)
    else:
        from afe_stream import AFE4950EVM
        src = AFE4950EVM(a.ports, log=lambda m: say("INFO " + m))
        say(f"INFO device={src.device()} firmware={src.firmware()}")
        src.configure(a.config) if a.config else src.configure()
        if a.led is not None:
            src.set_led(a.led)
            say(f"INFO led={a.led}")
    fs = src.rates.get("ECG", 500.0)
    heart = HeartMonitor(src.rates.get("TIA1-3", 50.0))
    gestures = GestureStream()

    buf = np.empty(0)            # ECG in mV
    total = 0                    # samples received so far
    processed = []               # absolute times of movements already classified
    last_step = last_hr = 0.0
    latencies = []
    log = open(a.log, "a")
    src.start()
    t_start = time.time()
    try:
        while True:
            new = src.read(0.05)
            ecg = new.get("ECG")
            if ecg is not None and len(ecg):
                buf = np.concatenate([buf, ecg * 1e3])[-int(BUFFER_S * fs):]
                total += len(ecg)
            if new.get("TIA1-3") is not None and len(new["TIA1-3"]):
                heart.add(new["TIA1-3"])
            now = total / fs                                   # stream time (s)

            if now - last_step >= STEP_S and len(buf) >= 4 * fs:
                last_step = now
                t_proc = time.time()
                y = F.preprocess(buf, fs)
                idx, d = F.find_candidates(y, fs)
                buf_t0 = now - len(buf) / fs
                ready = idx[(len(buf) - idx) / fs >= F.POST_S + SETTLE_S]
                for i in ready:
                    t_ev = buf_t0 + i / fs
                    if any(abs(t_ev - tp) < 0.05 for tp in processed[-50:]) or \
                            (processed and t_ev < processed[-1] - 2.0):
                        continue
                    processed.append(t_ev)
                    fv = np.asarray(F.feature_vector(y, d, i, fs)).reshape(1, -1)
                    pred = decide(model, fv, level)
                    for t_cmd, cmd in gestures.push(t_ev, pred, int(np.sign(d[i]))):
                        delay = (now - t_cmd) * 1e3 + (time.time() - t_proc) * 1e3
                        latencies.append(delay)
                        say(f"CMD {cmd} t={t_cmd:.2f} delay_ms={delay:.0f}", log)
                for t_cmd, cmd in gestures.tick(now - F.POST_S - SETTLE_S):
                    delay = (now - t_cmd) * 1e3
                    latencies.append(delay)
                    say(f"CMD {cmd} t={t_cmd:.2f} delay_ms={delay:.0f}", log)

            if now - last_hr >= 2.0:
                last_hr = now
                bpm, q, status, alert = heart.update()
                if bpm is not None or status != "no signal":
                    say(f"HR {bpm:.0f} {status} q={q:.2f}" if bpm else f"HR -- {status}", log)
                if alert:
                    say(f"ALERT {alert}", log)

            if stdin_ok:
                try:
                    r, _, _ = select.select([sys.stdin], [], [], 0)
                except (ValueError, OSError):
                    r, stdin_ok = [], False
                if r:
                    cmd = sys.stdin.readline()
                    if not cmd:
                        stdin_ok = False             # stdin closed (e.g. started in background)
                    else:
                        cmd = cmd.strip().lower().split()
                        if cmd and cmd[0] == "sens" and len(cmd) > 1:
                            try:
                                level = max(1.0, min(10.0, float(cmd[1])))
                                say(f"INFO sensitivity={level:g}")
                            except ValueError:
                                pass
                        elif cmd and cmd[0] in ("quit", "stop"):
                            break

            if a.minutes and time.time() - t_start > a.minutes * 60:
                break
            if a.replay and src.done():
                for t_cmd, cmd in gestures.tick(1e9):
                    say(f"CMD {cmd} t={t_cmd:.2f}", log)
                break
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
        src.close()
        log.close()
        if latencies:
            say(f"SUMMARY commands={len(latencies)} median_delay_ms={np.median(latencies):.0f} "
                f"max_delay_ms={max(latencies):.0f}")


if __name__ == "__main__":
    sys.exit(main())
