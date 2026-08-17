# ==============================================================================
# PROGRAM 1: DCS NORWAY CLUSTER CONTROL PANEL (DCS_RU_Control.py)
# PART 1 OF 5: INITIAL LIBRARY IMPORTS, THEME COLOR STYLES & CONFIG LIFE-CYCLES
# ==============================================================================
import os
import sys
import time
import socket
import threading
import json
import logging
from datetime import datetime

from PySide6.QtCore import Qt, QObject, Signal, Slot, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QPushButton, QLabel, QLineEdit,
    QTextEdit, QHeaderView, QMessageBox, QFrame, QCheckBox, QSizePolicy, QStyle
)
from PySide6.QtGui import QFont, QPixmap, QIcon

from brand_assets import (
    BRAND_ASSET_VERSION,
    BRAND_PNG_MD5,
    logo_png_bytes,
    materialize_icon_file,
)

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

from dcs_ru_common import (
    load_master_config,
    save_master_config,
    wrap_command,
    scrape_dcs_latest_version,
    github_api_headers,
)

CONTROL_PANEL_VERSION = "2.1.38"
GITHUB_REPO = "Chesster1981/DCS-Updater"
URL_GITHUB_API = "https://api.github.com/repos/"
TABLE_MAX_VISIBLE_ROWS = 10
TABLE_ROW_HEIGHT = 32
WINDOW_MAX_SCREEN_WIDTH_FRACTION = 0.5
WINDOW_MAX_SCREEN_HEIGHT_FRACTION = 0.75
CONTROL_PANEL_STARTUP_UPDATE_DELAY_MS = 2500
CONTROL_PANEL_UPDATE_INTERVAL_MS = 60 * 60 * 1000

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("dcs_control_panel.log", encoding="utf-8")
    ]
)

DEFAULT_TCP_PORT = 1015
SOCKET_TIMEOUT = 4.0
PING_INTERVAL = 10.0
DEPLOY_CHECK_INTERVAL = 3.0
CONFIG_FILE = "master_config.json"

STYLE_BG_DARK      = "#1C1C1F" 
STYLE_BG_CELL      = "#252529" 
STYLE_TEXT_WHITE   = "#E5E5EA" 
STYLE_TEXT_MUTED   = "#8E8E93" 

STYLE_STATUS_GREEN = "#248A3D" 
STYLE_STATUS_RED   = "#B33A3A" 
STYLE_STATUS_WARN  = "#D9A71E" 
STYLE_ACCENT_BLUE  = "#007AFF" 

STYLE_BTN_ADD      = "#195C2E" 
STYLE_BTN_EDIT     = "#1A5A99" 
STYLE_BTN_REMOVE   = "#8A2B2B" 
STYLE_BTN_DEPLOY   = "#00912E" 

STYLE_BTN_ADD_HOVER    = "#227C3F" 
STYLE_BTN_EDIT_HOVER   = "#247ACC" 
STYLE_BTN_REMOVE_HOVER = "#A83636" 
STYLE_BTN_DEPLOY_HOVER = "#00B339" 
# ==============================================================================
# PART 2 OF 5: NETWORK INFRASTRUCTURE, ASYNCHRONOUS STATUS ENGINE & SIGNALS
# ==============================================================================
class ClusterSignals(QObject):
    node_updated = Signal(str, str, str, str, str)
    cloud_version_updated = Signal(str)
    append_log = Signal(str)
    deployment_state_changed = Signal(bool)
    timeout_triggered = Signal(str, object)
    # Marshals Control Panel GitHub check results onto the GUI thread
    control_update_check_finished = Signal(str, str)  # latest_version, download_url ("" if none)
    control_update_check_failed = Signal()
    control_update_swap_started = Signal()  # close window after update bat is launched
    control_update_not_frozen = Signal(str)  # download_url for manual install hint

global_signals = ClusterSignals()
global_servers = []
is_deployment_running = False
cached_latest_cloud_version = "Fetching..."
cached_auth_token = ""
cached_discord_meta = {"panel_channel_id": None, "panel_message_id": None}


def load_config():
    data = load_master_config(CONFIG_FILE)
    return data


def save_config_to_file():
    global cached_auth_token, cached_discord_meta
    save_master_config(
        {
            "auth_token": cached_auth_token,
            "servers": global_servers,
            "discord": cached_discord_meta,
        },
        CONFIG_FILE,
    )


def parse_socket_response(answer):
    """
    Parse a node TCP reply.
    Returns: status, installed_ver, cloud_ver, active_task, node_ver
    status is ONLINE, DCS DOWN, UNAUTHORIZED, or OFFLINE.
    """
    if not answer:
        return "OFFLINE", "UNKNOWN", "Unknown", "Idle", ""

    try:
        if answer.startswith("{"):
            data = json.loads(answer)
            if data.get("status") == "UNAUTHORIZED":
                return "UNAUTHORIZED", "BAD TOKEN", "—", "Check auth_token", ""
            dcs_health = str(data.get("dcs_health", "")).strip().upper()
            dcs_running = data.get("dcs_running", True)
            active_task = data.get("active_task", "Idle")

            if dcs_health == "HEALTHY" or dcs_running is True:
                status = "ONLINE"
            elif dcs_health == "STARTING":
                status = "STARTING"
                if active_task == "Idle":
                    active_task = "DCS starting (waiting for port)"
            elif dcs_health == "NEVER_STARTED":
                status = "ONLINE"
                if active_task == "Idle":
                    active_task = "DCS not started"
            elif active_task == "Restarting DCS":
                status = "DCS DOWN"
            else:
                status = "DCS DOWN"
                if dcs_health == "UNHEALTHY" and active_task == "Idle":
                    active_task = "DCS not responding on port"
                elif dcs_health == "DEAD" and active_task == "Idle":
                    active_task = "DCS server stopped/crashed"
                elif dcs_running is False and active_task == "Idle":
                    active_task = "DCS_server.exe not running"
            return (
                status,
                data.get("installed_version", "Unknown"),
                data.get("latest_cloud_version", "Unknown"),
                active_task,
                data.get("node_version", "1.0"),
            )
        parts = answer.split(":")
        return (
            "ONLINE",
            parts[1].strip() if len(parts) > 1 else "Unknown",
            parts[2].strip() if len(parts) > 2 else "Unknown",
            "Idle",
            "1.0",
        )
    except Exception as err:
        logging.error("Error parsing network response: %s", err)
        return "OFFLINE", "UNKNOWN", "Unknown", "Idle", ""


