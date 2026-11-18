"""
Python wrapper (ctypes, standard library only) for the C eye-command recognizer.

The recognizer (eog.c + the random forest in eog_model.h) takes raw ECG-channel samples in volts at
500 Hz and returns finished commands: blink (1), left (2), right (3), select (4, two quick blinks),
power (5, three quick blinks) and sos (6, five or more). A group of exactly four blinks is ignored.

    from recognizer import Recognizer
    rec = Recognizer()                      # picks libeog_aarch64.so on the SK-AM62B
    for name, onset_sample in rec.push(samples_in_volts):
        ...
"""
import ctypes
import os
import platform

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = {1: "blink", 2: "left", 3: "right", 4: "select", 5: "power", 6: "sos"}
DEFAULT_THRESHOLD = 0.40            # 1 - P(none) needed for an event (same as EOG_EVENT_THR)


def library_path():
    """libeog.so built on the board wins; otherwise the prebuilt one for this CPU."""
    own = os.path.join(HERE, "libeog.so")
    if os.path.exists(own):
        return own
    return os.path.join(HERE, f"libeog_{platform.machine()}.so")


def available():
    return os.path.exists(library_path())


def threshold_for_sensitivity(level):
    """Sensitivity 1 (strict) .. 10 (catches weaker movements); 5 = the tested default (0.40)."""
    level = max(1.0, min(10.0, float(level)))
    return min(0.9, DEFAULT_THRESHOLD * 2.0 ** ((5.0 - level) / 5.0))


class Recognizer:
    def __init__(self, sensitivity=5, swap_lr=False, lib=None):
        self.lib = ctypes.CDLL(lib or library_path())
        L = self.lib
        L.eog_create.restype = ctypes.c_void_p
        L.eog_destroy.argtypes = [ctypes.c_void_p]
        L.eog_set_threshold.argtypes = [ctypes.c_void_p, ctypes.c_double]
        L.eog_set_swap_lr.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.eog_push_block.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_longlong), ctypes.c_int]
        L.eog_push_block.restype = ctypes.c_int
        self.h = L.eog_create()
        if not self.h:
            raise MemoryError("eog_create failed")
        self.set_sensitivity(sensitivity)
        L.eog_set_swap_lr(self.h, 1 if swap_lr else 0)
        self._codes = (ctypes.c_int * 64)()
        self._onsets = (ctypes.c_longlong * 64)()

    def set_sensitivity(self, level):
        self.lib.eog_set_threshold(self.h, threshold_for_sensitivity(level))

    def push(self, samples):
        """samples: iterable of floats (volts). Returns [(command_name, onset_sample_index)]."""
        vals = [float(v) for v in samples]
        if not vals:
            return []
        buf = (ctypes.c_float * len(vals))(*vals)
        n = self.lib.eog_push_block(self.h, buf, len(vals), self._codes, self._onsets, 64)
        return [(NAMES.get(self._codes[i], "?"), int(self._onsets[i])) for i in range(n)]

    def close(self):
        if self.h:
            self.lib.eog_destroy(self.h)
            self.h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Replay a recording: python3 recognizer.py ../../data/recordings/blink_every_3s.csv
    import csv
    import sys
    rec = Recognizer()
    with open(sys.argv[1], newline="") as f:
        rows = list(csv.reader(f))[1:]
    x = []
    for r in rows:
        try:
            x.append(float(r[1]))
        except (IndexError, ValueError):
            x.append(x[-1] if x else 0.0)
    for name, onset in rec.push(x):
        print(f"{onset / 500:7.2f} s  {name}")
