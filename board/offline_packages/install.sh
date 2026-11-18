#!/bin/sh
# BlinkTalk: install the board's offline Python package (SciPy) on the SK-AM62B.
# Run on the board:  sh /root/blinktalk/offline_packages/install.sh
set -e
cd "$(dirname "$0")"
if [ -f scipy_part_00 ] && ! ls scipy-*.whl >/dev/null 2>&1; then     # wheel split to fit small drives
  cat scipy_part_* > scipy-1.18.1-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl
fi
python3 -m pip install --no-index --find-links . --no-deps --break-system-packages scipy-*.whl
python3 -c "import numpy, scipy; print('OK: numpy', numpy.__version__, '| scipy', scipy.__version__)"
