"""
BlinkTalk training data: load recordings, extract the board's features, label every trigger.

    data/recordings/*.csv     raw recordings (AFE4950 ECG channel, volts, 500 Hz)
    data/recordings.csv       manifest: what each recording is and how it is used

Features come from the C recognizer itself (eog_features.c includes board/eog/eog.c), so training sees
exactly the 72 numbers the SK-AM62B computes for each trigger.

Labels come from the recording protocol, not from a model. Each command recording contains one action
(blink, left or right) repeated on a schedule, so:
  * the action's onsets are the strongest slope peaks in the action's direction ("go sign": a blink and a
    right look push the signal down, a left look up, with the red electrode beside the right eye);
    peaks at least 40% as tall as the typical action peak, at least 0.45 s apart
  * the trigger nearest each onset (within 80 ms) gets the action's label
  * other triggers within 0.4 s of an onset are left out (the eyes returning to centre, a blink's bounce)
  * every other trigger is "none"; control recordings (rest, chewing, scrolling) are all "none"
"""
import csv
import ctypes
import os
import subprocess

import numpy as np
from scipy.signal import butter, find_peaks, sosfilt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
BUILD = os.path.join(HERE, "build")
FS = 500
CLASSES = ["none", "blink", "left", "right"]          # class index = RF output index in eog_model.h
CLASS_ID = {c: i for i, c in enumerate(CLASSES)}
POST = int(1.0 * FS)                                  # onsets this close to the end are dropped
MATCH = int(0.08 * FS)                                # trigger <-> onset tolerance
IGNORE = int(0.4 * FS)                                # other triggers this close to an onset: unlabeled

_SOS = butter(2, [0.3, 12], "bandpass", fs=FS, output="sos")   # same filter as eog_config.h


# ---------------------------------------------------------------- recordings
def manifest():
    """Rows of data/recordings.csv as dicts."""
    with open(os.path.join(DATA, "recordings.csv"), newline="") as f:
        return [r for r in csv.DictReader(f) if not r["file"].startswith("#")]


def load(name):
    """Samples (volts) of data/recordings/<name>; blank values repeat the previous sample."""
    x = []
    with open(os.path.join(DATA, "recordings", name)) as f:
        next(f)
        for line in f:
            parts = line.split(",")
            try:
                x.append(float(parts[1]))
            except (ValueError, IndexError):
                x.append(x[-1] if x else 0.0)
    return np.asarray(x, dtype=np.float64)


def filtered_slope(x):
    """Bandpass + 40 ms moving-average slope, the same signal chain as the C recognizer."""
    y = sosfilt(_SOS, x)
    d = np.diff(y, prepend=y[0]) * FS
    k = int(0.04 * FS)
    return y, np.convolve(d, np.ones(k) / k)[:len(d)]


# ---------------------------------------------------------------- C feature extractor
def _library():
    src = os.path.join(HERE, "eog_features.c")
    lib = os.path.join(BUILD, "libeog_features.so")
    deps = [src] + [os.path.join(ROOT, "board", "eog", f) for f in ("eog.c", "eog.h", "eog_config.h", "eog_model.h")]
    if not os.path.exists(lib) or os.path.getmtime(lib) < max(os.path.getmtime(p) for p in deps):
        os.makedirs(BUILD, exist_ok=True)
        subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", lib, src, "-lm"], check=True)
    L = ctypes.CDLL(lib)
    L.eog_extract_features.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_longlong), ctypes.POINTER(ctypes.c_float), ctypes.c_int]
    L.eog_extract_features.restype = ctypes.c_int
    return L


_LIB = None


def features(x):
    """All triggers in a recording and the board's features for each: (trigger_sample_idx[n], X[n, 72])."""
    global _LIB
    _LIB = _LIB or _library()
    nf = _LIB.eog_feature_count()
    cap = max(64, len(x) // 50)
    xs = np.ascontiguousarray(x, dtype=np.float32)
    trig = np.zeros(cap, dtype=np.int64)
    feat = np.zeros((cap, nf), dtype=np.float32)
    n = _LIB.eog_extract_features(xs.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(xs),
                                  trig.ctypes.data_as(ctypes.POINTER(ctypes.c_longlong)),
                                  feat.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), cap)
    return trig[:n], feat[:n]


# ---------------------------------------------------------------- labels
def onsets(s, go_sign, end_s=None):
    """Sample indices where the recorded action starts (see module docstring)."""
    z = go_sign * s
    cand, _ = find_peaks(z, distance=int(0.45 * FS))
    if not len(cand):
        return np.array([], dtype=int)
    expected = max(8, int(len(s) / FS / 3.5))           # at least one action every ~3.5 s
    ref = np.median(np.sort(z[cand])[::-1][:expected])
    pk, _ = find_peaks(z, height=0.4 * ref, distance=int(0.75 * FS))
    last = len(s) - POST if end_s is None else min(len(s) - POST, int(end_s * FS))
    return np.array([p for p in pk if 2 * FS <= p < last], dtype=int)


def label_recording(row):
    """Everything training and evaluation need for one manifest row."""
    x = load(row["file"])
    y, s = filtered_slope(x)
    trig, X = features(x)
    action, go = row["action"], int(row["go_sign"] or 0)
    end_s = float(row["label_until_s"]) if row.get("label_until_s") else None
    on = onsets(s, go, end_s) if action in ("blink", "left", "right") else np.array([], dtype=int)
    lab = np.zeros(len(trig), dtype=int)
    ignore = np.zeros(len(trig), dtype=bool)
    for o in on:
        d = np.abs(trig - o)
        i = int(d.argmin()) if len(d) else -1
        if i >= 0 and d[i] <= MATCH:
            lab[i] = CLASS_ID[action]
        ignore |= (d < IGNORE) & (np.arange(len(trig)) != i)
    lab[ignore & (lab == 0)] = -1
    if end_s is not None:                               # unlabeled tail (e.g. ambiguous fast section)
        lab[trig >= int(end_s * FS)] = -1
    return dict(name=row["file"], action=action, role=row["role"], x=x, trig=trig, X=X, lab=lab, on=on,
                minutes=len(x) / FS / 60)
