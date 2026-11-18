# Setting up the SK-AM62B and AFE4950EVM

This takes you from a powered-off board to live eye commands on the patient screen.

```
electrodes + PPG ─► AFE4950EVM ──USB──► SK-AM62B ──serial console──► laptop: hub ─► SpacetimeDB ─► screens
                                         blinktalk_board.py                 (starts the board program,
                                         C eye-command model                 forwards commands + heart rate)
```

| Part | Runs on | File |
|---|---|---|
| AFE4950EVM setup and streaming (500 Hz eye channel, 50 Hz PPG) | board | `board/afe4950.py` |
| Eye-command model (C random forest, 72 features) | board | `board/eog/` |
| Heart rate from the PPG sensor | board | `board/heart_rate.py` |
| The board program that ties them together | board | `board/blinktalk_board.py` |
| Board link (logs in, starts the board program, forwards its output) | laptop | `hub/src/board.ts` |

You do not start anything on the board by hand in normal use: `npm start` on the laptop opens the
board's console, logs in, and runs the eye-command model.

## 1. Copy the board software (once, and whenever `board/` changes)

The board runs TI SDK Linux (Arago) offline, with Python 3 and NumPy already installed.

1. On the board type `poweroff`, wait 10 s, unplug power and take out the micro-SD card.
2. Put the card in the laptop. A small drive (often called `boot`) appears: copy the repo's `board` folder
   onto it, and the `data/recordings` folder too if you want to replay recordings on the board. Eject, put
   the card back and power on.
3. On the board (PuTTY, see step 3), copy it into place:
   ```
   ls /run/media/                                   # normally boot-mmcblk1p1 (a USB stick: sda1)
   rm -rf /root/blinktalk && cp -r /run/media/boot-mmcblk1p1/board /root/blinktalk
   cp -r /run/media/boot-mmcblk1p1/recordings /root/blinktalk/     # optional: recordings to replay
   ls /root/blinktalk/eog                           # should list libeog_aarch64.so
   ```
4. **SciPy (once, for the heart-rate filter):** on the laptop run `board\offline_packages\download_packages.bat`
   before step 2 so the wheel is copied with the folder, then on the board:
   `sh /root/blinktalk/offline_packages/install.sh`

`eog/libeog_aarch64.so` is the eye-command model already compiled for the AM62B's Cortex-A53. If the board
has `gcc` you can rebuild it there (`cd /root/blinktalk/eog && gcc -O2 -shared -fPIC -o libeog.so eog.c eog_api.c -lm`);
a `libeog.so` built on the board is used first.

## 2. Wire it up

| Connection | From | To |
|---|---|---|
| Power | USB-C charger (or a USB-C power bank when electrodes are on a person) | board **J11** |
| Console | board **UART** micro-USB | laptop (appears as a COM port, e.g. COM12) |
| EVM | AFE4950EVM **J10** micro-USB | board **USB-A** |
| PPG sensor | optical sensor (white dot to pin 10) | EVM **J1**, strapped to a fingertip |

**Electrodes (the model was trained with this placement):**
- **Red:** beside the outer corner of the **right** eye, at pupil height
- **Black:** beside the outer corner of the **left** eye, at pupil height
- **White:** centre of the forehead (reference)

Clean the skin with an alcohol wipe and use fresh gel electrodes. If red and black are swapped, left and
right swap too: set `BOARD_SWAP_LR=1` in `.env` (or swap them on the face).

Research EVM, not a medical device: get supervisor approval before connecting electrodes to people.

## 3. Check the board (first time only)

Open PuTTY on the board's COM port at **115200** baud and log in as `root`. Then:

```
cd /root/blinktalk
python3 test_board.py                                # replays recordings (needs the recordings folder)
python3 blinktalk_board.py --led 12                  # live: look left/right, blink twice; Ctrl+C to stop
```

Expected output:
```
BLINKTALK READY model=eog_c features=72 sensitivity=5
BLINKTALK CMD left t=12.40 delay_ms=900
BLINKTALK CMD select t=20.85 delay_ms=2000
BLINKTALK HR 74 good q=0.86
```
- Looks arrive about 0.9 s after you move; blink commands about 2 s after the last blink (the board waits
  to be sure no further blink is coming).
- A single blink prints `INFO blink` and is ignored on purpose (people blink all the time).
- 2 quick blinks = `select`, 3 = `toggle` (on/off), 5 or more = `sos`. Four blinks are ignored.
- Typing `sens 7` + Enter makes it more sensitive (1 strict ... 10 sensitive). `quit` stops.
- Heart rate missing or `saturated`: run `python3 afe4950.py --led-sweep` and put the best LED code in
  `BOARD_LED` in `.env`.
- Left comes out as right: the electrodes are mirrored (see step 2).

**Close PuTTY when you are done**: only one program can use the COM port, and the hub needs it.

## 4. Run BlinkTalk

On the laptop, set `BOARD_PORT` in `.env` to the board's COM port (`npm run ports` lists them), then:

```
spacetime start          # terminal 1
npm start                # terminal 2: hub + screens
```

The hub log shows the board coming up:
```
[board] connected to the board on COM12
[board] starting the eye-command model: cd /root/blinktalk && python3 blinktalk_board.py --sensitivity 5 --led 12
[board] model running: model=eog_c features=72 sensitivity=5
[board] left (900 ms after the movement)
```
Open the patient screen (`http://localhost:5173/?view=patient`, full screen with F11) and the caregiver
dashboard (`http://localhost:5173`). Glance to move the highlight, 2 blinks to select, 3 to switch on/off,
5 for SOS. If the board is unplugged or rebooted, the hub reconnects and starts the model again.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[board] cannot open COM12` | PuTTY is still open on the port, or the COM number is wrong (`npm run ports`) |
| `[board] no answer from blinktalk_board.py` | the board software is not in `/root/blinktalk` (step 1), or set `BOARD_DIR` |
| `ERROR no eye-command library` | `eog/libeog_aarch64.so` was not copied; redo step 1 |
| `OSError ... wrong ELF class` | the library is for another CPU; recopy, or rebuild on the board (step 1) |
| Left and right swapped | `BOARD_SWAP_LR=1`, or swap red and black |
| Too many false commands | `BOARD_SENSITIVITY=3`; check the electrodes and keep the face relaxed |
| Misses movements | `BOARD_SENSITIVITY=7`; make looks quicker and fuller; re-stick the electrodes |
| No heart rate | adjust `BOARD_LED` (step 3); keep the finger still |
