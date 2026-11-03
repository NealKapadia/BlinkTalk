"""
BlinkTalk - shared signal processing and feature extraction.

The SAME code runs on the laptop (training) and on the SK-AM62B (inference),
so features always match. Only numpy + scipy are needed.

Pipeline (documented for the Track 2 report):
  1. Load AFE4950 ECG-channel export (CSV: time_s, volts) -> millivolts
  2. 60 Hz notch (Q=30) + 2nd-order 10 Hz low-pass Butterworth (zero phase)
  3. Candidate events = rising and falling slope peaks above 6 x robust
     noise level, each at least 0.30 s from the previous one of its kind
  4. One feature vector per candidate from a window -0.4 s .. +0.8 s
  5. Quality gate: candidates whose window runs off the recording are dropped
"""
import csv

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks, iirnotch

FS_DEFAULT = 500.0          # AFE4950 default ECG output data rate (Hz)
PRE_S, POST_S = 0.4, 0.8    # feature window around each candidate (s)
REFRACTORY_S = 0.30         # minimum spacing between candidates (s)
SLOPE_K = 6.0               # candidate threshold, x robust SD of slope

FEATURES = [
    "slope_at_onset",    # mV/s, signed slope at the candidate
    "level_pre",         # mV, signal level 50 ms before the edge (vs recording median)
    "level_post",        # mV, signal level 150 ms after the edge
    "step_short",        # mV, level 100 ms after minus 100 ms before
    "loc_min", "loc_max",  # mV, extremes in -0.2..+0.3 s, relative to level_pre
    "loc_t_min_minus_t_max",  # ms, order of the local extremes
    "loc_width_half",    # ms the local deflection stays above half its size
    "slope_max", "slope_min",  # mV/s, steepest rise / fall in -0.2..+0.3 s
    "y_min", "y_max",    # mV, extremes in the full -0.4..+0.8 s window
    "ptp",               # mV, peak-to-peak in the full window
    "noise_pre",         # mV robust SD before the event (signal quality)
]


def load_afe_csv(path):
    """Read an AFE4950 GUI export: columns x (time, s), y (volts).
    Returns (t_seconds, signal_mV, fs_Hz)."""
    t, v = [], []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                t.append(float(row[0]))
                v.append(float(row[1]))
            except ValueError:
                continue   # header / blank lines
    t = np.asarray(t)
    x = np.asarray(v) * 1e3
    fs = 1.0 / np.median(np.diff(t)) if len(t) > 1 else FS_DEFAULT
    return t, x, round(fs)


def preprocess(x, fs):
    b, a = iirnotch(60.0, 30.0, fs)
    y = filtfilt(b, a, x)
    b, a = butter(2, 10.0 / (fs / 2), "low")
    y = filtfilt(b, a, y)
    return y - np.median(y)


def _robust_sd(v):
    return float(np.median(np.abs(v - np.median(v))) * 1.4826 + 1e-9)


def find_candidates(y, fs):
    """Indices of possible eye events (fast changes in the signal)."""
    d = np.gradient(y) * fs
    thr = SLOPE_K * _robust_sd(d)
    dist = max(1, int(REFRACTORY_S * fs))
    # rising and falling edges are found separately, so a strong edge of one
    # kind never hides a nearby edge of the other kind (fast repeated blinks)
    up, _ = find_peaks(d, height=thr, distance=dist)
    down, _ = find_peaks(-d, height=thr, distance=dist)
    peaks = np.sort(np.concatenate([up, down]))
    pre, post = int(PRE_S * fs), int(POST_S * fs)
    peaks = peaks[(peaks - pre >= 0) & (peaks + post < len(y))]   # quality gate
    return peaks, d


def feature_vector(y, d, i, fs):
    s = lambda sec: int(round(sec * fs))           # seconds -> samples
    pre, post = s(PRE_S), s(POST_S)
    dt = 1.0 / fs
    level_pre = y[i - s(0.05)]
    loc = y[i - s(0.2):i + s(0.3)] - level_pre      # short window around the edge
    dloc = d[i - s(0.2):i + s(0.3)]
    peak = max(abs(loc.min()), abs(loc.max())) + 1e-9
    w = y[i - pre:i + post] - level_pre             # full window
    return [
        d[i],
        level_pre,
        y[i + s(0.15)],
        y[i + s(0.1)] - y[i - s(0.1)],
        loc.min(), loc.max(),
        (int(np.argmin(loc)) - int(np.argmax(loc))) * dt * 1e3,
        (np.abs(loc) > 0.5 * peak).sum() * dt * 1e3,
        dloc.max(), dloc.min(),
        w.min(), w.max(),
        w.max() - w.min(),
        _robust_sd(y[i - pre:i - pre // 2]),
    ]


RETURN_S = 1.5       # opposite glance within this time = eyes returning to centre
MULTI_BLINK_S = 0.8  # blinks closer than this belong to one double/triple blink
SAME_BLINK_S = 0.2   # blink edges closer than this are the same blink (down + up edge)


def apply_context(times, preds):
    """Gesture rule applied after the classifier.
    A glance is 'look away and come back': the coming-back movement is a real
    eye movement in the opposite direction, so an opposite glance within
    RETURN_S of an accepted glance is relabelled 'none'."""
    out = list(preds)
    last_glance, last_t = None, -1e9
    for k, (t, p) in enumerate(zip(times, preds)):
        if p in ("left", "right"):
            if last_glance and p != last_glance and t - last_t <= RETURN_S:
                out[k] = "none"
                last_glance = None          # the return completes the gesture
                continue
            last_glance, last_t = p, t
    return out


def count_blinks(group):
    """Count blinks in a group of (pred, time, sign) events.
    One blink makes two edges (eyes close, eyes open) with opposite slope signs.
    Only edges with the same sign as the group's first blink edge start a new
    blink, so the reopening edge is never counted twice; edges closer than
    SAME_BLINK_S are also merged."""
    n, last_b, first_sign = 0, -1e9, 0
    for p, tp, sg in group:
        if p != "blink":
            continue
        if first_sign == 0:
            first_sign = sg
        if sg and first_sign and sg != first_sign:
            continue
        if tp - last_b >= SAME_BLINK_S:
            n += 1
        last_b = tp
    return n


def to_commands(times, preds, signs=None):
    """Turn per-event predictions into BlinkTalk commands:
    left / right glances, double blink = select, triple blink = sos.
    A single blink is treated as natural and ignored.
    signs = slope sign of each event (lets one blink's two edges count once)."""
    cmds, k = [], 0
    preds = apply_context(times, preds)
    signs = list(signs) if signs is not None else [0] * len(preds)
    while k < len(preds):
        p, t = preds[k], times[k]
        if p == "blink":
            j, last_blink = k, t
            while j + 1 < len(preds) and preds[j + 1] in ("blink", "none") and \
                    times[j + 1] - last_blink <= MULTI_BLINK_S:
                j += 1
                if preds[j] == "blink":
                    last_blink = times[j]
            n = count_blinks(list(zip(preds[k:j + 1], times[k:j + 1], signs[k:j + 1])))
            if n >= 3:
                cmds.append((t, "sos"))
            elif n == 2:
                cmds.append((t, "select"))
            k = j + 1
            continue
        if p in ("left", "right"):
            cmds.append((t, str(p)))
        k += 1
    return cmds


def extract(y, fs):
    """Return (candidate_indices, feature_matrix)."""
    idx, d = find_candidates(y, fs)
    X = np.array([feature_vector(y, d, i, fs) for i in idx]) if len(idx) else \
        np.empty((0, len(FEATURES)))
    return idx, X
