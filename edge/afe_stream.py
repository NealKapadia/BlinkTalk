"""
BlinkTalk - live AFE4950EVM reader for the SK-AM62B (Track 2 Step 20).

The AFE4950EVM's micro-USB (J10) plugs into the SK-AM62B's USB-A port and
shows up as two serial ports (/dev/ttyACM0, /dev/ttyACM1). This module speaks
the same text protocol as TI's Bio-Sensing GUI (BASE_PROFILE5 regProg.py /
regCapture.py), using only the Python standard library + numpy:

  port identification   "C:CDC?\\n"           -> "REG_EDIT\\n" or "CAPTURE\\n"
  device / firmware     "DEVICE?", "FIRMWARE_VER?"
  reset AFE             "C:RST_AFE"           -> "SUCCESS\\n"
  select SPI            "C:COMM_SEL:SPI"
  write register        "C:REG_W" + AA + DDDDDD + N  (hex address, 24-bit data, AFE 1)
  read register         "C:REG_R" + AA + N  -> "SUCCESS\n" + DDDDDD
                        register 0x01 selects page 0/1 (PPM registers are page 1)
  start capture         "C:CAP_START" + "00" (continuous) + "%08X" (data-ready count)
  stop capture          "C:CAP_STOP"
  data stream           little-endian 32-bit words; top byte 0x05 = AFE sample,
                        0x03 = status (REG_6D), 0x07 = extra register.
                        Samples follow the FIFO order in AFE4950_FIFO_SEQ.cfg
                        (10 x ECG + 1 x TIA1-3 = ECG 500 Hz, PPG 50 Hz).
                        Value = 24-bit two's complement x 1.2 / 2^21 volts.

Command-line test (records like the GUI's CSV export):
  python3 afe_stream.py --seconds 20 --out data/live_test
  -> data/live_test_ECG.csv and data/live_test_PPG.csv
"""
import argparse
import glob
import os
import select
import termios
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_DIR = os.path.join(HERE, "afe_config")
VOLTS_PER_LSB = 1.2 / 2 ** 21
# Page-1 (PPM) register of the LED phase in TI's default settings: bits 0-7 = LED
# driver 1 current code, bits 8-15 = driver 2 (TI default 0x1F each, register 0x801F1F)
LED_REG = 0x129


