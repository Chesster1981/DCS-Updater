# =========================================================================
# PROGRAM 2: DCS NORWAY REMOTE UPDATER NODE (DCS_RU_Node.py)
# BLOCK 1 OF 6: INITIALIZATION, VARIABLES, GLOBAL URLS & ENVIRONMENT
# =========================================================================
import os
import sys
import json
import time
import socket
import threading
import logging
import subprocess
import ctypes
import re
import shutil
import zipfile
import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog
from datetime import datetime
from PIL import Image, ImageDraw, ImageTk
import pystray
import urllib.request
import urllib.error

from dcs_ru_common import (
    parse_authenticated_command,
    scrape_dcs_latest_version,
    wrap_command,
    github_api_headers,
    NODE_SETTINGS_DEFAULTS,
    github_interval_label,
    github_interval_seconds,
    sanitize_node_settings,
    fetch_latest_srs_release,
    parse_srs_autoconnect_version_line,
    srs_version_is_current,
    SRS_AUTOCONNECT_LUA,
)
from brand_assets import (
    BRAND_ASSET_VERSION,
    BRAND_PNG_MD5,
    logo_pil_image,
    materialize_icon_file,
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _hidden_subprocess_kwargs(capture_output=True):
    """Run child processes without flashing a console window."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
    if not capture_output:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return kwargs


CONFIG_FILE = "dcs_node_config.json"

# --- GLOBAL URL & GITHUB CONFIGURATION (NODE) ---
CURRENT_NODE_VERSION = "2.1.56"
GITHUB_REPO = "Chesster1981/DCS-Updater"
URL_GITHUB_API = "https://api.github.com/repos/"

server_socket = None
listener_thread = None
is_listening = False
tray_icon = None

DCS_CLIENT_PROCESS = "DCS.exe"
# On disk in bin\ — this is what Node STARTS (underscore is the usual filename).
DCS_SERVER_PROCESS_DEFAULT = "DCS_server.exe"
DCS_SERVER_EXE_FILENAMES = (
    DCS_SERVER_PROCESS_DEFAULT,
    "DCS.server.exe",
    "DCS.dcs_serverrelease.exe",
)
# Runtime names — Task Manager often shows "DCS.server" while the file is DCS_server.exe.
DCS_SERVER_PROCESS_STEMS = frozenset({
    "dcs_server",              # tasklist image for DCS_server.exe
    "dcs.server",              # Task Manager label on many installs
    "dcs.dcs_serverrelease",   # release-branch installs
})
DCS_PROCESSES = [DCS_CLIENT_PROCESS, DCS_SERVER_PROCESS_DEFAULT]
WATCHDOG_DEFAULT_INTERVAL = 300  # 5 minutes
WATCHDOG_STARTUP_DELAY_SECONDS = 300  # wait one interval after Node boot
DCS_STARTUP_GRACE_SECONDS = 600  # process may exist minutes before the DCS port opens
DCS_DOWN_GRACE_SECONDS = 300  # wait before auto-restart after DCS goes unhealthy/dead
DCS_PORT_BASE = 10300
WATCHDOG_MAX_RESTARTS_PER_HOUR = 3
SRS_PROCESS_IMAGES = ("SR-Server.exe", "SRS-Server.exe", "SR_Server.exe")
SRS_PRESERVE_FILENAMES = ("server.cfg", "banned.txt")

# DCS health states reported to Control Panel / Discord
DCS_HEALTH_NEVER_STARTED = "NEVER_STARTED"
DCS_HEALTH_STARTING = "STARTING"
DCS_HEALTH_HEALTHY = "HEALTHY"
DCS_HEALTH_UNHEALTHY = "UNHEALTHY"
DCS_HEALTH_DEAD = "DEAD"

node_state = {
    "installed_version": "Unknown",
    "latest_cloud_version": "Unknown",
    "active_task": "Idle",
    "is_running": True,
    "dcs_running": False,
    "dcs_health": DCS_HEALTH_NEVER_STARTED,
    "dcs_ever_healthy": False,
    "dcs_process_seen_at": None,
    "dcs_down_since": None,
}

_watchdog_restart_times = []

appdata_dir = os.environ.get('APPDATA')
if appdata_dir:
    application_path = os.path.join(appdata_dir, "DCS_Norway_Node")
else:
    application_path = os.path.abspath(os.path.dirname(__file__))

os.makedirs(application_path, exist_ok=True)
log_file_absolute_path = os.path.join(application_path, "dcs_node_updater.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file_absolute_path, encoding="utf-8")
    ]
)

def handle_single_instance_takeover():
    config = load_node_settings()
    port = int(config.get("network_port", "1015"))
    try:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.settimeout(1.5)
        test_sock.connect(('127.0.0.1', port))
        logging.info("[SYSTEM] Previous instance detected on port. Sending shutdown signal...")
        payload = wrap_command("EXIT_NODE", config.get("auth_token", ""))
        test_sock.sendall(payload.encode("utf-8"))
        test_sock.close()
        time.sleep(2.0)
    except (ConnectionRefusedError, socket.timeout):
        pass

def get_resource_path(relative_path):
    """Resolve bundled assets (PyInstaller) or next to the script/exe."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, relative_path))
        candidates.append(os.path.join(os.path.dirname(sys.executable), relative_path))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path))
        candidates.append(os.path.join(os.path.abspath("."), relative_path))
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0] if candidates else relative_path

