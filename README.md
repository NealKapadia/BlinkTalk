# BlinkTalk

Eye-movement communication for people who cannot speak, running offline on TI hardware.
Electrodes around the eyes are read by the **TI AFE4950EVM** (its ECG channel picks up the
eye's electrooculogram, EOG). A **scikit-learn** model on the **TI SK-AM62B** classifies each
eye movement, and a speller turns glances and blinks into spoken phrases.

UT Dallas HackEdge 2026 · Track 2 (Intelligent Physiological Analytics) ·
Team BlinkTalk: Neal Kapadia, Adrian Miciano, Alexander Montesino

## Pipeline

```
AFE4950EVM (ECG channel, 500 Hz) -> CSV export
  -> 60 Hz notch + 10 Hz low-pass
  -> candidate eye movements (rising / falling slope peaks)
  -> 14 features per movement (edge shape, levels, extremes, noise)
  -> StandardScaler + RandomForest: blink / left / right / none      (on SK-AM62B)
  -> gesture rule: opposite glance within 1.5 s = return to centre
  -> commands: left, right, double blink = select, triple blink = SOS
```

Each user gets a **personal model trained on the board** from a short calibration
recording (`calibrate.py`), and the board then runs everything live (`blinktalk_live.py`):
it configures the AFE4950EVM over USB, classifies eye movements as they happen, tracks heart
rate from the PPG sensor, and sends commands to the laptop speller over the serial console.

```
[electrodes + PPG sensor] -> AFE4950EVM --USB--> SK-AM62B (edge AI, offline) --serial--> laptop speller (screen + voice)
```

## Repository

| Path | What it is |
|---|---|
| `edge/features.py` | Shared filtering, detection, features and gesture rule (laptop and board) |
| `edge/prepare_dataset.py` | Labels raw recordings from the recording protocol and writes `features.csv` |
| `edge/calibrate.py` | **On-board personal model**: train/test split, accuracy, timing, memory, saves `models/<user>.joblib` |
| `edge/calib_session.py` | Guided calibration driven from the laptop GUI: records each gesture with prompts, checks it, re-records weak gestures, trains |
| `edge/train_model.py` | Shared model across people: leave-one-subject-out + personal results, `model_card.json` |
| `edge/edge_infer.py` | Runs a model on a recording: commands, latency and peak memory |
| `edge/afe_stream.py` | Live AFE4950EVM reader on the board (TI GUI protocol, standard library only) |
| `edge/blinktalk_live.py` | Live pipeline: EVM -> model -> commands + heart rate, prints `BLINKTALK ...` lines |
| `edge/heart.py` | Heart rate from PPG (autocorrelation), signal quality, high/low alert |
| `edge/afe_config/` | TI's default AFE4950 register configuration (ECG 500 Hz, PPG 50 Hz) |
| `edge/tools/mock_evm.py` | Simulated EVM for testing without hardware |
| `edge/data/` | Raw AFE4950 recordings (neal, person1) and extracted features |
| `edge/models/` | Personal models and their calibration reports |
| `edge/artifacts/` | Shared model and model card |
| `speller/` | Laptop speller UI (`speller.py`), board controls + calibration screen (`board_panel.py`), earlier rule-based detector (baseline) |
| `board/` | Offline package installer for the SK-AM62B |

## On-board results (SK-AM62B, live AFE4950EVM, silicone electrodes)

Personal model `nealb`, calibrated on the board (4 x 60 s recordings + a second right-glance recording):

| Measure | Result |
|---|---|
| Spaced gestures (one every 5 s, like real speller use) | **100%** |
| Held-out accuracy (train first 30 s, test rest incl. fast repeats) | 86.3%, macro F1 0.84 |
| Alternate 5-second-block split | 91.9% |
| Recall blink / left / right | 100% / 100% / 60% (right misses all in the fast section) |
| Training time on the board | 1.65 s |
| Per-movement latency | ~90 ms |
| Peak memory | 168 MB |
| Command delay, live | ~1.4 s glance, ~2.4 s double blink |
| Heart rate (PPG, LED code 12) | quality 0.56-0.88, about 60-68 bpm |

TI's default green-LED current (code 31) saturates the PPG input on a fingertip; `afe_stream.py --led-sweep`
picks a working level (12 here).

## Earlier results from GUI recordings (2 people)

**Personal models** (train = first 30 s of each recording, about 6 of each gesture;
test = remaining 30 s, including a fast section never seen in training; classifier + gesture rule):

| Person | Accuracy | Macro F1 | Spaced gestures (1 per 5 s) | Fast repeats |
|---|---|---|---|---|
| neal | 80.8% | 0.70 | 100.0% | 72.3% |
| person1 | 85.6% | 0.80 | 94.4% | 80.6% |

With an alternate-5-second-block split (fast repeats in both train and test): neal 93.3%, person1 82.9%.
Measured on a laptop: training about 0.1 s, about 4 ms per eye movement, about 163 MB peak memory.
Board numbers come from running `calibrate.py` on the SK-AM62B.

**Shared model across people** (leave-one-subject-out): 82% accuracy, macro F1 0.66.
Signal size and direction differ between people (person1's blinks are about half as strong;
their electrodes were mirrored), which is why BlinkTalk calibrates per user.

**Limitations:** two people, one session each, recorded by the team. More people and
repeat sessions are needed for general claims.

## Recording protocol (per person)

AFE4950EVM ECG cable: **red** just above the centre of the **right** eyebrow, **black** beside
the outer corner of the **left** eye, **white** on the forehead. Laptop on battery.
In the Bio-Sensing GUI set X AXIS to 60 s, capture, then GLOBAL SETTINGS -> EXPORT.
Record 60 s each, acting on every 5-second mark:

1. `name_blink.csv`: hard blink every 5 s for 50 s, then quick repeated blinks for 10 s
2. `name_left.csv`: look left and back every 5 s, then quick repeats for 10 s
3. `name_right.csv`: look right and back every 5 s, then quick repeats for 10 s
4. `name_rest.csv`: 60 s reading or looking around normally, natural blinking only

If the electrodes were mirrored, `prepare_dataset.py` prints a NOTE; personal models still
work, and `--mirrored` fixes the labels for the shared model.
Research EVM, not a medical device: get supervisor approval before connecting electrodes to people.

## Test a new person (train/test split on the board)

Copy their 4 CSV files into `edge/data/` on the board, then:
```
cd /root/edge
python3 calibrate.py --user adrian --blink data/adrian_blink.csv --left data/adrian_left.csv \
    --right data/adrian_right.csv --rest data/adrian_rest.csv
python3 edge_infer.py --model models/adrian.joblib --raw data/adrian_blink.csv
```
`calibrate.py` prints held-out accuracy, F1, confusion matrix, training time, latency and
memory, and saves `models/adrian.joblib` plus `models/adrian_calibration.json`.

## Shared model (laptop)

```
cd edge
pip install numpy==2.4.3 scipy==1.18.1 scikit-learn==1.9.1 joblib==1.6.0 pandas matplotlib
python prepare_dataset.py --subject neal --blink data/neal_blink.csv --left data/neal_left.csv --right data/neal_right.csv --out data/features.csv --new --plot labels.png
python prepare_dataset.py --subject person1 --mirrored --blink data/person1_blink_test1.csv data/person1_blink_test2.csv --left data/person1_left.csv --right data/person1_right.csv --rest data/person1_rest.csv --out data/features.csv
python train_model.py --input data/features.csv --output artifacts/model.joblib
```
Keep scikit-learn at 1.9.1 so models load on the board.

## Laptop GUI (recommended)

Close PuTTY, then on the laptop:
```
pip install pyserial
python speller/speller.py --serial COM12
```
The bottom bar controls the board over the serial console (no network): pick a **User** and **Start** / **Stop**,
move **Sensitivity** (1 strict ... 10 catches weaker movements, applied live), or type a **New user** name and
**Calibrate**. Calibration shows big prompts (NOW / countdown / FAST), and the board records blink, left, right and rest,
checks each recording (gesture found at 6 of 9 prompts and moving the right way, otherwise it is recorded again),
trains and tests the personal model, re-records any gesture with held-out recall below 50%, and saves it.
Glance = move the highlight, double blink = select, triple blink = SOS.

## Live demo (command line)

Wiring: SK-AM62B **J11** <- USB-C wall charger (use a USB-C power bank when electrodes are on a person);
board **UART** micro-USB -> laptop; AFE4950EVM **J10** micro-USB -> board **USB-A**.
ECG cable: red above right eyebrow, black beside left eye, white forehead. Optical sensor on **J1**
(white dot to pin 10), strapped to a fingertip.

On the board (PuTTY, COM12):
```
cd /root/edge
python3 afe_stream.py --seconds 20 --out data/live_test       # first check: records ECG + PPG CSVs
python3 heart.py --ppg data/live_test_PPG.csv                   # heart rate from that recording
python3 blinktalk_live.py --model models/neal.joblib --led 12   # live; Ctrl+C to stop
python3 calib_session.py --user alex                            # guided calibration in the console
```
To show the speller: start `blinktalk_live.py` in PuTTY, close PuTTY (the program keeps running),
then on the laptop `pip install pyserial` and `python speller/speller.py --serial COM12`.
Without electrodes: `python3 blinktalk_live.py --model models/neal.joblib --replay data/neal_right.csv`.

Tested end to end with `tools/mock_evm.py` (simulated EVM replaying a real recording):
211 registers configured, ECG/PPG reproduced exactly, 5/5 commands, heart rate 72 bpm,
command delay 1.2-1.4 s after the eye movement (the model needs 0.8 s of signal after it).

## SK-AM62B setup (offline)

TI SDK Linux (Arago), Python 3.14, NumPy 2.4.3. Install packages once: run
`board/download_packages.bat` on a Windows laptop, copy the `board` folder to the SD card's
boot partition, then on the board `sh /run/media/boot-mmcblk1p1/board/install.sh`.
Copy `edge/` the same way (`cp -r /run/media/boot-mmcblk1p1/edge /root/`). For the Track 2
offline check run `ip route` (no route = offline) and `/usr/bin/time -v python3 calibrate.py ...`.