def send_socket_command(ip, port, command_str):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect((ip, int(port)))
        payload = wrap_command(command_str, cached_auth_token)
        sock.sendall(payload.encode("utf-8"))
        response_data = sock.recv(4096).decode("utf-8")
        sock.close()
        return response_data.strip()
    except Exception as e:
        logging.debug(f"Network connection broken to {ip}:{port} -> {e}")
        return None

def test_single_system_background(index, ip, port, window_ref=None):
    global is_deployment_running
    row_id_str = str(index)
    answer = send_socket_command(ip, port, "PING_STATUS")
    status, local_ver, cloud_ver, active_task, node_ver = parse_socket_response(answer)
    if status == "ONLINE" and cloud_ver not in ("Unknown", "Fetching..."):
        global_signals.cloud_version_updated.emit(cloud_ver)
    # Still accept cloud version when DCS is down — node agent is alive
    if status == "DCS DOWN" and cloud_ver not in ("Unknown", "Fetching..."):
        global_signals.cloud_version_updated.emit(cloud_ver)
    global_signals.node_updated.emit(row_id_str, status, local_ver, active_task, node_ver)

def automatic_status_monitor(window_ref=None):
    while True:
        if not is_deployment_running:
            for i, s in enumerate(global_servers):
                t = threading.Thread(target=test_single_system_background, args=(i, s["ip"], s["port"], window_ref), daemon=True)
                t.start()
        time.sleep(PING_INTERVAL)

try:
    import ctypes
    myappid = 'dcsnorway.remoteupdate.controlpanel.v1'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception as e:
    logging.debug(f"Could not set AppUserModelID for Windows Taskbar: {e}")