def load_node_settings():
    absolute_config_path = os.path.join(application_path, CONFIG_FILE)
    fallback_defaults = dict(NODE_SETTINGS_DEFAULTS)
    if os.path.exists(absolute_config_path):
        try:
            with open(absolute_config_path, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
                if isinstance(loaded_data, dict):
                    return sanitize_node_settings({}, loaded_data)
        except Exception as e:
            logging.error(f"Failed to read config layout: {e}")
    return fallback_defaults

handle_single_instance_takeover()


def write_node_settings_file(settings: dict) -> str:
    absolute_config_path = os.path.join(application_path, CONFIG_FILE)
    with open(absolute_config_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
    return absolute_config_path


def sync_settings_widgets(settings: dict):
    """Mirror saved settings onto the Node settings form if it exists."""

    def _apply():
        try:
            ent_dcs.delete(0, tk.END)
            ent_dcs.insert(0, str(settings.get("dcs_main_folder", r"D:\DCS")))
            ent_srs.delete(0, tk.END)
            ent_srs.insert(0, str(settings.get("srs_install_folder", "")))
            ent_port.delete(0, tk.END)
            ent_port.insert(0, str(settings.get("network_port", "1015")))
            ent_bind.delete(0, tk.END)
            ent_bind.insert(0, str(settings.get("bind_address", "0.0.0.0")))
            ent_auth.delete(0, tk.END)
            ent_auth.insert(0, str(settings.get("auth_token", "")))
            opt_update_var.set(github_interval_label(settings.get("github_check_interval", 43200)))
            v_preserve.set(bool(settings.get("preserve_mission_scripting", True)))
            v_reboot.set(bool(settings.get("reboot_after_deployment", True)))
            v_watchdog.set(bool(settings.get("watchdog_enabled", True)))
            v_auto_restart.set(bool(settings.get("auto_restart_dcs", True)))
        except Exception:
            pass

    try:
        if "root" in globals() and root.winfo_exists():
            root.after(0, _apply)
            return
    except Exception:
        pass
    _apply()


def apply_node_settings(incoming: dict, source="ui"):
    """Write sanitized settings and restart the listener if bind/port changed."""
    existing = load_node_settings()
    merged = sanitize_node_settings(incoming, existing)
    old_port = str(existing.get("network_port", "1015"))
    old_bind = str(existing.get("bind_address", "0.0.0.0"))
    write_node_settings_file(merged)
    sync_settings_widgets(merged)
    new_port = str(merged.get("network_port", "1015"))
    new_bind = str(merged.get("bind_address", "0.0.0.0"))
    listener_restart = new_port != old_port or new_bind != old_bind
    tag = "REMOTE" if source == "remote" else "SETTINGS"
    append_activity_log(f"[{tag}] Node settings saved.")
    if listener_restart:
        append_activity_log(f"[{tag}] Restarting listener on {new_bind}:{new_port}...")
        start_or_restart_listener(new_port, new_bind)
    return merged, listener_restart


# =========================================================================
# BLOCK 2 OF 6: ASYNCHRONOUS HTML WEB SCRAPER, DISK PARSING & SEPARATE AUTO-SWAP PROCESS
# =========================================================================
last_cloud_check_timestamp = 0.0
last_github_node_check_timestamp = 0.0
last_srs_github_check_timestamp = 0.0
cached_srs_latest_tag = "Unknown"
is_swapping = False  # NEW: Global safety flag to permanently freeze loops during updates
logging.info("[SYSTEM] Version parsing and deployment framework initialized.")

def _execute_silent_node_binary_swap(download_url):
    global is_swapping
    is_swapping = True  # CRITICAL: Freeze all background monitor threads instantly
    
    try:
        append_activity_log("[SYSTEM] Forking the successful separate update process...")
        if getattr(sys, "frozen", False):
            current_exe = os.path.abspath(sys.executable)
        else:
            current_exe = os.path.abspath(sys.argv[0])

        bat_path = os.path.join(application_path, "update_node.bat")
        exe_name = os.path.basename(current_exe)
        exe_dir = os.path.dirname(current_exe)
        clean_url = download_url.replace(",", ".")

        # Always swap the running exe in-place (absolute paths). Relative names
        # previously wrote into %APPDATA% / wrong CWD and left the real install untouched.
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("setlocal\n")
            f.write(f'set "EXE_PATH={current_exe}"\n')
            f.write(f'set "EXE_DIR={exe_dir}"\n')
            f.write(f'set "EXE_NAME={exe_name}"\n')
            f.write(f'set "DOWNLOAD_URL={clean_url}"\n')
            f.write('cd /d "%EXE_DIR%"\n')
            f.write("echo [1/5] Terminating active Node execution handles...\n")
            f.write('taskkill /f /im "%EXE_NAME%" >nul 2>&1\n')
            f.write("echo [2/5] Awaiting file handle release...\n")
            f.write("timeout /t 3 /nobreak > nul\n")
            f.write(":del_loop\n")
            f.write('if exist "%EXE_PATH%" (\n')
            f.write('    del /f /q "%EXE_PATH%" >nul 2>&1\n')
            f.write("    timeout /t 1 /nobreak > nul\n")
            f.write("    goto del_loop\n")
            f.write(")\n")
            f.write("echo [3/5] Old executable removed.\n")
            f.write("echo [4/5] Downloading update from GitHub...\n")
            f.write('curl.exe -L --fail --retry 3 -o "%EXE_PATH%" "%DOWNLOAD_URL%"\n')
            f.write("if errorlevel 1 (\n")
            f.write(
                "    powershell -NoProfile -Command "
                "\"[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                "Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile '%EXE_PATH%' -MaximumRedirection 5\"\n"
            )
            f.write(")\n")
            f.write('if not exist "%EXE_PATH%" (\n')
            f.write("    echo [X] CRITICAL ERROR: Download failed!\n")
            f.write("    pause\n")
            f.write("    exit /b 1\n")
            f.write(")\n")
            f.write("echo [5/5] Starting updated Node...\n")
            f.write('start "" "%EXE_PATH%"\n')
            f.write("timeout /t 2 /nobreak > nul\n")
            f.write('del "%~f0"\n')
            f.write("exit\n")

        subprocess.Popen(
            f'cmd.exe /c start /b "" "{bat_path}"',
            shell=True,
            **_hidden_subprocess_kwargs(capture_output=False),
        )
        
        global is_listening, server_socket, tray_icon
        is_listening = False
        if server_socket: 
            server_socket.close()
        if tray_icon: 
            tray_icon.stop()
            
        root.after(0, root.destroy)
        os._exit(0)
    except Exception as e:
        logging.error(f"Silent node swap crashed: {e}")
        append_activity_log(f" [SYSTEM] Binary swap critical failure: ❌ {e}")
        is_swapping = False  # Reset flag if the initialization crashed

def check_for_github_node_updates_silent():
    if is_swapping: return  # Safety check
    try:
        if "YOUR_GITHUB_USERNAME" in GITHUB_REPO:
            append_activity_log(" [SYSTEM] GitHub check skipped: Please set ⚠️ your GITHUB_REPO in Block 1!")
            return
        
        clean_api_base = URL_GITHUB_API.replace(",", ".")
        url = f"{clean_api_base}{GITHUB_REPO}/releases/latest"
        
        req = urllib.request.Request(
            url,
            headers=github_api_headers("DCS-Norway-Remote-Updater-Node"),
        )
        append_activity_log("[SYSTEM] Connecting to GitHub API...")
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            latest_version = data.get("tag_name", "").replace("v", "").strip()
            append_activity_log(f"[SYSTEM] GitHub comparison: Local version is v{CURRENT_NODE_VERSION} | GitHub latest is v{latest_version}")
            
            if latest_version and latest_version != CURRENT_NODE_VERSION:
                assets = data.get("assets", [])
                download_url = None
                append_activity_log(f"[SYSTEM] Found {len(assets)} assets on GitHub release. Scanning...")
                for asset in assets:
                    asset_name = asset.get("name", "").lower()
                    if "remote.updater.node.exe" in asset_name:
                        download_url = asset.get("browser_download_url")
                        append_activity_log(f" [SYSTEM] Target asset match 🎯 discovered: '{asset.get('name')}'")
                        break
                if download_url:
                    _execute_silent_node_binary_swap(download_url)
                else:
                    append_activity_log(" [SYSTEM] Version mismatch found, but no asset match on GitHub!")
            else:
                append_activity_log("[SYSTEM] Node is already running the latest version.")
    except urllib.error.HTTPError as he:
        if he.code == 403:
            append_activity_log(" [SYSTEM] GitHub API rate-limit hit. Please wait a bit.")
        else:
            append_activity_log(f" [SYSTEM] GitHub API error: HTTP ❌ {he.code}")
    except Exception as e:
        append_activity_log(f" [SYSTEM] GitHub connection failed: ❌ {e}")

def github_update_monitor_loop():
    global last_github_node_check_timestamp
    check_for_github_node_updates_silent()
    last_github_node_check_timestamp = time.time()
    
    while True:
        time.sleep(5) 
        if is_swapping:  # NEW: Permanently ignore loop executions if update is in progress
            continue
            
        cfg = load_node_settings()
        interval = int(cfg.get("github_check_interval", 43200))
        get_srs_latest_version_cached(allow_fetch=True)
        
        if interval <= 0:
            continue
            
        current_time = time.time()
        elapsed_seconds = current_time - last_github_node_check_timestamp
        
        if elapsed_seconds >= interval:
            check_for_github_node_updates_silent()
            last_github_node_check_timestamp = time.time()

def _run_dcs_html_scraper_background():
    version = scrape_dcs_latest_version(timeout=10.0)
    if version:
        node_state["latest_cloud_version"] = version
    else:
        logging.error("[SCRAPER] Failed to scrape DCS version from all known sources.")

def get_dcs_versions_local():
    global last_cloud_check_timestamp, last_github_node_check_timestamp
    config = load_node_settings()
    main_folder = config.get("dcs_main_folder", "").strip()
    current_time = time.time()
    
    if current_time - last_cloud_check_timestamp > 1800.0:
        last_cloud_check_timestamp = current_time
        threading.Thread(target=_run_dcs_html_scraper_background, daemon=True).start()
        
    cfg_path = os.path.join(main_folder, "autoupdate.cfg")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
                val = str(data.get("version", "Unknown"))
                if val != "Unknown":
                    node_state["installed_version"] = val
        except:
            pass
            
    if node_state["latest_cloud_version"] == "Unknown":
        node_state["latest_cloud_version"] = node_state["installed_version"]
        
    return node_state["installed_version"], node_state["latest_cloud_version"]

def check_active_processes_running():
    try:
        extras = _config_server_name_extras()
        for image, _pid in iter_tasklist_processes():
            if normalize_process_name(image) == normalize_process_name(DCS_CLIENT_PROCESS):
                return True
            if is_dcs_server_process_stem(process_name_stem(image), extras):
                return True
    except Exception:
        pass
    return False


def derive_dcs_port(node_port) -> int:
    """Last two digits of the Node port + 10300 = DCS server port (1015 -> 10315)."""
    try:
        suffix = int(str(node_port).strip()) % 100
    except (TypeError, ValueError):
        suffix = 15
    return DCS_PORT_BASE + suffix


def is_dcs_port_listening(node_port, host="127.0.0.1", timeout=2.0) -> bool:
    """True when something accepts TCP on the derived DCS server port."""
    port = derive_dcs_port(node_port)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def normalize_process_name(name: str) -> str:
    n = str(name or "").strip().lower()
    if n and not n.endswith(".exe"):
        n += ".exe"
    return n


def process_name_stem(name: str) -> str:
    n = normalize_process_name(name)
    return n[:-4] if n.endswith(".exe") else n


def task_manager_label_for_exe(filename: str) -> str:
    """Map on-disk exe name to the label operators see in Task Manager."""
    base = os.path.basename(str(filename or ""))
    if base.lower() == DCS_SERVER_PROCESS_DEFAULT.lower():
        return "DCS.server"
    if base.lower().endswith(".exe"):
        return base[:-4]
    return base or "DCS.server"


def _config_server_name_extras(config=None) -> list[str]:
    cfg = config if config is not None else load_node_settings()
    extras = []
    explicit = str(cfg.get("dcs_server_exe", "")).strip()
    if explicit:
        extras.append(os.path.basename(explicit))
    for item in cfg.get("dcs_server_process_names") or []:
        if str(item).strip():
            extras.append(str(item).strip())
    return extras


def is_dcs_server_exe_filename(name: str, extra_names=None) -> bool:
    """Match dedicated-server executable filenames on disk (not Task Manager labels)."""
    n = normalize_process_name(name)
    if not n or n == normalize_process_name(DCS_CLIENT_PROCESS):
        return False
    for candidate in list(DCS_SERVER_EXE_FILENAMES) + list(extra_names or []):
        if n == normalize_process_name(candidate):
            return True
    stem = process_name_stem(n)
    return stem in DCS_SERVER_PROCESS_STEMS


def is_dcs_server_process_stem(stem: str, extra_names=None) -> bool:
    """Match runtime process names/stems such as DCS.server or DCS_server."""
    s = process_name_stem(stem)
    if not s or s == "dcs":
        return False
    if s in DCS_SERVER_PROCESS_STEMS:
        return True
    for extra in extra_names or []:
        if s == process_name_stem(extra):
            return True
    return s.startswith("dcs") and "server" in s


def iter_tasklist_processes():
    """Yield (image_name, pid) from Windows tasklist."""
    try:
        output = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            errors="ignore",
            **_hidden_subprocess_kwargs(),
        )
    except Exception:
        return
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) < 2:
            continue
        image = parts[0]
        pid_text = parts[1].strip('"')
        pid = int(pid_text) if pid_text.isdigit() else None
        if pid is not None:
            yield image, pid


