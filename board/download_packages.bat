@echo off
REM Downloads the offline Python packages for the SK-AM62B (aarch64, Python 3.14)
REM Then copy this "board" folder onto the SD card's boot drive and run install.sh on the board.
cd /d %~dp0
python -m pip download scikit-learn==1.9.1 scipy==1.18.1 joblib==1.6.0 threadpoolctl narwhals cloudpickle pandas python-dateutil six tzdata --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64 --python-version 3.14 --implementation cp --only-binary=:all: --no-deps -d .
pause
