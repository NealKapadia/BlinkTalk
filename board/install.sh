#!/bin/sh
# BlinkTalk: install offline Python packages on the SK-AM62B
# Run with:  sh /run/media/boot-mmcblk1p1/board/install.sh
set -e
mkdir -p /root/board_packages
cp "$(dirname "$0")"/* /root/board_packages/
cd /root/board_packages
if [ -f scipy_part_00 ] && ! ls scipy-*.whl >/dev/null 2>&1; then
  cat scipy_part_* > scipy-1.18.1-cp314-cp314-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl
fi
python3 -m pip install --no-index --find-links . --no-deps --break-system-packages *.whl
python3 -c "import sklearn, scipy, pandas, joblib; print('OK: scikit-learn', sklearn.__version__, '| scipy', scipy.__version__, '| pandas', pandas.__version__)"
