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
import re
import shutil
import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog
from datetime import datetime
from PIL import Image, ImageDraw, ImageTk
import pystray
import urllib.request
import urllib.error

from dcs_ru_common import parse_authenticated_command, scrape_dcs_latest_version, wrap_command
from brand_assets import (
    BRAND_ASSET_VERSION,
    BRAND_PNG_MD5,
    logo_pil_image,
    materialize_icon_file,
)

CONFIG_FILE = "dcs_node_config.json"

# --- GLOBAL URL & GITHUB CONFIGURATION (NODE) ---
CURRENT_NODE_VERSION = "2.1.6"
GITHUB_REPO = "Chesster1981/DCS-Updater"
URL_GITHUB_API = "https://api.github.com/repos/"

server_socket = None
listener_thread = None
is_listening = False
tray_icon = None

DCS_SERVER_PROCESS = "DCS_server.exe"
DCS_PROCESSES = ["DCS.exe", "DCS_server.exe"]
WATCHDOG_DEFAULT_INTERVAL = 300  # 5 minutes

node_state = {
    "installed_version": "Unknown",
    "latest_cloud_version": "Unknown",
    "active_task": "Idle",
    "is_running": True,
    "dcs_running": False,
}

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
    fallback_defaults = {
        "dcs_main_folder": r"D:\DCS",
        "preserve_mission_scripting": True,
        "network_port": "1015",
        "bind_address": "0.0.0.0",
        "auth_token": "",
        "reboot_after_deployment": True,
        "github_check_interval": 43200,
        "watchdog_enabled": True,
        "watchdog_interval_seconds": WATCHDOG_DEFAULT_INTERVAL,
        "auto_restart_dcs": True,
    }
    if os.path.exists(absolute_config_path):
        try:
            with open(absolute_config_path, "r", encoding="utf-8") as f:
                loaded_data = json.load(f)
                if isinstance(loaded_data, dict):
                    for key, val in fallback_defaults.items():
                        if key not in loaded_data:
                            loaded_data[key] = val
                    return loaded_data
        except Exception as e:
            logging.error(f"Failed to read config layout: {e}")
    return fallback_defaults

handle_single_instance_takeover()

# =========================================================================
# BLOCK 2 OF 6: ASYNCHRONOUS HTML WEB SCRAPER, DISK PARSING & SEPARATE AUTO-SWAP PROCESS
# =========================================================================
last_cloud_check_timestamp = 0.0
last_github_node_check_timestamp = 0.0
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

        subprocess.Popen(f'cmd.exe /c start /b "" "{bat_path}"', shell=True)
        
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
        
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/vnd.github.v3+json"
        })
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
        output = subprocess.check_output("tasklist", shell=True, text=True, errors="ignore")
        for prog in DCS_PROCESSES:
            if prog.lower() in output.lower():
                return True
    except Exception:
        pass
    return False


def is_dcs_server_running():
    """True when the dedicated DCS World server process is alive."""
    try:
        output = subprocess.check_output("tasklist", shell=True, text=True, errors="ignore")
        return DCS_SERVER_PROCESS.lower() in output.lower()
    except Exception:
        return False


def refresh_dcs_running_state():
    running = is_dcs_server_running()
    node_state["dcs_running"] = running
    return running


