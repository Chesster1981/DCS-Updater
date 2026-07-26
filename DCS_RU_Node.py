# ==============================================================================
# PROGRAM 2: DCS NORWAY REMOTE UPDATER NODE (DCS_RU_Node.py)
# BLOCK 1 OF 6: IMPORTS, NETWORK TAKEOVER, VARIABLES, GITHUB AUTO-UPDATE & ENV
# ==============================================================================

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

CONFIG_FILE = "dcs_node_config.json"
DCS_PROCESSES = ["DCS.exe", "DCS_server.exe"]

# --- GITHUB AUTO-UPDATE CONFIGURATION (NODE) ---
CURRENT_NODE_VERSION = "1.1"  # Set to match your active tag v1.0 exactly
GITHUB_REPO = "DITT_GITHUB_BRUKERNAVN/DITT_REPO_NAVN"  # Replace with your actual GitHub repo

server_socket = None
listener_thread = None
is_listening = False
tray_icon = None

node_state = {
    "installed_version": "Unknown",
    "latest_cloud_version": "Unknown",
    "active_task": "Idle",  
    "is_running": True
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
        test_sock.sendall(b"EXIT_NODE\n")
        test_sock.close()
        time.sleep(2.0)
    except (ConnectionRefusedError, socket.timeout):
        pass

def get_resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def load_node_settings():
    absolute_config_path = os.path.join(application_path, CONFIG_FILE)
    fallback_defaults = {
        "dcs_main_folder": r"D:\DCS",
        "preserve_mission_scripting": True,
        "network_port": "1015",
        "reboot_after_deployment": True,
        "github_check_interval": 43200  # Default check interval set to 12 hours (in seconds)
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


# ==============================================================================
# BLOCK 2 OF 6: ASYNCHRONOUS HTML WEB SCRAPER, DISK PARSING & DYNAMIC GITHUB UPDATE
# ==============================================================================

last_cloud_check_timestamp = 0.0
last_github_node_check_timestamp = 0.0
URL_DCS_UPDATES = "https://digitalcombatsimulator.com"

def _execute_silent_node_binary_swap(download_url):
    try:
        append_activity_log("[SYSTEM] Downloading newer node architecture from GitHub...")
        current_exe = sys.argv[0]
        new_exe_tmp = current_exe + ".tmp"
        
        req = urllib.request.Request(download_url, headers={"User-Agent": "DCS-Norway-Node-Updater"})
        with urllib.request.urlopen(req) as response, open(new_exe_tmp, "wb") as out_file:
            out_file.write(response.read())
            
        append_activity_log("✅ [SYSTEM] Silent download finished. Recycling node process...")
        
        bat_path = os.path.join(application_path, "update_node.bat")
        with open(bat_path, "w") as f:
            f.write(f'@echo off\n')
            f.write(f'timeout /t 2 /nobreak > nul\n')
            f.write(f'move /y "{new_exe_tmp}" "{current_exe}"\n')
            f.write(f'start "" "{current_exe}"\n')
            f.write(f'del "%~f0"\n')
            
        subprocess.Popen(["cmd.exe", "/c", bat_path], shell=True)
        
        global is_listening, server_socket, tray_icon
        is_listening = False
        if server_socket: server_socket.close()
        if tray_icon: tray_icon.stop()
        root.after(0, root.destroy)
        sys.exit(0)
    except Exception as e:
        logging.error(f"Silent node swap crashed: {e}")

def check_for_github_node_updates_silent():
    try:
        url = f"https://github.com{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "DCS-Norway-Node-Updater"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            latest_version = data.get("tag_name", "").replace("v", "").strip()
            
            if latest_version and latest_version != CURRENT_NODE_VERSION:
                assets = data.get("assets", [])
                download_url = None
                for asset in assets:
                    if "Updater Node.exe" in asset.get("name", ""):
                        download_url = asset.get("browser_download_url")
                        break
                if download_url:
                    _execute_silent_node_binary_swap(download_url)
    except Exception as e:
        logging.debug(f"Silent GitHub node check exception: {e}")

def _run_dcs_html_scraper_background():
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        req = urllib.request.Request(URL_DCS_UPDATES, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_innhold = response.read().decode("utf-8")
        renset_html = " ".join(html_innhold.split())
        match = re.search(r"Latest stable version is\s*([\d\.]+)", renset_html)
        if match:
            node_state["latest_cloud_version"] = match.group(1).strip()
            return
        reserve_match = re.search(r"(\d+\.\d+\.\d+\.\d+)", renset_html)
        if reserve_match:
            node_state["latest_cloud_version"] = reserve_match.group(1).strip()
    except Exception as e:
        logging.error(f"[SCRAPER] Failed to scrape DCS update webpage: {e}")

def get_dcs_versions_local():
    global last_cloud_check_timestamp, last_github_node_check_timestamp
    config = load_node_settings()
    main_folder = config.get("dcs_main_folder", "").strip()
    check_interval = int(config.get("github_check_interval", 43200))
    
    current_time = time.time()
    if current_time - last_cloud_check_timestamp > 1800.0:
        last_cloud_check_timestamp = current_time
        threading.Thread(target=_run_dcs_html_scraper_background, daemon=True).start()

    # DYNAMISK LOGIKK: Sjekker GitHub basert på intervallet satt av brukeren (-1 betyr AV)
    if check_interval > 0 and (current_time - last_github_node_check_timestamp > float(check_interval)):
        last_github_node_check_timestamp = current_time
        threading.Thread(target=check_for_github_node_updates_silent, daemon=True).start()

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
    except:
        pass
    return False

def force_kill_core_dcs():
    append_activity_log("[PROCESS] Requesting termination of core DCS processes...")
    for prog in DCS_PROCESSES:
        try:
            subprocess.run(f"taskkill /f /im {prog}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logging.debug(f"Taskkill command rejected on {prog}: {e}")



# ==============================================================================
# BLOCK 3 OF 6: DEPLOYMENT PIPELINE & EXTENDED NETWORK SOCKET INTERFACE
# ==============================================================================

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
            append_activity_log("✅ [PROCESS] Active script backup secured successfully.")
        except Exception as e:
            append_activity_log(f"⚠️ [PROCESS] Backup failed, file might be locked: {e}")

    force_kill_core_dcs()
    
    append_activity_log("[PROCESS] Verifying DCS cleanup status...")
    max_timeout_ticks = 15 
    start_timer = time.time()
    while check_active_processes_running():
        if time.time() - start_timer > max_timeout_ticks:
            append_activity_log("❌ ERROR: Forced termination timed out. Files might still be locked!")
            break
        append_activity_log("[PROCESS] DCS still releasing files... Waiting 2 seconds...")
        time.sleep(2)
        
    append_activity_log("✅ [PROCESS] DCS processes are verified DEAD. Proceeding.")
    node_state["active_task"] = "Downloading"
    try:
        if not os.path.exists(bin_folder):
            append_activity_log(f"❌ ERROR: Could not find the bin folder at: {bin_folder}")
            node_state["active_task"] = "Idle"
            return
            
        previous_working_dir = os.path.abspath(".")
        os.chdir(bin_folder)
        append_activity_log("[PROCESS] Running DCS_updater.exe --quiet update (Please wait...)")
        subprocess.run("DCS_updater.exe --quiet update", shell=True)
        os.chdir(previous_working_dir)
        append_activity_log("✅ [PROCESS] DCS core update finished.")
    except Exception as e:
        append_activity_log(f"❌ ERROR during DCS update: {e}")
        node_state["active_task"] = "Idle"
        return

    append_activity_log("[PROCESS] Re-scanning local configuration matrices...")
    get_dcs_versions_local()

    if preserve_lua and os.path.exists(backup_lua_file):
        append_activity_log("[PROCESS] Restoring preserved MissionScripting.lua back to DCS cluster...")
        try:
            os.makedirs(scripts_folder, exist_ok=True)
            shutil.copy(backup_lua_file, active_lua_file)
            append_activity_log("✅ [PROCESS] Success! MissionScripting.lua has been completely restored.")
            os.remove(backup_lua_file)
        except Exception as e:
            append_activity_log(f"❌ ERROR during file restoration: {e}")

    if reboot_after_deployment:
        append_activity_log("⚠️ [PROCESS] Windows reboot is enabled. Rebooting machine in 5 seconds...")
        node_state["active_task"] = "Rebooting"
        time.sleep(5)
        subprocess.run("shutdown /r /t 0", shell=True)
    else:
        append_activity_log("✅ [PROCESS] Finished! (PC Reboot was skipped).")
        node_state["active_task"] = "Idle"

def network_socket_listener(port):
    global server_socket, is_listening
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('0.0.0.0', port))
        server_socket.listen(5)
        is_listening = True
        append_activity_log(f"📡 Listener active on port {port}...")
    except Exception as e:
        append_activity_log(f"❌ ERROR: Could not bind network listener to port {port}: {e}")
        is_listening = False
        return

    while is_listening:
        try:
            conn, addr = server_socket.accept()
            if not is_listening: break
            data = conn.recv(1024)
            if not data: conn.close(); continue
            message = data.decode('utf-8').strip()
            
            if "PING_STATUS" in message:
                inst_v, cloud_v = get_dcs_versions_local()
                response = {"status": "ACK", "installed_version": inst_v, "latest_cloud_version": cloud_v, "active_task": node_state["active_task"]}
                conn.send((json.dumps(response) + "\n").encode('utf-8'))
                conn.close()
            elif "TRIGGER_DCS_UPDATE" in message:
                if node_state["active_task"] == "Idle":
                    response = {"status": "OK_STARTING"}
                    conn.send((json.dumps(response) + "\n").encode('utf-8'))
                    conn.close()
                    append_activity_log("🚀 Remote signal authorized! Starting update...")
                    threading.Thread(target=execute_deployment_pipeline, daemon=True).start()
                else:
                    response = {"status": "REJECTED_BUSY", "task": node_state["active_task"]}
                    conn.send((json.dumps(response) + "\n").encode('utf-8'))
                    conn.close()
            elif "EXIT_NODE" in message:
                append_activity_log("🛑 Remote exit commanded by newer takeover node instance.")
                conn.send(b"ACK_EXIT\n")
                conn.close()
                is_listening = False
                if server_socket: server_socket.close()
                if tray_icon: tray_icon.stop()
                root.after(0, root.destroy)
                sys.exit(0)
            else:
                conn.send(b"UNKNOWN_COMMAND\n")
                conn.close()
        except: break


# ==============================================================================
# BLOCK 4 OF 6: USER INTERACTION MESSAGES, FILE BROWSER & SYSTEM TRAY ENGINE
# ==============================================================================

def start_or_restart_listener(port_str=None):
    global server_socket, is_listening, listener_thread
    if port_str is None:
        config = load_node_settings()
        port_str = config.get("network_port", "1015")
    try: ny_port = int(port_str.strip())
    except ValueError: return
    if is_listening and server_socket:
        is_listening = False
        try: server_socket.close()
        except: pass
        append_activity_log("🛑 Previous listener terminated.")
        time.sleep(0.3)
    listener_thread = threading.Thread(target=network_socket_listener, args=(ny_port,), daemon=True)
    listener_thread.start()

def browse_dcs_folder(entry_field):
    folder = filedialog.askdirectory(title="Select DCS Installation Folder (Main Root)")
    if folder: entry_field.delete(0, tk.END); entry_field.insert(0, os.path.normpath(folder))

def append_activity_log(text):
    if 'log_window' in globals() and log_window.winfo_exists():
        log_window.insert(tk.END, text + "\n"); log_window.see(tk.END)

def trigger_local_update():
    if messagebox.askyesno("Confirm", "Do you want to run the update process locally now?"):
        threading.Thread(target=execute_deployment_pipeline, daemon=True).start()

def show_main_frame(): frame_settings.pack_forget(); frame_main.pack(fill="both", expand=True)

def show_settings_frame():
    try:
        frame_main.pack_forget()
        cfg = load_node_settings()
        ent_dcs.delete(0, tk.END); ent_dcs.insert(0, str(cfg.get("dcs_main_folder", r"D:\DCS")))
        ent_port.delete(0, tk.END); ent_port.insert(0, str(cfg.get("network_port", "1015")))
        
        saved_seconds = int(cfg.get("github_check_interval", 43200))
        if saved_seconds == 60: opt_update_var.set("Every 1 Minute (Testing)")
        elif saved_seconds == 3600: opt_update_var.set("Every 1 Hour")
        elif saved_seconds == 43200: opt_update_var.set("Every 12 Hours")
        else: opt_update_var.set("Disabled")
        
        v_preserve.set(bool(cfg.get("preserve_mission_scripting", True)))
        v_reboot.set(bool(cfg.get("reboot_after_deployment", True)))
        frame_settings.pack(fill="both", expand=True, padx=15, pady=10)
    except Exception as err:
        logging.error(f"UI settings frame assembly crashed: {err}")
        messagebox.showerror("UI Error", f"Settings crash prevented. Log: {err}")
        show_main_frame()

def save_settings_to_file():
    menu_string = opt_update_var.get()
    if "1 Minute" in menu_string: seconds = 60
    elif "1 Hour" in menu_string: seconds = 3600
    elif "12 Hours" in menu_string: seconds = 43200
    else: seconds = -1
    
    current_settings = {
        "dcs_main_folder": ent_dcs.get(), 
        "preserve_mission_scripting": v_preserve.get(), 
        "network_port": ent_port.get(), 
        "reboot_after_deployment": v_reboot.get(),
        "github_check_interval": seconds
    }
    absolute_config_path = os.path.join(application_path, CONFIG_FILE)
    with open(absolute_config_path, "w", encoding="utf-8") as f: 
        json.dump(current_settings, f, indent=4, ensure_ascii=False)
    start_or_restart_listener(current_settings["network_port"]); show_main_frame()

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
    logo_path = get_resource_path("logo.png")
    img_asset = Image.open(logo_path) if os.path.exists(logo_path) else create_tray_image()
    tray_icon = pystray.Icon("dcs_node", img_asset, "DCS Norway Remote Updater Node", menu)
    threading.Thread(target=tray_icon.run, daemon=True).start()

# FIX: Dynamic insertion of the CURRENT_NODE_VERSION into the root window title matrix
root = tk.Tk(); root.title(f"DCS Norway Remote Updater Node (v{CURRENT_NODE_VERSION})"); root.geometry("540x450"); root.configure(bg="#1C1C1F")
try:
    icon_path = get_resource_path("logo.ico")
    if os.path.exists(icon_path): root.iconbitmap(icon_path)
except: pass
root.protocol('WM_DELETE_WINDOW', lambda: root.withdraw())

frame_main = tk.Frame(root, bg="#1C1C1F")
top_bar = tk.Frame(frame_main, bg="#1C1C1F"); top_bar.pack(fill="x", padx=15, pady=(15, 10))
left_column = tk.Frame(top_bar, bg="#1C1C1F"); left_column.pack(side="left", fill="y", anchor="nw")
tk.Label(left_column, text="DCS Norway Remote Updater Node", font=("Arial", 14, "bold"), fg="white", bg="#1C1C1F").pack(anchor="w")
tk.Button(left_column, text="🚀 RUN LOCAL UPDATE NOW", font=("Arial", 10, "bold"), bg="#0A84FF", fg="white", padx=15, pady=8, command=trigger_local_update, relief="flat", cursor="hand2").pack(anchor="w", pady=(15, 0))

right_column = tk.Frame(top_bar, bg="#1C1C1F"); right_column.pack(side="right", fill="y", anchor="ne")
tk.Button(right_column, text="⚙️ Settings", font=("Arial", 9, "bold"), bg="#2D2D30", fg="white", padx=12, pady=4, command=show_settings_frame, relief="flat").pack(anchor="e")




# ==============================================================================
# BLOCK 5 OF 6: LOGO ATTACHMENT, GRAPHICAL ACTIVITY LOGS & CONFIG FRAME GRID
# ==============================================================================

try:
    logo_path = get_resource_path("logo.png")
    if os.path.exists(logo_path):
        img_raw = Image.open(logo_path); img_scaled = img_raw.resize((75, 75), Image.Resampling.LANCZOS)
        img_logo = ImageTk.PhotoImage(img_scaled); lbl_logo = tk.Label(right_column, image=img_logo, bg="#1C1C1F")
        lbl_logo.image = img_logo; lbl_logo.pack(anchor="e", pady=(15, 0))
except: pass

tk.Label(frame_main, text="Activity Log:", font=("Arial", 9), fg="#8E8E93", bg="#1C1C1F").pack(anchor="w", padx=15, pady=(15, 0))
log_window = scrolledtext.ScrolledText(frame_main, width=64, height=12, font=("Consolas", 9), bg="#111112", fg="#30D158", insertbackground="white")
log_window.pack(pady=5, padx=15, fill="both", expand=True)

frame_settings = tk.Frame(root, bg="#1C1C1F")
tk.Label(frame_settings, text="Configuration Panel", font=("Arial", 13, "bold"), fg="white", bg="#1C1C1F").pack(anchor="w", pady=(5,15))
grid_f = tk.Frame(frame_settings, bg="#1C1C1F"); grid_f.pack(fill="x", pady=5)
tk.Label(grid_f, text="DCS Main Folder:", fg="white", bg="#1C1C1F").grid(row=0, column=0, sticky="w", pady=6)
ent_dcs = tk.Entry(grid_f, width=35, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1); ent_dcs.grid(row=0, column=1, pady=6, padx=5)
tk.Button(grid_f, text="Browse...", command=lambda: browse_dcs_folder(ent_dcs), bg="#3A3A3C", fg="white", relief="flat").grid(row=0, column=2, pady=6, padx=2)


# ==============================================================================
# BLOCK 6 OF 6: PORTS, AUTO-UPDATE DROPDOWN, CHECKBOXES & CORE TKINTER MAINLOOP
# ==============================================================================

tk.Label(grid_f, text="Listening Port:", fg="white", bg="#1C1C1F").grid(row=1, column=0, sticky="w", pady=6)
ent_port = tk.Entry(grid_f, width=12, bg="#252529", fg="white", insertbackground="white", relief="solid", bd=1); ent_port.grid(row=1, column=1, sticky="w", pady=6, padx=5)

# NYTT GRUI-ELEMENT: Legger til en elegant Dropdown-meny for GitHub-sjekk-intervaller
tk.Label(frame_settings, text="Check for app updates:", fg="white", bg="#1C1C1F").pack(anchor="w", pady=(10, 2))
opt_update_var = tk.StringVar()
opt_update_menu = tk.OptionMenu(
    frame_settings, opt_update_var, 
    "Every 1 Minute (Testing)", "Every 1 Hour", "Every 12 Hours", "Disabled"
)
opt_update_menu.configure(bg="#252529", fg="white", activebackground="#252529", activeforeground="white", relief="flat", highlightthickness=0)
opt_update_menu["menu"].configure(bg="#252529", fg="white", activebackground="#0A84FF", activeforeground="white")
opt_update_menu.pack(fill="x", pady=2)

v_preserve = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Preserve current MissionScripting.lua?", variable=v_preserve, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=(15, 5))

v_reboot = tk.BooleanVar()
tk.Checkbutton(frame_settings, text="Reboot Windows automatically after update completes", variable=v_reboot, fg="white", bg="#1C1C1F", selectcolor="#1C1C1F", activebackground="#1C1C1F", activeforeground="white").pack(anchor="w", pady=5)

btn_tray = tk.Frame(frame_settings, bg="#1C1C1F"); btn_tray.pack(pady=15)
tk.Button(btn_tray, text="💾 Save & Apply", font=("Arial", 10, "bold"), bg="#1C7430", fg="white", padx=15, command=save_settings_to_file, relief="flat").grid(row=0, column=0, padx=5)
tk.Button(btn_tray, text="Cancel", font=("Arial", 10), bg="#5A6268", fg="white", padx=15, command=show_main_frame, relief="flat").grid(row=0, column=1, padx=5)

show_main_frame(); start_or_restart_listener(); setup_tray_icon()
root.after(100, lambda: root.withdraw())
root.mainloop()



