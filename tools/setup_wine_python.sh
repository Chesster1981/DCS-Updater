#!/usr/bin/env bash
# One-time bootstrap: Wine win64 prefix + Python 3.13.5 + PyInstaller toolchain.
set -euo pipefail

WINEPREFIX="${WINEPREFIX:-$HOME/.wine-dcs-ru}"
export WINEARCH=win64
export WINEPREFIX
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"

PYVER="${PYVER:-3.13.5}"
WORKDIR="${WORKDIR:-/tmp/wine-build}"
mkdir -p "$WORKDIR"

run_wine() {
  if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a wine "$@"
  else
    wine "$@"
  fi
}

if ! command -v wine >/dev/null 2>&1; then
  echo "Install wine first (wine64 + wine32:i386)." >&2
  exit 1
fi

if [[ ! -f "$WINEPREFIX/drive_c/windows/system32/kernel32.dll" ]]; then
  echo "==> Initializing Wine prefix $WINEPREFIX"
  rm -rf "$WINEPREFIX"
  run_wine wineboot --init
fi

if [[ ! -f "$WINEPREFIX/drive_c/Python313/python.exe" ]]; then
  echo "==> Downloading Python ${PYVER} (Windows amd64)"
  curl -fsSL "https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-amd64.exe" \
    -o "$WORKDIR/python-installer.exe"
  echo "==> Installing Python into Wine (quiet)"
  run_wine "$WORKDIR/python-installer.exe" /quiet \
    InstallAllUsers=0 PrependPath=1 Include_test=0 Include_tcltk=1 Include_pip=1 \
    TargetDir='C:\Python313'
fi

if ! run_wine 'C:\Python313\python.exe' -m pip --version >/dev/null 2>&1; then
  echo "==> Bootstrapping pip"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$WORKDIR/get-pip.py"
  run_wine 'C:\Python313\python.exe' "Z:\\tmp\\wine-build\\get-pip.py"
fi

echo "==> Installing PyInstaller toolchain"
# PySide6 6.8.3: last line that loads under Wine without icuuc.dll (6.9+ needs ICU).
printf 'C:\\Python313\\python.exe -m pip install pyinstaller==6.22.0 Pillow pystray PySide6==6.8.3\r\n' \
  > "$WORKDIR/pip_toolchain.bat"
run_wine_cmd() {
  if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a wine cmd /c "$1"
  else
    wine cmd /c "$1"
  fi
}
run_wine_cmd "Z:\\tmp\\wine-build\\pip_toolchain.bat"

run_wine 'C:\Python313\python.exe' --version
echo "Bootstrap complete. Run tools/build_windows_exes.sh next."