class SerialPort:
    """Minimal raw serial port (USB CDC: the baud rate is ignored by the device)."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0                                        # iflag: raw input
        attrs[1] = 0                                        # oflag: raw output
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0                                        # lflag: no echo/canonical
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def write(self, text):
        data = text.encode() if isinstance(text, str) else text
        os.write(self.fd, data)

    def read_available(self, wait=0.0):
        r, _, _ = select.select([self.fd], [], [], wait)
        if not r:
            return b""
        out = b""
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            out += chunk
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                break
        return out

    def read_reply(self, timeout=1.0, until=b"\n"):
        """Read until a newline (or timeout); returns decoded text."""
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            buf += self.read_available(wait=0.05)
            if until in buf:
                break
        return buf.decode(errors="replace")

    def flush_input(self):
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def read_fifo_order(path=os.path.join(CFG_DIR, "AFE4950_FIFO_SEQ.cfg")):
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip()]


def read_data_rates(path=os.path.join(CFG_DIR, "AFE4950_DATARATE.cfg")):
    rates = {}
    with open(path) as f:
        for ln in f:
            if ":" in ln:
                k, v = ln.strip().split(":")
                rates[k] = float(v)
    return rates


def read_register_cfg(path=os.path.join(CFG_DIR, "AFE4950_Settings.cfg")):
    """[(address, value)] exactly as the GUI's loadCFG(): PPM lines go to page 1,
    address 0 lines are skipped."""
    regs = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if "|" not in ln:
                continue
            name, rest = ln.split("|", 1)
            addr_s, val_s = rest.split()
            addr, val = int(addr_s, 16), int(val_s, 16)
            if "PPM" in name:
                addr = (0x100 | addr) & 0x1FF
            if addr != 0:
                regs.append((addr, val))
    return regs


class AFE4950EVM:
    def __init__(self, ports=None, log=print):
        self.log = log
        self.reg = self.cap = None
        self.present_page = 3          # unknown, as in the GUI
        self.reg1 = 0
        self.fifo = read_fifo_order()
        self.rates = read_data_rates()
        self._buf = b""
        self._idx = 0
        candidates = ports or os.environ.get("BLINKTALK_EVM_PORTS", "").split() or \
            sorted(glob.glob("/dev/ttyACM*"))
        for path in candidates:
            try:
                p = SerialPort(path)
            except OSError as e:
                self.log(f"cannot open {path}: {e}")
                continue
            p.write("C:CDC?\n")
            time.sleep(0.2)
            role = p.read_reply(0.8).strip()
            if role == "REG_EDIT" and self.reg is None:
                self.reg = p
            elif role == "CAPTURE" and self.cap is None:
                self.cap = p
            else:
                p.close()
            self.log(f"{path}: {role or 'no answer'}")
        if self.reg is None or self.cap is None:
            raise RuntimeError("AFE4950EVM not found: need a REG_EDIT and a CAPTURE port. "
                               "Is the EVM plugged into the board's USB port?")

    # --- register side -------------------------------------------------
    def _command(self, cmd, expect_success=True, timeout=1.0):
        self.reg.flush_input()
        self.reg.write(cmd.upper())
        reply = self.reg.read_reply(timeout)
        if expect_success and not reply.startswith("SUCCESS"):
            raise RuntimeError(f"command {cmd!r} failed: {reply!r}")
        return reply

    def device(self):
        return self._command("DEVICE?", expect_success=False).strip()

    def firmware(self):
        return self._command("FIRMWARE_VER?", expect_success=False).strip()

    def reset(self):
        self._command("C:RST_AFE")
        self.present_page, self.reg1 = 3, 0
        time.sleep(0.5)            # let the AFE wake up before the first write

    def select_spi(self):
        self._command("C:COMM_SEL:SPI")

    def _write_device(self, address, data):
        if address == 0x01 and self.present_page == 0:
            self.reg1 = data
        self._command("C:REG_W%02X%06X%01X" % (address & 0xFF, data & 0xFFFFFF, 1))

    def write_reg(self, address, data):
        page = (address >> 8) & 1
        if self.present_page != page:
            self.present_page = page
            self._write_device(0x01, (self.reg1 & 0xFFFFFE) | page)
        if (address & 0xFF) != 0x01:
            self._write_device(address & 0xFF, data)
        else:
            self.present_page = data & 1
            self._write_device(0x01, (data & 0xFFFFFE) | self.present_page)

    def read_reg(self, address):
        """Read one register (page-aware, like the GUI's readReg). Returns int or None."""
        page = (address >> 8) & 1
        if self.present_page != page:
            self.present_page = page
            self._write_device(0x01, (self.reg1 & 0xFFFFFE) | page)
        self.reg.flush_input()
        self.reg.write("C:REG_R%02X%01X" % (address & 0xFF, 1))
        end, buf = time.time() + 1.0, b""
        while time.time() < end and len(buf) < 14:
            buf += self.reg.read_available(0.05)
        txt = buf.decode(errors="replace")
        if txt.startswith("SUCCESS") and len(txt) >= 14:
            try:
                return int(txt[8:14], 16)
            except ValueError:
                return None
        return None

    def verify(self, regs):
        """Read back every written register; returns [(addr, wanted, got)] that differ."""
        last = {}
        for addr, val in regs:
            if (addr & 0xFF) not in (0x00, 0x01):
                last[addr] = val                     # the final value written wins
        bad = []
        for addr, val in last.items():
            got = self.read_reg(addr)
            if got != val:
                bad.append((addr, val, got))
        return bad

    def configure(self, cfg=os.path.join(CFG_DIR, "AFE4950_Settings.cfg"), check=True):
        regs = read_register_cfg(cfg)
        self.reset()
        self.select_spi()
        self.reset()
        for addr, val in regs:
            self.write_reg(addr, val)
        self.log(f"configured {len(regs)} registers from {os.path.basename(cfg)}")
        if check:
            bad = self.verify(regs)
            if bad:
                self.log(f"read-back: {len(bad)} registers differ, writing them again")
                for addr, val, _ in bad:
                    self.write_reg(addr, val)
                bad = self.verify(regs)
            if bad:
                self.log("read-back still differs (may be read-only/status bits): " +
                         ", ".join(f"0x{a:03X} want {w:06X} got " + (f"{g:06X}" if g is not None else "none")
                                   for a, w, g in bad[:12]) + (" ..." if len(bad) > 12 else ""))
            else:
                self.log("read-back: all registers match")
        self.page0()                                 # leave page 0 selected, as the GUI does

    def page0(self):
        if self.present_page != 0:
            self.present_page = 0
            self._write_device(0x01, self.reg1 & 0xFFFFFE)

    def set_led(self, code, base=0x800000):
        """Set the green LED current code (0-255; TI default 31). Lower = dimmer."""
        code = max(0, min(255, int(code)))
        self.write_reg(LED_REG, (base & 0xFF0000) | (code << 8) | code)
        self.page0()
        self.led = code

    # --- capture side --------------------------------------------------
    def start(self, num_data_ready=10):
        self.reg.write("C:XTRA_REG00")             # no extra registers
        time.sleep(0.05)
        self.cap.read_available(0.05)              # drop stale bytes
        self._buf, self._idx = b"", 0
        self.cap.write("C:CAP_START%02X%08X" % (0, num_data_ready))

    def stop(self):
        self.cap.write("C:CAP_STOP")
        time.sleep(0.2)
        self.cap.read_available(0.2)

    def read(self, wait=0.05):
        """Return {'ECG': volts array, 'TIA1-3': volts array} of new samples."""
        self._buf += self.cap.read_available(wait)
        n = len(self._buf) // 4
        if n == 0:
            return {}
        words = np.frombuffer(self._buf[:n * 4], dtype="<u4")
        self._buf = self._buf[n * 4:]
        data = words[(words >> 24) == 0x05] & 0x00FFFFFF
        out = {k: [] for k in set(self.fifo)}
        L = len(self.fifo)
        for v in data:
            out[self.fifo[self._idx]].append(int(v))
            self._idx = (self._idx + 1) % L
        result = {}
        for k, vals in out.items():
            if vals:
                a = np.asarray(vals, dtype=np.int64)
                a[a >= 2 ** 23] -= 2 ** 24
                result[k] = a * VOLTS_PER_LSB
        return result

    def close(self):
        for p in (self.reg, self.cap):
            if p:
                p.close()


def save_csv(path, values, fs):
    """Same layout as the Bio-Sensing GUI export: header x0000,y0000 then time,volts,"""
    with open(path, "w") as f:
        f.write("x0000,y0000\n")
        for i, v in enumerate(values):
            f.write(f"{i / fs:.6g},{v:.10g},\n")


CUE_TEXT = {"blink": "BLINK (one hard blink)", "left": "LOOK LEFT and back",
            "right": "LOOK RIGHT and back", "rest": "(just sit / read normally)"}


def record(evm, seconds, cue=None):
    """Record; with cue='blink'|'left'|'right'|'rest' print the calibration prompts:
    one action at every 5 s mark up to 45 s, then quick repeats from 50 to 60 s."""
    got = {k: [] for k in set(evm.fifo)}
    evm.start()
    t0 = time.time()
    next_cue = 5
    if cue:
        print(f"recording {seconds:.0f} s for '{cue}' - get ready, first prompt at 5 s", flush=True)
    while time.time() - t0 < seconds:
        for k, v in evm.read(0.1).items():
            got[k].extend(v.tolist())
        el = time.time() - t0
        if cue and el >= next_cue:
            if cue == "rest":
                print(f"  {next_cue:2d} s", flush=True)
            elif next_cue < 50:
                print(f"  {next_cue:2d} s  >>> {CUE_TEXT[cue]}", flush=True)
            elif next_cue == 50:
                print(f"  50 s  >>> {CUE_TEXT[cue].split(' (')[0]} QUICKLY, again and again until 60 s", flush=True)
            next_cue += 5
    evm.stop()
    if cue:
        print("  done - relax", flush=True)
    return got, time.time() - t0


def led_sweep(evm, codes=(31, 20, 12, 8, 5, 3, 2), seconds=6.0):
    """Record a few seconds at several LED currents; report level, pulse and quality."""
    from heart import heart_rate
    fs = evm.rates.get("TIA1-3", 50.0)
    print("Keep the finger still and covered. Each step takes about 6 s.")
    print(" LED   PPG min..max (mV)   at limit   heart rate   quality")
    best = None
    for code in codes:
        evm.set_led(code)
        time.sleep(0.3)
        got, _ = record(evm, seconds)
        x = np.asarray(got.get("TIA1-3", []))[int(fs):]          # skip the first second
        if len(x) < 2 * fs:
            print(f" {code:3d}   (no data)")
            continue
        at_rail = float(np.mean(np.abs(x) >= 1.28))
        bpm, q = heart_rate(x, fs) if len(x) >= 8 * fs else heart_rate(np.tile(x, 2), fs)
        print(f" {code:3d}   {x.min() * 1e3:8.1f}..{x.max() * 1e3:8.1f}   {at_rail * 100:5.0f}%   "
              + (f"{bpm:6.1f} bpm" if bpm else "   --- bpm") + f"   {q:.2f}")
        score = q - at_rail
        if best is None or score > best[0]:
            best = (score, code, q, at_rail)
    if best:
        _, code, q, at_rail = best
        print(f"\nBest LED code: {code} (quality {q:.2f}, {at_rail * 100:.0f}% at the limit).")
        print(f"Use it with:  python3 afe_stream.py --led {code} ...   and   python3 blinktalk_live.py --led {code} ...")
    evm.close()
    return 0


def main():
    p = argparse.ArgumentParser(description="Record from the AFE4950EVM on the SK-AM62B")
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--out", default="data/live_test")
    p.add_argument("--ports", nargs="*", help="serial ports (default: all /dev/ttyACM*)")
    p.add_argument("--config", default=os.path.join(CFG_DIR, "AFE4950_Settings.cfg"),
                   help="register settings file (e.g. one exported from TI's GUI)")
    p.add_argument("--led", type=int, help="green LED current code 0-255 (TI default 31)")
    p.add_argument("--cue", choices=["blink", "left", "right", "rest"],
                   help="print calibration prompts every 5 s (use --seconds 60)")
    p.add_argument("--led-sweep", action="store_true",
                   help="try several LED currents (6 s each) and recommend one")
    a = p.parse_args()

    evm = AFE4950EVM(a.ports)
    try:
        print("device:", evm.device(), "| firmware:", evm.firmware())
        evm.configure(a.config)
        if a.led_sweep:
            return led_sweep(evm)
        if a.led is not None:
            evm.set_led(a.led)
            print(f"LED current code set to {a.led}")
        got, el = record(evm, a.seconds, a.cue)
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        for k, vals in got.items():
            fs = evm.rates.get(k, 0)
            print(f"{k}: {len(vals)} samples in {el:.1f} s = {len(vals) / el:.1f} Hz "
                  f"(expected {fs:.0f} Hz)" + (f", range {min(vals) * 1e3:.2f}..{max(vals) * 1e3:.2f} mV"
                                              if vals else ""))
            name = "ECG" if k == "ECG" else "PPG"
            if vals:
                save_csv(f"{a.out}_{name}.csv", vals, fs)
                print(f"  saved {a.out}_{name}.csv")
    finally:
        evm.close()


if __name__ == "__main__":
    main()