def iter_windows_processes():
    """
    Yield (image_name, pid, executable_path) without spawning PowerShell.
    ExecutablePath is the reliable link between DCS_server.exe on disk and DCS.server in Task Manager.
    """
    if sys.platform != "win32":
        yield from ((image, pid, "") for image, pid in iter_tasklist_processes())
        return

    try:
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot in (0, INVALID_HANDLE_VALUE):
            raise OSError("CreateToolhelp32Snapshot failed")

        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pid = int(entry.th32ProcessID)
            image = entry.szExeFile
            exe_path = ""
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    size = wintypes.DWORD(32768)
                    buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        exe_path = buf.value
                finally:
                    kernel32.CloseHandle(handle)
            yield image, pid, exe_path
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)
        return
    except Exception as e:
        logging.debug("Native process scan failed, falling back to tasklist: %s", e)
    for image, pid in iter_tasklist_processes():
        yield image, pid, ""


def list_running_dcs_server_processes(config=None):
    """
    Return [(task_manager_label, pid, kill_image), ...].
    kill_image is the real exe filename used by taskkill (e.g. DCS_server.exe).
    """
    extras = _config_server_name_extras(config)
    seen = set()
    matches = []
    for image, pid, exe_path in iter_windows_processes():
        if pid in seen:
            continue
        exe_file = os.path.basename(exe_path) if exe_path else image
        stem = process_name_stem(image or exe_file)
        by_path = bool(exe_path) and is_dcs_server_exe_filename(exe_file, extras)
        by_stem = is_dcs_server_process_stem(stem, extras)
        if not (by_path or by_stem):
            continue
        seen.add(pid)
        label = task_manager_label_for_exe(exe_file if exe_path else stem)
        kill_image = exe_file or image
        matches.append((label, pid, kill_image))
    return matches


def is_dcs_server_process_running(config=None) -> bool:
    """True when any known/pattern-matched DCS dedicated-server process is alive."""
    return bool(list_running_dcs_server_processes(config))


def get_pids_listening_on_port(port: int) -> list[int]:
    """Return PIDs with a TCP listener on the given local port."""
    pids = []
    try:
        output = subprocess.check_output(
            f'netstat -ano -p tcp | findstr ":{port} "',
            shell=True,
            text=True,
            errors="ignore",
            **_hidden_subprocess_kwargs(),
        )
        for line in output.splitlines():
            upper = line.upper()
            if "LISTENING" not in upper:
                continue
            parts = line.split()
            if not parts:
                continue
            pid_text = parts[-1]
            if pid_text.isdigit():
                pids.append(int(pid_text))
    except Exception as e:
        logging.debug("netstat scan for port %s failed: %s", port, e)
    return list(dict.fromkeys(pids))


def discover_dcs_server_executables(bin_folder: str, config=None) -> list[str]:
    """Find dedicated-server executables in the DCS bin folder (disk names, not Task Manager labels)."""
    cfg = config if config is not None else load_node_settings()
    extras = _config_server_name_extras(cfg)
    if not os.path.isdir(bin_folder):
        return []

    explicit = str(cfg.get("dcs_server_exe", "")).strip()
    if explicit:
        if os.path.isabs(explicit) and os.path.exists(explicit):
            return [explicit]
        candidate = os.path.join(bin_folder, os.path.basename(explicit))
        if os.path.exists(candidate):
            return [candidate]

    preferred = os.path.join(bin_folder, DCS_SERVER_PROCESS_DEFAULT)
    if os.path.exists(preferred):
        return [preferred]

    discovered = []
    for fname in sorted(os.listdir(bin_folder)):
        if not fname.lower().endswith(".exe"):
            continue
        if is_dcs_server_exe_filename(fname, extras):
            discovered.append(os.path.join(bin_folder, fname))
    return discovered


def resolve_dcs_server_exe_path(config=None) -> str:
    cfg = config if config is not None else load_node_settings()
    main_folder = str(cfg.get("dcs_main_folder", "")).strip()
    bin_folder = os.path.join(main_folder, "bin")
    matches = discover_dcs_server_executables(bin_folder, cfg)
    return matches[0] if matches else os.path.join(bin_folder, DCS_SERVER_PROCESS_DEFAULT)


def describe_running_dcs_server_processes(config=None) -> str:
    matches = list_running_dcs_server_processes(config)
    if not matches:
        return "no dedicated-server process"
    return ", ".join(
        f"{label} ({kill_image} pid {pid})" for label, pid, kill_image in matches
    )


