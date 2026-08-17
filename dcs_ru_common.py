"""
Shared helpers for DCS Norway Remote Updater (Control, Node, Discord Bot).

- Unified master_config schema
- TCP command auth wrapping
- DCS version scraping with primary + fallback URLs
- .env loading and GitHub API auth headers
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("dcs_ru_common")

MASTER_CONFIG_FILE = "master_config.json"
_DOTENV_LOADED = False

DCS_UPDATE_URLS = (
    "https://updates.digitalcombatsimulator.com/",
    "https://www.digitalcombatsimulator.com/en/news/",
)

SRS_GITHUB_REPO = "ciribob/DCS-SimpleRadioStandalone"
SRS_AUTOCONNECT_LUA = "DCS-SRS-AutoConnectGameGUI.lua"
_UNKNOWN_SRS_VERSIONS = frozenset({"", "unknown", "missing", "not set", "—", "-"})


def normalize_release_tag(value: Any) -> str:
    return str(value or "").strip().lstrip("vV").strip()


def parse_srs_autoconnect_version_line(line: str) -> str:
    """Installed SRS version is the first line of DCS-SRS-AutoConnectGameGUI.lua."""
    text = str(line or "").strip()
    match = re.search(r"(\d+(?:\.\d+){1,})", text)
    if match:
        return match.group(1)
    cleaned = text.lstrip("-").strip()
    if cleaned.lower().startswith("version"):
        cleaned = cleaned[7:].strip()
    return cleaned


def srs_version_is_current(installed: Any, latest: Any) -> bool:
    installed_tag = normalize_release_tag(installed).lower()
    latest_tag = normalize_release_tag(latest).lower()
    if installed_tag in _UNKNOWN_SRS_VERSIONS or latest_tag in _UNKNOWN_SRS_VERSIONS:
        return False
    return installed_tag == latest_tag

VERSION_PATTERNS = (
    r"Latest stable version is\s*([\d\.]+)",
    r"stable version[^0-9]*([\d]+\.[\d]+\.[\d]+\.[\d]+)",
    r"(\d+\.\d+\.\d+\.\d+)",
)

DEFAULT_MASTER_CONFIG: dict[str, Any] = {
    "auth_token": "",
    "servers": [],
    "discord": {
        "panel_channel_id": None,
        "panel_message_id": None,
    },
}

NODE_SETTINGS_DEFAULTS: dict[str, Any] = {
    "dcs_main_folder": r"D:\DCS",
    "preserve_mission_scripting": True,
    "network_port": "1015",
    "bind_address": "0.0.0.0",
    "auth_token": "",
    "reboot_after_deployment": True,
    "github_check_interval": 43200,
    "watchdog_enabled": True,
    "watchdog_interval_seconds": 300,
    "auto_restart_dcs": True,
    "dcs_server_exe": "",
    "dcs_server_process_names": [],
    "srs_install_folder": "",
}

NODE_LOCAL_ONLY_SETTING_KEYS = (
    "dcs_main_folder",
    "dcs_server_exe",
    "dcs_server_process_names",
    "srs_install_folder",
)

NODE_GITHUB_INTERVAL_CHOICES: tuple[tuple[int, str], ...] = (
    (600, "Every 10 minutes"),
    (3600, "Every 1 Hour"),
    (43200, "Every 12 Hours"),
    (-1, "Disabled"),
)


def github_interval_label(seconds: Any) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = 43200
    for choice, label in NODE_GITHUB_INTERVAL_CHOICES:
        if choice == value:
            return label
    return "Every 12 Hours"


def github_interval_seconds(label: str) -> int:
    text = str(label or "")
    for seconds, choice in NODE_GITHUB_INTERVAL_CHOICES:
        if choice == text:
            return seconds
    if "10 minutes" in text:
        return 600
    if "1 Hour" in text:
        return 3600
    if "12 Hours" in text:
        return 43200
    return -1


def sanitize_node_settings(
    incoming: Optional[dict] = None,
    existing: Optional[dict] = None,
    remote: bool = False,
) -> dict[str, Any]:
    """Merge known Node settings with defaults. Unknown keys on existing are kept.

    Path/exe settings are local-only: a remote payload cannot overwrite them.
    """
    merged: dict[str, Any] = dict(NODE_SETTINGS_DEFAULTS)
    if isinstance(existing, dict):
        for key, value in existing.items():
            merged[key] = value
    if not isinstance(incoming, dict):
        return merged

    bool_keys = {
        "preserve_mission_scripting",
        "reboot_after_deployment",
        "watchdog_enabled",
        "auto_restart_dcs",
    }
    for key in NODE_SETTINGS_DEFAULTS:
        if key not in incoming:
            continue
        if remote and key in NODE_LOCAL_ONLY_SETTING_KEYS:
            continue
        value = incoming[key]
        if key in bool_keys:
            if isinstance(value, str):
                merged[key] = value.strip().lower() in {"1", "true", "yes", "on"}
            else:
                merged[key] = bool(value)
        elif key == "github_check_interval":
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                merged[key] = NODE_SETTINGS_DEFAULTS[key]
        elif key == "watchdog_interval_seconds":
            try:
                seconds = int(value)
            except (TypeError, ValueError):
                seconds = NODE_SETTINGS_DEFAULTS[key]
            merged[key] = max(60, seconds)
        elif key == "network_port":
            text = str(value).strip()
            try:
                port = int(text)
            except (TypeError, ValueError):
                continue
            if 1 <= port <= 65535:
                merged[key] = str(port)
        elif key == "dcs_server_process_names":
            if isinstance(value, str):
                merged[key] = [part.strip() for part in value.split(",") if part.strip()]
            elif isinstance(value, list):
                merged[key] = [str(part).strip() for part in value if str(part).strip()]
        elif key == "bind_address":
            merged[key] = str(value).strip() or "0.0.0.0"
        elif key in ("dcs_main_folder", "auth_token", "dcs_server_exe", "srs_install_folder"):
            merged[key] = str(value).strip() if value is not None else ""
    return merged


def wrap_command(command: str, auth_token: Optional[str] = None) -> str:
    """Build a wire payload. When auth_token is set: TOKEN|COMMAND\\n"""
    cmd = command.strip()
    token = (auth_token or "").strip()
    if token:
        return f"{token}|{cmd}\n"
    return cmd if cmd.endswith("\n") else cmd + "\n"


def parse_authenticated_command(raw: str, expected_token: Optional[str] = None) -> tuple[Optional[str], bool]:
    """
    Parse an inbound TCP message.
    Returns (command, authorized).
    If expected_token is empty, all commands are authorized (legacy mode).
    """
    message = (raw or "").strip()
    if not message:
        return None, False

    expected = (expected_token or "").strip()

    if "|" in message:
        token, _, command = message.partition("|")
        command = command.strip()
        if not command:
            return None, False
        if not expected:
            return command, True
        return command, token.strip() == expected

    # Legacy plain command (no TOKEN| prefix)
    if not expected:
        return message, True
    return message, False


def normalize_server(entry: dict) -> dict:
    """Normalize a server entry to {name, ip, port} (port as string)."""
    return {
        "name": str(entry.get("name") or entry.get("server_name") or "Unknown").strip(),
        "ip": str(entry.get("ip") or entry.get("address") or "").strip(),
        "port": str(entry.get("port") or "1015").strip(),
    }


def migrate_master_dict(data: dict) -> dict:
    """Upgrade legacy cluster_nodes / missing keys into the unified schema."""
    result = {
        "auth_token": str(data.get("auth_token") or ""),
        "servers": [],
        "discord": {
            "panel_channel_id": None,
            "panel_message_id": None,
        },
    }

    discord = data.get("discord") if isinstance(data.get("discord"), dict) else {}
    result["discord"]["panel_channel_id"] = discord.get("panel_channel_id")
    result["discord"]["panel_message_id"] = discord.get("panel_message_id")

    servers_raw = data.get("servers")
    if isinstance(servers_raw, list) and servers_raw:
        result["servers"] = [normalize_server(s) for s in servers_raw if isinstance(s, dict)]
    else:
        legacy = data.get("cluster_nodes")
        if isinstance(legacy, list):
            result["servers"] = [normalize_server(s) for s in legacy if isinstance(s, dict)]

    return result


def load_master_config(path: str = MASTER_CONFIG_FILE) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_MASTER_CONFIG)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_MASTER_CONFIG)
        return migrate_master_dict(data)
    except Exception as e:
        logger.error("Could not read master config %s: %s", path, e)
        return dict(DEFAULT_MASTER_CONFIG)


def save_master_config(config: dict, path: str = MASTER_CONFIG_FILE) -> None:
    payload = migrate_master_dict(config if isinstance(config, dict) else {})
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error("Could not save master config %s: %s", path, e)


def scrape_dcs_latest_version(timeout: float = 10.0) -> Optional[str]:
    """
    Fetch latest stable DCS version from Eagle Dynamics pages.
    Tries multiple URLs and regex patterns. Returns None on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    for url in DCS_UPDATE_URLS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                html = response.read().decode("utf-8", errors="ignore")
            cleaned = " ".join(html.split())
            for pattern in VERSION_PATTERNS:
                match = re.search(pattern, cleaned, flags=re.IGNORECASE)
                if match:
                    version = match.group(1).strip()
                    if re.fullmatch(r"\d+(?:\.\d+){2,}", version):
                        logger.info("DCS version scrape OK from %s -> %s", url, version)
                        return version
        except urllib.error.HTTPError as e:
            logger.warning("DCS scrape HTTP %s from %s", e.code, url)
        except Exception as e:
            logger.warning("DCS scrape failed for %s: %s", url, e)

    return None


