"""
BlinkTalk - offline inference on the SK-AM62B (Track 2 Steps 19-22).

Two modes:
  Raw recording (full pipeline: filter -> detect -> features -> model -> commands)
      python3 edge_infer.py --model artifacts/model.joblib --raw data/neal_blink.csv
  Feature file (the Track 2 guide's format; prints accuracy if a 'label' column exists)
      python3 edge_infer.py --model artifacts/model.joblib --input data/features.csv

Benchmarks printed for the report:
  * per-event latency (features + model for ONE eye movement, as in live use):
    median, 95th percentile and worst case over all events x --repeat runs
  * whole-file processing time
  * peak resident memory of this process
Everything runs locally; no network access is used.
"""
import argparse
import platform
import resource
import time

import joblib
import numpy as np

import features as F


def peak_rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0   # Linux: KB -> MB


def lat_stats(ms):
    ms = np.asarray(ms)
    return f"median {np.median(ms):.2f} ms | p95 {np.percentile(ms, 95):.2f} ms | worst {ms.max():.2f} ms"


def run_raw(bundle, path, repeat):
    model = bundle["model"]
    t0 = time.perf_counter()
    t, x, fs = F.load_afe_csv(path)
    y = F.preprocess(x, fs)
    idx, d = F.find_candidates(y, fs)
    t_prep = (time.perf_counter() - t0) * 1e3

    # per-event latency: features + prediction for one detected eye movement
    lat, preds = [], []
    for r in range(repeat):
        preds = []
        for i in idx:
            s = time.perf_counter()
            fv = np.asarray(F.feature_vector(y, d, i, fs)).reshape(1, -1)
            preds.append(model.predict(fv)[0])
            lat.append((time.perf_counter() - s) * 1e3)
    times = idx / fs
    cmds = F.to_commands(times, preds, np.sign(d[idx]) if len(idx) else [])
    total = (time.perf_counter() - t0) * 1e3

    print(f"\nRecording: {path}  ({len(x)} samples, {len(x) / fs:.1f} s at {fs} Hz)")
    print(f"Detected eye movements: {len(idx)}")
    counts = {c: preds.count(c) for c in ("blink", "left", "right", "none")}
    print(f"Classifier output: {counts}")
    print("BlinkTalk commands (after gesture rule):")
    for tt, c in cmds:
        print(f"  {tt:7.2f} s  {c}")
    if not cmds:
        print("  (none)")
    print("\nPerformance on this device")
    print(f"  per-event latency ({len(lat)} runs): {lat_stats(lat)}")
    print(f"  filtering + detection for whole file: {t_prep:.1f} ms")
    print(f"  total wall time incl. {repeat} pass(es): {total:.1f} ms")


def run_features(bundle, path, repeat):
    import csv
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    feats = bundle["features"]
    missing = set(feats) - set(rows[0].keys())
    if missing:
        raise ValueError(f"Missing features: {sorted(missing)}")
    X = np.array([[float(r[c]) for c in feats] for r in rows])
    model = bundle["model"]
    lat = []
    for _ in range(repeat):
        for k in range(len(X)):
            s = time.perf_counter()
            model.predict(X[k:k + 1])
            lat.append((time.perf_counter() - s) * 1e3)
    pred = model.predict(X)
    for i, p in enumerate(pred[:20]):
        print(f"window={i} predicted_class={p}")
    if len(pred) > 20:
        print(f"... ({len(pred)} windows total)")
    if "label" in rows[0]:
        truth = np.array([r["label"] for r in rows])
        print(f"\nAccuracy vs labels in file: {(pred == truth).mean():.1%} ({len(pred)} windows)")
        print("  note: if these rows were used for training, this is NOT a held-out score;"
              " use the cross-validation result in model_card.json")
    print("\nPerformance on this device")
    print(f"  per-window latency ({len(lat)} runs): {lat_stats(lat)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--raw", help="raw AFE4950 CSV export")
    g.add_argument("--input", help="feature CSV (Track 2 guide format)")
    p.add_argument("--repeat", type=int, default=5, help="passes for latency statistics")
    a = p.parse_args()

    print(f"Device: {platform.machine()} | Python {platform.python_version()} | "
          f"{open('/proc/device-tree/model').read().strip(chr(0)) if __import__('os').path.exists('/proc/device-tree/model') else platform.node()}")
    s = time.perf_counter()
    bundle = joblib.load(a.model)       # only load model files you created yourself
    print(f"Model loaded in {(time.perf_counter() - s) * 1e3:.0f} ms | classes {bundle['classes']}")

    if a.raw:
        run_raw(bundle, a.raw, a.repeat)
    else:
        run_features(bundle, a.input, a.repeat)
    print(f"  peak memory (resident): {peak_rss_mb():.1f} MB")


if __name__ == "__main__":
    main()