def has_dcs_crash_dialog() -> bool:
    """Detect Eagle Dynamics crash/report dialogs while the process may still be alive."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = False
        keywords = (
            "crash",
            "problem",
            "eagle dynamics",
            "error report",
            "stopped working",
            "send report",
        )

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            nonlocal found
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd) + 1
            if length <= 1:
                return True
            buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, buf, length)
            title = buf.value.lower()
            if any(keyword in title for keyword in keywords):
                if "dcs" in title or "digital combat simulator" in title:
                    found = True
                    return False
            return True

        user32.EnumWindows(callback, 0)
        return found
    except Exception as e:
        logging.debug("Crash dialog scan failed: %s", e)
        return False


def evaluate_dcs_health(node_port) -> str:
    """
    Classify DCS server state on every scan (nothing is locked from the first check):
    - HEALTHY: listening port (primary) and no crash dialog
    - STARTING: process seen but port not open yet, and DCS was never healthy this boot
    - UNHEALTHY: crash dialog, or process alive / port closed after a prior healthy run
    - DEAD: process/port gone after having been healthy before
    - NEVER_STARTED: never seen a dedicated-server process this boot
    """
    config = load_node_settings()
    port_up = is_dcs_port_listening(node_port)
    crash_dialog = has_dcs_crash_dialog()
    process_up = is_dcs_server_process_running(config)

    if process_up and node_state.get("dcs_process_seen_at") is None:
        node_state["dcs_process_seen_at"] = time.time()

    if crash_dialog:
        return DCS_HEALTH_UNHEALTHY
    if port_up:
        return DCS_HEALTH_HEALTHY
    if process_up:
        if node_state.get("dcs_ever_healthy"):
            return DCS_HEALTH_UNHEALTHY
        seen_at = node_state.get("dcs_process_seen_at") or time.time()
        if time.time() - seen_at < DCS_STARTUP_GRACE_SECONDS:
            return DCS_HEALTH_STARTING
        return DCS_HEALTH_UNHEALTHY
    if node_state.get("dcs_ever_healthy"):
        return DCS_HEALTH_DEAD
    return DCS_HEALTH_NEVER_STARTED


def refresh_dcs_health_state(node_port=None):
    """Refresh node_state from process + port health checks."""
    if node_port is None:
        node_port = load_node_settings().get("network_port", "1015")
    health = evaluate_dcs_health(node_port)
    if health == DCS_HEALTH_HEALTHY:
        node_state["dcs_ever_healthy"] = True
        node_state["dcs_down_since"] = None
    elif health in (DCS_HEALTH_UNHEALTHY, DCS_HEALTH_DEAD) and node_state.get("dcs_ever_healthy"):
        if node_state.get("dcs_down_since") is None:
            node_state["dcs_down_since"] = time.time()
    else:
        node_state["dcs_down_since"] = None
    node_state["dcs_health"] = health
    node_state["dcs_running"] = health == DCS_HEALTH_HEALTHY
    return health


def dcs_down_grace_elapsed() -> bool:
    """True when DCS has been unhealthy/dead long enough to allow auto-restart."""
    down_since = node_state.get("dcs_down_since")
    if down_since is None:
        return False
    return (time.time() - down_since) >= DCS_DOWN_GRACE_SECONDS


def can_auto_restart_dcs() -> bool:
    now = time.time()
    _watchdog_restart_times[:] = [
        ts for ts in _watchdog_restart_times if now - ts < 3600
    ]
    return len(_watchdog_restart_times) < WATCHDOG_MAX_RESTARTS_PER_HOUR


def record_dcs_auto_restart():
    _watchdog_restart_times.append(time.time())


def start_dcs_server_process():
    """Launch the dedicated DCS server executable from the configured bin folder."""
    config = load_node_settings()
    main_folder = config.get("dcs_main_folder", "").strip()
    bin_folder = os.path.join(main_folder, "bin")
    exe_path = resolve_dcs_server_exe_path(config)
    exe_name = os.path.basename(exe_path)

    if not os.path.exists(exe_path):
        append_activity_log(
            f" [WATCHDOG] ERROR: no dedicated-server exe found in ❌ {bin_folder}"
        )
        return False

    try:
        label = task_manager_label_for_exe(exe_name)
        append_activity_log(
            f"[WATCHDOG] Starting {label} via {exe_name} from {bin_folder}..."
        )
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [exe_path],
            cwd=bin_folder,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        return True
    except Exception as e:
        append_activity_log(f" [WATCHDOG] Failed to start DCS server: ❌ {e}")
        logging.error("DCS server restart failed: %s", e)
        return False


def force_kill_dcs_server(node_port=None):
    config = load_node_settings()
    killed = set()
    process_desc = describe_running_dcs_server_processes(config)
    append_activity_log(f"[WATCHDOG] Terminating DCS server ({process_desc})...")

    if node_port is not None:
        for pid in get_pids_listening_on_port(derive_dcs_port(node_port)):
            try:
                subprocess.run(
                    f"taskkill /f /pid {pid}",
                    shell=True,
                    **_hidden_subprocess_kwargs(capture_output=False),
                )
                killed.add(pid)
            except Exception as e:
                logging.debug("Taskkill pid %s failed: %s", pid, e)

    for _label, pid, kill_image in list_running_dcs_server_processes(config):
        if pid in killed:
            continue
        try:
            subprocess.run(
                f'taskkill /f /im "{kill_image}"',
                shell=True,
                **_hidden_subprocess_kwargs(capture_output=False),
            )
            killed.add(pid)
        except Exception as e:
            logging.debug("Taskkill on %s failed: %s", kill_image, e)


def execute_dcs_restart(node_port=None, source="watchdog") -> bool:
    """Kill DCS if needed and start it again. Safe to run from a background thread."""
    cfg = load_node_settings()
    if node_port is None:
        node_port = cfg.get("network_port", "1015")
    health = refresh_dcs_health_state(node_port)
    tag = "REMOTE" if source == "remote" else "WATCHDOG"
    node_state["active_task"] = "Restarting DCS"
    try:
        if is_dcs_server_process_running(cfg) or health == DCS_HEALTH_UNHEALTHY:
            force_kill_dcs_server(node_port)
            time.sleep(3)

        started = start_dcs_server_process()
        if not started:
            append_activity_log(f"[{tag}] DCS restart failed — could not start the server exe.")
            return False

        record_dcs_auto_restart()
        time.sleep(15)
        new_health = refresh_dcs_health_state(node_port)
        if new_health == DCS_HEALTH_HEALTHY:
            append_activity_log(f"[{tag}] DCS server restart verified ✅")
        else:
            append_activity_log(
                f"[{tag}] Restart launched but health is still {new_health} ⚠️"
            )
        return new_health == DCS_HEALTH_HEALTHY
    except Exception as e:
        append_activity_log(f"[{tag}] DCS restart failed: {e}")
        logging.error("DCS restart failed (%s): %s", source, e)
        return False
    finally:
        node_state["active_task"] = "Idle"


def attempt_dcs_auto_restart(health: str, node_port) -> bool:
    """Restart DCS only after it was previously healthy (not on fresh boot)."""
    if health in (DCS_HEALTH_NEVER_STARTED, DCS_HEALTH_STARTING):
        append_activity_log(
            "[WATCHDOG] DCS has not finished starting since Node boot — skipping auto-restart."
        )
        return False
    if not node_state.get("dcs_ever_healthy"):
        append_activity_log(
            "[WATCHDOG] DCS was never healthy — skipping auto-restart."
        )
        return False
    if not can_auto_restart_dcs():
        append_activity_log(
            f"[WATCHDOG] Auto-restart limit reached ({WATCHDOG_MAX_RESTARTS_PER_HOUR}/hour)."
        )
        return False
    return execute_dcs_restart(node_port, source="watchdog")


def dcs_watchdog_loop():
    """Every N seconds: verify DCS health (process + port); restart only after prior healthy run."""
    append_activity_log(
        f"[WATCHDOG] Waiting {WATCHDOG_STARTUP_DELAY_SECONDS}s after Node boot before first DCS health scan."
    )
    time.sleep(WATCHDOG_STARTUP_DELAY_SECONDS)
    while True:
        try:
            if is_swapping:
                time.sleep(5)
                continue

            cfg = load_node_settings()
            enabled = bool(cfg.get("watchdog_enabled", True))
            interval = int(cfg.get("watchdog_interval_seconds", WATCHDOG_DEFAULT_INTERVAL))
            if interval < 60:
                interval = 60
            node_port = cfg.get("network_port", "1015")

            if not enabled:
                time.sleep(interval)
                continue

            if node_state["active_task"] not in ("Idle",):
                append_activity_log(
                    f"[WATCHDOG] Skipping check — node busy ({node_state['active_task']})."
                )
                time.sleep(interval)
                continue

            health = refresh_dcs_health_state(node_port)
            dcs_port = derive_dcs_port(node_port)
            if health == DCS_HEALTH_HEALTHY:
                append_activity_log(
                    f"[WATCHDOG] DCS healthy (port {dcs_port}, {describe_running_dcs_server_processes(cfg)}) ✅"
                )
            elif health == DCS_HEALTH_NEVER_STARTED:
                append_activity_log(
                    f"[WATCHDOG] DCS not running (never started since Node boot, port {dcs_port})."
                )
            elif health == DCS_HEALTH_STARTING:
                append_activity_log(
                    f"[WATCHDOG] DCS starting — {describe_running_dcs_server_processes(cfg)}; "
                    f"waiting for port {dcs_port}."
                )
            elif health == DCS_HEALTH_UNHEALTHY:
                append_activity_log(
                    f"[WATCHDOG] DCS unhealthy — {describe_running_dcs_server_processes(cfg)} "
                    f"but port {dcs_port} not responding."
                )
                if bool(cfg.get("auto_restart_dcs", True)):
                    if dcs_down_grace_elapsed():
                        attempt_dcs_auto_restart(health, node_port)
                    else:
                        down_since = node_state.get("dcs_down_since") or time.time()
                        remaining = max(0, int(DCS_DOWN_GRACE_SECONDS - (time.time() - down_since)))
                        append_activity_log(
                            f"[WATCHDOG] Waiting {remaining}s grace before auto-restart "
                            f"(allows scheduled/manual DCS stop)."
                        )
                else:
                    append_activity_log("[WATCHDOG] Auto-restart disabled in settings.")
            elif health == DCS_HEALTH_DEAD:
                append_activity_log(f"[WATCHDOG] DCS stopped/crashed (port {dcs_port}).")
                if bool(cfg.get("auto_restart_dcs", True)):
                    if dcs_down_grace_elapsed():
                        attempt_dcs_auto_restart(health, node_port)
                    else:
                        down_since = node_state.get("dcs_down_since") or time.time()
                        remaining = max(0, int(DCS_DOWN_GRACE_SECONDS - (time.time() - down_since)))
                        append_activity_log(
                            f"[WATCHDOG] Waiting {remaining}s grace before auto-restart "
                            f"(allows scheduled/manual DCS stop)."
                        )
                else:
                    append_activity_log("[WATCHDOG] Auto-restart disabled in settings.")

            time.sleep(interval)
        except Exception as e:
            logging.error("Watchdog loop error: %s", e)
            time.sleep(60)


def force_kill_core_dcs():
    append_activity_log("[PROCESS] Requesting termination of core DCS processes...")
    config = load_node_settings()
    extras = _config_server_name_extras(config)
    killed_images = set()
    for image, _pid in iter_tasklist_processes():
        n = normalize_process_name(image)
        if n == normalize_process_name(DCS_CLIENT_PROCESS) or is_dcs_server_process_stem(
            process_name_stem(image), extras
        ):
            if n in killed_images:
                continue
            killed_images.add(n)
            try:
                subprocess.run(
                    f'taskkill /f /im "{image}"',
                    shell=True,
                    **_hidden_subprocess_kwargs(capture_output=False),
                )
            except Exception as e:
                logging.debug(f"Taskkill command rejected on {image}: {e}")


def srs_server_dir(install_root: str) -> str:
    root = os.path.normpath(str(install_root or "").strip().strip('"'))
    if not root:
        return ""
    if os.path.basename(root).lower() == "server":
        return root
    return os.path.join(root, "Server")


def srs_install_root(install_root=None) -> str:
    cfg = load_node_settings()
    root = install_root if install_root is not None else cfg.get("srs_install_folder", "")
    return os.path.normpath(str(root or "").strip().strip('"'))


def srs_autoconnect_lua_path(install_root: str) -> str:
    root = srs_install_root(install_root)
    if not root:
        return ""
    preferred = os.path.join(root, "scripts", SRS_AUTOCONNECT_LUA)
    for scripts_dir in ("scripts", "Scripts"):
        candidate = os.path.join(root, scripts_dir, SRS_AUTOCONNECT_LUA)
        if os.path.isfile(candidate):
            return candidate
    return preferred


def get_srs_installed_version(install_root=None) -> str:
    root = srs_install_root(install_root)
    if not root:
        return "Not set"
    lua_path = srs_autoconnect_lua_path(root)
    if not lua_path or not os.path.isfile(lua_path):
        return "Missing"
    try:
        with open(lua_path, encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline()
        version = parse_srs_autoconnect_version_line(first_line)
        return version or "Unknown"
    except Exception:
        return "Unknown"


def write_srs_installed_version_line(install_root: str, version: str) -> bool:
    """Keep AutoConnect lua as the version source of truth after a Server extract."""
    lua_path = srs_autoconnect_lua_path(install_root)
    if not lua_path or not os.path.isfile(lua_path):
        return False
    try:
        with open(lua_path, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
        newline = "\r\n" if "\r\n" in content else "\n"
        lines = content.splitlines()
        new_first = f"-- Version {version}"
        if lines:
            lines[0] = new_first
        else:
            lines = [new_first]
        with open(lua_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(newline.join(lines) + newline)
        return True
    except Exception as e:
        logging.warning("Could not update SRS AutoConnect version line: %s", e)
        return False


def get_srs_latest_version_cached(allow_fetch: bool = True) -> str:
    global last_srs_github_check_timestamp, cached_srs_latest_tag
    now = time.time()
    if last_srs_github_check_timestamp and now - last_srs_github_check_timestamp < 600:
        return cached_srs_latest_tag
    if not allow_fetch:
        return cached_srs_latest_tag
    last_srs_github_check_timestamp = now
    release = fetch_latest_srs_release()
    if release and release.get("tag"):
        cached_srs_latest_tag = release["tag"]
    return cached_srs_latest_tag


def is_srs_process_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["tasklist"],
            capture_output=True,
            text=True,
            timeout=8,
            **_hidden_subprocess_kwargs(capture_output=True),
        )
        listing = (result.stdout or "").lower()
        return any(name.lower() in listing for name in SRS_PROCESS_IMAGES)
    except Exception:
        return False


def force_kill_srs():
    append_activity_log("[SRS] Stopping SRS Server process...")
    for image in SRS_PROCESS_IMAGES:
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", image],
                capture_output=True,
                text=True,
                timeout=15,
                **_hidden_subprocess_kwargs(capture_output=True),
            )
        except Exception as e:
            logging.debug("SRS taskkill %s failed: %s", image, e)
    time.sleep(2)


def start_srs_server_process(server_dir: str) -> bool:
    exe_path = ""
    for image in SRS_PROCESS_IMAGES:
        candidate = os.path.join(server_dir, image)
        if os.path.isfile(candidate):
            exe_path = candidate
            break
    if not exe_path:
        append_activity_log(f"[SRS] Could not find SR-Server.exe in {server_dir}")
        return False
    try:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [exe_path],
            cwd=server_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        append_activity_log(f"[SRS] Started {os.path.basename(exe_path)}")
        return True
    except Exception as e:
        append_activity_log(f"[SRS] Failed to start SRS Server: {e}")
        return False


def _zip_server_prefix(zf: zipfile.ZipFile) -> str:
    exe_prefixes = []
    server_prefixes = []
    for name in zf.namelist():
        parts = [part for part in name.replace("\\", "/").split("/") if part]
        if not parts:
            continue
        lower = [part.lower() for part in parts]
        if lower[-1] in {image.lower() for image in SRS_PROCESS_IMAGES}:
            parent = parts[:-1]
            if parent and parent[-1].lower() == "server":
                exe_prefixes.append("/".join(parent) + "/")
        if "server" in lower:
            idx = lower.index("server")
            server_prefixes.append("/".join(parts[: idx + 1]) + "/")
    if exe_prefixes:
        return min(exe_prefixes, key=len)
    if server_prefixes:
        return min(server_prefixes, key=len)
    return ""


def _download_url_to_file(url: str, dest_path: str, label: str):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    headers = github_api_headers("DCS-Norway-Remote-Updater-Node")
    headers["Accept"] = "application/octet-stream"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as response, open(dest_path, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        read = 0
        last_pct = -10
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if total:
                pct = int(read * 100 / total)
                if pct >= last_pct + 10:
                    append_activity_log(f"[SRS] Downloading {label}: {pct}% ({read // (1024 * 1024)} MB)")
                    last_pct = pct
    append_activity_log(f"[SRS] Download complete: {dest_path}")


def execute_srs_update_pipeline():
    config = load_node_settings()
    install_root = str(config.get("srs_install_folder") or "").strip()
    dest = srs_server_dir(install_root)
    if not dest:
        append_activity_log("[SRS] ERROR: SRS installation folder is not set. Set it in Node Settings.")
        return
    if node_state["active_task"] not in ("Idle",):
        append_activity_log(f"[SRS] Skipping — node busy ({node_state['active_task']}).")
        return

    node_state["active_task"] = "Updating SRS"
    append_activity_log(f"\n[SRS] Starting SRS Server update into {dest}")
    try:
        release = fetch_latest_srs_release()
        if not release:
            append_activity_log("[SRS] ERROR: Could not read the latest SRS GitHub release.")
            return
        latest = release["tag"]
        zip_name = release["name"]
        zip_url = release["url"]
        installed = get_srs_installed_version(install_root)
        append_activity_log(f"[SRS] Installed {installed} | GitHub latest {latest}")
        if srs_version_is_current(installed, latest):
            append_activity_log(f"[SRS] Already on {latest} — skipping download.")
            return

        was_running = is_srs_process_running()
        if was_running:
            force_kill_srs()

        tmp_dir = os.path.join(application_path, "srs_update_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        zip_path = os.path.join(tmp_dir, zip_name)
        append_activity_log(f"[SRS] Downloading {zip_name} ...")
        _download_url_to_file(zip_url, zip_path, zip_name)

        preserved = {}
        os.makedirs(dest, exist_ok=True)
        for filename in SRS_PRESERVE_FILENAMES:
            existing = os.path.join(dest, filename)
            if os.path.isfile(existing):
                with open(existing, "rb") as handle:
                    preserved[filename] = handle.read()
                append_activity_log(f"[SRS] Preserving {filename}")

        with zipfile.ZipFile(zip_path, "r") as zf:
            prefix = _zip_server_prefix(zf)
            if not prefix:
                append_activity_log("[SRS] ERROR: Zip does not contain a Server folder.")
                return
            append_activity_log(f"[SRS] Extracting '{prefix}' to {dest}")
            for info in zf.infolist():
                norm = info.filename.replace("\\", "/")
                if norm == prefix.rstrip("/"):
                    continue
                if not (norm.startswith(prefix) or norm + "/" == prefix):
                    continue
                rel = norm[len(prefix):]
                if not rel:
                    continue
                target = os.path.join(dest, *rel.split("/"))
                if info.is_dir() or norm.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

        for filename, payload in preserved.items():
            with open(os.path.join(dest, filename), "wb") as handle:
                handle.write(payload)
            append_activity_log(f"[SRS] Restored {filename}")

        if write_srs_installed_version_line(install_root, latest):
            append_activity_log(f"[SRS] Recorded version {latest} in {SRS_AUTOCONNECT_LUA}")
        else:
            append_activity_log(
                f"[SRS] Warning: {SRS_AUTOCONNECT_LUA} not found — installed version cannot be recorded."
            )
        global cached_srs_latest_tag, last_srs_github_check_timestamp
        cached_srs_latest_tag = latest
        last_srs_github_check_timestamp = time.time()

        try:
            os.remove(zip_path)
        except Exception:
            pass

        if was_running:
            start_srs_server_process(dest)
        append_activity_log(f"[SRS] SRS Server updated to {latest} ✅")
    except Exception as e:
        append_activity_log(f"[SRS] ERROR during SRS update: {e}")
        logging.error("SRS update failed: %s", e)
    finally:
        node_state["active_task"] = "Idle"


# =========================================================================
# BLOCK 3 OF 6: DEPLOYMENT PIPELINE & EXTENDED NETWORK SOCKET INTERFACE
# =========================================================================
def execute_deployment_pipeline():
    config = load_node_settings()
    main_folder = config.get("dcs_main_folder", "").strip()
    preserve_lua = config.get("preserve_mission_scripting", True)
    reboot_after_deployment = config.get("reboot_after_deployment", True)
    bin_folder = os.path.join(main_folder, "bin")
    scripts_folder = os.path.join(main_folder, "Scripts")
    active_lua_file = os.path.join(scripts_folder, "MissionScripting.lua")
    backup_lua_file = os.path.join(application_path, "MissionScripting.lua.bak")
    
    node_state["active_task"] = "Updating"
    append_activity_log("\n[PROCESS] Starting update sequence...")
    
    if preserve_lua and os.path.exists(active_lua_file):
        append_activity_log("[PROCESS] Preserving active MissionScripting.lua to node directory...")
        try:
            shutil.copy(active_lua_file, backup_lua_file)
            append_activity_log(" [PROCESS] Active script backup secured ✅ successfully.")
        except Exception as e:
            append_activity_log(f" [PROCESS] Backup failed, file might be ⚠️ locked: {e}")
            
    force_kill_core_dcs()
    append_activity_log("[PROCESS] Verifying DCS cleanup status...")
    max_timeout_ticks = 15 
    start_timer = time.time()
    
    while check_active_processes_running():
        if time.time() - start_timer > max_timeout_ticks:
            append_activity_log(" ERROR: Forced termination timed out. Files ❌ might still be locked!")
            break
        append_activity_log("[PROCESS] DCS still releasing files... Waiting 2 seconds...")
        time.sleep(2)
        
    append_activity_log(" [PROCESS] DCS processes are verified DEAD. ✅ Proceeding.")
    node_state["active_task"] = "Downloading"
    
    try:
        if not os.path.exists(bin_folder):
            append_activity_log(f" ERROR: Could not find the bin folder at: ❌ {bin_folder}")
            node_state["active_task"] = "Idle"
            return
            
        append_activity_log("[PROCESS] Launching DCS_updater.exe with elevated credentials pipeline...")
        updater_path = os.path.join(bin_folder, "DCS_updater.exe")
        ps_cmd = f"Start-Process -FilePath '{updater_path}' -ArgumentList '--quiet update' -WorkingDirectory '{bin_folder}' -Verb RunAs -Wait"
        
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            **_hidden_subprocess_kwargs(capture_output=False),
        )
        append_activity_log(" [PROCESS] DCS core update finished. ✅")
    except Exception as e:
        append_activity_log(f" ERROR during DCS update: ❌ {e}")
        node_state["active_task"] = "Idle"
        return
        
    append_activity_log("[PROCESS] Re-scanning local configuration matrices...")
    get_dcs_versions_local()
    
    if preserve_lua and os.path.exists(backup_lua_file):
        append_activity_log("[PROCESS] Restoring preserved MissionScripting.lua back to DCS cluster...")
        try:
            os.makedirs(scripts_folder, exist_ok=True)
            shutil.copy(backup_lua_file, active_lua_file)
            append_activity_log(" [PROCESS] Success! MissionScripting.lua has ✅ been completely restored.")
            os.remove(backup_lua_file)
        except Exception as e:
            append_activity_log(f" ERROR during file restoration: ❌ {e}")
            
    if reboot_after_deployment:
        append_activity_log(" [PROCESS] Windows reboot is enabled. ⚠️ Rebooting machine in 5 seconds...")
        node_state["active_task"] = "Rebooting"
        time.sleep(5)
        subprocess.run("shutdown /r /t 0", shell=True, **_hidden_subprocess_kwargs(capture_output=False))
    else:
        append_activity_log(" [PROCESS] Finished! (PC Reboot was ✅ skipped).")
        node_state["active_task"] = "Idle"

def network_socket_listener(port, bind_address="0.0.0.0"):
    global server_socket, is_listening
    config = load_node_settings()
    auth_token = str(config.get("auth_token", "")).strip()
    bind_host = (bind_address or config.get("bind_address") or "0.0.0.0").strip()

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind((bind_host, port))
        server_socket.listen(5)
        is_listening = True
    except Exception as e:
        logging.error("Could not bind network listener to %s:%s: %s", bind_host, port, e)
        append_activity_log(f" ERROR: Could not bind network listener to ❌ {bind_host}:{port}: {e}")
        is_listening = False
        return

    auth_note = "auth ON" if auth_token else "auth OFF (set auth_token in settings)"
    append_activity_log(f" Listener active on {bind_host}:{port} ({auth_note})...")
    if bind_host == "0.0.0.0":
        append_activity_log(" [SECURITY] Bound to all interfaces. Prefer a LAN IP or firewall lock-down.")

    while is_listening:
        try:
            conn, addr = server_socket.accept()
            if not is_listening:
                break

            try:
                # Reload token so settings changes apply without full restart of process
                live_token = str(load_node_settings().get("auth_token", "")).strip()
                data = conn.recv(65536)
                if not data:
                    conn.close()
                    continue
                raw_message = data.decode("utf-8").strip()
                command, authorized = parse_authenticated_command(raw_message, live_token)

                if not authorized or not command:
                    append_activity_log(f" [SECURITY] Rejected unauthorized command from {addr[0]}")
                    conn.send((json.dumps({"status": "UNAUTHORIZED"}) + "\n").encode("utf-8"))
                    conn.close()
                    continue

                if command == "PING_STATUS":
                    inst_v, cloud_v = get_dcs_versions_local()
                    node_port = load_node_settings().get("network_port", "1015")
                    dcs_health = refresh_dcs_health_state(node_port)
                    srs_configured = bool(
                        str(load_node_settings().get("srs_install_folder") or "").strip()
                    )
                    response = {
                        "status": "ACK",
                        "installed_version": inst_v,
                        "latest_cloud_version": cloud_v,
                        "active_task": node_state["active_task"],
                        "node_version": CURRENT_NODE_VERSION,
                        "dcs_running": dcs_health == DCS_HEALTH_HEALTHY,
                        "dcs_health": dcs_health,
                        "srs_configured": srs_configured,
                        "srs_running": is_srs_process_running() if srs_configured else False,
                        "srs_installed_version": get_srs_installed_version(),
                        "srs_latest_version": get_srs_latest_version_cached(allow_fetch=False),
                    }
                    conn.send((json.dumps(response) + "\n").encode("utf-8"))
                    conn.close()
                elif command == "GET_SETTINGS":
                    response = {
                        "status": "ACK",
                        "settings": load_node_settings(),
                    }
                    conn.send((json.dumps(response) + "\n").encode("utf-8"))
                    conn.close()
                elif command.startswith("SET_SETTINGS"):
                    if is_swapping:
                        conn.send((json.dumps({
                            "status": "REJECTED_BUSY",
                            "task": "Updating",
                        }) + "\n").encode("utf-8"))
                        conn.close()
                    else:
                        raw_payload = command[len("SET_SETTINGS"):].strip()
                        try:
                            incoming = json.loads(raw_payload) if raw_payload else {}
                            if not isinstance(incoming, dict):
                                raise ValueError("settings payload must be a JSON object")
                        except Exception as e:
                            conn.send((json.dumps({
                                "status": "ERROR",
                                "message": f"Invalid settings JSON: {e}",
                            }) + "\n").encode("utf-8"))
                            conn.close()
                        else:
                            existing = load_node_settings()
                            merged = sanitize_node_settings(incoming, existing, remote=True)
                            old_port = str(existing.get("network_port", "1015"))
                            old_bind = str(existing.get("bind_address", "0.0.0.0"))
                            write_node_settings_file(merged)
                            listener_restart = (
                                str(merged.get("network_port", "1015")) != old_port
                                or str(merged.get("bind_address", "0.0.0.0")) != old_bind
                            )
                            conn.send((json.dumps({
                                "status": "ACK",
                                "settings": merged,
                                "listener_restart": listener_restart,
                            }) + "\n").encode("utf-8"))
                            conn.close()
                            append_activity_log("[REMOTE] Node settings updated from Control Panel.")
                            sync_settings_widgets(merged)
                            if listener_restart:
                                def _restart_after_settings():
                                    time.sleep(0.2)
                                    start_or_restart_listener(
                                        merged.get("network_port"),
                                        merged.get("bind_address"),
                                    )
                                threading.Thread(target=_restart_after_settings, daemon=True).start()
                elif command == "TRIGGER_SRS_UPDATE":
                    if is_swapping or node_state["active_task"] != "Idle":
                        response = {
                            "status": "REJECTED_BUSY",
                            "task": node_state["active_task"],
                        }
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                    elif not str(load_node_settings().get("srs_install_folder") or "").strip():
                        conn.send((json.dumps({
                            "status": "ERROR",
                            "message": "SRS install folder is not set on this Node.",
                        }) + "\n").encode("utf-8"))
                        conn.close()
                    else:
                        response = {"status": "OK_STARTING"}
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                        append_activity_log("[REMOTE] Control Panel requested SRS Server update.")
                        threading.Thread(target=execute_srs_update_pipeline, daemon=True).start()
                elif command == "TRIGGER_DCS_UPDATE":
                    if node_state["active_task"] == "Idle":
                        response = {"status": "OK_STARTING"}
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                        append_activity_log(" Remote signal authorized! Starting 🚀 update...")
                        threading.Thread(target=execute_deployment_pipeline, daemon=True).start()
                    else:
                        response = {"status": "REJECTED_BUSY", "task": node_state["active_task"]}
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                elif command == "RESTART_DCS":
                    if is_swapping or node_state["active_task"] != "Idle":
                        response = {
                            "status": "REJECTED_BUSY",
                            "task": node_state["active_task"],
                        }
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                    elif not can_auto_restart_dcs():
                        response = {"status": "REJECTED_LIMIT"}
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                        append_activity_log(
                            "[REMOTE] Discord bot asked for DCS restart, but the hourly limit is reached."
                        )
                    else:
                        node_state["active_task"] = "Restarting DCS"
                        response = {"status": "OK_STARTING"}
                        conn.send((json.dumps(response) + "\n").encode("utf-8"))
                        conn.close()
                        append_activity_log("[REMOTE] Discord bot requested DCS restart.")
                        threading.Thread(
                            target=execute_dcs_restart,
                            kwargs={
                                "node_port": load_node_settings().get("network_port", "1015"),
                                "source": "remote",
                            },
                            daemon=True,
                        ).start()
                elif command == "EXIT_NODE":
                    # Local takeover only: require loopback when auth is enabled
                    if live_token and addr[0] not in ("127.0.0.1", "::1"):
                        conn.send((json.dumps({"status": "UNAUTHORIZED"}) + "\n").encode("utf-8"))
                        conn.close()
                        continue
                    append_activity_log(" Remote exit commanded by newer takeover 🛑 node instance.")
                    conn.send(b"ACK_EXIT\n")
                    conn.close()
                    is_listening = False
                    if server_socket:
                        server_socket.close()
                    if tray_icon:
                        tray_icon.stop()
                    root.after(0, root.destroy)
                    sys.exit(0)
                else:
                    conn.send(b"UNKNOWN_COMMAND\n")
                    conn.close()
            except Exception as inner_err:
                logging.error(f"Active transaction broken mid-stream: {inner_err}")
                try:
                    conn.close()
                except Exception:
                    pass
                continue
        except Exception as outer_err:
            logging.error(f"Critical socket acceptor loop crash: {outer_err}")
            break



# =========================================================================
# BLOCK 4 OF 6: USER INTERACTION MESSAGES, FILE BROWSER & SYSTEM TRAY ENGINE
# =========================================================================
def start_or_restart_listener(port_str=None, bind_address=None):
    global server_socket, is_listening, listener_thread
    config = load_node_settings()
    if port_str is None:
        port_str = config.get("network_port", "1015")
    if bind_address is None:
        bind_address = config.get("bind_address", "0.0.0.0")
    try:
        new_port = int(str(port_str).strip())
    except ValueError:
        return

    if is_listening and server_socket:
        is_listening = False
        try:
            server_socket.close()
        except Exception:
            pass
        append_activity_log(" Previous listener terminated. 🛑")

    time.sleep(0.3)
    listener_thread = threading.Thread(
        target=network_socket_listener,
        args=(new_port, str(bind_address).strip() or "0.0.0.0"),
        daemon=True,
    )
    listener_thread.start()

def browse_dcs_folder(entry_field):
    folder = filedialog.askdirectory(title="Select DCS Installation Folder (Main Root)")
    if folder: 
        entry_field.delete(0, tk.END)
        entry_field.insert(0, os.path.normpath(folder))


def browse_srs_folder(entry_field):
    folder = filedialog.askdirectory(title="Select SRS Installation Folder (root, Server is a subfolder)")
    if folder:
        entry_field.delete(0, tk.END)
        entry_field.insert(0, os.path.normpath(folder))

def append_activity_log(text):
    """Write to file/console always; update the Tk log from the GUI thread only."""
    logging.info(text)

    def _write_to_widget():
        try:
            if "log_window" in globals() and log_window.winfo_exists():
                log_window.insert(tk.END, text + "\n")
                log_window.see(tk.END)
        except Exception:
            pass

    try:
        if "root" in globals() and root.winfo_exists():
            root.after(0, _write_to_widget)
            return
    except Exception:
        pass
    _write_to_widget()

def trigger_local_update():
    if messagebox.askyesno("Confirm", "Update DCS on this machine now?"):
        threading.Thread(target=execute_deployment_pipeline, daemon=True).start()


def trigger_local_srs_update():
    cfg = load_node_settings()
    if not str(cfg.get("srs_install_folder") or "").strip():
        messagebox.showwarning(
            "SRS",
            "Set the SRS installation folder in Settings first.\n"
            "The Server folder from the GitHub zip is extracted into that folder\\Server.",
        )
        return
    if node_state["active_task"] != "Idle":
        messagebox.showwarning("Busy", f"Node is busy ({node_state['active_task']}).")
        return
    if messagebox.askyesno("Confirm", "Update SRS Server on this machine now?"):
        threading.Thread(target=execute_srs_update_pipeline, daemon=True).start()

def force_github_update_check():
    """NEW: Forces an immediate GitHub update check from the settings panel."""
    global last_github_node_check_timestamp
    if is_swapping:
        messagebox.showwarning("Busy", "A node update sequence is already active.")
        return
    
    append_activity_log("\n[SYSTEM] Manual GitHub update check requested by operator...")
    # Reset the loop timestamp so the background thread alignment stays correct
    last_github_node_check_timestamp = time.time()
    
    # Run the check in a separate background thread to keep the UI perfectly fluid
    threading.Thread(target=check_for_github_node_updates_silent, daemon=True).start()
    messagebox.showinfo("Update Check", "GitHub update check initiated in the background.\nCheck the activity log for details.")

def show_main_frame(): 
    frame_settings.pack_forget()
    frame_main.pack(fill="both", expand=True)

def show_settings_frame():
    try:
        frame_main.pack_forget()
        cfg = load_node_settings()
        ent_dcs.delete(0, tk.END)
        ent_dcs.insert(0, str(cfg.get("dcs_main_folder", r"D:\DCS")))
        ent_srs.delete(0, tk.END)
        ent_srs.insert(0, str(cfg.get("srs_install_folder", "")))
        ent_port.delete(0, tk.END)
        ent_port.insert(0, str(cfg.get("network_port", "1015")))
        ent_bind.delete(0, tk.END)
        ent_bind.insert(0, str(cfg.get("bind_address", "0.0.0.0")))
        ent_auth.delete(0, tk.END)
        ent_auth.insert(0, str(cfg.get("auth_token", "")))

        saved_seconds = int(cfg.get("github_check_interval", 43200))
        opt_update_var.set(github_interval_label(saved_seconds))
        
        v_preserve.set(bool(cfg.get("preserve_mission_scripting", True)))
        v_reboot.set(bool(cfg.get("reboot_after_deployment", True)))
        v_watchdog.set(bool(cfg.get("watchdog_enabled", True)))
        v_auto_restart.set(bool(cfg.get("auto_restart_dcs", True)))
        frame_settings.pack(fill="both", expand=True, padx=15, pady=10)
    except Exception as err:
        logging.error(f"UI settings frame assembly crashed: {err}")
        messagebox.showerror("UI Error", f"Settings crash prevented. Log: {err}")
        show_main_frame()

def save_settings_to_file():
    incoming = {
        "dcs_main_folder": ent_dcs.get(),
        "srs_install_folder": ent_srs.get().strip(),
        "preserve_mission_scripting": v_preserve.get(),
        "network_port": ent_port.get(),
        "bind_address": ent_bind.get().strip() or "0.0.0.0",
        "auth_token": ent_auth.get().strip(),
        "reboot_after_deployment": v_reboot.get(),
        "github_check_interval": github_interval_seconds(opt_update_var.get()),
        "watchdog_enabled": v_watchdog.get(),
        "auto_restart_dcs": v_auto_restart.get(),
    }
    apply_node_settings(incoming, source="ui")
    show_main_frame()

def create_tray_image():
    img = Image.new('RGB', (64, 64), color='#1C1C1F')
    d = ImageDraw.Draw(img)
    d.rectangle([(8, 8), (56, 56)], fill='#0A84FF')
    return img

def setup_tray_icon():
    menu = pystray.Menu(
        pystray.MenuItem('Show Node Window', lambda icon, item: root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force())), default=True), 
        pystray.MenuItem('Exit Node', lambda icon, item: (icon.stop(), root.after(0, root.destroy)))
    )
    global tray_icon
    try:
        raw = logo_pil_image().resize((64, 64), Image.Resampling.LANCZOS)
        bg = Image.new("RGBA", raw.size, (28, 28, 31, 255))
        img_asset = Image.alpha_composite(bg, raw)
    except Exception:
        img_asset = create_tray_image()
    tray_icon = pystray.Icon("dcs_node", img_asset, "DCS Norway Remote Updater Node", menu)
    threading.Thread(target=tray_icon.run, daemon=True).start()



# =========================================================================
# BLOCK 5 OF 6: LOGO ATTACHMENT, GRAPHICAL ACTIVITY LOGS & CONFIG FRAME GRID
# =========================================================================
root = tk.Tk()
root.title(f"DCS Norway Remote Updater Node (v{CURRENT_NODE_VERSION})")
root.geometry("560x580")
root.configure(bg="#1C1C1F")

root.protocol('WM_DELETE_WINDOW', lambda: root.withdraw())

frame_main = tk.Frame(root, bg="#1C1C1F")
top_bar = tk.Frame(frame_main, bg="#1C1C1F")
top_bar.pack(fill="x", padx=15, pady=(15, 10))

# Left: logo + two-line title (aligned like Control Panel)
left_column = tk.Frame(top_bar, bg="#1C1C1F")
left_column.pack(side="left", fill="y", anchor="w")

header_row = tk.Frame(left_column, bg="#1C1C1F")
header_row.pack(anchor="w")

lbl_logo = tk.Label(header_row, bg="#1C1C1F", bd=0)
lbl_logo.pack(side="left", padx=(0, 12))

title_column = tk.Frame(header_row, bg="#1C1C1F")
title_column.pack(side="left", fill="y")
tk.Frame(title_column, bg="#1C1C1F").pack(expand=True, fill="both")
tk.Label(
    title_column,
    text="DCS Norway",
    font=("Arial", 18, "bold"),
    fg="white",
    bg="#1C1C1F",
).pack(anchor="w")
tk.Label(
    title_column,
    text="Remote Updater Node",
    font=("Arial", 11),
    fg="#8E8E93",
    bg="#1C1C1F",
).pack(anchor="w", pady=(2, 0))
tk.Frame(title_column, bg="#1C1C1F").pack(expand=True, fill="both")

try:
    icon_path = materialize_icon_file(application_path)
    root.iconbitmap(icon_path)
except Exception as e:
    logging.warning("[UI] Window icon failed: %s", e)

try:
    img_raw = logo_pil_image()
    img_scaled = img_raw.resize((88, 88), Image.Resampling.LANCZOS)
    bg = Image.new("RGBA", img_scaled.size, (28, 28, 31, 255))
    img_comp = Image.alpha_composite(bg, img_scaled).convert("RGB")
    img_logo = ImageTk.PhotoImage(img_comp)
    lbl_logo.configure(image=img_logo)
    lbl_logo.image = img_logo
    logging.info(
        "[UI] Embedded brand logo %s (md5=%s)",
        BRAND_ASSET_VERSION,
        BRAND_PNG_MD5,
    )
except Exception as e:
    logging.error("[UI] Failed to load embedded header logo: %s", e)

# Right: Settings on top, green Update DCS Now below
right_column = tk.Frame(top_bar, bg="#1C1C1F")
right_column.pack(side="right", fill="y", anchor="ne")

tk.Button(
    right_column,
    text=" ⚙️ Settings",
    font=("Arial", 9, "bold"),
    bg="#2D2D30",
    fg="white",
    padx=12,
    pady=4,
    command=show_settings_frame,
    relief="flat",
    bd=0,
    highlightthickness=0,
    cursor="hand2",
).pack(anchor="e")

btn_local_update = tk.Button(
    right_column,
    text=" 🚀 UPDATE DCS NOW",
    font=("Arial", 10, "bold"),
    bg="#00912E",
    fg="white",
    activebackground="#00B339",
    activeforeground="white",
    padx=16,
    pady=10,
    command=trigger_local_update,
    relief="flat",
    bd=0,
    highlightthickness=0,
    cursor="hand2",
)
btn_local_update.pack(anchor="e", pady=(12, 0))

btn_local_srs_update = tk.Button(
    right_column,
    text=" 📻 UPDATE SRS NOW",
    font=("Arial", 10, "bold"),
    bg="#0A84FF",
    fg="white",
    activebackground="#409CFF",
    activeforeground="white",
    padx=16,
    pady=10,
    command=trigger_local_srs_update,
    relief="flat",
    bd=0,
    highlightthickness=0,
    cursor="hand2",
)
btn_local_srs_update.pack(anchor="e", pady=(8, 0))

tk.Label(frame_main, text="Activity Log:", font=("Arial", 9), fg="#8E8E93", bg="#1C1C1F").pack(anchor="w", padx=15, pady=(15, 0))
log_window = scrolledtext.ScrolledText(frame_main, width=64, height=12, font=("Consolas", 9), bg="#111112", fg="#30D158", insertbackground="white")
log_window.pack(pady=5, padx=15, fill="both", expand=True)

frame_settings = tk.Frame(root, bg="#1C1C1F")
tk.Label(frame_settings, text="Configuration Panel", font=("Arial", 13, "bold"), fg="white", bg="#1C1C1F").pack(anchor="w", pady=(5,15))

grid_f = tk.Frame(frame_settings, bg="#1C1C1F")
grid_f.pack(fill="x", pady=5)

tk.Label(grid_f, text="DCS Main Folder:", fg="white", bg="#1C1C1F").grid(row=0, column=0, sticky="w", pady=6)
ent_dcs = tk.Entry(grid_f, width=35, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1)
ent_dcs.grid(row=0, column=1, pady=6, padx=5)

tk.Button(grid_f, text="Browse...", command=lambda: browse_dcs_folder(ent_dcs), bg="#3A3A3C", fg="white", relief="flat").grid(row=0, column=2, pady=6, padx=2)

tk.Label(grid_f, text="SRS Install Folder:", fg="white", bg="#1C1C1F").grid(row=1, column=0, sticky="w", pady=6)
ent_srs = tk.Entry(grid_f, width=35, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1)
ent_srs.grid(row=1, column=1, pady=6, padx=5)
tk.Button(grid_f, text="Browse...", command=lambda: browse_srs_folder(ent_srs), bg="#3A3A3C", fg="white", relief="flat").grid(row=1, column=2, pady=6, padx=2)
tk.Label(grid_f, text="Server is extracted into \\Server", fg="#8E8E93", bg="#1C1C1F", font=("Arial", 8)).grid(row=1, column=3, sticky="w")

# =========================================================================
# BLOCK 6 OF 6: PORTS, AUTO-UPDATE DROPDOWN, CHECKBOXES & CORE TKINTER MAINLOOP
# =========================================================================
tk.Label(grid_f, text="Listening Port:", fg="white", bg="#1C1C1F").grid(row=2, column=0, sticky="w", pady=6)
ent_port = tk.Entry(grid_f, width=12, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1)
ent_port.grid(row=2, column=1, sticky="w", pady=6, padx=5)

tk.Label(grid_f, text="Bind Address:", fg="white", bg="#1C1C1F").grid(row=3, column=0, sticky="w", pady=6)
ent_bind = tk.Entry(grid_f, width=18, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1)
ent_bind.grid(row=3, column=1, sticky="w", pady=6, padx=5)
tk.Label(grid_f, text="LAN IP (not 0.0.0.0)", fg="#8E8E93", bg="#1C1C1F", font=("Arial", 8)).grid(row=3, column=2, sticky="w")

tk.Label(grid_f, text="Auth Token:", fg="white", bg="#1C1C1F").grid(row=4, column=0, sticky="w", pady=6)
ent_auth = tk.Entry(grid_f, width=28, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1, show="*")
ent_auth.grid(row=4, column=1, columnspan=2, sticky="w", pady=6, padx=5)

tk.Label(frame_settings, text="Check for GitHub app updates:", fg="white", bg="#1C1C1F").pack(anchor="w", pady=(10, 2))

opt_update_var = tk.StringVar()
opt_update_menu = tk.OptionMenu(frame_settings, opt_update_var, "Every 10 minutes", "Every 1 Hour", "Every 12 Hours", "Disabled")
opt_update_menu.configure(bg="#252529", fg="white", activebackground="#252529", activeforeground="white", relief="flat", highlightthickness=0)
opt_update_menu["menu"].configure(bg="#252529", fg="white", activebackground="#0A84FF", activeforeground="white")
opt_update_menu.pack(fill="x", pady=2)

# NEW: Manual override action button configured directly underneath the interval drop-down matrix
btn_force_update = tk.Button(
    frame_settings, 
    text=" 🔄 Check for App Updates Now ", 
    font=("Arial", 9, "bold"), 
    bg="#1A5A99", 
    fg="white", 
    padx=10, 
    pady=5, 
    command=force_github_update_check, 
    relief="flat", 
    cursor="hand2"
)
btn_force_update.pack(anchor="w", pady=(5, 10))

v_preserve = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Preserve current MissionScripting.lua?", variable=v_preserve, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=(5, 5))

v_reboot = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Reboot Windows automatically after update completes", variable=v_reboot, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=5)

v_watchdog = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Watch DCS server health every 5 minutes (process + port)", variable=v_watchdog, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=5)

v_auto_restart = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Auto-restart DCS only after it was previously running", variable=v_auto_restart, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=5)

btn_tray = tk.Frame(frame_settings, bg="#1C1C1F")
btn_tray.pack(pady=15)

tk.Button(btn_tray, text=" 💾 Save & Apply", font=("Arial", 10, "bold"), bg="#1C7430", fg="white", padx=15, command=save_settings_to_file, relief="flat").grid(row=0, column=0, padx=5)
tk.Button(btn_tray, text="Cancel", font=("Arial", 10), bg="#5A6268", fg="white", padx=15, command=show_main_frame, relief="flat").grid(row=0, column=1, padx=5)

show_main_frame()
append_activity_log(
    f"[UI] Embedded brand logo {BRAND_ASSET_VERSION} (md5={BRAND_PNG_MD5[:8]}…)"
)
start_or_restart_listener()
setup_tray_icon()
get_dcs_versions_local()

threading.Thread(target=github_update_monitor_loop, daemon=True).start()
threading.Thread(target=dcs_watchdog_loop, daemon=True).start()
threading.Thread(target=lambda: get_srs_latest_version_cached(allow_fetch=True), daemon=True).start()
refresh_dcs_health_state()

root.after(100, lambda: root.withdraw())
root.mainloop()






