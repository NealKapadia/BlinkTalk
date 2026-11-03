"""
BlinkTalk - personal calibration, trained ON the SK-AM62B.

A new user records the standard protocol (one action every 5 s, fast repeats
in the last 10 s): blink, look left, look right, and optionally rest. This
script then, entirely on the board and offline:

  1. labels every detected eye movement from the protocol
  2. TRAIN/TEST SPLIT: trains on each recording's first --cal-s seconds
     (default 30 s = about 5-6 of each gesture, the "calibration") and tests on
     the rest, which includes the fast section the model never saw. Accuracy is
     also shown separately for spaced gestures and for the fast section, plus a
     second split (alternate 5-second blocks) where both sides include fast repeats
  3. reports accuracy, macro F1, per-gesture recall, confusion matrix,
     training time, per-event latency and peak memory
  4. retrains on everything and saves models/<user>.joblib for edge_infer.py

Because each person gets their own model, it learns their own signal sizes and
directions; a mirrored electrode placement still works for that person.

Usage (on the board or the laptop):
  python3 calibrate.py --user adrian --blink data/adrian_blink.csv \
      --left data/adrian_left.csv --right data/adrian_right.csv --rest data/adrian_rest.csv
"""
import argparse
import json
import os
import platform
import resource
import time

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix, f1_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import features as F
from prepare_dataset import label_file

LABELS = ["blink", "left", "right", "none"]


def new_model():
    return make_pipeline(
        StandardScaler(),
        RandomForestClassifier(n_estimators=80, max_depth=8, class_weight="balanced",
                               random_state=42))


def collect(files):
    T, X, Y, S = [], [], [], []
    for action, paths in files.items():
        for path in paths or []:
            times, Xf, lab, fs, _ = label_file(path, action)
            T += list(times)
            X.append(Xf)
            Y += list(lab)
            S += [os.path.basename(path)] * len(times)
            n = {c: int(sum(1 for l in lab if l == c)) for c in LABELS}
            print(f"  {action:5s} {os.path.basename(path)}: {n}")
    return np.array(T), np.vstack(X), np.array(Y, dtype=object), np.array(S)


def with_gesture_rule(T, S, pred):
    out = pred.copy()
    for src in np.unique(S):
        idx = np.where(S == src)[0]
        idx = idx[np.argsort(T[idx])]
        out[idx] = F.apply_context(T[idx], list(pred[idx]))
    return out


