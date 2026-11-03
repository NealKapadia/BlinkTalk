"""
Simulated AFE4950EVM for testing afe_stream.py / blinktalk_live.py without hardware.

Creates two pseudo-terminals that answer like the EVM's REG_EDIT and CAPTURE
ports and, after C:CAP_START, streams data words in real time:
ECG = a recorded CSV (replayed in a loop), PPG = a synthetic 72 bpm pulse.

  python3 tools/mock_evm.py --ecg data/neal_blink.csv --ports-file /tmp/mock_ports.txt
  python3 afe_stream.py --ports $(cat /tmp/mock_ports.txt) --seconds 5
"""
import argparse
import os
import select
import struct
import sys
import threading
import time
import tty

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import features as F  # noqa: E402

FIFO = ["ECG"] * 6 + ["TIA1-3"] + ["ECG"] * 4      # 10 ECG + 1 PPG per frame
LSB = 1.2 / 2 ** 21


def to_word(volts, tag=0x05):
    c = int(round(volts / LSB)) & 0xFFFFFF
    return struct.pack("<I", (tag << 24) | c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ecg", required=True, nargs="+",
                    help="recording(s) to replay; with several, each capture start plays the next")
    ap.add_argument("--speed", type=float, default=1.0, help="stream faster than real time")
    ap.add_argument("--ports-file", required=True)
    ap.add_argument("--hr", type=float, default=72.0)
    a = ap.parse_args()

    recs = [F.load_afe_csv(p)[1] / 1e3 for p in a.ecg]
    m_reg, s_reg = os.openpty()
    m_cap, s_cap = os.openpty()
    for s in (s_reg, s_cap):
        tty.setraw(s)
    with open(a.ports_file, "w") as f:
        f.write(f"{os.ttyname(s_reg)} {os.ttyname(s_cap)}")
    state = {"streaming": False, "writes": 0, "pages": [], "page": 0, "regs": {}, "rec": -1, "k": 0}

    def streamer():
        frame_period = 10 / 500.0 / a.speed      # one FIFO frame = 20 ms of data
        t_next = time.time()
        while True:
            if not state["streaming"]:
                time.sleep(0.01)
                t_next = time.time()
                continue
            ecg_v = recs[state["rec"] % len(recs)]
            k = state["k"]
            out = struct.pack("<I", (0x03 << 24) | 11)      # status word, like REG_6D
            for name in FIFO:
                if name == "ECG":
                    out += to_word(ecg_v[k % len(ecg_v)])
                    k += 1
                else:
                    t = k / 500.0
                    ppg = 40e-6 * np.sin(2 * np.pi * a.hr / 60 * t) ** 21 + 5e-6 * np.random.randn()
                    out += to_word(ppg)
            state["k"] = k
            os.write(m_cap, out)
            t_next += frame_period
            time.sleep(max(0, t_next - time.time()))

    threading.Thread(target=streamer, daemon=True).start()
    print("mock EVM ready:", open(a.ports_file).read(), flush=True)
    while True:
        r, _, _ = select.select([m_reg, m_cap], [], [], 0.5)
        for m in r:
            cmd = os.read(m, 4096).decode(errors="replace").strip()
            if not cmd:
                continue
            if cmd.startswith("C:CDC?"):
                os.write(m, b"REG_EDIT\n" if m == m_reg else b"CAPTURE\n")
            elif cmd == "DEVICE?":
                os.write(m, b"AFE4950\n")
            elif cmd == "FIRMWARE_VER?":
                os.write(m, b"V3.0.0\n")
            elif cmd.startswith("C:CAP_START"):
                state["rec"] += 1
                state["k"] = 0
                state["streaming"] = True
                print("mock: capture started", cmd, flush=True)
            elif cmd.startswith("C:CAP_STOP"):
                state["streaming"] = False
                print("mock: capture stopped", flush=True)
            elif cmd.startswith("C:REG_W"):
                state["writes"] += 1
                addr, val = int(cmd[7:9], 16), int(cmd[9:15], 16)
                if addr == 0x01:
                    state["page"] = val & 1
                    state["pages"].append(val & 1)
                else:
                    state["regs"][(state["page"] << 8) | addr] = val
                os.write(m, b"SUCCESS\n")
            elif cmd.startswith("C:REG_R"):
                addr = (state["page"] << 8) | int(cmd[7:9], 16)
                os.write(m, b"SUCCESS\n" + b"%06X" % state["regs"].get(addr, 0))
            elif cmd.startswith("C:XTRA_REG"):
                pass
            elif cmd.startswith("C:"):
                os.write(m, b"SUCCESS\n")
                if cmd.startswith("C:RST_AFE"):
                    state["regs"], state["page"] = {}, 0
                    print(f"mock: reset (register writes so far {state['writes']}, page switches {state['pages']})",
                          flush=True)
            else:
                os.write(m, b"FAILED\n")


if __name__ == "__main__":
    main()
