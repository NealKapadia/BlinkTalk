"""
Train BlinkTalk's eye-command model and export it for the board.

    python train_model.py              # cross-validate, train on everything, write output/eog_model.h,
                                       # then test it end to end next to the model on the board
    python train_model.py --install    # also copy it to board/eog/eog_model.h (and rebuild the x86 library)

Model: random forest, 50 trees, max depth 8 (small enough to be plain C tables of ~120 KB), classes
none / blink / left / right, trained on the 72 features the board computes for every trigger
(dataset.py). Training data: the "train" and "control" rows of data/recordings.csv.

Cross-validation is leave-one-recording-out: every command recording is scored by a model that never
saw it, at the event level, using the board's decision rule (an event fires when 1 - P(none) >= 0.40,
then 0.4 s lockout). The end-to-end test then compiles the exported header into the real C recognizer
and replays recordings that were never used for training (evaluate.py).
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import time

import numpy as np
import sklearn
from sklearn.base import clone
from sklearn.ensemble import RandomForestClassifier

import dataset as D
import evaluate as E
from export_model import check, write_header

N_TREES, MAX_DEPTH = 50, 8
EVENT_THR, LOCK = 0.40, int(0.4 * D.FS)               # same as EOG_EVENT_THR / EOG_LOCK in eog_config.h
MODEL = RandomForestClassifier(N_TREES, max_depth=MAX_DEPTH, class_weight="balanced_subsample",
                               random_state=0, n_jobs=-1)
OUT = os.path.join(D.HERE, "output")


def emit(trig, proba):
    """The board's decision rule on per-trigger probabilities -> [(sample, class)]."""
    out, lock = [], -10 ** 9
    for t, p in zip(trig, proba):
        if t < lock:
            continue
        if 1.0 - p[0] >= EVENT_THR:
            out.append((t, 1 + int(np.argmax(p[1:]))))
            lock = t + LOCK
    return out


def score(rec, events):
    """Each recorded action is right if the nearest unused event within 0.3 s has its class."""
    used, right, wrong = set(), 0, 0
    want = D.CLASS_ID[rec["action"]]
    for o in rec["on"]:
        near = [i for i, (t, _) in enumerate(events) if abs(t - o) <= 0.3 * D.FS and i not in used]
        if near:
            i = min(near, key=lambda i: abs(events[i][0] - o))
            used.add(i)
            right += events[i][1] == want
            wrong += events[i][1] != want
    return right, len(rec["on"]), wrong


def fit(recs):
    X = np.vstack([r["X"][r["lab"] >= 0] for r in recs])
    y = np.concatenate([r["lab"][r["lab"] >= 0] for r in recs])
    return clone(MODEL).fit(X, y), X, y


def main():
    ap = argparse.ArgumentParser(description="Train and export the BlinkTalk eye-command model")
    ap.add_argument("--install", action="store_true", help="copy the new model to board/eog/eog_model.h")
    ap.add_argument("--skip-cv", action="store_true", help="skip leave-one-recording-out cross-validation")
    a = ap.parse_args()

    t0 = time.time()
    rows = [r for r in D.manifest() if r["role"] in ("train", "control")]
    recs = [D.label_recording(r) for r in rows]
    commands = [r for r in recs if r["role"] == "train"]
    counts = {c: int(sum((r["lab"] == i).sum() for r in recs)) for i, c in enumerate(D.CLASSES)}
    print(f"{len(recs)} recordings ({len(commands)} command, {len(recs) - len(commands)} control), "
          f"labeled triggers: {counts}  [{time.time() - t0:.1f} s]")

    report = {"classes": D.CLASSES, "label_counts": counts, "model": f"RandomForest({N_TREES} trees, depth {MAX_DEPTH})",
              "features": 72, "sklearn": sklearn.__version__, "python": platform.python_version()}
    if not a.skip_cv:
        print("\nLeave-one-recording-out (each recording scored by a model that never saw it):")
        per = {c: [0, 0] for c in ("blink", "left", "right")}
        false_total, minutes = 0, 0.0
        for held in recs:
            model, _, _ = fit([r for r in recs if r is not held])
            events = emit(held["trig"], model.predict_proba(held["X"]))
            if held["role"] == "control":
                false_total += len(events)
                minutes += held["minutes"]
                print(f"  {held['name']:36s} {len(events)} false events")
                continue
            right, n, wrong = score(held, events)
            per[held["action"]][0] += right
            per[held["action"]][1] += n
            print(f"  {held['name']:36s} {right:3d}/{n:<3d} right, {wrong} wrong type")
        right = sum(v[0] for v in per.values())
        n = sum(v[1] for v in per.values())
        print(f"  ACCURACY {right}/{n} = {right / n:.1%}  " + "  ".join(f"{c} {v[0]}/{v[1]}" for c, v in per.items())
              + f"  | false events in held-out controls: {false_total} in {minutes:.1f} min")
        report["cross_validation"] = {"events": n, "right": right, "accuracy": round(right / n, 4),
                                      "per_class": per, "control_false_events": false_total,
                                      "control_minutes": round(minutes, 2)}

    model, X, y = fit(recs)
    os.makedirs(OUT, exist_ok=True)
    header = os.path.join(OUT, "eog_model.h")
    nodes, _ = write_header(model, header, MAX_DEPTH)
    diff = check(model, X)
    print(f"\nTrained on {len(y)} triggers -> {os.path.relpath(header, D.ROOT)} ({len(nodes)} nodes, "
          f"{os.path.getsize(header) / 1024:.0f} KB); C tree walk vs scikit-learn: max difference {diff:.1e}")

    print("\nEnd to end through the C recognizer, on recordings never used for training:")
    print(" new model:")
    report["end_to_end_new"] = E.evaluate(E.build_recognizer(header, "new"))
    print(" model on the board now:")
    report["end_to_end_board"] = E.evaluate(E.build_recognizer(E.DEPLOYED, "board"))
    with open(os.path.join(OUT, "model_report.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(f"\nReport: {os.path.relpath(os.path.join(OUT, 'model_report.json'), D.ROOT)}")

    if a.install:
        shutil.copy(header, E.DEPLOYED)
        if platform.machine() == "x86_64":
            subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", os.path.join(E.EOG_DIR, "libeog_x86_64.so"),
                            os.path.join(E.EOG_DIR, "eog.c"), os.path.join(E.EOG_DIR, "eog_api.c"), "-lm"], check=True)
        print("Installed in board/eog/eog_model.h. Rebuild the board library on the SK-AM62B:\n"
              "  cd /root/blinktalk/eog && gcc -O2 -shared -fPIC -o libeog.so eog.c eog_api.c -lm")


if __name__ == "__main__":
    main()
