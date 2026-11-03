"""
BlinkTalk - build a labeled feature dataset from AFE4950 recordings.

Each recording follows the team's protocol: one action every 5 s for ~50 s,
then the same action repeated quickly for the last ~10 s. The action is
known from the file, so labels come from the protocol, not from the model:

  * candidate events are found with features.find_candidates()
  * a candidate is the file's action if its slope has the action's onset
    direction (blink/left = signal falls, right = signal rises) and it is at
    least 35% as strong as the typical event in that file
  * a candidate within 0.35 s after a strong opposite edge is the overshoot
    of that edge, so it is "none"
  * a --rest recording (normal reading and looking around) is all "none"
  * every other candidate is "none": the eyes returning to centre, the
    bounce after a blink, small natural blinks, noise

Usage:
  python prepare_dataset.py --subject neal --blink data/neal_blink.csv \
      --left data/neal_left.csv --right data/neal_right.csv --out data/features.csv --new
  python prepare_dataset.py --subject person1 --mirrored \
      --blink data/person1_blink_test1.csv data/person1_blink_test2.csv \
      --left data/person1_left.csv --right data/person1_right.csv --rest data/person1_rest.csv

Several files per action are allowed. --mirrored marks a recording made with the
electrodes mirrored (red above the LEFT eyebrow, black beside the RIGHT eye): its
left/right labels are swapped so they match the standard placement in a pooled model.

Split: events before --split-s seconds (default 30 s, about 5 of each gesture =
the calibration) are "train"; later ones, including the fast section, are "test".
"""
import argparse
import csv
import os

import numpy as np

import features as F

ONSET_SIGN = {"blink": -1, "left": -1, "right": +1}


def label_file(path, action, fs_override=None):
    t, x, fs = F.load_afe_csv(path)
    fs = fs_override or fs
    y = F.preprocess(x, fs)
    idx, X = F.extract(y, fs)
    times = idx / fs
    if action == "rest":   # sitting normally: every movement is "none"
        return times, X, np.array(["none"] * len(times), dtype=object), fs, y
    slope = X[:, 0]
    # Direction of the first strong edge after each 5 s cue tells us which way
    # this person's signal moves for this action (depends on electrode placement).
    first = []
    for g in np.arange(5, 50, 5):
        near = np.where((times > g - 1.6) & (times < g + 1.0))[0]
        if len(near):
            strong = near[np.abs(slope[near]) >= 0.5 * np.abs(slope[near]).max()]
            first.append(np.sign(slope[strong[0]]))
    onset = int(np.sign(np.sum(first))) if first and np.sum(first) != 0 else ONSET_SIGN[action]
    if onset != ONSET_SIGN[action]:
        print(f"  NOTE {action}: onset goes {'up' if onset > 0 else 'down'}, opposite of the reference "
              f"placement. Check electrode placement (red above RIGHT eyebrow, black beside LEFT eye).")
    sign_ok = np.sign(slope) == onset

    # typical event strength: strongest correctly-signed candidate near each 5 s cue
    picks = []
    for g in np.arange(5, 50, 5):
        near = np.where(sign_ok & (times > g - 1.6) & (times < g + 1.0))[0]
        if len(near):
            picks.append(near[np.argmax(np.abs(slope[near]))])
    typical = np.median(np.abs(slope[picks])) if picks else np.abs(slope).max()

    strong = np.abs(slope) >= 0.35 * typical
    labels = np.where(sign_ok & strong, action, "none")
    # The overshoot right after a return/recovery edge falls back quickly; that
    # edge belongs to the same gesture, so it is not a new action.
    for k in range(len(times)):
        if labels[k] == "none":
            continue
        before = (times < times[k]) & (times > times[k] - 0.35) & strong & ~sign_ok
        if before.any():
            labels[k] = "none"
    return times, X, labels, fs, y


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--subject", required=True)
    p.add_argument("--blink", nargs="+")
    p.add_argument("--left", nargs="+")
    p.add_argument("--right", nargs="+")
    p.add_argument("--rest", nargs="+", help="recording(s) of normal behaviour: reading, looking around, natural blinks")
    p.add_argument("--mirrored", action="store_true", help="electrodes were mirrored: swap left/right labels")
    p.add_argument("--out", default="data/features.csv")
    p.add_argument("--split-s", type=float, default=30.0)
    p.add_argument("--plot", help="save a label-check plot (PNG)")
    p.add_argument("--new", action="store_true", help="start a fresh features file instead of appending")
    a = p.parse_args()

    rows, plots = [], []
    swap = {"left": "right", "right": "left"}
    jobs = [(act, path) for act in ("blink", "left", "right", "rest") for path in getattr(a, act) or []]
    for action, path in jobs:
        times, X, labels, fs, y = label_file(path, action)
        if a.mirrored:
            labels = np.array([swap.get(l, l) for l in labels], dtype=object)
        plots.append((f"{action} ({os.path.basename(path)})", y, fs, times, labels))
        for tt, feats, lab in zip(times, X, labels):
            rows.append([a.subject, os.path.basename(path), f"{tt:.3f}", lab,
                         "train" if tt < a.split_s else "test"] + [f"{v:.6g}" for v in feats])
        n = {c: int((labels == c).sum()) for c in ("blink", "left", "right", "none")}
        print(f"{action:6s} {path}: {len(labels)} candidates -> {n}"
              + ("  (mirrored: left/right swapped)" if a.mirrored and action in swap else ""))

    if a.new and os.path.exists(a.out):
        os.remove(a.out)
    exists = os.path.exists(a.out)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "a" if exists else "w", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["subject_id", "source", "time_s", "label", "split"] + F.FEATURES)
        w.writerows(rows)
    print(f"{'appended' if exists else 'wrote'} {len(rows)} rows -> {a.out}")

    if a.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        col = {"rest": "tab:green", "blink": "tab:red", "left": "tab:purple", "right": "tab:orange", "none": "0.6"}
        fig, ax = plt.subplots(len(plots), 1, figsize=(16, 3.2 * len(plots)))
        for axx, (action, y, fs, times, labels) in zip(np.atleast_1d(ax), plots):
            tt = np.arange(len(y)) / fs
            axx.plot(tt, y, lw=0.9, color="tab:blue")
            for t0, lab in zip(times, labels):
                axx.axvline(t0, color=col[lab], lw=1.6 if lab != "none" else 0.8,
                            ls="-" if lab != "none" else ":")
            axx.axvline(a.split_s, color="k", ls="--", lw=1)
            axx.set_title(f"{action}: solid = gesture, dotted = none, dashed = train/test split")
            axx.set_ylabel("mV")
        plt.tight_layout()
        plt.savefig(a.plot, dpi=60)
        print("plot ->", a.plot)


if __name__ == "__main__":
    main()
