#!/usr/bin/env bash
# Build Windows Node + Control Panel EXEs via Wine (Python 3.13 + PyInstaller 6.22.0 --noupx).
# Required for every GitHub release so Node/Control can auto-update.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
WORKDIR="${WORKDIR:-/tmp/wine-build}"
WINEPREFIX="${WINEPREFIX:-$HOME/.wine-dcs-ru}"
export WINEARCH="${WINEARCH:-win64}"
export WINEPREFIX
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree,mshtml=}"
export PYTHONUTF8=1

mkdir -p "$DIST" "$ROOT/build" "$WORKDIR"

run_wine_cmd() {
  # Do NOT redirect wine stdout/stderr to a file — that breaks Python std handles.
  if command -v xvfb-run >/dev/null 2>&1; then
    xvfb-run -a wine cmd /c "$1"
  else
    wine cmd /c "$1"
  fi
}

if [[ ! -f "$WINEPREFIX/drive_c/Python313/python.exe" ]]; then
  echo "Wine Python missing. Run: bash tools/setup_wine_python.sh" >&2
  exit 1
fi

# Driver scripts avoid Wine "Invalid handle" on some console setups.
cat > "$WORKDIR/run_node_pyi.py" <<'PY'
import sys, traceback
log_path = r"Z:\tmp\wine-build\node_pyi_inner.log"
open(log_path, "w", encoding="utf-8").write("start\n")
try:
    sys.argv = [
        "pyinstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx",
        "--name", "DCS.Norway.Remote.Updater.Node",
        "--icon", r"Z:\workspace\Logo.ico",
        "--hidden-import=dcs_ru_common",
        "--hidden-import=brand_assets",
        "--collect-all=tkinter",
        "--distpath", r"Z:\workspace\dist",
        "--workpath", r"Z:\workspace\build\node",
        "--specpath", r"Z:\workspace\build",
        r"Z:\workspace\DCS_RU_Node.py",
    ]
    from PyInstaller.__main__ import run
    run()
    open(log_path, "a", encoding="utf-8").write("NODE_BUILD_OK\n")
except Exception:
    open(log_path, "a", encoding="utf-8").write(traceback.format_exc())
    raise
PY

cat > "$WORKDIR/run_control_pyi.py" <<'PY'
import sys, traceback
log_path = r"Z:\tmp\wine-build\control_pyi_inner.log"
open(log_path, "w", encoding="utf-8").write("start\n")
try:
    # Pin analysis to PySide6 hooks only (no --collect-all): matches ~50MB known-good builds.
    sys.argv = [
        "pyinstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx",
        "--name", "DCS.Norway.Remote.Updater.Control.Panel",
        "--icon", r"Z:\workspace\Logo.ico",
        "--hidden-import=dcs_ru_common",
        "--hidden-import=brand_assets",
        "--distpath", r"Z:\workspace\dist",
        "--workpath", r"Z:\workspace\build\control",
        "--specpath", r"Z:\workspace\build",
        r"Z:\workspace\DCS_RU_Control.py",
    ]
    from PyInstaller.__main__ import run
    run()
    open(log_path, "a", encoding="utf-8").write("CONTROL_BUILD_OK\n")
except Exception:
    open(log_path, "a", encoding="utf-8").write(traceback.format_exc())
    raise
PY

printf 'C:\\Python313\\python.exe Z:\\tmp\\wine-build\\run_node_pyi.py\r\n' > "$WORKDIR/run_node.bat"
printf 'C:\\Python313\\python.exe Z:\\tmp\\wine-build\\run_control_pyi.py\r\n' > "$WORKDIR/run_control.bat"

echo "==> Building Node EXE..."
run_wine_cmd "Z:\\tmp\\wine-build\\run_node.bat"
grep -q NODE_BUILD_OK "$WORKDIR/node_pyi_inner.log"

echo "==> Building Control Panel EXE..."
run_wine_cmd "Z:\\tmp\\wine-build\\run_control.bat"
grep -q CONTROL_BUILD_OK "$WORKDIR/control_pyi_inner.log"

ls -lh "$DIST/DCS.Norway.Remote.Updater.Node.exe" \
  "$DIST/DCS.Norway.Remote.Updater.Control.Panel.exe"

# Quick sanity: Control must embed the Windows Qt platform plugin
if ! strings "$DIST/DCS.Norway.Remote.Updater.Control.Panel.exe" | grep -q qwindows; then
  echo "ERROR: Control Panel EXE missing qwindows plugin — Qt hooks failed." >&2
  exit 1
fi
echo "Done."