def start_dcs_server_process():
    """Launch DCS_server.exe from the configured DCS bin folder."""
    config = load_node_settings()
    main_folder = config.get("dcs_main_folder", "").strip()
    bin_folder = os.path.join(main_folder, "bin")
    exe_path = os.path.join(bin_folder, DCS_SERVER_PROCESS)

    if not os.path.exists(exe_path):
        append_activity_log(f" [WATCHDOG] ERROR: {DCS_SERVER_PROCESS} not found at ❌ {exe_path}")
        return False

    try:
        append_activity_log(f"[WATCHDOG] Starting {DCS_SERVER_PROCESS} from {bin_folder}...")
        ps_cmd = (
            f"Start-Process -FilePath '{exe_path}' "
            f"-WorkingDirectory '{bin_folder}'"
        )
        subprocess.Popen(
            ["powershell", "-Command", ps_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        append_activity_log(f" [WATCHDOG] Failed to start DCS server: ❌ {e}")
        logging.error("DCS server restart failed: %s", e)
        return False


def dcs_watchdog_loop():
    """Every N seconds: verify DCS_server.exe; auto-restart if crashed/stopped."""
    time.sleep(30)  # allow boot / first login before first check
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

            if not enabled:
                time.sleep(interval)
                continue

            if node_state["active_task"] not in ("Idle",):
                append_activity_log(
                    f"[WATCHDOG] Skipping check — node busy ({node_state['active_task']})."
                )
                time.sleep(interval)
                continue

            running = refresh_dcs_running_state()
            if running:
                append_activity_log(f"[WATCHDOG] {DCS_SERVER_PROCESS} is running ✅")
            else:
                append_activity_log(f"[WATCHDOG] {DCS_SERVER_PROCESS} is NOT running.")
                if bool(cfg.get("auto_restart_dcs", True)):
                    node_state["active_task"] = "Restarting DCS"
                    started = start_dcs_server_process()
                    if started:
                        time.sleep(15)
                        if refresh_dcs_running_state():
                            append_activity_log("[WATCHDOG] DCS server restart verified ✅")
                        else:
                            append_activity_log(
                                "[WATCHDOG] Restart launched but process not detected yet ⚠️"
                            )
                    node_state["active_task"] = "Idle"
                else:
                    append_activity_log("[WATCHDOG] Auto-restart disabled in settings.")

            time.sleep(interval)
        except Exception as e:
            logging.error("Watchdog loop error: %s", e)
            time.sleep(60)


def force_kill_core_dcs():
    append_activity_log("[PROCESS] Requesting termination of core DCS processes...")
    for prog in DCS_PROCESSES:
        try:
            subprocess.run(f"taskkill /f /im {prog}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logging.debug(f"Taskkill command rejected on {prog}: {e}")


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
        
        subprocess.run(["powershell", "-Command", ps_cmd], shell=True)
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
        subprocess.run("shutdown /r /t 0", shell=True)
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
        auth_note = "auth ON" if auth_token else "auth OFF (set auth_token in settings)"
        append_activity_log(f" Listener active on {bind_host}:{port} ({auth_note})...")
        if bind_host == "0.0.0.0":
            append_activity_log(" [SECURITY] Bound to all interfaces. Prefer a LAN IP or firewall lock-down.")
    except Exception as e:
        append_activity_log(f" ERROR: Could not bind network listener to ❌ {bind_host}:{port}: {e}")
        is_listening = False
        return

    while is_listening:
        try:
            conn, addr = server_socket.accept()
            if not is_listening:
                break

            try:
                # Reload token so settings changes apply without full restart of process
                live_token = str(load_node_settings().get("auth_token", "")).strip()
                data = conn.recv(1024)
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
                    dcs_alive = refresh_dcs_running_state()
                    response = {
                        "status": "ACK",
                        "installed_version": inst_v,
                        "latest_cloud_version": cloud_v,
                        "active_task": node_state["active_task"],
                        "node_version": CURRENT_NODE_VERSION,
                        "dcs_running": dcs_alive,
                    }
                    conn.send((json.dumps(response) + "\n").encode("utf-8"))
                    conn.close()
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

def append_activity_log(text):
    if 'log_window' in globals() and log_window.winfo_exists():
        log_window.insert(tk.END, text + "\n")
        log_window.see(tk.END)

def trigger_local_update():
    if messagebox.askyesno("Confirm", "Do you want to run the update process locally now?"):
        threading.Thread(target=execute_deployment_pipeline, daemon=True).start()

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
        ent_port.delete(0, tk.END)
        ent_port.insert(0, str(cfg.get("network_port", "1015")))
        ent_bind.delete(0, tk.END)
        ent_bind.insert(0, str(cfg.get("bind_address", "0.0.0.0")))
        ent_auth.delete(0, tk.END)
        ent_auth.insert(0, str(cfg.get("auth_token", "")))

        saved_seconds = int(cfg.get("github_check_interval", 43200))
        if saved_seconds == 600: opt_update_var.set("Every 10 minutes")
        elif saved_seconds == 3600: opt_update_var.set("Every 1 Hour")
        elif saved_seconds == 43200: opt_update_var.set("Every 12 Hours")
        else: opt_update_var.set("Disabled")
        
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
    menu_string = opt_update_var.get()
    if "10 minutes" in menu_string: seconds = 600
    elif "1 Hour" in menu_string: seconds = 3600
    elif "12 Hours" in menu_string: seconds = 43200
    else: seconds = -1
    
    current_settings = {
        "dcs_main_folder": ent_dcs.get(),
        "preserve_mission_scripting": v_preserve.get(),
        "network_port": ent_port.get(),
        "bind_address": ent_bind.get().strip() or "0.0.0.0",
        "auth_token": ent_auth.get().strip(),
        "reboot_after_deployment": v_reboot.get(),
        "github_check_interval": seconds,
        "watchdog_enabled": v_watchdog.get(),
        "watchdog_interval_seconds": WATCHDOG_DEFAULT_INTERVAL,
        "auto_restart_dcs": v_auto_restart.get(),
    }
    absolute_config_path = os.path.join(application_path, CONFIG_FILE)
    with open(absolute_config_path, "w", encoding="utf-8") as f:
        json.dump(current_settings, f, indent=4, ensure_ascii=False)

    start_or_restart_listener(current_settings["network_port"], current_settings["bind_address"])
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

# Right: Settings on top, green Run Local Update below
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
    text=" 🚀 RUN LOCAL UPDATE NOW",
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

# =========================================================================
# BLOCK 6 OF 6: PORTS, AUTO-UPDATE DROPDOWN, CHECKBOXES & CORE TKINTER MAINLOOP
# =========================================================================
tk.Label(grid_f, text="Listening Port:", fg="white", bg="#1C1C1F").grid(row=1, column=0, sticky="w", pady=6)
ent_port = tk.Entry(grid_f, width=12, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1)
ent_port.grid(row=1, column=1, sticky="w", pady=6, padx=5)

tk.Label(grid_f, text="Bind Address:", fg="white", bg="#1C1C1F").grid(row=2, column=0, sticky="w", pady=6)
ent_bind = tk.Entry(grid_f, width=18, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1)
ent_bind.grid(row=2, column=1, sticky="w", pady=6, padx=5)
tk.Label(grid_f, text="LAN IP (not 0.0.0.0)", fg="#8E8E93", bg="#1C1C1F", font=("Arial", 8)).grid(row=2, column=2, sticky="w")

tk.Label(grid_f, text="Auth Token:", fg="white", bg="#1C1C1F").grid(row=3, column=0, sticky="w", pady=6)
ent_auth = tk.Entry(grid_f, width=28, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1, show="*")
ent_auth.grid(row=3, column=1, columnspan=2, sticky="w", pady=6, padx=5)

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
tk.Checkbutton(frame_settings, text="Watch DCS_server.exe every 5 minutes", variable=v_watchdog, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=5)

v_auto_restart = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Auto-restart DCS server if it stopped/crashed", variable=v_auto_restart, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=5)

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
refresh_dcs_running_state()

root.after(100, lambda: root.withdraw())
root.mainloop()






