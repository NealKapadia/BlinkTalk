@echo off
REM Downloads the board's one extra Python package (SciPy, for the heart-rate filter) for the
REM SK-AM62B (aarch64, Python 3.14). NumPy is already part of TI's SDK Linux image.
REM Then copy the "board" folder onto the SD card's boot drive and run offline_packages/install.sh on the board.
cd /d %~dp0
python -m pip download scipy==1.18.1 --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 --python-version 3.14 --implementation cp --only-binary=:all: --no-deps -d .
pause
