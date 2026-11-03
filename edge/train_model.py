"""
BlinkTalk - train the eye-gesture classifier (TI Track 2 template, extended).

Model: StandardScaler + RandomForestClassifier (as in the Track 2 guide).
Input:  features.csv from prepare_dataset.py
        (columns subject_id, label, split, + numeric features)
Output: artifacts/model.joblib  (model + exact feature order + config)
        artifacts/model_card.json (classes, window, filters, versions, results)

Evaluation (all printed and saved in model_card.json):
  * shared model: leave-one-subject-out when there are 2+ people (every person is
    tested by a model that never saw them); with 1 person, leave-one-5-second-block-out
  * personal models: for each person, train on their 'train' rows (first 30 s of
    each recording = calibration) and test on their 'test' rows (the rest)

Usage:
  python train_model.py --input data/features.csv --output artifacts/model.joblib
"""
import argparse
import json
import pathlib
import platform

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix, f1_score

from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import features as F

p = argparse.ArgumentParser()
p.add_argument("--input", required=True)
p.add_argument("--output", required=True)
a = p.parse_args()

d = pd.read_csv(a.input)
assert {"subject_id", "label"}.issubset(d.columns)
cols = [c for c in F.FEATURES if c in d.columns]
assert cols == F.FEATURES, "feature columns do not match features.py"
X, y = d[cols].to_numpy(dtype=float), d["label"]   # plain arrays: same input type as on the board


def new_model():
    return make_pipeline(
        StandardScaler(),
        RandomForestClassifier(n_estimators=80, max_depth=8, class_weight="balanced",
                               random_state=42))


subjects = d["subject_id"].unique()
labels = ["blink", "left", "right", "none"]
if len(subjects) >= 2:
    # Leave-one-subject-out: every person is tested by a model that never saw them
    groups = d["subject_id"].astype(str).values
    split_desc = "leave-one-subject-out cross-validation"
