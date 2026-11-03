"""
BlinkTalk - EOG event detector (prototype, threshold-based).

Turns a recorded eye signal (CSV exported from the AFE4950EVM GUI, or any
CSV with one column of samples) into BlinkTalk events:

    select  = double blink
    sos     = triple blink
    left    = glance left
    right   = glance right

Usage:
    python eog_detect.py recording.csv --fs 250 --col 1 --plot
    python eog_detect.py --demo --plot          (synthetic signal, no hardware)

Tuning tips (use --plot to see what is detected):
    --invert      blinks point downward in your recording
    --swap-lr     left and right come out reversed
    --blink-k     lower = more sensitive blink detection (default 6)
    --glance-k    lower = more sensitive glance detection (default 5)
"""
import argparse
import csv
import sys

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks


# ---------------------------------------------------------------- loading
def load_csv(path, col=0, skip=None):
    """Read one numeric column from a CSV. Header/text rows are skipped
    automatically unless --skip is given."""
    values = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if skip is not None and i < skip:
                continue
            if col >= len(row):
                continue
            try:
                values.append(float(row[col]))
            except ValueError:
                continue  # header or text line
    if not values:
        sys.exit(f"No numeric data found in column {col} of {path}")
    return np.asarray(values, dtype=float)


def synthetic_signal(fs=250, seed=1):
    """Fake EOG: double blink, glance right, triple blink, glance left, single blink."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, 20, 1 / fs)
    x = rng.normal(0, 8, t.size)                      # noise (uV)
    x += 15 * np.sin(2 * np.pi * 0.05 * t)             # slow drift

    def blink(t0, amp=300):
        return amp * np.exp(-((t - t0) / 0.06) ** 2)

    def glance(t0, amp):  # step that decays (ECG front ends high-pass the signal)
        s = np.where(t >= t0, amp * np.exp(-(t - t0) / 1.5), 0.0)
        return s

    for t0 in (2.0, 2.45):            # double blink -> select
        x += blink(t0)
    x += glance(5.0, 180)             # look right
    x += glance(6.2, -180)            # look back to centre
    for t0 in (9.0, 9.4, 9.8):        # triple blink -> sos
        x += blink(t0)
    x += glance(13.0, -180)           # look left
    x += glance(14.2, 180)            # back to centre
    x += blink(17.0)                  # single (natural) blink -> ignored
    return x


# ---------------------------------------------------------------- detection
def _robust_sd(v):
    return np.median(np.abs(v - np.median(v))) * 1.4826 + 1e-12


def detect(x, fs, invert=False, swap_lr=False, blink_k=6.0, glance_k=5.0,
           multi_gap=0.7, return_gap=1.6):
    """Return (events, debug). events = sorted list of (time_s, name)."""
    x = np.asarray(x, dtype=float)
    x = x - np.median(x)
    if invert:
        x = -x

    # 10 Hz low-pass removes muscle/mains noise; eye movements are slower.
    cutoff = min(10.0, 0.45 * fs)
    b, a = butter(2, cutoff / (fs / 2), "low")
    y = filtfilt(b, a, x)

    # --- blinks: tall, narrow peaks
    sd = _robust_sd(y)
    peaks, _ = find_peaks(y, prominence=blink_k * sd,
                          width=(None, 0.4 * fs),
                          distance=max(1, int(0.15 * fs)))
    blink_times = peaks / fs

    events = []
    i = 0
    while i < len(blink_times):
        j = i
        while j + 1 < len(blink_times) and blink_times[j + 1] - blink_times[j] <= multi_gap:
            j += 1
        n = j - i + 1
        if n >= 3:
            events.append((blink_times[i + 2], "sos"))
        elif n == 2:
            events.append((blink_times[i + 1], "select"))
        # n == 1: natural blink, ignored on purpose
        i = j + 1

    # --- glances: fast steps in the signal, away from blinks
    d = np.gradient(y) * fs
    mask = np.ones(d.size, dtype=bool)
    pad = int(0.35 * fs)
    for p in peaks:
        mask[max(0, p - pad):p + pad] = False
    edge = int(0.5 * fs)          # filter edge effects at start/end
    mask[:edge] = False
    mask[-edge:] = False
    d_masked = np.where(mask, d, 0.0)
    dsd = _robust_sd(d[mask]) if mask.any() else _robust_sd(d)

    glances = []
    for sign, name in ((1, "right"), (-1, "left")):
        gp, _ = find_peaks(sign * d_masked, height=glance_k * dsd,
                           distance=max(1, int(0.6 * fs)))
        glances += [(p / fs, name) for p in gp]
    glances.sort()

    # Looking back to centre makes an opposite step: drop it.
    kept = []
    last_step = -1e9
    for t, name in glances:
        if t - last_step < 0.5:
            continue  # filter ringing right after a step, not a new glance
        last_step = t
        if kept and t - kept[-1][0] <= return_gap and name != kept[-1][1] \
                and not kept[-1][2]:
            kept[-1] = (kept[-1][0], kept[-1][1], True)  # mark: return consumed
            continue
        kept.append((t, name, False))
    for t, name, _ in kept:
        if swap_lr:
            name = "left" if name == "right" else "right"
        events.append((t, name))

    events.sort()
    return events, {"y": y, "peaks": peaks, "d": d}


# ---------------------------------------------------------------- plotting
def plot(x, fs, events, dbg):
    import matplotlib.pyplot as plt
    t = np.arange(len(x)) / fs
    fig, ax = plt.subplots(2, 1, sharex=True, figsize=(12, 6))
    ax[0].plot(t, x - np.median(x), color="0.75", lw=0.8, label="raw")
    ax[0].plot(t, dbg["y"], lw=1.2, label="filtered")
    ax[0].plot(dbg["peaks"] / fs, dbg["y"][dbg["peaks"]], "o", label="blink peaks")
    colors = {"select": "tab:green", "sos": "tab:red",
              "left": "tab:purple", "right": "tab:orange"}
    for tt, name in events:
        for a in ax:
            a.axvline(tt, color=colors[name], ls="--", lw=1)
        ax[0].text(tt, ax[0].get_ylim()[1] * 0.9, name, color=colors[name],
                   rotation=90, va="top", ha="right")
    ax[0].set_ylabel("signal")
    ax[0].legend(loc="lower right")
    ax[1].plot(t, dbg["d"], lw=0.8)
    ax[1].set_ylabel("slope")
    ax[1].set_xlabel("time (s)")
    fig.suptitle("BlinkTalk EOG detection")
    plt.tight_layout()
    plt.show()


# ---------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(description="Detect blinks and glances in an EOG recording.")
    p.add_argument("csv", nargs="?", help="recording (CSV)")
    p.add_argument("--fs", type=float, default=250, help="sample rate in Hz (default 250)")
    p.add_argument("--col", type=int, default=0, help="column with the eye signal (0 = first)")
    p.add_argument("--skip", type=int, default=None, help="header rows to skip")
    p.add_argument("--invert", action="store_true", help="blinks point downward")
    p.add_argument("--swap-lr", action="store_true", help="left/right reversed")
    p.add_argument("--blink-k", type=float, default=6.0)
    p.add_argument("--glance-k", type=float, default=5.0)
    p.add_argument("--plot", action="store_true", help="show a plot for tuning")
    p.add_argument("--demo", action="store_true", help="use a synthetic signal")
    return p


def get_signal(args):
    if args.demo:
        return synthetic_signal(int(args.fs)), args.fs
    if not args.csv:
        sys.exit("Give a CSV file or --demo")
    return load_csv(args.csv, args.col, args.skip), args.fs


def main():
    args = build_parser().parse_args()
    x, fs = get_signal(args)
    events, dbg = detect(x, fs, args.invert, args.swap_lr, args.blink_k, args.glance_k)
    print(f"{len(x)} samples, {len(x) / fs:.1f} s, {len(events)} events")
    for t, name in events:
        print(f"{t:8.2f} s  {name}")
    if args.plot:
        plot(x, fs, events, dbg)


if __name__ == "__main__":
    main()