def get_app_base_dir() -> str:
    """Directory of the running app (.exe or entry .py), for locating .env."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if main_file:
        return os.path.dirname(os.path.abspath(main_file))
    return os.path.dirname(os.path.abspath(__file__))


def _parse_dotenv_line(line: str) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].strip()
    if "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not key:
        return None
    return key, value


def load_dotenv(extra_paths: Optional[list[str]] = None) -> None:
    """Load KEY=VALUE pairs from .env without overwriting existing env vars."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return

    candidates = [
        os.path.join(get_app_base_dir(), ".env"),
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    if extra_paths:
        candidates.extend(extra_paths)

    seen_paths: set[str] = set()
    for path in candidates:
        norm = os.path.normpath(path)
        if norm in seen_paths or not os.path.isfile(norm):
            continue
        seen_paths.add(norm)
        try:
            with open(norm, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = _parse_dotenv_line(line)
                    if not parsed:
                        continue
                    key, value = parsed
                    if key not in os.environ:
                        os.environ[key] = value
            logger.info("Loaded environment from %s", norm)
        except Exception as e:
            logger.warning("Could not read .env from %s: %s", norm, e)

    _DOTENV_LOADED = True


def get_github_token() -> Optional[str]:
    """Read GitHub token from environment / .env (never from source code)."""
    load_dotenv()
    token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("DCS_GITHUB_TOKEN")
        or ""
    ).strip()
    return token or None


def github_api_headers(user_agent: str = "DCS-Norway-Remote-Updater") -> dict[str, str]:
    """Standard GitHub REST headers, with Authorization when GITHUB_TOKEN is set."""
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github.v3+json",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_latest_srs_release(timeout: float = 15.0) -> Optional[dict[str, str]]:
    """Return {tag, name, url} for DCS-SimpleRadioStandalone-x.x.x.x.zip, or None."""
    url = f"https://api.github.com/repos/{SRS_GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(
            url,
            headers=github_api_headers("DCS-Norway-Remote-Updater"),
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.warning("SRS GitHub latest release failed: %s", e)
        return None
    tag = str(data.get("tag_name") or "").lstrip("vV").strip()
    zip_asset = None
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        lowered = name.lower()
        if lowered.startswith("dcs-simpleradiostandalone-") and lowered.endswith(".zip"):
            zip_asset = asset
            break
    if zip_asset is None:
        logger.warning("SRS release %s has no DCS-SimpleRadioStandalone-*.zip asset", tag)
        return None
    download_url = str(zip_asset.get("browser_download_url") or "")
    if not tag or not download_url:
        return None
    return {
        "tag": tag,
        "name": str(zip_asset.get("name") or ""),
        "url": download_url,
    }


def get_discord_bot_token() -> Optional[str]:
    """Read Discord bot token from environment / .env (never from source code)."""
    load_dotenv()
    token = (
        os.environ.get("DISCORD_BOT_TOKEN")
        or os.environ.get("DCS_DISCORD_BOT_TOKEN")
        or ""
    ).strip()
    return token or None


load_dotenv()