else:
    # One person: leave out one 5-second block of one recording at a time, so
    # test events are never neighbours of training events in the same block
    groups = (d["source"].astype(str) + "_" + (d["time_s"] // 5).astype(int).astype(str)).values
    split_desc = "single subject: leave-one-5-second-block-out cross-validation"

pred = np.empty(len(d), dtype=object)
for g in np.unique(groups):
    te_mask = groups == g
    pred[te_mask] = new_model().fit(X[~te_mask], y[~te_mask]).predict(X[te_mask])
tr, te = np.arange(len(d)), np.arange(len(d))   # every event is tested exactly once
yt = y.values
report = classification_report(yt, pred, labels=labels, zero_division=0)
cm = confusion_matrix(yt, pred, labels=labels)
macro_f1 = f1_score(yt, pred, labels=labels, average="macro", zero_division=0)
acc = float((pred == yt).mean())

print(f"Evaluation ({split_desc}, {len(np.unique(groups))} folds)")
print(f"events tested: {len(d)} (each one predicted by a model that did not train on it)")
print("1) Classifier alone, per detected eye movement:")
print(report)
print("Confusion matrix (rows = true, cols = predicted):", labels)
print(pd.DataFrame(cm, index=labels, columns=labels))

# 2) With the gesture rule (features.apply_context) that the board also runs:
#    an opposite glance right after a glance is the eyes returning to centre.
pred_ctx = pred.copy()
for src, s in d.groupby("source"):
    order = s.sort_values("time_s").index
    pred_ctx[order] = F.apply_context(d.loc[order, "time_s"].values, list(pred[order]))
report_ctx = classification_report(yt, pred_ctx, labels=labels, zero_division=0)
cm_ctx = confusion_matrix(yt, pred_ctx, labels=labels)
macro_f1_ctx = f1_score(yt, pred_ctx, labels=labels, average="macro", zero_division=0)
acc_ctx = float((pred_ctx == yt).mean())
print("\n2) Classifier + gesture rule (what the board runs):")
print(report_ctx)
print(pd.DataFrame(cm_ctx, index=labels, columns=labels))

# 3) Personal models: one per person, train = calibration part, test = the rest
def ctx_rule(frame, p):
    out = p.copy()
    for src, s in frame.groupby("source"):
        o = s.sort_values("time_s").index
        out[o] = F.apply_context(frame.loc[o, "time_s"].values, list(p[o]))
    return out


personal = {}
print("\n3) Personal models (train = first 30 s of each recording, test = rest):")
for subj in subjects:
    sd = d[d["subject_id"] == subj]
    trm, tem = (sd["split"] == "train").values, (sd["split"] == "test").values
    Xs, ys = sd[cols].to_numpy(dtype=float), sd["label"].values
    if not trm.any() or not tem.any():
        continue
    ps = pd.Series(new_model().fit(Xs[trm], ys[trm]).predict(Xs[tem]), index=sd.index[tem])
    ps = ctx_rule(sd[tem], ps)
    yts = ys[tem]
    fast = (sd["time_s"].values[tem] >= 49.5)
    r = {"accuracy": float((ps.values == yts).mean()),
         "macro_f1": float(f1_score(yts, ps.values, labels=labels, average="macro", zero_division=0)),
         "spaced_accuracy": float((ps.values[~fast] == yts[~fast]).mean()),
         "fast_accuracy": float((ps.values[fast] == yts[fast]).mean()) if fast.any() else None,
         "n_train": int(trm.sum()), "n_test": int(tem.sum())}
    personal[str(subj)] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
    print(f"  {subj}: accuracy {r['accuracy']:.1%} | macro F1 {r['macro_f1']:.2f} | "
          f"spaced {r['spaced_accuracy']:.1%} | fast "
          f"{'n/a' if r['fast_accuracy'] is None else format(r['fast_accuracy'], '.1%')} "
          f"(train {r['n_train']}, test {r['n_test']})")

# Final model: refit on all data for deployment
final = new_model().fit(X, y)
out = pathlib.Path(a.output)
out.parent.mkdir(parents=True, exist_ok=True)
config = {"fs_hz": F.FS_DEFAULT, "window_s": [-F.PRE_S, F.POST_S],
          "refractory_s": F.REFRACTORY_S, "slope_k": F.SLOPE_K,
          "return_s": F.RETURN_S, "multi_blink_s": F.MULTI_BLINK_S,
          "filters": "60 Hz notch (Q=30) + 10 Hz 2nd-order Butterworth low-pass, zero-phase",
          "units": "mV, mV/s, ms, mV*s"}
joblib.dump({"model": final, "features": cols, "classes": list(final.classes_),
             "config": config}, out)

card = {
    "task": "BlinkTalk eye-gesture classification from EOG recorded on the AFE4950 ECG channel",
    "classes": list(final.classes_),
    "features": cols,
    "config": config,
    "model": "StandardScaler + RandomForestClassifier(n_estimators=80, max_depth=8, class_weight=balanced)",
    "data": {"rows": int(len(d)), "subjects": [str(s) for s in subjects],
             "label_counts": {k: int(v) for k, v in y.value_counts().items()}},
    "evaluation": {"split": split_desc, "folds": int(len(np.unique(groups))), "events_tested": int(len(d)),
                   "classifier_only": {"accuracy": round(acc, 4), "macro_f1": round(float(macro_f1), 4),
                                       "confusion_matrix": cm.tolist()},
                   "with_gesture_rule": {"accuracy": round(acc_ctx, 4),
                                         "macro_f1": round(float(macro_f1_ctx), 4),
                                         "confusion_matrix": cm_ctx.tolist()},
                   "confusion_labels": labels,
                   "personal_models_time_split": personal},
    "limitations": ("Single subject, one session; accuracy will be lower on new people "
                    "until more subjects are recorded." if len(subjects) < 2 else
                    f"{len(subjects)} subjects. Signal size and direction differ between people and "
                    "electrode placements, so the shared model transfers poorly; BlinkTalk therefore "
                    "trains a personal model per user on the board (calibrate.py)."),
    "versions": {"python": platform.python_version(), "scikit-learn": sklearn.__version__,
                 "numpy": np.__version__, "joblib": joblib.__version__},
}
(out.parent / "model_card.json").write_text(json.dumps(card, indent=2))
print(f"saved {out} and {out.parent / 'model_card.json'}")