def evaluate(T, X, Y, S, train):
    """Train on `train`, test on the rest. Returns metrics incl. spaced vs fast parts."""
    test = ~train
    model = new_model().fit(X[train], Y[train])
    pred = with_gesture_rule(T[test], S[test], model.predict(X[test]))
    yt, fast = Y[test], T[test] >= 49.5
    part = lambda m: float((pred[m] == yt[m]).mean()) if m.any() else None
    return {"accuracy": float((pred == yt).mean()),
            "macro_f1": float(f1_score(yt, pred, labels=LABELS, average="macro", zero_division=0)),
            "spaced_accuracy": part(~fast), "fast_accuracy": part(fast),
            "n_train": int(train.sum()), "n_test": int(test.sum())}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user", required=True)
    p.add_argument("--blink", nargs="+", required=True)
    p.add_argument("--left", nargs="+", required=True)
    p.add_argument("--right", nargs="+", required=True)
    p.add_argument("--rest", nargs="+")
    p.add_argument("--cal-s", type=float, default=30.0,
                   help="seconds at the start of each recording used for training")
    p.add_argument("--out-dir", default="models")
    a = p.parse_args()

    print(f"BlinkTalk calibration for '{a.user}' on {platform.machine()} "
          f"(Python {platform.python_version()}, scikit-learn {sklearn.__version__})")
    t0 = time.perf_counter()
    T, X, Y, S = collect({"blink": a.blink, "left": a.left, "right": a.right, "rest": a.rest})
    t_feat = time.perf_counter() - t0

    train = T < a.cal_s
    test = ~train
    print(f"\nTrain (first {a.cal_s:.0f} s of each recording): {train.sum()} movements "
          f"{ {c: int((Y[train] == c).sum()) for c in LABELS} }")
    print(f"Test  (the rest, incl. fast section):        {test.sum()} movements "
          f"{ {c: int((Y[test] == c).sum()) for c in LABELS} }")
    missing = [c for c in ("blink", "left", "right") if (Y[train] == c).sum() == 0]
    if missing:
        raise SystemExit(f"No {missing} found in the calibration part; check the recordings.")

    s = time.perf_counter()
    model = new_model().fit(X[train], Y[train])
    t_train = time.perf_counter() - s

    lat = []
    pred = np.empty(test.sum(), dtype=object)
    Xt = X[test]
    for k in range(len(Xt)):
        s = time.perf_counter()
        pred[k] = model.predict(Xt[k:k + 1])[0]
        lat.append((time.perf_counter() - s) * 1e3)
    pred = with_gesture_rule(T[test], S[test], pred)
    yt = Y[test]
    acc = float((pred == yt).mean())
    f1 = float(f1_score(yt, pred, labels=LABELS, average="macro", zero_division=0))
    recall = {c: (float((pred[yt == c] == c).mean()) if (yt == c).any() else None) for c in LABELS}
    false_trig = int(((yt == "none") & (pred != "none")).sum())
    cm = confusion_matrix(yt, pred, labels=LABELS)

    print("\n=== Held-out test (classifier + gesture rule) ===")
    print(f"accuracy {acc:.1%} | macro F1 {f1:.2f}")
    print("recall: " + ", ".join(f"{c} {v:.0%}" for c, v in recall.items() if v is not None))
    print(f"false triggers: {false_trig} of {(yt == 'none').sum()} non-gestures")
    print("confusion (rows = true, cols = predicted):", LABELS)
    for c, row in zip(LABELS, cm):
        print(f"  {c:6s} {row}")
    fast = T[test] >= 49.5
    print(f"spaced gestures (one every 5 s) accuracy {(pred[~fast] == yt[~fast]).mean():.1%} | "
          f"fast section accuracy {(pred[fast] == yt[fast]).mean():.1%}")

    alt = evaluate(T, X, Y, S, (T // 5).astype(int) % 2 == 0)
    print("\n=== Second split: alternate 5-second blocks (fast repeats on both sides) ===")
    print(f"accuracy {alt['accuracy']:.1%} | macro F1 {alt['macro_f1']:.2f} | "
          f"spaced {alt['spaced_accuracy']:.1%} | fast {alt['fast_accuracy']:.1%} "
          f"(train {alt['n_train']}, test {alt['n_test']})")

    lat = np.array(lat)
    print("\n=== On-device performance ===")
    print(f"feature extraction (all recordings): {t_feat * 1e3:.0f} ms")
    print(f"training time: {t_train * 1e3:.0f} ms")
    print(f"per-event latency: median {np.median(lat):.2f} ms | p95 {np.percentile(lat, 95):.2f} ms | "
          f"worst {lat.max():.2f} ms")
    print(f"peak memory: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.1f} MB")

    # Deploy: retrain on all of this person's data
    final = new_model().fit(X, Y)
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"{a.user}.joblib")
    config = {"fs_hz": F.FS_DEFAULT, "window_s": [-F.PRE_S, F.POST_S], "refractory_s": F.REFRACTORY_S,
              "slope_k": F.SLOPE_K, "return_s": F.RETURN_S, "multi_blink_s": F.MULTI_BLINK_S}
    joblib.dump({"model": final, "features": F.FEATURES, "classes": list(final.classes_),
                 "config": config, "user": a.user}, path)
    report = {
        "user": a.user, "split": f"train = first {a.cal_s:.0f} s of each recording, test = rest",
        "train_counts": {c: int((Y[train] == c).sum()) for c in LABELS},
        "test_counts": {c: int((Y[test] == c).sum()) for c in LABELS},
        "accuracy": round(acc, 4), "macro_f1": round(f1, 4),
        "recall": {c: (round(v, 4) if v is not None else None) for c, v in recall.items()},
        "false_triggers": false_trig, "confusion": {"labels": LABELS, "matrix": cm.tolist()},
        "spaced_accuracy": round(float((pred[~fast] == yt[~fast]).mean()), 4),
        "fast_accuracy": round(float((pred[fast] == yt[fast]).mean()), 4) if fast.any() else None,
        "alternate_block_split": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in alt.items()},
        "training_ms": round(t_train * 1e3, 1),
        "latency_ms": {"median": round(float(np.median(lat)), 3),
                       "p95": round(float(np.percentile(lat, 95)), 3), "worst": round(float(lat.max()), 3)},
        "peak_memory_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "device": platform.machine(), "python": platform.python_version(),
        "scikit_learn": sklearn.__version__,
    }
    with open(os.path.join(a.out_dir, f"{a.user}_calibration.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nSaved {path} (trained on all {len(Y)} movements) and {a.user}_calibration.json")
    print(f"Run it: python3 edge_infer.py --model {path} --raw <new recording.csv>")


if __name__ == "__main__":
    main()