# ==============================================================================
# PART 3 OF 5: MAINWINDOW INITIALIZATION, STYLE GEOMETRY & VIEWPORT APPARATUS
# ==============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DCS Norway Cluster Control Dashboard (Cloud Ver: Fetching...)")
        self.resize(800, 600)  # temporary; locked to columns/rows after table load
        self.setStyleSheet(f"background-color: {STYLE_BG_DARK}; color: {STYLE_TEXT_WHITE};")
        
        _icon_dir = os.path.join(
            os.environ.get("APPDATA") or os.path.dirname(os.path.abspath(__file__)),
            "DCS_Norway_Control",
        )
        icon_path = materialize_icon_file(_icon_dir)
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(15)
        
        self.header_layout = QHBoxLayout()
        self.header_layout.setSpacing(16)
        self.header_layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.lbl_logo = QLabel()
        self.lbl_logo.setFixedSize(120, 120)
        self.lbl_logo.setAlignment(Qt.AlignCenter)
        self.lbl_logo.setStyleSheet(f"background-color: {STYLE_BG_DARK}; border: none;")
        pix = QPixmap()
        if pix.loadFromData(logo_png_bytes()):
            scaled = pix.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_logo.setPixmap(scaled)
            logging.info(
                "[UI] Embedded brand logo %s (md5=%s)",
                BRAND_ASSET_VERSION,
                BRAND_PNG_MD5,
            )
        self.header_layout.addWidget(self.lbl_logo, 0, Qt.AlignVCenter)

        self.title_wrap = QWidget()
        self.title_wrap.setFixedHeight(120)
        self.title_wrap.setStyleSheet(f"background-color: {STYLE_BG_DARK};")
        self.title_column = QVBoxLayout(self.title_wrap)
        self.title_column.setContentsMargins(0, 0, 0, 0)
        self.title_column.setSpacing(4)
        self.title_column.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.lbl_title = QLabel("DCS Norway")
        self.lbl_title.setFont(QFont("Arial", 28, QFont.Bold))
        self.lbl_title.setStyleSheet(f"color: {STYLE_TEXT_WHITE}; background-color: {STYLE_BG_DARK};")
        self.lbl_subtitle = QLabel("Remote Update Control Panel")
        self.lbl_subtitle.setFont(QFont("Arial", 16))
        self.lbl_subtitle.setStyleSheet(f"color: {STYLE_TEXT_MUTED}; background-color: {STYLE_BG_DARK};")
        self.title_column.addStretch()
        self.title_column.addWidget(self.lbl_title)
        self.title_column.addWidget(self.lbl_subtitle)
        self.title_column.addStretch()
        self.header_layout.addWidget(self.title_wrap, 0, Qt.AlignVCenter)
        self.header_layout.addStretch()

        self.main_layout.addLayout(self.header_layout)
        
        self.table = QTableWidget()
        self.table.setColumnCount(7) 
        self.table.setHorizontalHeaderLabels(["Select", "Server Name", "IP Address", "Port", "Installed Ver.", "Latest ED Ver.", "Live Status"])
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: #111112; gridline-color: #2C2C30; border: 1px solid #2C2C30; }}"
            f"QTableWidget::item {{ color: {STYLE_TEXT_WHITE}; selection-color: {STYLE_TEXT_WHITE}; padding: 4px 10px; }}"
            f"QTableWidget::item:selected {{ background-color: #3A3A3C; }}"
            f"QHeaderView::section {{ background-color: #1C1C1F; color: {STYLE_TEXT_WHITE}; padding: 6px 10px; border: 1px solid #2C2C30; }}"
        )
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(48)
        header.setStretchLastSection(False)
        for col in range(7):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemClicked.connect(self.handle_row_click)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.main_layout.addWidget(self.table)

        self.actions_row_layout = QHBoxLayout()
        self.admin_layout = QHBoxLayout()
        self.btn_add = QPushButton(" Add Server ➕ ")
        self.btn_edit = QPushButton(" Edit Server ✏️")
        self.btn_remove = QPushButton(" Remove Server ❌ ")
        for btn, color_hex, hover_hex in [(self.btn_add, STYLE_BTN_ADD, STYLE_BTN_ADD_HOVER), (self.btn_edit, STYLE_BTN_EDIT, STYLE_BTN_EDIT_HOVER), (self.btn_remove, STYLE_BTN_REMOVE, STYLE_BTN_REMOVE_HOVER)]:
            btn.setStyleSheet(f"QPushButton {{ background-color: {color_hex}; color: {STYLE_TEXT_WHITE}; font-size: 10px; font-weight: bold; border-radius: 4px; padding: 3px 8px; border: none; }} QPushButton:hover {{ background-color: {hover_hex}; }}")
            btn.setFixedWidth(100)
            self.admin_layout.addWidget(btn)
        self.actions_row_layout.addLayout(self.admin_layout)
        self.actions_row_layout.addStretch()

        self.lbl_auth = QLabel("Shared Auth Token:")
        self.lbl_auth.setStyleSheet(f"color: {STYLE_TEXT_MUTED};")
        self.ent_auth = QLineEdit()
        self.ent_auth.setEchoMode(QLineEdit.Password)
        self.ent_auth.setPlaceholderText("Same token as on every Node — saved to master_config.json")
        self.ent_auth.setText(cached_auth_token)
        self.ent_auth.setFixedWidth(280)
        self.ent_auth.setStyleSheet(f"background-color: {STYLE_BG_CELL}; color: white; border: 1px solid #2C2C30; padding: 4px; border-radius: 3px;")
        self.btn_save_auth = QPushButton("Save Token")
        self.btn_save_auth.setStyleSheet(f"background-color: {STYLE_BTN_EDIT}; color: white; font-weight: bold; padding: 4px 10px; border: none; border-radius: 3px;")
        self.btn_save_auth.clicked.connect(self.save_auth_token)
        self.ent_auth.editingFinished.connect(self.save_auth_token)
        self.actions_row_layout.addWidget(self.lbl_auth)
        self.actions_row_layout.addWidget(self.ent_auth)
        self.actions_row_layout.addWidget(self.btn_save_auth)
        self.main_layout.addLayout(self.actions_row_layout)
        
        self.deploy_row_layout = QHBoxLayout()
        self.button_deploy = QPushButton(" DEPLOY SEQUENTIAL UPDATES 🚀 ")
        self.button_deploy.setStyleSheet(f"QPushButton {{ background-color: {STYLE_BTN_DEPLOY}; color: white; font-size: 13px; font-weight: bold; border-radius: 6px; padding: 22px; border: none; }} QPushButton:hover {{ background-color: {STYLE_BTN_DEPLOY_HOVER}; }}")
        self.button_deploy.setFixedWidth(340)
        self.button_deploy.setFixedHeight(58)
        self.deploy_row_layout.addStretch()
        self.deploy_row_layout.addWidget(self.button_deploy, 0, Qt.AlignRight)
        self.main_layout.addLayout(self.deploy_row_layout)
        
        self.lbl_logs = QLabel("Deployment Activity Console Logs:")
        self.main_layout.addWidget(self.lbl_logs)
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setStyleSheet("background-color: #111112; color: #30D158; font-family: Consolas; font-size: 11px; border: 1px solid #2C2C30;")
        self.log_console.setFixedHeight(150)
        self.log_console.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.main_layout.addWidget(self.log_console)
        
        global_signals.node_updated.connect(self.slot_node_updated)
        global_signals.cloud_version_updated.connect(self.slot_cloud_version_updated)
        global_signals.append_log.connect(self.slot_append_log)
        global_signals.deployment_state_changed.connect(self.slot_deployment_state_changed)
        global_signals.control_update_check_finished.connect(self._finish_update_check)
        global_signals.control_update_check_failed.connect(self._fail_update_check)
        global_signals.control_update_swap_started.connect(self.close)
        global_signals.control_update_not_frozen.connect(self._show_not_frozen_update_hint)
        
        self.selected_row_index = None
        self.btn_add.clicked.connect(lambda: self.show_form_dialog(edit_mode=False))
        self.btn_edit.clicked.connect(lambda: self.show_form_dialog(edit_mode=True))
        self.btn_remove.clicked.connect(self.remove_server)
        self.button_deploy.clicked.connect(self.confirm_and_deploy)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(50)
        self._fit_timer.timeout.connect(self.fit_table_and_window)
        self.load_table_data()
        QTimer.singleShot(0, self.fit_table_and_window)

        self._update_check_in_progress = False
        self._update_prompt_open = False
        self.update_check_timer = QTimer(self)
        self.update_check_timer.setInterval(CONTROL_PANEL_UPDATE_INTERVAL_MS)
        self.update_check_timer.timeout.connect(self._scheduled_update_check)
        self.update_check_timer.start()
        QTimer.singleShot(CONTROL_PANEL_STARTUP_UPDATE_DELAY_MS, self._scheduled_update_check)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.fit_table_and_window)

    def handle_row_click(self, item):
        self.selected_row_index = item.row()

    def save_auth_token(self):
        global cached_auth_token
        cached_auth_token = self.ent_auth.text().strip()
        save_config_to_file()
        global_signals.append_log.emit("Shared auth token saved to master_config.json")
        for i in range(self.table.rowCount()):
            s = global_servers[i]
            threading.Thread(
                target=test_single_system_background,
                args=(i, s["ip"], s["port"], self),
                daemon=True,
            ).start()

    def _scheduled_update_check(self):
        """Startup + hourly GitHub check; prompt only when a newer Control Panel exists."""
        if self._update_check_in_progress or self._update_prompt_open:
            return
        self._update_check_in_progress = True
        global_signals.append_log.emit("[SYSTEM] Checking GitHub for Control Panel updates…")
        threading.Thread(target=self._check_for_updates_worker, daemon=True).start()

    @staticmethod
    def _version_tuple(version_str: str):
        parts = []
        for chunk in str(version_str or "").strip().lstrip("vV").split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                digits = "".join(ch for ch in chunk if ch.isdigit())
                parts.append(int(digits) if digits else 0)
        return tuple(parts) if parts else (0,)

    def _check_for_updates_worker(self):
        import urllib.request

        try:
            url = f"{URL_GITHUB_API}{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers=github_api_headers("DCS-Norway-Control-Panel"))
            with urllib.request.urlopen(req, timeout=12) as response:
                data = json.loads(response.read().decode("utf-8"))
            latest = str(data.get("tag_name", "")).lstrip("v").strip()
            global_signals.append_log.emit(
                f"[SYSTEM] Control Panel v{CONTROL_PANEL_VERSION} | GitHub latest v{latest or '?'}"
            )
            download_url = ""
            for asset in data.get("assets", []):
                name = str(asset.get("name", "")).lower()
                if "remote.updater.control.panel.exe" in name:
                    download_url = str(asset.get("browser_download_url") or "")
                    break
            global_signals.control_update_check_finished.emit(latest, download_url)
        except Exception as e:
            global_signals.append_log.emit(f"[SYSTEM] GitHub check failed: {e}")
            global_signals.control_update_check_failed.emit()

    @Slot()
    def _fail_update_check(self):
        self._update_check_in_progress = False

    @Slot(str, str)
    def _finish_update_check(self, latest: str, download_url: str):
        self._update_check_in_progress = False
        if not latest or not download_url:
            return

        local_v = self._version_tuple(CONTROL_PANEL_VERSION)
        remote_v = self._version_tuple(latest)
        if remote_v <= local_v:
            return

        self._update_prompt_open = True
        reply = QMessageBox.question(
            self,
            "Update Available",
            f"A newer Control Panel is available: v{latest}\n"
            f"(you have v{CONTROL_PANEL_VERSION})\n\n"
            "Download and install now?\n"
            "The Control Panel will close, update, and reopen.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        self._update_prompt_open = False
        if reply == QMessageBox.Yes:
            global_signals.append_log.emit(
                f"[SYSTEM] Operator accepted update to v{latest}. Downloading…"
            )
            threading.Thread(
                target=self._swap_control_panel_binary,
                args=(download_url,),
                daemon=True,
            ).start()

    def _swap_control_panel_binary(self, download_url: str):
        """Replace this exe in-place and relaunch (compiled builds only)."""
        import subprocess

        if not getattr(sys, "frozen", False):
            global_signals.append_log.emit(
                "[SYSTEM] App update swap only works from the compiled .exe (not python script)."
            )
            global_signals.control_update_not_frozen.emit(download_url)
            return

        current_exe = os.path.abspath(sys.executable)
        appdata = os.environ.get("APPDATA") or os.path.dirname(current_exe)
        work_dir = os.path.join(appdata, "DCS_Norway_Control")
        os.makedirs(work_dir, exist_ok=True)
        bat_path = os.path.join(work_dir, "update_control_panel.bat")
        exe_name = os.path.basename(current_exe)
        exe_dir = os.path.dirname(current_exe)
        clean_url = download_url.replace(",", ".")

        global_signals.append_log.emit("[SYSTEM] Downloading Control Panel update…")
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write("setlocal\n")
            f.write(f'set "EXE_PATH={current_exe}"\n')
            f.write(f'set "EXE_DIR={exe_dir}"\n')
            f.write(f'set "EXE_NAME={exe_name}"\n')
            f.write(f'set "DOWNLOAD_URL={clean_url}"\n')
            f.write('cd /d "%EXE_DIR%"\n')
            f.write('taskkill /f /im "%EXE_NAME%" >nul 2>&1\n')
            f.write("timeout /t 3 /nobreak > nul\n")
            f.write(":del_loop\n")
            f.write('if exist "%EXE_PATH%" (\n')
            f.write('    del /f /q "%EXE_PATH%" >nul 2>&1\n')
            f.write("    timeout /t 1 /nobreak > nul\n")
            f.write("    goto del_loop\n")
            f.write(")\n")
            f.write('curl.exe -L --fail --retry 3 -o "%EXE_PATH%" "%DOWNLOAD_URL%"\n')
            f.write("if errorlevel 1 (\n")
            f.write(
                "    powershell -NoProfile -Command "
                "\"[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
                "Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile '%EXE_PATH%' -MaximumRedirection 5\"\n"
            )
            f.write(")\n")
            f.write('if not exist "%EXE_PATH%" ( echo Download failed & pause & exit /b 1 )\n')
            f.write('start "" "%EXE_PATH%"\n')
            f.write("timeout /t 2 /nobreak > nul\n")
            f.write('del "%~f0"\n')
            f.write("exit\n")

        subprocess.Popen(f'cmd.exe /c start /b "" "{bat_path}"', shell=True)
        global_signals.control_update_swap_started.emit()

    @Slot(str)
    def _show_not_frozen_update_hint(self, download_url: str):
        QMessageBox.information(
            self,
            "Update",
            "Run the compiled Control Panel .exe to install updates automatically.\n"
            f"Download manually:\n{download_url}",
        )

    # ==============================================================================
    # PART 4 OF 5: DATA GENERATION CONTEXTS, DIALOG MATRIX LAYOUTS & CRUD INTERCEPTS
    # ==============================================================================
    def show_form_dialog(self, edit_mode=False):
        if edit_mode and self.selected_row_index is None:
            QMessageBox.warning(self, "Selection Error", "Please select a server row first.")
            return
        from PySide6.QtWidgets import QDialog
        self.dialog = QDialog(self)
        self.dialog.setWindowTitle("Edit Server" if edit_mode else "Add Server")
        self.dialog.setFixedSize(420, 290)
        self.dialog.setStyleSheet(f"background-color: {STYLE_BG_DARK}; color: white;")
        layout = QVBoxLayout(self.dialog)
        self.ent_name, self.ent_ip, self.ent_port = QLineEdit(), QLineEdit(), QLineEdit("1015")
        for ent in [self.ent_name, self.ent_ip, self.ent_port]:
            ent.setStyleSheet(f"background-color: {STYLE_BG_CELL}; color: white; border: 1px solid #2C2C30; padding: 6px; border-radius: 3px;")
        if edit_mode:
            d = global_servers[self.selected_row_index]
            self.ent_name.setText(d["name"]); self.ent_ip.setText(d["ip"]); self.ent_port.setText(d["port"])
        layout.addWidget(QLabel("Server Name:")); layout.addWidget(self.ent_name)
        layout.addWidget(QLabel("IP / Hostname:")); layout.addWidget(self.ent_ip)
        layout.addWidget(QLabel("Port:")); layout.addWidget(self.ent_port)
        self.btn_save = QPushButton(" Save 💾 ")
        self.btn_save.setStyleSheet(f"background-color: {STYLE_BTN_ADD}; color: white; font-weight: bold; padding: 8px;")
        self.btn_save.clicked.connect(self.save_form_data)
        layout.addWidget(self.btn_save); self.dialog.exec_()

    def save_form_data(self):
        n, ip, p = self.ent_name.text().strip(), self.ent_ip.text().strip(), self.ent_port.text().strip()
        if not n or not ip or not p: return
        is_edit = hasattr(self, 'selected_row_index') and self.selected_row_index is not None and "Edit" in self.dialog.windowTitle()
        for idx, s in enumerate(global_servers):
            if is_edit and idx == self.selected_row_index: continue
            if s["ip"].lower() == ip.lower() and str(s["port"]) == str(p):
                QMessageBox.critical(self, "Error", "Server already exists!"); return
        if is_edit: global_servers[self.selected_row_index] = {"name": n, "ip": ip, "port": p}
        else: global_servers.append({"name": n, "ip": ip, "port": p})
        save_config_to_file(); self.selected_row_index = None; self.load_table_data(); self.dialog.close()

    def remove_server(self):
        if self.selected_row_index is not None and QMessageBox.question(self, "Delete", "Permanently remove server?") == QMessageBox.Yes:
            global_servers.pop(self.selected_row_index); save_config_to_file(); self.selected_row_index = None; self.load_table_data()

    def load_table_data(self):
        self.table.setRowCount(len(global_servers))
        for idx, s in enumerate(global_servers):
            cw = QWidget(); cl = QHBoxLayout(cw); cb = QCheckBox(); cb.setChecked(True); cl.addWidget(cb)
            cl.setAlignment(Qt.AlignCenter); cl.setContentsMargins(0,0,0,0); cw.setStyleSheet("QWidget:selected { background-color: #3A3A3C; }")
            self.table.setCellWidget(idx, 0, cw)
            self.table.setItem(idx, 1, QTableWidgetItem(s["name"]))
            self.table.setItem(idx, 2, QTableWidgetItem(s["ip"]))
            self.table.setItem(idx, 3, QTableWidgetItem(s["port"]))
            for col, name, txt, color in [(4, "ver_text", "FETCHING...", STYLE_STATUS_WARN), (5, "cloud_text", cached_latest_cloud_version, STYLE_TEXT_WHITE)]:
                w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(6,0,6,0); lbl = QLabel(txt); lbl.setObjectName(name)
                lbl.setStyleSheet(f"color: {color}; font-weight: bold; background: transparent;"); l.addWidget(lbl)
                l.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); w.setStyleSheet("QWidget:selected { background-color: #3A3A3C; }")
                self.table.setCellWidget(idx, col, w)
            sc = QWidget(); sl = QHBoxLayout(sc); sl.setContentsMargins(8,0,8,0); sl.setSpacing(10)
            lf = QFrame(); lf.setObjectName("status_lamp"); lf.setStyleSheet(f"background-color: {STYLE_STATUS_WARN}; border-radius: 8px;"); lf.setFixedSize(16,16)
            st = QLabel("CHECKING"); st.setObjectName("status_text"); st.setStyleSheet(f"color: {STYLE_STATUS_WARN}; font-weight: bold; background: transparent;")
            st.setWordWrap(False)
            st.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            sl.addWidget(lf); sl.addWidget(st); sl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); sc.setStyleSheet("QWidget:selected { background-color: #3A3A3C; }")
            self.table.setCellWidget(idx, 6, sc)
        for idx in range(self.table.rowCount()):
            self.table.setRowHeight(idx, TABLE_ROW_HEIGHT)
        self.fit_table_and_window()

    def schedule_fit(self):
        if getattr(self, "_fit_timer", None) is not None:
            self._fit_timer.start()
        else:
            self.fit_table_and_window()

    def apply_column_widths(self):
        self.fit_table_and_window()

    def apply_live_status_column_width(self):
        self.schedule_fit()

    def fit_window_width_to_columns(self):
        self.fit_table_and_window()

    def fit_window_height_to_rows(self):
        self.fit_table_and_window()

    def _screen_caps(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 960, 540
        geo = screen.availableGeometry()
        return (
            max(400, int(geo.width() * WINDOW_MAX_SCREEN_WIDTH_FRACTION)),
            max(300, int(geo.height() * WINDOW_MAX_SCREEN_HEIGHT_FRACTION)),
        )

    def _measure_column_widths(self):
        """Each column is as wide as its header or widest cell content."""
        header = self.table.horizontalHeader()
        header_fm = header.fontMetrics()
        widths = []
        for col in range(self.table.columnCount()):
            header_item = self.table.horizontalHeaderItem(col)
            title = header_item.text() if header_item is not None else ""
            width = header_fm.horizontalAdvance(title) + 24
            if col == 0:
                width = max(width, 48)
            elif col in (4, 5):
                width = max(width, 160)
                for row in range(self.table.rowCount()):
                    cell = self.table.cellWidget(row, col)
                    if cell is None:
                        continue
                    lbl = cell.findChild(QLabel)
                    if lbl is None:
                        continue
                    width = max(width, lbl.fontMetrics().horizontalAdvance(lbl.text()) + 20)
            elif col == 6:
                width = max(width, 120)
                for row in range(self.table.rowCount()):
                    cell = self.table.cellWidget(row, 6)
                    if cell is None:
                        continue
                    st = cell.findChild(QLabel, "status_text")
                    if st is None:
                        continue
                    text_w = st.fontMetrics().horizontalAdvance(st.text())
                    width = max(width, text_w + 16 + 16 + 10 + 16)
            else:
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, col)
                    if item is None:
                        continue
                    fm = self.table.fontMetrics()
                    width = max(width, fm.horizontalAdvance(item.text()) + 24)
            widths.append(width)
        return widths

    def fit_table_and_window(self):
        """
        Columns follow widest content. Window follows the column sum and row count,
        capped at 50% of the screen (and 10 visible server rows).
        """
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        widths = self._measure_column_widths()
        for col, width in enumerate(widths):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)

        rows = self.table.rowCount()
        header_h = header.sizeHint().height()
        if header.height() > 1:
            header_h = header.height()
        frame = self.table.frameWidth() * 2
        vh = self.table.verticalHeader()
        vh_w = vh.width() if vh is not None and not vh.isHidden() else 0
        max_w, max_h = self._screen_caps()
        margins = self.main_layout.contentsMargins()
        spacing = self.main_layout.spacing() * max(0, self.main_layout.count() - 1)
        actions_h = max(self.btn_add.sizeHint().height(), self.ent_auth.sizeHint().height())
        chrome_h = (
            margins.top()
            + margins.bottom()
            + spacing
            + 120
            + actions_h
            + self.button_deploy.height()
            + self.lbl_logs.sizeHint().height()
            + 150
        )

        visible = min(rows, TABLE_MAX_VISIBLE_ROWS)
        table_h = header_h + (visible * TABLE_ROW_HEIGHT) + frame
        if chrome_h + table_h > max_h:
            available = max(header_h + TABLE_ROW_HEIGHT + frame, max_h - chrome_h)
            visible = max(1, (available - header_h - frame) // TABLE_ROW_HEIGHT)
            visible = min(visible, TABLE_MAX_VISIBLE_ROWS, max(rows, 1))
            table_h = header_h + (visible * TABLE_ROW_HEIGHT) + frame
        overflow_v = rows > visible
        sb_v = self.table.style().pixelMetric(QStyle.PM_ScrollBarExtent) if overflow_v else 0
        self.table.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if overflow_v else Qt.ScrollBarAlwaysOff
        )
        self.table.setFixedHeight(table_h)

        cols_w = sum(widths)
        table_chrome_w = vh_w + frame + sb_v + margins.left() + margins.right()
        ideal_w = cols_w + table_chrome_w
        overflow_h = ideal_w > max_w
        self.table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if overflow_h else Qt.ScrollBarAlwaysOff
        )
        if overflow_h:
            sb_h = self.table.style().pixelMetric(QStyle.PM_ScrollBarExtent)
            self.table.setFixedHeight(table_h + sb_h)

        window_w = min(ideal_w, max_w)
        window_h = min(chrome_h + self.table.height(), max_h)
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.setFixedSize(window_w, window_h)

