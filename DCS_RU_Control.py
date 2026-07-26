# ==============================================================================
# BLOKK 1 AV 5: IMPORTER, FARGESTILER, KONFIGURASJON OG SYSTEM-SIGNALER
# ==============================================================================

import os
import sys
import time
import socket
import threading
import json
import logging
from datetime import datetime

from PySide6.QtCore import Qt, QObject, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QPushButton, QLabel, QLineEdit,
    QTextEdit, QHeaderView, QMessageBox, QFrame, QCheckBox, QSizePolicy
)
from PySide6.QtGui import QFont, QPixmap, QIcon

def get_resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

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

class ClusterSignals(QObject):
    node_updated = Signal(str, str, str, str)
    cloud_version_updated = Signal(str)
    append_log = Signal(str)
    deployment_state_changed = Signal(bool)
    timeout_triggered = Signal(str, object)

global_signals = ClusterSignals()
global_servers = []
is_deployment_running = False
cached_latest_cloud_version = "Fetching..."

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "servers" in data and isinstance(data["servers"], list):
                    return data
        except Exception as e:
            logging.error(f"Kunne ikke lese konfigurasjonsfilen: {e}")
    empty_profile = {"servers": []}
    save_config(empty_profile)
    return empty_profile

def save_config(config_data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Kunne ikke lagre konfigurasjonsfilen: {e}")

def save_config_to_file():
    save_config({"servers": global_servers})

try:
    import ctypes
    myappid = 'dcsnorway.remoteupdate.controlpanel.v1'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception as e:
    logging.debug(f"Kunne ikke sette AppUserModelID for Windows Taskbar: {e}")
# ==============================================================================
# BLOKK 2 AV 5: NETTVERKSKOMMUNIKASJON OG ASYNKRON STATUS-MONITORERING
# ==============================================================================

def send_socket_command(ip, port, command_str):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        sock.connect((ip, int(port)))
        payload = command_str if command_str.endswith("\n") else command_str + "\n"
        sock.sendall(payload.encode('utf-8'))
        response_data = sock.recv(4096).decode('utf-8')
        sock.close()
        return response_data.strip()
    except Exception as e:
        logging.debug(f"Nettverksforbindelse brutt mot {ip}:{port} -> {e}")
    return None

def test_single_system_background(index, ip, port):
    row_id_str = str(index)
    answer = send_socket_command(ip, port, "PING_STATUS")
    if answer:
        try:
            if answer.startswith("{"):
                response_json = json.loads(answer)
                local_ver = response_json.get("installed_version", "Unknown")
                cloud_ver = response_json.get("latest_cloud_version", "Unknown")
                active_task = response_json.get("active_task", "Idle")
            else:
                parts = answer.split(":")
                local_ver = parts[1].strip() if len(parts) > 1 else "Unknown"
                cloud_ver = parts[2].strip() if len(parts) > 2 else "Unknown"
                active_task = "Idle"
            if cloud_ver and cloud_ver != "Unknown" and cloud_ver != "Fetching...":
                global_signals.cloud_version_updated.emit(cloud_ver)
            global_signals.node_updated.emit(row_id_str, "ONLINE", local_ver, active_task)
        except Exception as err:
            logging.error(f"Feil under parsing av nettverkssvar fra {ip}: {err}")
            global_signals.node_updated.emit(row_id_str, "OFFLINE", "FETCHING...", "Idle")
    else:
        global_signals.node_updated.emit(row_id_str, "OFFLINE", "FETCHING...", "Idle")

def automatic_status_monitor():
    while True:
        if not is_deployment_running:
            for i, s in enumerate(global_servers):
                t = threading.Thread(target=test_single_system_background, args=(i, s["ip"], s["port"]), daemon=True)
                t.start()
        time.sleep(PING_INTERVAL)
# ==============================================================================
# BLOKK 3 AV 5: MAINWINDOW KLASSEN – VISUELT DESIGN OG LAYOUT-STRUKTUR
# ==============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DCS Norway Cluster Control Dashboard")
        self.resize(1120, 680)
        self.setStyleSheet(f"background-color: {STYLE_BG_DARK}; color: {STYLE_TEXT_WHITE};")
        
        icon_path = get_resource_path("logo.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(15)
        
        self.header_layout = QHBoxLayout()
        self.lbl_title = QLabel("DCS Norway Remote Update Control Panel")
        self.lbl_title.setFont(QFont("Arial", 28, QFont.Bold))
        self.header_layout.addWidget(self.lbl_title)
        
        self.lbl_logo = QLabel()
        logo_path = get_resource_path("logo.png")
        if os.path.exists(logo_path):
            pix = QPixmap(logo_path)
            self.lbl_logo.setPixmap(pix.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.header_layout.addWidget(self.lbl_logo, 0, Qt.AlignRight | Qt.AlignVCenter)
        self.main_layout.addLayout(self.header_layout)
        
        self.table = QTableWidget()
        self.table.setColumnCount(7)  
        self.table.setHorizontalHeaderLabels(["Select", "Server Name", "IP Address", "Port", "Installed Ver.", "Latest ED Ver.", "Live Status"])
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: #111112; gridline-color: #2C2C30; border: 1px solid #2C2C30; }}"
            f"QTableWidget::item {{ color: {STYLE_TEXT_WHITE}; selection-color: {STYLE_TEXT_WHITE}; }}"
            f"QTableWidget::item:selected {{ background-color: #3A3A3C; }}" 
            f"QHeaderView::section {{ background-color: #2C2C30; color: {STYLE_TEXT_WHITE}; font-weight: bold; border: 1px solid #111112; padding: 5px; }}"
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemClicked.connect(self.handle_row_click)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.main_layout.addWidget(self.table)

        self.actions_row_layout = QHBoxLayout()
        self.admin_layout = QHBoxLayout()
        self.btn_add = QPushButton("➕ Add Server")
        self.btn_edit = QPushButton("✏️ Edit Server")
        self.btn_remove = QPushButton("❌ Remove Server")

        for btn, color_hex, hover_hex in [(self.btn_add, STYLE_BTN_ADD, STYLE_BTN_ADD_HOVER), (self.btn_edit, STYLE_BTN_EDIT, STYLE_BTN_EDIT_HOVER), (self.btn_remove, STYLE_BTN_REMOVE, STYLE_BTN_REMOVE_HOVER)]:
            btn.setStyleSheet(f"QPushButton {{ background-color: {color_hex}; color: {STYLE_TEXT_WHITE}; font-size: 10px; font-weight: bold; border-radius: 4px; padding: 3px 8px; border: none; }} QPushButton:hover {{ background-color: {hover_hex}; }}")
            btn.setFixedWidth(100)
            self.admin_layout.addWidget(btn)

        self.actions_row_layout.addLayout(self.admin_layout)
        self.actions_row_layout.addStretch()
        self.main_layout.addLayout(self.actions_row_layout)

        self.deploy_row_layout = QHBoxLayout()
        self.button_deploy = QPushButton("🚀 DEPLOY SEQUENTIAL UPDATES")
        self.button_deploy.setStyleSheet(f"QPushButton {{ background-color: {STYLE_BTN_DEPLOY}; color: white; font-size: 13px; font-weight: bold; border-radius: 6px; padding: 22px; border: none; }} QPushButton:hover {{ background-color: {STYLE_BTN_DEPLOY_HOVER}; }}")
        self.button_deploy.setFixedWidth(340)
        self.button_deploy.setFixedHeight(58)
        self.deploy_row_layout.addStretch()
        self.deploy_row_layout.addWidget(self.button_deploy, 0, Qt.AlignRight)
        self.main_layout.addLayout(self.deploy_row_layout)

        self.main_layout.addWidget(QLabel("Deployment Activity Console Logs:"))
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setStyleSheet("background-color: #111112; color: #30D158; font-family: Consolas; font-size: 11px; border: 1px solid #2C2C30;")
        self.log_console.setFixedHeight(120)
        self.main_layout.addWidget(self.log_console)

        global_signals.node_updated.connect(self.slot_node_updated)
        global_signals.cloud_version_updated.connect(self.slot_cloud_version_updated)
        global_signals.append_log.connect(self.slot_append_log)
        global_signals.deployment_state_changed.connect(self.slot_deployment_state_changed)

        self.selected_row_index = None
        self.btn_add.clicked.connect(lambda: self.show_form_dialog(edit_mode=False))
        self.btn_edit.clicked.connect(lambda: self.show_form_dialog(edit_mode=True))
        self.btn_remove.clicked.connect(self.remove_server)
        self.button_deploy.clicked.connect(self.confirm_and_deploy)
        self.load_table_data()

    def handle_row_click(self, item): self.selected_row_index = item.row()
# ==============================================================================
# BLOKK 4 AV 5: CRUD-DIALOGER, DATALAGRING OG GENERERING AV MATRISE-RADER
# ==============================================================================

    def show_form_dialog(self, edit_mode=False):
        if edit_mode and self.selected_row_index is None:
            QMessageBox.warning(self, "Selection Error", "Vennligst marker en serverrad først.")
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
        self.btn_save = QPushButton("💾 Save")
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
                QMessageBox.critical(self, "Error", "Server eksisterer allerede!"); return
        if is_edit: global_servers[self.selected_row_index] = {"name": n, "ip": ip, "port": p}
        else: global_servers.append({"name": n, "ip": ip, "port": p})
        save_config_to_file(); self.selected_row_index = None; self.load_table_data(); self.dialog.close()

    def remove_server(self):
        if self.selected_row_index is not None and QMessageBox.question(self, "Slett", "Fjerne permanent?") == QMessageBox.Yes:
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
            sl.addWidget(lf); sl.addWidget(st); sl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter); sc.setStyleSheet("QWidget:selected { background-color: #3A3A3C; }")
            self.table.setCellWidget(idx, 6, sc)
# ==============================================================================
# BLOKK 5 AV 5: TRÅDSIKRE SLOTS, INTERAKTIV TIMEOUT-MOTOR OG OPPSTART
# ==============================================================================

    @Slot(str, str, str, str)
    def slot_node_updated(self, row_id_str, status, installed_ver, active_task):
        idx = int(row_id_str)
        if idx >= self.table.rowCount(): return
        sc = self.table.cellWidget(idx, 6)
        if sc:
            lf, st = sc.findChild(QFrame, "status_lamp"), sc.findChild(QLabel, "status_text")
            if lf and st:
                c = STYLE_STATUS_GREEN if status == "ONLINE" else STYLE_STATUS_RED
                lf.setStyleSheet(f"background-color: {c}; border-radius: 8px;")
                st.setText(status if active_task == "Idle" else f"{status} ({active_task})")
                st.setStyleSheet(f"color: {c}; font-weight: bold; background: transparent;")
        vc = self.table.cellWidget(idx, 4)
        if vc:
            lbl = vc.findChild(QLabel, "ver_text")
            if lbl:
                cv = STYLE_STATUS_WARN if installed_ver in ["UNKNOWN", "FETCHING..."] else (STYLE_STATUS_GREEN if installed_ver == cached_latest_cloud_version else STYLE_STATUS_RED)
                lbl.setText(installed_ver); lbl.setStyleSheet(f"color: {cv}; font-weight: bold; background: transparent;")

    @Slot(str)
    def slot_cloud_version_updated(self, version_str):
        global cached_latest_cloud_version; cached_latest_cloud_version = version_str
        for i in range(self.table.rowCount()):
            cc = self.table.cellWidget(i, 5)
            if cc:
                lbl = cc.findChild(QLabel, "cloud_text")
                if lbl: lbl.setText(version_str)

    @Slot(str)
    def slot_append_log(self, text): self.log_console.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    @Slot(bool)
    def slot_deployment_state_changed(self, run):
        for b in [self.button_deploy, self.btn_add, self.btn_edit, self.btn_remove]: b.setEnabled(not run)

    @Slot(str, object)
    def slot_handle_timeout(self, server_name, response_container):
        msg = f"Oppdateringen av {server_name} har pågått i over 15 minutter.\n\nHva ønsker du å gjøre?"
        box = QMessageBox(self)
        box.setWindowTitle("⏰ Deployment Timeout")
        box.setText(msg); box.setIcon(QMessageBox.Warning)
        btn_continue = box.addButton("Fortsett å vente", QMessageBox.AcceptRole)
        btn_skip = box.addButton("Hopp over serveren", QMessageBox.RejectRole)
        box.exec_()
        response_container["action"] = "continue" if box.clickedButton() == btn_continue else "skip"
        response_container["event"].set()

    def run_sequential_deployment_thread(self):
        global is_deployment_running; is_deployment_running = True
        global_signals.deployment_state_changed.emit(True)
        global_signals.append_log.emit("=== STARTING SEQUENTIAL CLUSTER DEPLOYMENT ===")
        for idx in range(self.table.rowCount()):
            cw = self.table.cellWidget(idx, 0)
            if cw and (cb := cw.findChild(QCheckBox)) and not cb.isChecked(): continue
            sd = global_servers[idx]; n, ip, p = sd["name"], sd["ip"], sd["port"]
            global_signals.append_log.emit(f"▶️ [QUEUE] Processing {n} ({ip}:{p})...")
            global_signals.node_updated.emit(str(idx), "ONLINE", "FETCHING...", "Updating")
            try:
                ans = send_socket_command(ip, p, "TRIGGER_DCS_UPDATE")
                if ans and "OK_STARTING" in ans:
                    global_signals.append_log.emit(f"✅ [{n}] Update triggered. Waiting...")
                    global_signals.node_updated.emit(str(idx), "OFFLINE", "FETCHING...", "Rebooting")
                    time.sleep(15); verified = False
                    while not verified:
                        start = time.time()
                        while time.time() - start < 900:
                            chk = send_socket_command(ip, p, "PING_STATUS")
                            if chk and ("ONLINE" in chk or "ACK" in chk):
                                global_signals.append_log.emit(f"🎉 [{n}] Server is ONLINE!"); verified = True; break
                            time.sleep(DEPLOY_CHECK_INTERVAL)
                        if not verified:
                            global_signals.append_log.emit(f"⚠️ [{n}] 15 minutter passert uten svar. Spør operatør...")
                            res_box = {"event": threading.Event(), "action": "skip"}
                            global_signals.timeout_triggered.emit(n, res_box); res_box["event"].wait()
                            if res_box["action"] == "skip":
                                global_signals.append_log.emit(f"⏭️ [{n}] Operatør valgte å hoppe over serveren."); break
                            else:
                                global_signals.append_log.emit(f"⏳ [{n}] Operatør valgte å fortsette ventingen.")
                else: global_signals.append_log.emit(f"❌ [{n}] Request rejected.")
            except Exception as e: global_signals.append_log.emit(f"❌ [{n}] Failed: {e}")
            finally: global_signals.node_updated.emit(str(idx), "ONLINE", "UNKNOWN", "Idle")
        global_signals.append_log.emit("\n=== ALL SEQUENTIAL DEPLOYMENTS COMPLETED ===")
        is_deployment_running = False; global_signals.deployment_state_changed.emit(False)

    def confirm_and_deploy(self):
        if QMessageBox.question(self, "⚠️ Confirm", "Iverksette sekvensiell oppdatering?") == QMessageBox.Yes:
            global_signals.timeout_triggered.connect(self.slot_handle_timeout)
            threading.Thread(target=self.run_sequential_deployment_thread, daemon=True).start()

if __name__ == "__main__":
    app = QApplication(sys.argv); config_data = load_config(); global_servers = config_data.get("servers", [])
    window = MainWindow(); window.show()
    threading.Thread(target=automatic_status_monitor, daemon=True).start()
    sys.exit(app.exec())
