"""
End-to-end evaluation of an eye-command model with the real C recognizer (the code the board runs).

    python evaluate.py                                  # the model on the board (board/eog/eog_model.h)
    python evaluate.py --model output/eog_model.h       # a newly trained model

Recordings never used for training:
  * 3 random cued sequences: a prompter (training/prompter.html) showed 16 random commands each, ~3-4 s
    apart; a command counts if the recognizer outputs it 0.3 s before to 1.3 s after its cue
  * 1 self-paced random sequence: 21 commands in a known order, scored by order
  * ordinary blinking with no command meant: every left/right/select/on-off/SOS it outputs is false
Plus the control recordings (rest, chewing, scrolling), which the model did see as "none" in training.

The sequences were recorded when three quick blinks meant SOS; today three blinks switch BlinkTalk on/off
(SOS is five), so a cued "sos" is scored against the on/off command.
"""
import argparse
import csv
import difflib
import os
import shutil
import subprocess
import sys

import numpy as np

import dataset as D

sys.path.insert(0, os.path.join(D.ROOT, "board", "eog"))
import recognizer  # noqa: E402

EOG_DIR = os.path.join(D.ROOT, "board", "eog")
DEPLOYED = os.path.join(EOG_DIR, "eog_model.h")
TOKEN = {"blink": "b", "select": "s", "power": "sos", "sos": "sos5", "left": "l", "right": "r"}


def build_recognizer(header, tag):
    """Compile board/eog with `header` as eog_model.h into training/build/<tag>/libeog.so."""
    out = os.path.join(D.BUILD, tag)
    os.makedirs(out, exist_ok=True)
    for f in ("eog.c", "eog.h", "eog_config.h", "eog_api.c"):
        shutil.copy(os.path.join(EOG_DIR, f), out)
    if os.path.abspath(header) != os.path.abspath(os.path.join(out, "eog_model.h")):
        shutil.copy(header, os.path.join(out, "eog_model.h"))
    lib = os.path.join(out, "libeog.so")
    subprocess.run(["gcc", "-O2", "-shared", "-fPIC", "-o", lib, os.path.join(out, "eog.c"),
                    os.path.join(out, "eog_api.c"), "-lm"], check=True)
    return lib


def run(lib, x):
    """[(time_s, token)] for every command the recognizer outputs on a recording."""
    rec = recognizer.Recognizer(lib=lib)
    tail = [float(x[-1])] * int(1.6 * D.FS)                 # let the last decision come out
    return [(onset / D.FS, TOKEN[name]) for name, onset in rec.push(list(x) + tail)]


def score_cued(events, cues):
    """Best alignment of the capture clock to the prompter clock, then match cue by cue."""
    times = np.array([t for t, _ in events])

    def match(off):
        used, res = set(), []
        for t, want in cues:
            if t + off < 2.2:                               # recognizer warm-up
                continue
            window = [i for i in range(len(events)) if t + off - 0.3 <= times[i] <= t + off + 1.3 and i not in used]
            hit = [i for i in window if events[i][1] == want]
            pick = hit[0] if hit else (window[0] if window else None)
            if pick is not None:
                used.add(pick)
            res.append((t, want, events[pick][1] if pick is not None else "missed"))
        return res, used

    off = max(np.arange(-6, 3.01, 0.05), key=lambda o: sum(w == g for _, w, g in match(o)[0]))
    res, used = match(off)
    extra = sum(1 for i, (_, tok) in enumerate(events) if i not in used and tok != "b")
    return sum(w == g for _, w, g in res), len(res), extra, res


def score_ordered(events, order):
    toks = [tok for _, tok in events]
    sm = difflib.SequenceMatcher(a=order, b=toks, autojunk=False)
    matched = {j for b in sm.get_matching_blocks() for j in range(b.b, b.b + b.size)}
    extra = sum(1 for j, tok in enumerate(toks) if j not in matched and tok != "b")
    return len(matched), len(order), extra


def read_cues(k):
    with open(os.path.join(D.DATA, "sequences", f"random_sequence_{k}_cues.csv"), newline="") as f:
        return [(float(r["time_s"]), r["action"]) for r in csv.DictReader(f)]


def read_order():
    with open(os.path.join(D.DATA, "sequences", "self_paced_sequence_order.tsv")) as f:
        return [line.split()[0] for line in f if line.strip() and not line.startswith("action")]


def evaluate(lib, verbose=True):
    rows = {r["file"]: r for r in D.manifest()}
    say = print if verbose else (lambda *a, **k: None)
    total = [0, 0, 0]
    for k in (1, 2, 3):
        x = D.load(f"neal_random_sequence_{k}.csv")
        ok, n, extra, res = score_cued(run(lib, x), read_cues(k))
        total = [total[0] + ok, total[1] + n, total[2] + extra]
        wrong = [f"{t:.0f}s {w}->{g}" for t, w, g in res if w != g]
        say(f"  random sequence {k}: {ok}/{n} commands right, {extra} extra" + (f"  ({', '.join(wrong)})" if wrong else ""))
    ok, n, extra = score_ordered(run(lib, D.load("neal_self_paced_sequence.csv")), read_order())
    total = [total[0] + ok, total[1] + n, total[2] + extra]
    say(f"  self-paced sequence: {ok}/{n} commands in order, {extra} extra")
    say(f"  ALL SEQUENCES: {total[0]}/{total[1]} = {total[0] / total[1]:.0%} recognised, {total[2]} extra commands"
        " (single blinks are not counted: the screens ignore them)")

    false, minutes = 0, 0.0
    for name, r in rows.items():
        if r["role"] not in ("control", "test_control"):
            continue
        x = D.load(name)
        ev = [tok for _, tok in run(lib, x) if tok != "b"]  # a single blink is ignored by design
        false += len(ev)
        minutes += len(x) / D.FS / 60
        say(f"  {name:32s} {len(ev)} false commands in {len(x) / D.FS:.0f} s {ev if ev else ''}"
            + ("   (never trained on)" if r["role"] == "test_control" else ""))
    say(f"  FALSE COMMANDS: {false} in {minutes:.1f} min of no-command recordings ({false / minutes:.1f}/min)")
    return dict(seq_ok=total[0], seq_n=total[1], seq_extra=total[2], false=false, false_minutes=minutes)


def main():
    ap = argparse.ArgumentParser(description="Evaluate an eye-command model end to end with the C recognizer")
    ap.add_argument("--model", default=DEPLOYED, help="eog_model.h to test (default: the one on the board)")
    a = ap.parse_args()
    print(f"Model: {os.path.relpath(a.model, D.ROOT)}")
    evaluate(build_recognizer(a.model, "evaluate"))


if __name__ == "__main__":
    main()
