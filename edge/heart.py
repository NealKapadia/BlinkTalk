"""
BlinkTalk - heart rate from the AFE4950 optical (PPG) channel, with a distress alert.

Method (documented for the report):
  * PPG = TIA1-3 channel (green LED, 50 Hz in TI's default configuration)
  * band-pass 0.7-3.5 Hz (42-210 bpm), 2nd-order Butterworth, zero phase
  * heart rate = 60 / (lag of the strongest autocorrelation peak) over an 8 s window;
    autocorrelation works whichever way the PPG waveform points
  * signal quality = height of that autocorrelation peak (0..1); below 0.5 the
    reading is reported as "poor" and never raises an alert
  * alert = heart rate above HIGH_BPM or below LOW_BPM for ALERT_S seconds in a row
  * when the reading is not good, the status says why, so the sensor can be fixed on the spot:
      saturated  - signal stuck at the input limit: loosen the strap / cover from room light
      no_pulse   - almost flat: move the sensor to the fingertip pad, warm the hand
      moving     - large irregular swings: rest the hand and keep still
Input is in volts (the CSV, saved in mV, is converted).

This is a research prototype, not a medical device.

Offline use:
  python3 heart.py --ppg data/live_test_PPG.csv
"""
import argparse

import numpy as np
from scipy.signal import butter, filtfilt

WINDOW_S = 8.0
HIGH_BPM, LOW_BPM = 120.0, 45.0
ALERT_S = 10.0
MIN_QUALITY = 0.5
RAIL_V = 1.28            # AFE4950 PPG input reads about +/-1.3 V at most
RAIL_FRAC = 0.3          # more than this share of samples at the limit = saturated
FLAT_V = 0.02            # less than 20 mV of movement = no pulse (a good pulse is 100s of mV)
HINT = {"saturated": "loosen strap / cover from light",
        "no_pulse": "move sensor to fingertip pad",
        "moving": "rest hand and keep still"}


def diagnose(ppg, fs, quality):
    """Why a reading is not good: 'saturated', 'no_pulse', 'moving' (or '' if fine)."""
    x = np.asarray(ppg[-int(WINDOW_S * fs):], dtype=float)
    if len(x) == 0:
        return "no_pulse"
    if np.mean(np.abs(x) >= RAIL_V) > RAIL_FRAC:
        return "saturated"
    if quality >= MIN_QUALITY:
        return ""
    if np.ptp(x) < FLAT_V:
        return "no_pulse"
    return "moving"


def heart_rate(ppg, fs):
    """Return (bpm, quality) for the most recent WINDOW_S seconds, or (None, 0)."""
    n = int(WINDOW_S * fs)
    if len(ppg) < n:
        return None, 0.0
    x = np.asarray(ppg[-n:], dtype=float)
    if np.ptp(x) == 0:
        return None, 0.0
    b, a = butter(2, [0.7 / (fs / 2), 3.5 / (fs / 2)], "band")
    x = filtfilt(b, a, x - x.mean())
    x = x / (np.std(x) + 1e-12)
    ac = np.correlate(x, x, "full")[n - 1:] / n
    lo, hi = int(fs * 60 / 210), int(fs * 60 / 42)
    seg = ac[lo:hi + 1]
    # first strong local maximum (avoids picking a multiple of the beat)
    peaks = [k for k in range(1, len(seg) - 1) if seg[k] >= seg[k - 1] and seg[k] >= seg[k + 1]]
    if not peaks:
        return None, 0.0
    best = max(seg[k] for k in peaks)
    k = next(k for k in peaks if seg[k] >= 0.8 * best)
    # parabolic interpolation for sub-sample lag
    y0, y1, y2 = seg[k - 1], seg[k], seg[k + 1]
    shift = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) if (y0 - 2 * y1 + y2) != 0 else 0.0
    lag = (lo + k + shift) / fs
    return 60.0 / lag, float(max(0.0, min(1.0, seg[k])))


class HeartMonitor:
    """Feed PPG samples; call update() regularly to get readings and alerts."""

    def __init__(self, fs=50.0):
        self.fs = fs
        self.buf = np.empty(0)
        self.t = 0.0
        self.abnormal_since = None
        self.alerted = False

    def add(self, samples):
        self.buf = np.concatenate([self.buf, np.asarray(samples, dtype=float)])[-int(30 * self.fs):]
        self.t += len(samples) / self.fs

    def update(self):
        bpm, q = heart_rate(self.buf, self.fs)
        status, alert = "no signal", None
        if len(self.buf) >= int(WINDOW_S * self.fs):
            why = diagnose(self.buf, self.fs, q)
            status = "good" if (bpm is not None and not why) else (why or "poor")
        abnormal = status == "good" and (bpm > HIGH_BPM or bpm < LOW_BPM)
        if abnormal:
            if self.abnormal_since is None:
                self.abnormal_since = self.t
            if self.t - self.abnormal_since >= ALERT_S and not self.alerted:
                alert = "high_heart_rate" if bpm > HIGH_BPM else "low_heart_rate"
                self.alerted = True
        else:
            self.abnormal_since, self.alerted = None, False
        return bpm, q, status, alert


def main():
    import features as F
    ap = argparse.ArgumentParser(description="Heart rate from a PPG CSV (AFE4950 export)")
    ap.add_argument("--ppg", required=True)
    ap.add_argument("--fs", type=float, help="sample rate (default: from the time column)")
    a = ap.parse_args()
    t, x_mv, fs = F.load_afe_csv(a.ppg)
    fs = a.fs or fs
    x_v = x_mv / 1e3
    mon = HeartMonitor(fs)
    step = int(2 * fs)
    print(f"PPG range {x_mv.min():.0f}..{x_mv.max():.0f} mV")
    for i in range(0, len(x_v), step):
        mon.add(x_v[i:i + step])
        bpm, q, status, alert = mon.update()
        txt = f"{bpm:5.1f} bpm" if bpm else "  --- bpm"
        hint = f" -> {HINT[status]}" if status in HINT else ""
        print(f"{mon.t:6.1f} s  {txt}  quality {q:.2f} ({status}){hint}" + (f"  ALERT {alert}" if alert else ""))


if __name__ == "__main__":
    main()
