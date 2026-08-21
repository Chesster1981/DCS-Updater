# DCS Norway Remote Updater

Remote update system for DCS World servers in the DCS Norway cluster. Three components communicate over TCP:

| Program | Role |
|---------|------|
| `DCS_RU_Node.py` | Agent on each DCS machine |
| `DCS_RU_Control.py` | Desktop control panel (PySide6) |
| `DCS_RU_Discord_Bot.py` | Discord live panel and deploy queue |
| `dcs_ru_common.py` | Shared config, auth, and version scraping |
| `brand_assets.py` | Embedded logo bytes (regenerate via tools script) |

## Changing the logo

1. Replace `Logo.png` in the repo root  
2. Run `python tools/refresh_brand_assets.py`  
3. Rebuild Node and Control Panel  

UI logos are loaded from **embedded bytes** inside `brand_assets.py` (not from loose files next to the exe), so version upgrades always carry the logo you baked in.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

### 1. `master_config.json` (Control + Discord Bot)

Unified schema (legacy `cluster_nodes` is migrated automatically):

```json
{
  "auth_token": "a-long-secret-string",
  "servers": [
    { "name": "Syria", "ip": "dcsnorge.no", "port": "1015" }
  ],
  "discord": {
    "panel_channel_id": null,
    "panel_message_id": null
  }
}
```

This file is gitignored. Set the **same** `auth_token` on every node.

### 2. Node (`%APPDATA%\DCS_Norway_Node\dcs_node_config.json`)

Important fields:

- `network_port` — TCP port (e.g. 1015)
- `bind_address` — prefer a LAN IP instead of `0.0.0.0` when possible
- `auth_token` — must match `master_config.json`
- `dcs_main_folder` — DCS root folder
- `preserve_mission_scripting` / `reboot_after_deployment`

### 3. Discord token (environment variable)

**Do not** put the token in source code. Regenerate it in the [Discord Developer Portal](https://discord.com/developers/applications) if it was previously hardcoded.

```powershell
$env:DISCORD_BOT_TOKEN = "your-new-token"
python DCS_RU_Discord_Bot.py
```

Or copy `.env.example` → `.env` and load it from your shell / systemd / Task Scheduler.

## Security

1. Set `auth_token` on Control/Bot **and** every Node (commands are sent as `TOKEN|COMMAND`).
2. Bind the Node to a LAN IP (`bind_address`); do not expose it to the open internet without a firewall.
3. `EXIT_NODE` with auth also requires localhost (single-instance takeover).
4. Discord roles: Administrator, `DCS Admin`, or `Moderator`.

Without `auth_token`, the system runs in legacy mode (open commands) — do not use that in production.

## Running

```powershell
python DCS_RU_Node.py
python DCS_RU_Control.py
$env:DISCORD_BOT_TOKEN = "..."
python DCS_RU_Discord_Bot.py
```

In Discord: `/dcs-panel-init` pins the live panel. On every bot restart the panel and pinned legend are refreshed automatically. `/dcs-panel-guide` refreshes the pinned legend. `/dcs-update-wiki` shows an ephemeral status-logic wiki that auto-dismisses when you switch channel, go offline, press Lukk, or after 15 minutes. Yellow/red servers can be selected for Update, Restart DCS, Restart SRS, or Reboot. Channel/message IDs are stored in `master_config.json`.

In the Control Panel: right-click a server row for **Start / Restart DCS**, **Start / Restart SRS**, or **Reboot Windows** (same Node commands as Discord).

## TCP protocol

| Command | Response |
|---------|----------|
| `PING_STATUS` | JSON with version, `active_task`, `node_version`, `dcs_running`, SRS fields |
| `TRIGGER_DCS_UPDATE` | `OK_STARTING` / `REJECTED_BUSY` / `UNAUTHORIZED` |
| `TRIGGER_SRS_UPDATE` | `OK_STARTING` / `REJECTED_BUSY` / `ERROR` / `UNAUTHORIZED` |
| `OPERATOR_RESTART_DCS` | Start/restart DCS (no hourly auto-restart limit) |
| `RESTART_SRS` | Start/restart SRS Server |
| `REBOOT_WINDOWS` | Schedule host reboot (~10s) |
| `EXIT_NODE` | `ACK_EXIT` (localhost + auth when a token is set) |

With auth: `mytoken|PING_STATUS`

## PyInstaller

When building an exe, include `dcs_ru_common.py` (e.g. `--hidden-import=dcs_ru_common`, or ship the file next to the entrypoint).