# ==============================================================================
# PART 5 OF 5: THREAD-SAFE SLOTS, EXPLICIT STRING IDENTITY MATCHERS & MAIN LOOPS
# ==============================================================================
    @Slot(str, str, str, str, str)
    def slot_node_updated(self, row_id_str, status, installed_ver, active_task, node_ver):
        idx = int(row_id_str)
        if idx >= self.table.rowCount(): return
        
        base_name = global_servers[idx]["name"]
        if status in ("ONLINE", "DCS DOWN", "STARTING") and node_ver:
            display_name = f"{base_name} (Node v{node_ver})"
        else:
            display_name = base_name
        self.table.setItem(idx, 1, QTableWidgetItem(display_name))
        
        sc = self.table.cellWidget(idx, 6)
        if sc:
            lf, st = sc.findChild(QFrame, "status_lamp"), sc.findChild(QLabel, "status_text")
            if lf and st:
                if status == "ONLINE":
                    c = STYLE_STATUS_GREEN
                    label = status if active_task == "Idle" else f"{status} ({active_task})"
                elif status == "STARTING":
                    c = STYLE_STATUS_WARN
                    label = "STARTING"
                elif status == "DCS DOWN":
                    c = STYLE_STATUS_WARN
                    # Keep column compact; details go in tooltip
                    label = "DCS DOWN"
                elif status == "UNAUTHORIZED":
                    c = STYLE_STATUS_WARN
                    label = "UNAUTHORIZED"
                else:
                    c = STYLE_STATUS_RED
                    label = status if active_task == "Idle" else f"{status} ({active_task})"
                lf.setStyleSheet(f"background-color: {c}; border-radius: 8px;")
                st.setText(label)
                tip = active_task if active_task not in ("Idle",) else status
                st.setToolTip(tip)
                sc.setToolTip(tip)
                st.setStyleSheet(f"color: {c}; font-weight: bold; background: transparent;")
                
        vc = self.table.cellWidget(idx, 4)
        if vc:
            lbl = vc.findChild(QLabel, "ver_text")
            if lbl:
                inst_clean = str(installed_ver).strip()
                cloud_clean = str(cached_latest_cloud_version).strip()

                if status == "UNAUTHORIZED":
                    cv = STYLE_STATUS_WARN
                elif status == "DCS DOWN":
                    cv = STYLE_STATUS_WARN
                elif inst_clean in ["UNKNOWN", "FETCHING...", "BAD TOKEN"]:
                    cv = STYLE_STATUS_WARN
                elif cloud_clean in ["Fetching...", "Unknown"]:
                    cv = STYLE_STATUS_GREEN
                elif inst_clean != cloud_clean:
                    cv = STYLE_STATUS_RED  
                else:
                    cv = STYLE_STATUS_GREEN
                    
                lbl.setText(installed_ver)
                lbl.setStyleSheet(f"color: {cv}; font-weight: bold; background: transparent;")

        self.schedule_fit()

    @Slot(str)
    def slot_cloud_version_updated(self, version_str):
        global cached_latest_cloud_version
        cached_latest_cloud_version = version_str
        self.setWindowTitle(f"DCS Norway Cluster Control Dashboard (Cloud Ver: {version_str})")
        
        for i in range(self.table.rowCount()):
            cc = self.table.cellWidget(i, 5)
            if cc and (cloud_lbl := cc.findChild(QLabel, "cloud_text")):
                cloud_lbl.setText(version_str)
                
            vc = self.table.cellWidget(i, 4)
            if vc and (ver_lbl := vc.findChild(QLabel, "ver_text")):
                current_inst_text = str(ver_lbl.text()).strip()
                cloud_clean = str(version_str).strip()
                
                if current_inst_text in ["UNKNOWN", "FETCHING..."]:
                    cv = STYLE_STATUS_WARN
                elif cloud_clean in ["Fetching...", "Unknown"]:
                    cv = STYLE_STATUS_GREEN
                elif current_inst_text != cloud_clean:
                    cv = STYLE_STATUS_RED  
                else:
                    cv = STYLE_STATUS_GREEN
                    
                ver_lbl.setStyleSheet(f"color: {cv}; font-weight: bold; background: transparent;")

    @Slot(str)
    def slot_append_log(self, text): 
        self.log_console.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    @Slot(bool)
    def slot_deployment_state_changed(self, run):
        for b in [self.button_deploy, self.btn_add, self.btn_edit, self.btn_remove]: b.setEnabled(not run)

    @Slot(str, object)
    def slot_handle_timeout(self, server_name, response_container):
        msg = f"The update for {server_name} has been running for over 15 minutes.\n\nWhat do you want to do?"
        box = QMessageBox(self)
        box.setWindowTitle(" Deployment Timeout ⏰ ")
        box.setText(msg); box.setIcon(QMessageBox.Warning)
        btn_continue = box.addButton("Continue waiting", QMessageBox.AcceptRole)
        btn_skip = box.addButton("Skip this server", QMessageBox.RejectRole)
        box.exec_()
        response_container["action"] = "continue" if box.clickedButton() == btn_continue else "skip"
        response_container["event"].set()

    def run_sequential_deployment_thread(self):
        global is_deployment_running; is_deployment_running = True
        global_signals.deployment_state_changed.emit(True)
        global_signals.append_log.emit("=== STARTING SEQUENTIAL CLUSTER DEPLOYMENT ===")
        
        for idx in range(self.table.rowCount()):
            cw = self.table.cellWidget(idx, 0)
            if cw and (cb := cw.findChild(QCheckBox)) and not cb.isChecked(): 
                continue
                
            sd = global_servers[idx]; n, ip, p = sd["name"], sd["ip"], sd["port"]
            global_signals.append_log.emit(f" [QUEUE] Processing ▶️ {n} ({ip}:{p})...")
            global_signals.node_updated.emit(str(idx), "ONLINE", "FETCHING...", "Updating", "")
            
            try:
                ans = send_socket_command(ip, p, "TRIGGER_DCS_UPDATE")
                if ans and ans.startswith("{"):
                    res_json = json.loads(ans)
                    if res_json.get("status") == "UNAUTHORIZED":
                        global_signals.append_log.emit(
                            f" [ 🔐 {n}] Unauthorized — set the same auth_token in Control Panel and on this Node."
                        )
                        global_signals.node_updated.emit(str(idx), "UNAUTHORIZED", "BAD TOKEN", "Check auth_token", "")
                        continue
                    if res_json.get("status") == "REJECTED_BUSY":
                        global_signals.append_log.emit(f" [ ⚠️ {n}] Node busy: {res_json.get('task', 'unknown')}")
                        continue
                if ans and "OK_STARTING" in ans:
                    global_signals.append_log.emit(f" [ ✅ {n}] Update triggered. Monitoring bandwidth usage...")
                    
                    time.sleep(15)
                    download_finished = False
                    
                    while not download_finished:
                        start_time = time.time()
                        while time.time() - start_time < 900:
                            chk = send_socket_command(ip, p, "PING_STATUS")
                            if chk and chk.startswith("{"):
                                response_json = json.loads(chk)
                                if response_json.get("status") == "UNAUTHORIZED":
                                    global_signals.append_log.emit(f" [ 🔐 {n}] Unauthorized during monitor.")
                                    global_signals.node_updated.emit(str(idx), "UNAUTHORIZED", "BAD TOKEN", "Check auth_token", "")
                                    break
                                local_task = response_json.get("active_task", "Idle")
                                local_ver = response_json.get("installed_version", "Unknown")
                                node_ver = response_json.get("node_version", "1.0")
                                
                                global_signals.node_updated.emit(str(idx), "ONLINE", local_ver, local_task, node_ver)
                                
                                if local_task in ["Rebooting", "Idle"]:
                                    global_signals.append_log.emit(f" [ 🎉 {n}] Download complete! Bandwidth released for next server.")
                                    download_finished = True
                                    break
                                    
                            elif download_finished:
                                break
                            
                            time.sleep(DEPLOY_CHECK_INTERVAL)
                            
                        if download_finished:
                            break
                            
                        global_signals.append_log.emit(f" [ ⏳ {n}] Download taking unusually long. Prompting operator...")
                        res_box = {"event": threading.Event(), "action": "skip"}
                        global_signals.timeout_triggered.emit(n, res_box)
                        res_box["event"].wait()
                        
                        if res_box["action"] == "skip":
                            global_signals.append_log.emit(f" [ ⏭️ {n}] Operator skipped wait. Moving to next server to free queue.")
                            break
                        else:
                            global_signals.append_log.emit(f" [ ⏳ {n}] Operator extended wait time. Continuing monitor...")
                else:
                    global_signals.append_log.emit(f" [ ❌ {n}] Request rejected by node.")
            except Exception as e:
                global_signals.append_log.emit(f" [ ❌ {n}] Sequential step failed: {e}")
            finally:
                final_chk = send_socket_command(ip, p, "PING_STATUS")
                if final_chk:
                    status, inst, _, task, node_ver = parse_socket_response(final_chk)
                    global_signals.node_updated.emit(str(idx), status, inst, task, node_ver)
                else:
                    global_signals.node_updated.emit(str(idx), "OFFLINE", "FETCHING...", "Rebooting", "")
                    
        global_signals.append_log.emit("\n=== ALL SEQUENTIAL DEPLOYMENTS COMPLETED ===")
        is_deployment_running = False
        global_signals.deployment_state_changed.emit(False)

    def confirm_and_deploy(self):
        if QMessageBox.question(self, " Confirm ⚠️ ", "Execute sequential cluster updates?") == QMessageBox.Yes:
            global_signals.timeout_triggered.connect(self.slot_handle_timeout)
            threading.Thread(target=self.run_sequential_deployment_thread, daemon=True).start()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    config_data = load_config()
    global_servers = config_data.get("servers", [])
    cached_auth_token = config_data.get("auth_token", "")
    cached_discord_meta = config_data.get("discord") or {
        "panel_channel_id": None,
        "panel_message_id": None,
    }
    if not cached_auth_token:
        logging.warning(
            "auth_token is empty in master_config.json — TCP commands are unauthenticated. "
            "Set the same token on Control/Bot and every Node."
        )

    window = MainWindow()
    window.show()

    threading.Thread(target=automatic_status_monitor, args=(window,), daemon=True).start()
    sys.exit(app.exec())
