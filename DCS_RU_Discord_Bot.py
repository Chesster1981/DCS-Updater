import discord
from discord import app_commands
from discord.ext import tasks, commands
import json
import asyncio
import logging
import aiohttp
import os
import sys
import subprocess
import re
import time
from datetime import datetime

from dcs_ru_common import (
    DCS_UPDATE_URLS,
    VERSION_PATTERNS,
    get_discord_bot_token,
    github_api_headers,
    load_master_config,
    resolve_master_config_path,
    save_master_config,
    wrap_command,
    fetch_latest_srs_release,
)

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DCS_Discord_Bot")

CURRENT_BOT_VERSION = "2.1.76"
GITHUB_REPO = "Chesster1981/DCS-Updater"
URL_GITHUB_API = "https://api.github.com/repos/"
BOT_SELF_UPDATE_FILES = ("DCS_RU_Discord_Bot.py", "dcs_ru_common.py")
BOT_GITHUB_CHECK_DELAY_SECONDS = 20
BOT_GITHUB_CHECK_MINUTES = 5
PANEL_CHANNEL_GUIDE_MARKER = "<!-- dcs-panel-guide -->"
PANEL_CHANNEL_GUIDE = """**DCS Norway — update panel**

Use `/dcs-panel-init` to create or restore the live panel at the bottom of this channel.

**Status icon next to the server name (traffic light)**
🟢 **UP TO DATE** — Node answers, DCS is OK (or idle on purpose), SRS is running (when configured), versions match.
⚠️ **UPDATE READY** — DCS version is behind the latest ED release.
⚠️ **SRS OUTDATED** — SRS version is behind the latest GitHub release.
⚠️ **SRS DOWN** — SRS is configured on the node, but the SR-Server process is not running (DCS is OK). Caution.
⚠️ **SRS + DCS DOWN** — both SRS and DCS are down, but DCS has never run (NEVER_STARTED). Caution.
🛑 **SRS + DCS DOWN** — both SRS and DCS are down after DCS had been running (UNHEALTHY/DEAD). Red.
⏸️ **NOT STARTED** — DCS has never been started on the node (intentional idle). Yellow status (update/SRS) overrides this.
⏳ **STARTING** — DCS process is running, but the port is not answering yet.
🛑 **DCS DOWN** — DCS had been running and then crashed/stopped (UNHEALTHY/DEAD), while SRS is running.
🔐 **UNAUTHORIZED** — auth_token does not match between bot and node.
🔴 **OFFLINE** — No reply from the node (down, wrong IP/port, or firewall).

**In the status box under each server**
ℹ️ Status text (e.g. UP TO DATE, SRS DOWN, DCS DOWN)
⚙️ Installed DCS version
📻 Installed SRS version (from `scripts/DCS-SRS-AutoConnectGameGUI.lua` on the node)
🖥️ Task / machine status (e.g. Ready, Action required, Port pending)

**Footer under the panel**
ED Release Version — latest DCS World version from ED
SRS Release Version — latest SRS from GitHub (ciribob/DCS-SimpleRadioStandalone)
Bot version — version of this Discord bot

**Buttons and menu**
🔄 **Refresh Server Status** — manually refresh the panel
🚀 **Select Actions** — after choosing from the dropdown: opens the action menu (start/restart, update, reboot)
Dropdown **Select server(s)** — pick one or more yellow/red servers. Selection is kept across automatic refresh (every 30 s).
✅ **All servers operational** — no yellow/red servers right now

**Deploy logic**
• Only SRS outdated → SRS update only (`TRIGGER_SRS_UPDATE`), DCS is not touched.
• Only DCS outdated → DCS update only.
• Both outdated → DCS first, then SRS.
• Idle server (DCS not started) can still receive an SRS update.
• SRS DOWN alone (without a version mismatch) can be restarted from the panel (Restart SRS).
• Yellow/red status enables the action button: Update, Restart DCS, Restart SRS, or Reboot.

**DM alerts (attention round)**
Sent on **OFFLINE** and **DCS DOWN** (crash) — not when SRS is down or when a new DCS/SRS release becomes available.
Flow: 5 min grace → automatic DCS restart via node → 10 min wait → DM channel members if the server is still not online.

**Slash commands**
`/dcs-panel-wiki` — temporary status-logic explanation (removed when you switch channel, close Discord, or press Close).
`/dcs-panel-init` · `/dcs-panel-update`

**Other symbols**
🛸 Panel title · ⏳ / 🎉 / ❌ Messages during the deploy queue"""
# Set to a Discord username to DM only that person while testing.
# None = page panel-channel members one at a time (online first).
STATUS_ALERT_TEST_USERNAME = None
STATUS_ALERT_TEST_NAME_ALIASES = ("Chesster", "Chesster1981")
STATUS_ALERT_DELAY_SECONDS = 300
STATUS_RESTART_WAIT_SECONDS = 600
STATUS_LOG_FILE = "dcs_ru_server_status.log"
STATUS_LOG_REPEAT_SECONDS = 60
ATTENTION_REPLY_TIMEOUT_SECONDS = 300
ATTENTION_QUESTION = "Are you available to attend the issue ? Please reply (Yes/No)"
ATTENTION_YES_REPLIES = {"ja", "yes"}
ATTENTION_NO_REPLIES = {"no", "nei"}
STATUS_MESSAGE_DISMISS_SECONDS = 10
WIKI_AUTO_DISMISS_SECONDS = 900
BOT_PID_FILE = "dcs_ru_discord_bot.pid"
DISCORD_SELECT_MAX_OPTIONS = 25
STATUS_UP_TO_DATE = "UP TO DATE"
STATUS_SRS_OUTDATED = "SRS OUTDATED"
STATUS_SRS_DOWN = "SRS DOWN"
STATUS_SRS_AND_DCS_DOWN = "SRS + DCS DOWN"
STATUS_RUNNING = {"UP TO DATE", "UPDATE READY", STATUS_SRS_OUTDATED, STATUS_SRS_DOWN}
STATUS_DOWN = {"DCS DOWN", "OFFLINE"}
STATUS_BOOT = {"DCS STARTING", "DCS NOT STARTED"}
HEALTH_CRASHED = {"DEAD", "UNHEALTHY"}
TASK_AWAITING_OPERATOR = "Action required"

PANEL_ACTION_LABELS = {
    "restart_dcs": "Start/Restart DCS",
    "restart_srs": "Start/Restart SRS",
    "update_dcs": "Update DCS",
    "update_srs": "Update SRS",
    "update": "Update",
    "reboot": "Reboot Server",
}
STILL_ACTIVE = 259


def _bot_pid_path():
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    base = os.path.join(appdata, "DCS_Norway_Discord_Bot")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, BOT_PID_FILE)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def _process_exe_name(pid: int) -> str:
    if pid <= 0 or sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buf))
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
        handle, 0, buf, ctypes.byref(size)
    )
    ctypes.windll.kernel32.CloseHandle(handle)
    if not ok:
        return ""
    return os.path.basename(buf.value).lower()


def _running_under_nssm() -> bool:
    try:
        return _process_exe_name(os.getppid()) in {"nssm.exe", "nssm64.exe", "nssm32.exe"}
    except Exception:
        return False


def ensure_single_bot_instance():
    """Record this PID. Do not scan processes with PowerShell — that hangs under NSSM."""
    path = _bot_pid_path()
    my_pid = os.getpid()
    old_pid = None
    try:
        if os.path.exists(path):
            old_pid = int(open(path, encoding="utf-8").read().strip())
    except Exception as e:
        logger.warning("Could not inspect previous bot pid file: %s", e)

    if old_pid and old_pid != my_pid and _pid_is_running(old_pid):
        # NSSM restart already stops the previous service process. Wait for it
        # instead of launching PowerShell/WMI, which freezes Session 0.
        deadline = time.time() + (8 if _running_under_nssm() else 1)
        while time.time() < deadline and _pid_is_running(old_pid):
            time.sleep(0.25)
        if _pid_is_running(old_pid):
            logger.warning("Stopping previous Discord Bot process pid=%s", old_pid)
            _terminate_pid(old_pid)
            time.sleep(1.0)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(my_pid))
    except Exception as e:
        logger.warning("Could not write bot pid file: %s", e)


class DCSClusterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(command_prefix="!", intents=intents)

        self.config_path = resolve_master_config_path("master_config.json")
        self.deployment_queue = asyncio.Queue()
        self.is_processing_queue = False
        self.socket_timeout = 4.0

        self.active_panel_view = None
        self.panel_channel_id = None
        self.panel_message_id = None
        self.panel_selected_server_names = []
        self._panel_selection_rev = 0
        self._panel_update_lock = asyncio.Lock()
        self.cached_dcs_version = "Unknown"
        self.last_cache_time = 0
        self.cached_srs_version = "Unknown"
        self.last_srs_cache_time = 0
        self.auth_token = ""
        self._restoring_panel = False
        self._self_updating = False
        self._last_node_status = {}
        self._pending_status_alerts = {}
        self._last_status_log = {}
        self._status_alert_lock = asyncio.Lock()
        self._attention_task = None
        self._attention_wait = None
        self._attention_issue_body = ""
        self._attention_stop = asyncio.Event()
        self._attention_contacted = []
        self._attention_server_names = []
        self._wiki_sessions = {}

    def dismiss_status_message_later(self, message):
        """Delete a transient Discord status message after a short delay."""
        if message is None:
            return

        async def _delete():
            try:
                await asyncio.sleep(STATUS_MESSAGE_DISMISS_SECONDS)
                await message.delete()
            except Exception:
                pass

        asyncio.create_task(_delete())

    def load_cluster_config(self):
        data = load_master_config(self.config_path)
        self.auth_token = str(data.get("auth_token") or "")
        discord_meta = data.get("discord") or {}

        def _as_id(value):
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        channel_id = _as_id(discord_meta.get("panel_channel_id"))
        message_id = _as_id(discord_meta.get("panel_message_id"))
        if channel_id and not self.panel_channel_id:
            self.panel_channel_id = channel_id
        if message_id and not self.panel_message_id:
            self.panel_message_id = message_id
        return data

    def load_persisted_panel_selection(self):
        """Load dropdown selection from disk once (not on every status refresh)."""
        try:
            data = load_master_config(self.config_path)
            saved = (data.get("discord") or {}).get("panel_selected_servers")
            if isinstance(saved, list):
                self.panel_selected_server_names = [
                    str(name).strip() for name in saved if str(name).strip()
                ]
        except Exception as e:
            logger.warning("Could not load persisted panel selection: %s", e)

    def load_cluster_nodes(self):
        data = self.load_cluster_config()
        return data.get("servers", [])

    def persist_panel_ids(self):
        data = load_master_config(self.config_path)
        discord_meta = dict(data.get("discord") or {})
        discord_meta["panel_channel_id"] = self.panel_channel_id
        discord_meta["panel_message_id"] = self.panel_message_id
        discord_meta["panel_selected_servers"] = list(self.panel_selected_server_names or [])
        data["discord"] = discord_meta
        save_master_config(data, self.config_path)
        logger.info(
            "Persisted Discord panel IDs (channel=%s message=%s selection=%s)",
            self.panel_channel_id,
            self.panel_message_id,
            self.panel_selected_server_names,
        )

    def persist_panel_selection(self):
        """Write current dropdown selection to master_config without touching other fields."""
        try:
            data = load_master_config(self.config_path)
            discord_meta = dict(data.get("discord") or {})
            discord_meta["panel_selected_servers"] = list(self.panel_selected_server_names or [])
            if self.panel_channel_id is not None:
                discord_meta["panel_channel_id"] = self.panel_channel_id
            if self.panel_message_id is not None:
                discord_meta["panel_message_id"] = self.panel_message_id
            data["discord"] = discord_meta
            save_master_config(data, self.config_path)
        except Exception as e:
            logger.warning("Could not persist panel selection: %s", e)

    async def fetch_latest_dcs_release(self):
        """Async scrape with the same URL/pattern set as Control/Node."""
        current_time = asyncio.get_event_loop().time()
        if self.cached_dcs_version != "Unknown" and (current_time - self.last_cache_time < 900):
            return self.cached_dcs_version

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                for url in DCS_UPDATE_URLS:
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                            if response.status != 200:
                                logger.warning("DCS scrape HTTP %s from %s", response.status, url)
                                continue
                            html_content = await response.text()
                            cleaned_html = " ".join(html_content.split())
                            for pattern in VERSION_PATTERNS:
                                match = re.search(pattern, cleaned_html, flags=re.IGNORECASE)
                                if match:
                                    version = match.group(1).strip()
                                    if re.fullmatch(r"\d+(?:\.\d+){2,}", version):
                                        self.cached_dcs_version = version
                                        self.last_cache_time = current_time
                                        logger.info("DCS Scraper Success: v%s from %s", version, url)
                                        return self.cached_dcs_version
                    except Exception as e:
                        logger.warning("DCS scrape failed for %s: %s", url, e)
        except Exception as e:
            logger.error("Failed to execute HTML scraping against DCS update gateway: %s", e)

        return self.cached_dcs_version

    async def fetch_latest_srs_release_cached(self):
        current_time = asyncio.get_event_loop().time()
        if self.cached_srs_version != "Unknown" and (current_time - self.last_srs_cache_time < 900):
            return self.cached_srs_version
        try:
            release = await asyncio.to_thread(fetch_latest_srs_release)
            if release and release.get("tag"):
                self.cached_srs_version = release["tag"]
                self.last_srs_cache_time = current_time
                logger.info("SRS GitHub latest: v%s", self.cached_srs_version)
        except Exception as e:
            logger.warning("SRS GitHub latest fetch failed: %s", e)
        return self.cached_srs_version

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

    def _bot_install_dir(self):
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.abspath(__file__))

    async def check_github_self_update(self, apply=True):
        """Compare GitHub latest release and install source files if newer.

        Returns a short status dict for slash-command feedback.
        """
        if self._self_updating:
            return {"ok": False, "message": "A self-update is already in progress."}
        if getattr(sys, "frozen", False):
            return {"ok": False, "message": "Self-update is not available for a compiled bot."}

        headers = github_api_headers("DCS-Norway-Discord-Bot")
        url = f"{URL_GITHUB_API}{GITHUB_REPO}/releases/latest"
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        logger.warning("GitHub bot update check HTTP %s", response.status)
                        return {
                            "ok": False,
                            "message": f"GitHub check failed (HTTP {response.status}).",
                        }
                    data = await response.json()
            latest = str(data.get("tag_name", "")).lstrip("v").strip()
            tag = str(data.get("tag_name", "")).strip() or f"v{latest}"
            logger.info(
                "Discord Bot v%s | GitHub latest v%s",
                CURRENT_BOT_VERSION,
                latest or "?",
            )
            if not latest:
                return {"ok": False, "message": "GitHub did not return a latest version tag."}
            if self._version_tuple(latest) <= self._version_tuple(CURRENT_BOT_VERSION):
                return {
                    "ok": True,
                    "updated": False,
                    "message": (
                        f"Already on latest: **v{CURRENT_BOT_VERSION}** "
                        f"(GitHub: v{latest})."
                    ),
                }

            downloads = {}
            assets = {str(a.get("name", "")): a.get("browser_download_url") for a in data.get("assets") or []}
            download_headers = github_api_headers("DCS-Norway-Discord-Bot")
            async with aiohttp.ClientSession(headers=download_headers) as session:
                for filename in BOT_SELF_UPDATE_FILES:
                    content = None
                    asset_url = assets.get(filename)
                    if asset_url:
                        async with session.get(asset_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                            if response.status == 200:
                                content = await response.read()
                    if not content:
                        raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{tag}/{filename}"
                        async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                            if response.status == 200:
                                content = await response.read()
                    if not content:
                        logger.error("Could not download %s for bot self-update.", filename)
                        return {
                            "ok": False,
                            "message": f"Could not download `{filename}` from GitHub.",
                        }
                    downloads[filename] = content

            logger.info("Newer Discord Bot v%s available — applying self-update.", latest)
            result = {
                "ok": True,
                "updated": True,
                "downloads": downloads,
                "message": (
                    f"Update found: **v{CURRENT_BOT_VERSION}** → **v{latest}**. "
                    "Restarting now…"
                ),
            }
            if apply:
                await self._apply_self_update(downloads)
            return result
        except Exception as e:
            logger.error("GitHub bot update check failed: %s", e)
            return {"ok": False, "message": f"GitHub check failed: {e}"}

    async def _apply_self_update(self, downloads: dict):
        self._self_updating = True
        install_dir = self._bot_install_dir()
        try:
            for filename, content in downloads.items():
                target = os.path.join(install_dir, filename)
                tmp_path = target + ".new"
                with open(tmp_path, "wb") as f:
                    f.write(content)
                os.replace(tmp_path, target)
                logger.info("Updated %s", target)
        except Exception as e:
            logger.error("Failed to write bot update files: %s", e)
            self._self_updating = False
            return

        script = os.path.abspath(sys.argv[0] if sys.argv else __file__)
        cwd = install_dir or os.getcwd()
        if _running_under_nssm():
            logger.info(
                "Updated Discord Bot files in %s; exiting so NSSM can restart the service",
                cwd,
            )
            os._exit(0)

        argv = [sys.executable, script, *sys.argv[1:]]
        logger.info("Relaunching Discord Bot from %s", script)
        kwargs = {
            "cwd": cwd,
            "env": os.environ.copy(),
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        subprocess.Popen(argv, **kwargs)
        # Do not await close() — this runs inside the update loop and would deadlock,
        # leaving the old process alive so the panel flips between versions.
        os._exit(0)

    async def sync_slash_commands(self, guild=None):
        """Register slash commands. Guild sync is instant; global sync can take up to an hour."""
        try:
            synced = await self.tree.sync()
            logger.info("Synced %s global slash command(s)", len(synced))
        except Exception as e:
            logger.error("Global slash sync failed: %s", e)

        targets = []
        if guild is not None:
            targets.append(guild)
        elif self.panel_channel_id:
            try:
                channel = await self.get_panel_channel()
                if channel and getattr(channel, "guild", None):
                    targets.append(channel.guild)
            except Exception as e:
                logger.warning("Could not resolve panel guild for slash sync: %s", e)

        for target_guild in targets:
            try:
                guild_synced = await self.tree.sync(guild=target_guild)
                logger.info(
                    "Synced %s guild slash command(s) for '%s'",
                    len(guild_synced),
                    target_guild.name,
                )
            except Exception as e:
                logger.warning("Guild slash sync failed for %s: %s", target_guild.name, e)

    def _owns_panel_refresh(self) -> bool:
        """Only the PID-file owner may edit the live panel (avoids dual-bot flicker)."""
        path = _bot_pid_path()
        try:
            if os.path.exists(path):
                return int(open(path, encoding="utf-8").read().strip()) == os.getpid()
        except Exception:
            pass
        return True

    async def setup_hook(self):
        await self.sync_slash_commands()
        self.queue_processor_loop.start()
        self.persistent_panel_refresh_loop.start()
        self.github_self_update_loop.start()
        self.load_cluster_config()
        self.load_persisted_panel_selection()
        token_set = bool(str(self.auth_token or "").strip())
        logger.info(
            "Discord Bot v%s running. Config: %s (auth_token %s, %s server(s)). Auto panel restore enabled.",
            CURRENT_BOT_VERSION,
            self.config_path,
            "set" if token_set else "EMPTY",
            len(self.load_cluster_nodes()),
        )

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)
        # Defer slightly so guild/channel cache is warm after PC reboot
        await asyncio.sleep(2)
        await self.restore_or_recreate_panel()
        await self.sync_slash_commands()

    async def get_panel_channel(self):
        if not self.panel_channel_id:
            return None
        channel = self.get_channel(int(self.panel_channel_id))
        if channel is None:
            channel = await self.fetch_channel(int(self.panel_channel_id))
        return channel

    async def restore_or_recreate_panel(self):
        """
        After bot/PC restart: reattach to the saved panel message.
        If the message is gone or uneditable, recreate the panel in the same channel.
        """
        if self._restoring_panel:
            return
        self._restoring_panel = True
        try:
            self.load_cluster_config()
            if not self.panel_channel_id:
                logger.info(
                    "No saved panel_channel_id — run /dcs-panel-init once to enable auto-restore."
                )
                return

            view = LiveControlPanelView(self)
            self.add_view(view)
            self.active_panel_view = view

            for attempt in range(1, 4):
                try:
                    channel = await self.get_panel_channel()
                    if channel is None:
                        raise RuntimeError("panel channel not found")

                    if self.panel_message_id:
                        message = await channel.fetch_message(int(self.panel_message_id))
                        new_embed = await view.generate_embed(guild=channel.guild)
                        await message.edit(embed=new_embed, view=view)
                        logger.info(
                            "Restored persistent panel (attempt %s) message=%s",
                            attempt,
                            self.panel_message_id,
                        )
                        await self.ensure_panel_channel_guide(channel)
                        await self.purge_old_bot_messages(channel)
                        return

                    raise RuntimeError("no panel_message_id saved")
                except Exception as e:
                    logger.warning("Panel restore attempt %s failed: %s", attempt, e)
                    await asyncio.sleep(2 * attempt)

            try:
                channel = await self.get_panel_channel()
                if channel is None:
                    logger.error(
                        "Cannot recreate panel — channel %s unavailable", self.panel_channel_id
                    )
                    return

                logger.info(
                    "Recreating Discord panel automatically in channel %s", self.panel_channel_id
                )
                await self.purge_old_bot_messages(channel)
                await self.ensure_panel_channel_guide(channel)
                embed = await view.generate_embed(guild=channel.guild)
                message = await channel.send(embed=embed, view=view)
                self.panel_message_id = message.id
                self.panel_channel_id = channel.id
                self.active_panel_view = view
                self.persist_panel_ids()
                logger.info("Auto-recreated panel message ID: %s", message.id)
            except Exception as e:
                logger.error("Automatic panel recreate failed: %s", e)
        finally:
            self._restoring_panel = False

    async def purge_old_bot_messages(self, channel):
        """Remove leftover bot chatter; keep the live panel and pinned guide."""
        keep_ids = set()
        if self.panel_message_id:
            try:
                keep_ids.add(int(self.panel_message_id))
            except (TypeError, ValueError):
                pass
        try:
            async for message in channel.history(limit=80):
                if message.author.id != self.user.id:
                    continue
                if message.id in keep_ids:
                    continue
                content = message.content or ""
                if PANEL_CHANNEL_GUIDE_MARKER in content:
                    continue
                # Delete embeds/components and plain status text (update/restart notices, logs).
                try:
                    await message.delete()
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
        except Exception as e:
            logger.error("Error purging historic channel messages: %s", e)

    async def ensure_panel_channel_guide(self, channel):
        """Refresh the pinned legend message after restart/init."""
        if channel is None:
            return None
        body = f"{PANEL_CHANNEL_GUIDE_MARKER}\n{PANEL_CHANNEL_GUIDE}"
        try:
            async for message in channel.history(limit=30):
                if message.author.id == self.user.id and PANEL_CHANNEL_GUIDE_MARKER in (
                    message.content or ""
                ):
                    if message.content != body:
                        await message.edit(content=body)
                    try:
                        await message.pin()
                    except Exception:
                        pass
                    return message
            message = await channel.send(body)
            try:
                await message.pin()
            except Exception:
                pass
            logger.info("Posted panel channel guide in #%s", getattr(channel, "name", channel.id))
            return message
        except Exception as e:
            logger.error("Failed to ensure panel channel guide: %s", e)
            return None

    @staticmethod
    def _member_matches_alert_name(member, wanted_name):
        wanted = str(wanted_name or "").strip().lower()
        if not wanted:
            return False
        names = [
            getattr(member, "name", "") or "",
            getattr(member, "global_name", None) or "",
            getattr(member, "display_name", None) or "",
            getattr(member, "nick", None) or "",
        ]
        for name in names:
            lowered = name.strip().lower()
            if not lowered:
                continue
            if lowered == wanted or lowered.startswith(wanted) or wanted in lowered:
                return True
        return False

    def _member_matches_test_user(self, member):
        for alias in STATUS_ALERT_TEST_NAME_ALIASES:
            if self._member_matches_alert_name(member, alias):
                return True
        return False

    def _status_log_path(self):
        return os.path.join(self._bot_install_dir(), STATUS_LOG_FILE)

    def _append_status_log_line(self, line):
        try:
            with open(self._status_log_path(), "a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except Exception as e:
            logger.warning("Could not write server status log: %s", e)
        logger.info("Server status log: %s", line)

    @staticmethod
    def _is_server_online(snap):
        status = snap.get("status_text") or ""
        return status in STATUS_RUNNING

    def _log_non_online_status(self, snap, extra="", force=False):
        """Append non-online server status to dcs_ru_server_status.log next to the bot."""
        if snap is None or self._is_server_online(snap):
            return
        key = snap.get("key") or snap.get("name") or "?"
        name = snap.get("name") or key
        status = snap.get("status_text") or "UNKNOWN"
        task = snap.get("task_info") or ""
        health = str(snap.get("dcs_health") or "").strip() or "-"
        signature = f"{status}|{task}|{health}|{extra}"
        now = time.monotonic()
        previous = self._last_status_log.get(key) or {}
        if (
            not force
            and previous.get("signature") == signature
            and now - previous.get("at", 0) < STATUS_LOG_REPEAT_SECONDS
        ):
            return
        self._last_status_log[key] = {"signature": signature, "at": now}
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [stamp, name, status]
        if task:
            parts.append(task)
        if health and health != "-":
            parts.append(f"health={health}")
        if extra:
            parts.append(extra)
        self._append_status_log_line(" | ".join(parts))

    async def _request_node_restart(self, snap):
        name = snap.get("name") or snap.get("key") or "unknown"
        ip = snap.get("ip")
        port = snap.get("port")
        self._log_non_online_status(snap, extra="action=RESTART_DCS", force=True)
        if not ip or not port:
            logger.warning("Cannot restart %s — missing ip/port", name)
            self._log_non_online_status(snap, extra="restart=skipped_no_address", force=True)
            return "missing-address"
        logger.info("Requesting DCS restart on %s (%s:%s)", name, ip, port)
        answer = await self.send_socket_command(ip, port, "RESTART_DCS")
        if not answer:
            self._log_non_online_status(snap, extra="restart=unreachable", force=True)
            return "unreachable"
        result = "unknown"
        if answer.startswith("{"):
            try:
                result = str(json.loads(answer).get("status") or "unknown")
            except Exception:
                result = answer[:80]
        elif "UNKNOWN_COMMAND" in answer:
            result = "UNKNOWN_COMMAND"
        else:
            result = answer[:80]
        self._log_non_online_status(snap, extra=f"restart={result}", force=True)
        logger.info("DCS restart response from %s: %s", name, result)
        return result

    def _describe_status_alert(self, prev, curr):
        if prev is None:
            logger.info(
                "Status baseline stored for %s: %s (no DM on first poll)",
                curr.get("name"),
                curr.get("status_text"),
            )
            return None

        prev_status = prev.get("status_text") or ""
        curr_status = curr.get("status_text") or ""
        left_up_to_date = prev_status == STATUS_UP_TO_DATE and curr_status != STATUS_UP_TO_DATE

        prev_health = str(prev.get("dcs_health") or "").strip().upper()
        curr_health = str(curr.get("dcs_health") or "").strip().upper()
        prev_running = prev_status in STATUS_RUNNING or prev_health == "HEALTHY"
        curr_down = curr_status in STATUS_DOWN or curr_health in HEALTH_CRASHED
        crashed = prev_running and curr_down and prev_status not in STATUS_BOOT

        if not left_up_to_date and not crashed:
            return None

        name = curr.get("name") or "Unknown server"
        icon = curr.get("icon") or ""
        logger.info(
            "Status alert trigger for %s: %s -> %s (left_up_to_date=%s crashed=%s)",
            name,
            prev_status,
            curr_status,
            left_up_to_date,
            crashed,
        )
        lines = [f"**{name}**"]
        if crashed:
            lines.append("DCS_server.exe appears to have crashed or stopped responding.")
        if left_up_to_date:
            lines.append(
                f"Status changed from :green_circle: **{STATUS_UP_TO_DATE}** "
                f"to {icon} **{curr_status}**."
            )
        else:
            lines.append(f"New status: {icon} **{curr_status}**.")
        ver_info = curr.get("ver_info")
        if ver_info and ver_info != "Unknown":
            lines.append(f"Version: `{ver_info}`")
        task_info = curr.get("task_info")
        if task_info:
            lines.append(f"Detail: {task_info}")
        return "\n".join(lines)

    async def _collect_guild_members(self, guild):
        members = []
        seen = set()

        def _add(member):
            if member is None or getattr(member, "bot", False) or member.id in seen:
                return
            seen.add(member.id)
            members.append(member)

        if not guild.chunked:
            try:
                await guild.chunk()
            except Exception as e:
                logger.warning("Could not chunk guild members for status alerts: %s", e)

        for member in guild.members:
            _add(member)

        if STATUS_ALERT_TEST_USERNAME:
            for alias in STATUS_ALERT_TEST_NAME_ALIASES:
                try:
                    named = guild.get_member_named(alias)
                    _add(named)
                except Exception:
                    pass
                try:
                    queried = await guild.query_members(query=alias, limit=10, cache=True)
                    for member in queried:
                        _add(member)
                except Exception as e:
                    logger.warning("query_members(%s) failed: %s", alias, e)

            if not any(self._member_matches_test_user(member) for member in members):
                try:
                    async for member in guild.fetch_members(limit=None):
                        _add(member)
                except Exception as e:
                    logger.warning("Could not fetch guild members for status alerts: %s", e)

        return members

    async def _status_alert_recipients(self, guild):
        if guild is None:
            logger.warning("Status alert skipped recipient lookup: guild is None")
            return []

        channel = None
        try:
            channel = await self.get_panel_channel()
        except Exception:
            channel = None

        members = await self._collect_guild_members(guild)
        recipients = []
        seen = set()
        for member in members:
            if member.bot or member.id in seen:
                continue
            if channel is not None and not STATUS_ALERT_TEST_USERNAME:
                perms = channel.permissions_for(member)
                if not getattr(perms, "view_channel", False):
                    continue
            if STATUS_ALERT_TEST_USERNAME and not self._member_matches_test_user(member):
                continue
            seen.add(member.id)
            recipients.append(member)

        if STATUS_ALERT_TEST_USERNAME and not recipients:
            sample = [
                f"{m.display_name}/{m.name}"
                for m in members[:12]
                if not m.bot
            ]
            logger.warning(
                "No DM recipient matching %s in guild '%s' (%s cached members). Sample: %s",
                STATUS_ALERT_TEST_NAME_ALIASES,
                guild.name,
                len(members),
                sample,
            )
        recipients.sort(key=self._presence_sort_key)
        return recipients

    @staticmethod
    def _presence_sort_key(member):
        status = getattr(member, "status", discord.Status.offline)
        rank = {
            discord.Status.online: 0,
            discord.Status.idle: 1,
            discord.Status.dnd: 2,
            discord.Status.invisible: 3,
            discord.Status.offline: 4,
        }.get(status, 5)
        name = (getattr(member, "display_name", None) or getattr(member, "name", "") or "").lower()
        return (rank, name)

    @staticmethod
    def _normalize_attention_reply(text):
        return str(text or "").strip().lower().strip("!.? ")

    def _any_server_still_needs_attention(self):
        for snap in self._last_node_status.values():
            status = snap.get("status_text") or ""
            health = str(snap.get("dcs_health") or "").strip().upper()
            if status == STATUS_UP_TO_DATE:
                continue
            if status in STATUS_DOWN or health in HEALTH_CRASHED:
                return True
        return bool(self._pending_status_alerts)

    def _remember_attention_contact(self, member):
        if member is None:
            return
        seen = {existing.id for existing in self._attention_contacted}
        if member.id not in seen:
            self._attention_contacted.append(member)

    def _attention_server_label(self):
        names = [name for name in self._attention_server_names if name]
        if len(names) == 1:
            return f"**{names[0]}** is"
        if len(names) > 1:
            listed = ", ".join(f"**{name}**" for name in names)
            return f"{listed} are"
        return "The server is"

    async def _notify_servers_back_online(self):
        members = list(self._attention_contacted)
        self._attention_contacted = []
        if not members:
            return
        body = (
            f"✅ {self._attention_server_label()} back in **ONLINE** status.\n"
            "No further action is required."
        )
        for member in members:
            try:
                await member.send(body)
                logger.info("Sent ONLINE recovery DM to %s", member.display_name)
            except Exception as e:
                logger.warning(
                    "Failed to send ONLINE recovery DM to %s: %s",
                    getattr(member, "display_name", member),
                    e,
                )

    async def _cancel_attention_round(self, reason=""):
        if reason:
            logger.info("Stopping attention DM round (%s)", reason)
        self._attention_stop.set()
        wait = self._attention_wait
        if wait is not None:
            wait["answer"] = "cancel"
            event = wait.get("event")
            if event is not None and not event.is_set():
                event.set()
        if "recovered" in str(reason or "").lower():
            await self._notify_servers_back_online()

    async def _start_attention_round(self, body, guild):
        self._attention_issue_body = body
        self._attention_server_names = [
            snap.get("name")
            for snap in self._last_node_status.values()
            if snap.get("name") and not self._is_server_online(snap)
        ]
        task = self._attention_task
        if task is not None and not task.done():
            logger.info("Attention DM round already running — updated issue body")
            return
        self._attention_stop = asyncio.Event()
        recipients = await self._status_alert_recipients(guild)
        if not recipients:
            logger.warning(
                "Status alert had no DM recipients (looking for %s).",
                STATUS_ALERT_TEST_USERNAME or "panel channel members",
            )
            await self._post_alert_to_panel_channel(body, [])
            return
        self._attention_task = asyncio.create_task(
            self._run_attention_round(body, recipients, guild),
            name="dcs-attention-dm-round",
        )

    async def _run_attention_round(self, body, recipients, guild):
        question = ATTENTION_QUESTION
        contacted = []
        for member in recipients:
            if self._attention_stop.is_set():
                logger.info("Attention DM round cancelled before paging %s", member.display_name)
                return
            if not self._any_server_still_needs_attention():
                logger.info("Attention DM round stopped — servers recovered")
                return

            wait = {
                "user_id": member.id,
                "event": asyncio.Event(),
                "answer": None,
            }
            self._attention_wait = wait
            dm_body = f"{self._attention_issue_body or body}\n\n{question}"
            try:
                await member.send(dm_body)
                contacted.append(member)
                self._remember_attention_contact(member)
                logger.info(
                    "Attention DM sent to %s (%s / %s) status=%s",
                    member.display_name,
                    member.name,
                    member.id,
                    getattr(member, "status", "?"),
                )
            except discord.Forbidden:
                logger.warning(
                    "Cannot DM %s (%s) — DMs closed. Trying next member.",
                    member.display_name,
                    member.id,
                )
                self._attention_wait = None
                continue
            except Exception as e:
                logger.warning("Failed to DM %s (%s): %s", member.display_name, member.id, e)
                self._attention_wait = None
                continue

            try:
                await asyncio.wait_for(wait["event"].wait(), timeout=ATTENTION_REPLY_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                wait["answer"] = "timeout"
                logger.info(
                    "No attention reply from %s within %ss — paging next",
                    member.display_name,
                    ATTENTION_REPLY_TIMEOUT_SECONDS,
                )
                try:
                    timeout_msg = await member.send("No reply received — contacting the next person.")
                    self.dismiss_status_message_later(timeout_msg)
                except Exception:
                    pass

            answer = wait.get("answer")
            self._attention_wait = None
            if answer == "yes":
                logger.info("%s accepted the attention DM — round closed", member.display_name)
                return
            if answer == "cancel":
                return
            logger.info(
                "%s declined or did not reply (%s) — paging next",
                member.display_name,
                answer,
            )

        logger.warning("Attention DM round exhausted the member list without an acceptance")
        await self._post_alert_to_panel_channel(
            f"{self._attention_issue_body or body}\n\n"
            "No one accepted the DM page. Please check the server.",
            contacted[:5],
        )

    async def on_message(self, message):
        await self.process_commands(message)
        if message.author.bot:
            return
        session = self._wiki_sessions.get(message.author.id)
        if session and message.guild is not None:
            if message.channel.id != session.get("channel_id"):
                await self.dismiss_wiki_for_user(message.author.id, "other_channel")
        if message.guild is not None:
            return
        wait = self._attention_wait
        if wait is None or message.author.id != wait["user_id"]:
            return
        reply = self._normalize_attention_reply(message.content)
        try:
            if reply in ATTENTION_YES_REPLIES:
                wait["answer"] = "yes"
                wait["event"].set()
                ack = await message.channel.send("Thanks — the alert round is closed.")
                self.dismiss_status_message_later(ack)
            elif reply in ATTENTION_NO_REPLIES:
                wait["answer"] = "no"
                wait["event"].set()
                ack = await message.channel.send("Understood — contacting the next person.")
                self.dismiss_status_message_later(ack)
        except Exception as e:
            logger.warning("Failed to acknowledge attention DM reply: %s", e)

    async def _post_alert_to_panel_channel(self, body, recipients):
        try:
            channel = await self.get_panel_channel()
            if channel is None:
                logger.warning("Cannot post status alert — panel channel unavailable")
                return
            mentions = " ".join(member.mention for member in recipients)
            prefix = f"{mentions}\n" if mentions else ""
            if STATUS_ALERT_TEST_USERNAME and not mentions:
                prefix = f"**{STATUS_ALERT_TEST_USERNAME}**\n"
            await channel.send(prefix + body)
            logger.info("Posted status alert in panel channel %s", channel.id)
        except Exception as e:
            logger.warning("Failed to post status alert in panel channel: %s", e)

    async def notify_status_changes(self, snapshots, guild):
        now = time.monotonic()
        async with self._status_alert_lock:
            problems = []
            for snap in snapshots:
                key = snap.get("key")
                if not key:
                    continue
                prev = self._last_node_status.get(key)
                self._last_node_status[key] = snap
                pending = self._pending_status_alerts.get(key)
                name = snap.get("name") or key
                curr_status = snap.get("status_text") or ""
                online = self._is_server_online(snap)

                if not online:
                    self._log_non_online_status(snap)

                if pending and online:
                    logger.info(
                        "Cancelling delayed status alert for %s — recovered to %s",
                        name,
                        curr_status,
                    )
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._append_status_log_line(
                        f"{stamp} | {name} | {curr_status} | recovered=ONLINE"
                    )
                    self._pending_status_alerts.pop(key, None)
                    continue

                if pending:
                    pending["latest_snap"] = snap
                    phase = pending.get("phase") or "grace"
                    if phase == "grace":
                        elapsed = now - pending["started_at"]
                        if elapsed < STATUS_ALERT_DELAY_SECONDS:
                            continue
                        logger.info(
                            "Grace period elapsed for %s — requesting DCS restart",
                            name,
                        )
                        pending["phase"] = "restart_wait"
                        pending["restart_at"] = now
                        pending["restart_attempted"] = True
                        await self._request_node_restart(snap)
                        continue

                    restart_elapsed = now - pending.get("restart_at", pending["started_at"])
                    if restart_elapsed < STATUS_RESTART_WAIT_SECONDS:
                        continue
                    text = self._describe_status_alert(pending["from_snap"], snap)
                    self._pending_status_alerts.pop(key, None)
                    if not text:
                        icon = snap.get("icon") or ""
                        text = (
                            f"**{name}**\n"
                            f"Still not ONLINE after automatic restart.\n"
                            f"New status: {icon} **{curr_status}**."
                        )
                    text += (
                        "\nAutomatic DCS restart was attempted after the grace period, "
                        "but the server did not return to ONLINE."
                    )
                    problems.append(text)
                    self._log_non_online_status(
                        snap,
                        extra="action=DM_ALERT restart_did_not_recover",
                        force=True,
                    )
                    continue

                if online:
                    continue
                text = self._describe_status_alert(prev, snap)
                if not text:
                    continue
                self._pending_status_alerts[key] = {
                    "started_at": now,
                    "from_snap": prev,
                    "latest_snap": snap,
                    "phase": "grace",
                    "restart_attempted": False,
                }
                logger.info(
                    "Delaying status alert for %s by %ss then restart (%s -> %s)",
                    name,
                    STATUS_ALERT_DELAY_SECONDS,
                    (prev or {}).get("status_text"),
                    curr_status,
                )
            recovered = not self._any_server_still_needs_attention()
            if not problems:
                body = None
            else:
                body = "🚨 **DCS Norway — server alert**\n\n" + "\n\n".join(problems)

        if recovered:
            await self._cancel_attention_round("all servers recovered")
            return
        if not body:
            return
        await self._start_attention_round(body, guild)

    async def send_socket_command(self, ip, port, command_str):
        payload = wrap_command(command_str, self.auth_token)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, int(port)),
                timeout=self.socket_timeout,
            )
            writer.write(payload.encode("utf-8"))
            await writer.drain()
            data = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            return data.decode("utf-8").strip()
        except Exception:
            return None

    @tasks.loop(seconds=3)
    async def queue_processor_loop(self):
        if self.is_processing_queue or self.deployment_queue.empty():
            return

        self.is_processing_queue = True
        task_data = await self.deployment_queue.get()

        try:
            await self.execute_node_deployment(task_data)
        except Exception as e:
            logger.error("Critical error in deployment queue: %s", e)
        finally:
            self.deployment_queue.task_done()
            self.is_processing_queue = False

    async def _monitor_node_task_until_idle(
        self,
        node,
        status_msg,
        name,
        *,
        success_text,
        timeout_seconds=900,
        done_tasks=("Rebooting", "Idle"),
    ):
        start_time = asyncio.get_event_loop().time()
        while True:
            if asyncio.get_event_loop().time() - start_time > timeout_seconds:
                await status_msg.edit(
                    content=f"⏰ **[{name}]** Update exceeded {timeout_seconds // 60} minutes (Timeout reached)."
                )
                return False

            chk = await self.send_socket_command(node["ip"], node["port"], "PING_STATUS")
            if chk is None:
                await status_msg.edit(content=f"🎉 **[{name}]** Connection closed. Server completed sequence!")
                return True

            if chk.startswith("{"):
                try:
                    res = json.loads(chk)
                    if res.get("active_task", "Idle") in done_tasks:
                        await status_msg.edit(content=success_text)
                        return True
                except Exception:
                    pass
            await asyncio.sleep(4)

    async def _run_dcs_update(self, node, channel, status_msg, name):
        ans = await self.send_socket_command(node["ip"], node["port"], "TRIGGER_DCS_UPDATE")

        if not ans:
            await status_msg.edit(content=f"❌ **[{name}]** Connection timeout. Node did not respond.")
            return False

        try:
            res_json = json.loads(ans)
            if res_json.get("status") == "UNAUTHORIZED":
                await status_msg.edit(
                    content=f"🔐 **[{name}]** Unauthorized — check shared auth_token on Bot and Node."
                )
                return False
            if res_json.get("status") == "REJECTED_BUSY":
                await status_msg.edit(content=f"⚠️ **[{name}]** Server busy executing: `{res_json.get('task')}`.")
                return False
            if res_json.get("status") != "OK_STARTING":
                await status_msg.edit(content=f"❌ **[{name}]** Rejected with status: `{res_json.get('status')}`.")
                return False
        except Exception:
            await status_msg.edit(content=f"❌ **[{name}]** Error parsing JSON response payload.")
            return False

        await status_msg.edit(content=f"🚀 **[{name}]** DCS update authorized! Downloading patch payload...")
        await asyncio.sleep(15)
        return await self._monitor_node_task_until_idle(
            node,
            status_msg,
            name,
            success_text=f"🎉 **[{name}]** DCS update successfully completed and verified!",
            timeout_seconds=900,
            done_tasks=("Rebooting", "Idle"),
        )

    async def _run_srs_update(self, node, channel, status_msg, name):
        ans = await self.send_socket_command(node["ip"], node["port"], "TRIGGER_SRS_UPDATE")

        if not ans:
            await status_msg.edit(content=f"❌ **[{name}]** Connection timeout. Node did not respond.")
            return False

        if "UNKNOWN_COMMAND" in str(ans):
            await status_msg.edit(content=f"❌ **[{name}]** Node is too old for SRS updates.")
            return False

        try:
            res_json = json.loads(ans)
            if res_json.get("status") == "UNAUTHORIZED":
                await status_msg.edit(
                    content=f"🔐 **[{name}]** Unauthorized — check shared auth_token on Bot and Node."
                )
                return False
            if res_json.get("status") == "REJECTED_BUSY":
                await status_msg.edit(content=f"⚠️ **[{name}]** Server busy executing: `{res_json.get('task')}`.")
                return False
            if res_json.get("status") == "ERROR":
                await status_msg.edit(
                    content=f"❌ **[{name}]** {res_json.get('message') or 'SRS install folder is not set on this Node.'}"
                )
                return False
            if res_json.get("status") != "OK_STARTING":
                await status_msg.edit(content=f"❌ **[{name}]** Rejected with status: `{res_json.get('status')}`.")
                return False
        except Exception:
            await status_msg.edit(content=f"❌ **[{name}]** Error parsing JSON response payload.")
            return False

        await status_msg.edit(content=f"📻 **[{name}]** SRS update authorized! Installing latest release...")
        return await self._monitor_node_task_until_idle(
            node,
            status_msg,
            name,
            success_text=f"🎉 **[{name}]** SRS update successfully completed and verified!",
            timeout_seconds=1800,
            done_tasks=("Idle",),
        )

    async def execute_node_deployment(self, task_data):
        node = task_data["node"]
        channel = task_data["channel"]
        name = node["name"]
        action = str(task_data.get("action") or "update").strip().lower()
        srs_latest = await self.fetch_latest_srs_release_cached()

        status_msg = await channel.send(
            f"⏳ **[QUEUE]** Connecting to `{name}` for **{action}**..."
        )
        status_messages = [status_msg]
        try:
            if action in ("update", "update_dcs", "update_srs"):
                ping = await self.send_socket_command(node["ip"], node["port"], "PING_STATUS")
                classified = classify_node_answer(ping, srs_latest_release=srs_latest)
                needs_dcs = classified.get("needs_dcs_update", False)
                needs_srs = classified.get("needs_srs_update", False)

                if action == "update_dcs":
                    if not needs_dcs:
                        await status_msg.edit(
                            content=f"✅ **[{name}]** DCS already up to date — nothing to deploy."
                        )
                        return
                    await status_msg.edit(content=f"⏳ **[QUEUE]** Connecting to `{name}` for DCS update...")
                    await self._run_dcs_update(node, channel, status_msg, name)
                elif action == "update_srs":
                    if not needs_srs:
                        await status_msg.edit(
                            content=f"✅ **[{name}]** SRS already up to date — nothing to deploy."
                        )
                        return
                    await status_msg.edit(content=f"⏳ **[QUEUE]** Connecting to `{name}` for SRS update...")
                    await self._run_srs_update(node, channel, status_msg, name)
                else:
                    # Legacy combined update
                    if not needs_dcs and not needs_srs:
                        await status_msg.edit(
                            content=f"✅ **[{name}]** Already up to date — nothing to deploy."
                        )
                        return

                    if needs_dcs:
                        await status_msg.edit(content=f"⏳ **[QUEUE]** Connecting to `{name}` for DCS update...")
                        await self._run_dcs_update(node, channel, status_msg, name)

                    if needs_srs:
                        if needs_dcs:
                            srs_msg = await channel.send(f"⏳ **[QUEUE]** Connecting to `{name}` for SRS update...")
                            status_messages.append(srs_msg)
                            await self._run_srs_update(node, channel, srs_msg, name)
                        else:
                            await status_msg.edit(content=f"⏳ **[QUEUE]** Connecting to `{name}` for SRS update...")
                            await self._run_srs_update(node, channel, status_msg, name)

            elif action == "restart_dcs":
                await self._run_operator_command(
                    node,
                    status_msg,
                    name,
                    "OPERATOR_RESTART_DCS",
                    success_text=f"🎉 **[{name}]** DCS restart completed.",
                    timeout_seconds=600,
                    done_tasks=("Idle",),
                )

            elif action == "restart_srs":
                await self._run_operator_command(
                    node,
                    status_msg,
                    name,
                    "RESTART_SRS",
                    success_text=f"🎉 **[{name}]** SRS restart completed.",
                    timeout_seconds=300,
                    done_tasks=("Idle",),
                )

            elif action == "reboot":
                ans = await self.send_socket_command(node["ip"], node["port"], "REBOOT_WINDOWS")
                if not ans:
                    await status_msg.edit(content=f"❌ **[{name}]** Connection timeout.")
                elif "UNKNOWN_COMMAND" in str(ans):
                    await status_msg.edit(content=f"❌ **[{name}]** Node is too old for REBOOT_WINDOWS.")
                else:
                    try:
                        res = json.loads(ans)
                        if res.get("status") == "OK_STARTING":
                            await status_msg.edit(
                                content=f"🔁 **[{name}]** Windows reboot scheduled."
                            )
                        else:
                            await status_msg.edit(
                                content=f"❌ **[{name}]** Reboot rejected: `{res.get('status')}`."
                            )
                    except Exception:
                        await status_msg.edit(content=f"❌ **[{name}]** Unexpected reboot reply.")
            else:
                await status_msg.edit(content=f"❌ **[{name}]** Unknown action `{action}`.")

            if self.active_panel_view:
                try:
                    await self.active_panel_view.refresh_panel()
                except Exception:
                    pass
        finally:
            for msg in status_messages:
                self.dismiss_status_message_later(msg)

    async def _run_operator_command(
        self,
        node,
        status_msg,
        name,
        command,
        *,
        success_text,
        timeout_seconds,
        done_tasks,
    ):
        ans = await self.send_socket_command(node["ip"], node["port"], command)
        if not ans:
            await status_msg.edit(content=f"❌ **[{name}]** Connection timeout.")
            return False
        if "UNKNOWN_COMMAND" in str(ans):
            await status_msg.edit(content=f"❌ **[{name}]** Node is too old for `{command}`.")
            return False
        try:
            res = json.loads(ans)
            if res.get("status") == "UNAUTHORIZED":
                await status_msg.edit(content=f"🔐 **[{name}]** Unauthorized.")
                return False
            if res.get("status") == "REJECTED_BUSY":
                await status_msg.edit(
                    content=f"⚠️ **[{name}]** Busy: `{res.get('task')}`."
                )
                return False
            if res.get("status") == "ERROR":
                await status_msg.edit(
                    content=f"❌ **[{name}]** {res.get('message') or 'Command failed.'}"
                )
                return False
            if res.get("status") != "OK_STARTING":
                await status_msg.edit(
                    content=f"❌ **[{name}]** Rejected: `{res.get('status')}`."
                )
                return False
        except Exception:
            await status_msg.edit(content=f"❌ **[{name}]** Could not parse reply.")
            return False

        await status_msg.edit(content=f"⏳ **[{name}]** `{command}` started...")
        return await self._monitor_node_task_until_idle(
            node,
            status_msg,
            name,
            success_text=success_text,
            timeout_seconds=timeout_seconds,
            done_tasks=done_tasks,
        )

    async def dismiss_wiki_for_user(self, user_id, reason=""):
        session = self._wiki_sessions.pop(user_id, None)
        if not session:
            return
        message = session.get("message")
        if message is None:
            return
        try:
            await message.delete()
            if reason:
                logger.info("Dismissed wiki for user %s (%s)", user_id, reason)
        except Exception:
            pass

    def track_wiki_session(self, user_id, channel_id, message):
        previous = self._wiki_sessions.get(user_id)
        self._wiki_sessions[user_id] = {
            "channel_id": channel_id,
            "message": message,
            "created_at": time.monotonic(),
        }
        if previous and previous.get("message") is not None:
            async def _delete_old():
                try:
                    await previous["message"].delete()
                except Exception:
                    pass
            asyncio.create_task(_delete_old())

        async def _timeout():
            await asyncio.sleep(WIKI_AUTO_DISMISS_SECONDS)
            session = self._wiki_sessions.get(user_id)
            if session and session.get("message") is message:
                await self.dismiss_wiki_for_user(user_id, "timeout")

        asyncio.create_task(_timeout())

    async def on_presence_update(self, before, after):
        if after.bot:
            return
        if after.status in (discord.Status.offline, discord.Status.invisible):
            await self.dismiss_wiki_for_user(after.id, "discord_closed_or_offline")

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.user is None or interaction.channel is None:
            return
        session = self._wiki_sessions.get(interaction.user.id)
        if not session:
            return
        command_name = getattr(getattr(interaction, "command", None), "name", None)
        if command_name == "dcs-panel-wiki":
            return
        if interaction.channel.id != session.get("channel_id"):
            await self.dismiss_wiki_for_user(interaction.user.id, "other_channel")

    @tasks.loop(seconds=30)
    async def persistent_panel_refresh_loop(self):
        if not self._owns_panel_refresh():
            return
        if self.active_panel_view and self.panel_channel_id and self.panel_message_id:
            try:
                await self.active_panel_view.refresh_panel()
            except Exception as e:
                logger.error("Suppressed automated background refresh exception: %s", e)

    @tasks.loop(minutes=BOT_GITHUB_CHECK_MINUTES)
    async def github_self_update_loop(self):
        await self.check_github_self_update()

    @github_self_update_loop.before_loop
    async def before_github_self_update_loop(self):
        await self.wait_until_ready()
        await asyncio.sleep(BOT_GITHUB_CHECK_DELAY_SECONDS)


bot = DCSClusterBot()


def has_dcs_management_permission():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        allowed_roles = ["DCS Admin", "Moderator"]
        has_valid_role = any(role.name in allowed_roles for role in interaction.user.roles)
        if not has_valid_role:
            await interaction.response.send_message(
                "❌ **Permission Denied:** Restricted to Management and Staff.",
                ephemeral=True,
            )
            try:
                bot.dismiss_status_message_later(await interaction.original_response())
            except Exception:
                pass
            return False
        return True

    return app_commands.check(predicate)


PANEL_BOX_LINE_WIDTH = 13
PANEL_STATUS_SHORT = {
    "DCS NOT STARTED": "NOT STARTED",
    "DCS STARTING": "STARTING",
    "SRS OUTDATED": "SRS OUTDATED",
    "SRS DOWN": "SRS DOWN",
    "SRS + DCS DOWN": "SRS+DCS DOWN",
}
PANEL_TASK_SHORT = {
    "Awaiting server boot": "Boot pending",
    "Awaiting DCS port": "Port pending",
    "Port not responding": "Port down",
    "Server stopped/crashed": "Crashed",
    "DCS_server stopped": "Stopped",
    "Awaiting operator action": "Action needed",
    "Action required": "Action needed",
}


def _panel_line(text: str, width: int = PANEL_BOX_LINE_WIDTH) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= width:
        return cleaned
    return cleaned[: width - 1] + "…"


def format_server_status_box(status_text: str, ver_info: str, task_info: str, srs_info: str = "—") -> str:
    """Fixed four-line status block so every server tile is the same height."""
    status_text = PANEL_STATUS_SHORT.get(status_text, status_text)
    task_info = PANEL_TASK_SHORT.get(task_info, task_info)
    rows = [
        f"ℹ️ {_panel_line(status_text)}",
        f"⚙️ {_panel_line(ver_info)}",
        f"📻 {_panel_line(srs_info)}",
        f"🖥️ {_panel_line(task_info)}",
    ]
    return "```yaml\n" + "\n".join(rows) + "\n```"


SRS_UNKNOWN_INSTALLED = frozenset(
    {"UNKNOWN", "FETCHING...", "MISSING", "NOT SET", "Not set", "—", ""}
)
SRS_UNKNOWN_LATEST = frozenset({"Unknown", "Fetching...", "—", ""})


def is_srs_outdated(installed: str, latest: str) -> bool:
    inst = str(installed or "").strip()
    lat = str(latest or "").strip()
    if not inst or inst.upper() in {v.upper() for v in SRS_UNKNOWN_INSTALLED} or inst in SRS_UNKNOWN_INSTALLED:
        return False
    if not lat or lat in SRS_UNKNOWN_LATEST:
        return False
    return inst != lat


def classify_node_answer(answer, srs_latest_release=None):
    status_text = STATUS_UP_TO_DATE
    ver_info = "Unknown"
    srs_info = "—"
    task_info = "Ready"
    is_outdated = False
    needs_action = False
    needs_dcs_update = False
    needs_srs_update = False
    srs_down = False
    icon = "🟢"
    dcs_health = ""
    dcs_running = None
    srs_running = None

    if answer and answer.startswith("{"):
        try:
            res = json.loads(answer)
            if res.get("status") == "UNAUTHORIZED":
                status_text = "UNAUTHORIZED"
                icon = "🔐"
            else:
                installed_ver = res.get("installed_version", "Unknown")
                latest_ver = res.get("latest_cloud_version", installed_ver)
                dcs_health = str(res.get("dcs_health", "")).strip().upper()
                dcs_running = res.get("dcs_running", True)
                active_task = res.get("active_task", "Idle")
                srs_installed_raw = str(res.get("srs_installed_version") or "").strip()
                srs_configured = bool(res.get("srs_configured", True))
                srs_running = bool(res.get("srs_running", False)) if srs_configured else None
                srs_info = srs_installed_raw or "—"
                if not srs_configured and srs_info in ("", "Not set", "—"):
                    srs_info = "Not set"

                srs_latest = str(
                    srs_latest_release or res.get("srs_latest_version") or ""
                ).strip()
                needs_dcs_update = (
                    str(installed_ver).strip() != str(latest_ver).strip()
                    and latest_ver != "Unknown"
                )
                needs_srs_update = srs_configured and is_srs_outdated(
                    srs_installed_raw, srs_latest
                )
                is_outdated = needs_dcs_update or needs_srs_update
                srs_down = srs_configured and srs_running is False
                dcs_crashed = dcs_health in HEALTH_CRASHED
                dcs_not_running = dcs_crashed or dcs_health == "NEVER_STARTED"
                needs_action = (
                    is_outdated or srs_down or dcs_crashed or dcs_health == "NEVER_STARTED"
                )
                ver_info = f"{installed_ver}"

                if active_task == "Restarting DCS":
                    task_info = "Restarting..."
                elif active_task == "Restarting SRS":
                    task_info = "Restarting SRS"
                elif active_task != "Idle":
                    task_info = active_task
                elif dcs_health == "STARTING":
                    # Real wait: process is up, port not ready yet.
                    task_info = "Awaiting DCS port"
                elif dcs_health == "NEVER_STARTED" or srs_down or dcs_crashed:
                    # No automatic boot is queued — operator must act.
                    task_info = TASK_AWAITING_OPERATOR
                else:
                    task_info = "Ready"

                # Updates first, then combined outages, then single-service issues.
                if needs_dcs_update:
                    status_text = "UPDATE READY"
                    icon = "⚠️"
                elif needs_srs_update:
                    status_text = STATUS_SRS_OUTDATED
                    icon = "⚠️"
                elif srs_down and dcs_not_running:
                    status_text = STATUS_SRS_AND_DCS_DOWN
                    # Red only if DCS has run before and then failed.
                    icon = "🛑" if dcs_crashed else "⚠️"
                    if active_task == "Idle":
                        task_info = TASK_AWAITING_OPERATOR
                elif srs_down:
                    status_text = STATUS_SRS_DOWN
                    icon = "⚠️"
                    if active_task == "Idle":
                        task_info = TASK_AWAITING_OPERATOR
                elif dcs_health == "STARTING":
                    status_text = "DCS STARTING"
                    icon = "⏳"
                elif dcs_health == "NEVER_STARTED":
                    status_text = "DCS NOT STARTED"
                    icon = "⏸️"
                    if active_task == "Idle":
                        task_info = TASK_AWAITING_OPERATOR
                elif dcs_crashed:
                    status_text = "DCS DOWN"
                    icon = "🛑"
                    if active_task == "Restarting DCS":
                        task_info = "Restarting..."
                    elif dcs_health == "UNHEALTHY":
                        task_info = "Port not responding"
                    elif dcs_health == "DEAD":
                        task_info = "Server stopped/crashed"
                    else:
                        task_info = "DCS_server stopped"
                else:
                    status_text = STATUS_UP_TO_DATE
                    icon = "🟢"
        except Exception:
            status_text = "OFFLINE"
            icon = "🔴"
    else:
        status_text = "OFFLINE"
        icon = "🔴"

    return {
        "status_text": status_text,
        "ver_info": ver_info,
        "srs_info": srs_info,
        "task_info": task_info,
        "is_outdated": is_outdated,
        "needs_action": needs_action,
        "needs_dcs_update": needs_dcs_update,
        "needs_srs_update": needs_srs_update,
        "srs_down": srs_down,
        "icon": icon,
        "dcs_health": dcs_health,
        "dcs_running": dcs_running,
        "srs_running": srs_running,
    }


# ==================== INTERACTIVE MULTI-SELECT PANEL ENGINE ====================


class WikiDismissView(discord.ui.View):
    def __init__(self, bot_instance, user_id):
        super().__init__(timeout=WIKI_AUTO_DISMISS_SECONDS)
        self.bot = bot_instance
        self.user_id = user_id

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This wiki belongs to another user.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.bot.dismiss_wiki_for_user(self.user_id, "closed")
        self.stop()


class PanelActionPickerView(discord.ui.View):
    def __init__(self, bot_instance, nodes, channel):
        super().__init__(timeout=120)
        self.bot = bot_instance
        self.nodes = nodes
        self.channel = channel

    @discord.ui.select(
        placeholder="Choose action…",
        options=[
            discord.SelectOption(
                label="Start/Restart DCS",
                value="restart_dcs",
                description="Start or restart DCS_server.exe",
                emoji="🔄",
            ),
            discord.SelectOption(
                label="Start/Restart SRS",
                value="restart_srs",
                description="Start or restart SR-Server.exe",
                emoji="📻",
            ),
            discord.SelectOption(
                label="Update DCS",
                value="update_dcs",
                description="Install available DCS update only",
                emoji="🚀",
            ),
            discord.SelectOption(
                label="Update SRS",
                value="update_srs",
                description="Install available SRS update only",
                emoji="📡",
            ),
            discord.SelectOption(
                label="Reboot Server",
                value="reboot",
                description="Reboot the Windows host (10s delay)",
                emoji="🔁",
            ),
        ],
    )
    async def action_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        action = select.values[0]
        action_label = PANEL_ACTION_LABELS.get(action, action)
        names = ", ".join(n["name"] for n in self.nodes)

        if action == "reboot":
            confirm = RebootConfirmView(self.bot, self.nodes, self.channel)
            await interaction.response.edit_message(
                content=(
                    f"⚠️ Confirm **Reboot Server** for **{names}**?\n"
                    "This will reboot the host OS in ~10 seconds."
                ),
                view=confirm,
            )
            return

        await interaction.response.edit_message(
            content=f"Queued **{action_label}** for **{names}**.",
            view=None,
        )
        try:
            self.bot.dismiss_status_message_later(await interaction.original_response())
        except Exception:
            pass
        log_msg = await self.channel.send(
            f"🚨 **[ACTION LOG]** {action_label} for: **{names}**."
        )
        self.bot.dismiss_status_message_later(log_msg)
        for node in self.nodes:
            await self.bot.deployment_queue.put(
                {"node": node, "channel": self.channel, "action": action}
            )
        if self.bot.active_panel_view:
            self.bot.active_panel_view._set_selection([])
            await self.bot.active_panel_view.refresh_panel()
        self.stop()


class RebootConfirmView(discord.ui.View):
    def __init__(self, bot_instance, nodes, channel):
        super().__init__(timeout=60)
        self.bot = bot_instance
        self.nodes = nodes
        self.channel = channel

    @discord.ui.button(label="Confirm reboot", style=discord.ButtonStyle.danger)
    async def btn_confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        names = ", ".join(n["name"] for n in self.nodes)
        action_label = PANEL_ACTION_LABELS["reboot"]
        await interaction.response.edit_message(
            content=f"Queued **{action_label}** for **{names}**.",
            view=None,
        )
        try:
            self.bot.dismiss_status_message_later(await interaction.original_response())
        except Exception:
            pass
        log_msg = await self.channel.send(
            f"🚨 **[ACTION LOG]** {action_label} for: **{names}**."
        )
        self.bot.dismiss_status_message_later(log_msg)
        for node in self.nodes:
            await self.bot.deployment_queue.put(
                {"node": node, "channel": self.channel, "action": "reboot"}
            )
        if self.bot.active_panel_view:
            self.bot.active_panel_view._set_selection([])
            await self.bot.active_panel_view.refresh_panel()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Reboot cancelled.", view=None)
        try:
            self.bot.dismiss_status_message_later(await interaction.original_response())
        except Exception:
            pass
        self.stop()


class LiveControlPanelView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.all_nodes_cached = []
        self.select_menu = None
        self.currently_selected_server_names = list(
            getattr(bot_instance, "panel_selected_server_names", []) or []
        )

    def _selection_names(self):
        names = getattr(self.bot, "panel_selected_server_names", None)
        if names is not None:
            return [str(n) for n in names if n]
        active = getattr(self.bot, "active_panel_view", None)
        if active is not None and active is not self:
            active_names = getattr(active, "currently_selected_server_names", None)
            if active_names:
                return [str(n) for n in active_names if n]
        return [str(n) for n in (self.currently_selected_server_names or []) if n]

    @staticmethod
    def _selection_from_message(message):
        """Read currently selected values from the panel message components."""
        if message is None:
            return []
        selected = []
        for row in getattr(message, "components", None) or []:
            for child in getattr(row, "children", None) or []:
                values = getattr(child, "values", None)
                if values:
                    selected.extend(str(v) for v in values if v)
        ordered = []
        seen = set()
        for name in selected:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    def _resolve_selection(self, interaction: discord.Interaction | None = None):
        selected = self._selection_names()
        if selected:
            return selected
        if interaction is not None:
            from_msg = self._selection_from_message(interaction.message)
            if from_msg:
                self._set_selection(from_msg)
                return from_msg
        if self.select_menu is not None:
            defaults = [
                opt.value
                for opt in self.select_menu.options
                if getattr(opt, "default", False)
            ]
            if defaults:
                self._set_selection(defaults)
                return defaults
        return []

    def _set_selection(self, names, *, persist=True, bump_rev=True):
        ordered = []
        seen = set()
        for name in names or []:
            if name and name not in seen:
                seen.add(name)
                ordered.append(str(name))
        self.currently_selected_server_names = ordered
        self.bot.panel_selected_server_names = list(ordered)
        if bump_rev:
            self.bot._panel_selection_rev = int(getattr(self.bot, "_panel_selection_rev", 0)) + 1
        active = getattr(self.bot, "active_panel_view", None)
        if active is not None and active is not self:
            active.currently_selected_server_names = list(ordered)
        if persist:
            try:
                self.bot.persist_panel_selection()
            except Exception:
                pass

    def _apply_deploy_button_state(self, has_actionable, selected_count=None):
        if selected_count is None:
            selected_count = len(self._selection_names())
        else:
            selected_count = int(selected_count)
        if not has_actionable and not selected_count:
            self.btn_deploy_selected.disabled = True
            self.btn_deploy_selected.label = "✅ All servers operational"
            self.btn_deploy_selected.style = discord.ButtonStyle.secondary
            return
        if selected_count:
            self.btn_deploy_selected.disabled = False
            self.btn_deploy_selected.label = f"Select Actions ({selected_count})"
            self.btn_deploy_selected.style = discord.ButtonStyle.primary
        else:
            self.btn_deploy_selected.disabled = True
            self.btn_deploy_selected.label = "Select Actions"
            self.btn_deploy_selected.style = discord.ButtonStyle.secondary

    def _stash_option_rows(self, options):
        rows = []
        for opt in options or []:
            rows.append(
                {
                    "label": opt.label,
                    "value": opt.value,
                    "description": opt.description or None,
                    "emoji": opt.emoji,
                }
            )
        self._panel_option_rows = rows
        return rows

    def _options_from_rows(self, rows, selected_set):
        options = []
        for row in rows or []:
            kwargs = {
                "label": row["label"],
                "value": row["value"],
                "description": row.get("description") or None,
                "default": row["value"] in selected_set,
            }
            emoji = row.get("emoji")
            if emoji is not None:
                kwargs["emoji"] = emoji
            options.append(discord.SelectOption(**kwargs))
        return options

    def _ensure_selected_servers_in_options(self, options, nodes, snapshots_by_name):
        """Keep currently selected servers in the dropdown across status refreshes."""
        known = {n["name"]: n for n in nodes}
        selected = [n for n in self._selection_names() if n in known]
        have = {opt.value for opt in options}
        for name in selected:
            if name in have:
                continue
            node = known[name]
            snap = snapshots_by_name.get(name) or {}
            icon = snap.get("icon")
            options.append(
                discord.SelectOption(
                    label=name,
                    description=(
                        f"{snap.get('status_text', 'Selected')} | Port {node['port']}"
                    )[:100],
                    value=name,
                    emoji=icon if icon in {"⚠️", "🛑"} else "📌",
                    default=True,
                )
            )
            have.add(name)

        if len(options) <= DISCORD_SELECT_MAX_OPTIONS:
            return options

        # Prefer keeping selected entries when Discord's 25-option cap is hit.
        selected_set = set(selected)
        kept = [opt for opt in options if opt.value in selected_set]
        for opt in options:
            if opt.value in selected_set:
                continue
            if len(kept) >= DISCORD_SELECT_MAX_OPTIONS:
                break
            kept.append(opt)
        return kept[:DISCORD_SELECT_MAX_OPTIONS]

    def _resync_select_menu_from_selection(self):
        """Re-apply defaults from bot selection onto the last option rows (under lock)."""
        rows = getattr(self, "_panel_option_rows", None)
        if not rows:
            remembered = self._selection_names()
            self._apply_deploy_button_state(
                has_actionable=False,
                selected_count=len(remembered),
            )
            return
        selected_set = set(self._selection_names())
        self._rebuild_select_menu(self._options_from_rows(rows, selected_set))

    def _rebuild_select_menu(self, options):
        """Install dropdown with defaults from current bot selection. Never clears selection."""
        if self.select_menu in self.children:
            self.remove_item(self.select_menu)

        remembered = self._selection_names()
        self._stash_option_rows(options)
        if not options:
            self.select_menu = None
            self._apply_deploy_button_state(
                has_actionable=False,
                selected_count=len(remembered),
            )
            return

        options = options[:DISCORD_SELECT_MAX_OPTIONS]
        valid_values = {opt.value for opt in options}
        selected_in_menu = [name for name in remembered if name in valid_values]
        selected_set = set(selected_in_menu)
        for opt in options:
            opt.default = opt.value in selected_set

        self.select_menu = discord.ui.Select(
            placeholder=(
                f"Select server(s) — {len(remembered)} selected"
                if remembered
                else "Select server(s)"
            ),
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
            custom_id="dcs_panel:select",
        )
        self.select_menu.callback = self.select_menu_callback
        self.add_item(self.select_menu)
        # Button reflects full remembered selection, not only "needs action" rows.
        self._apply_deploy_button_state(
            has_actionable=True,
            selected_count=len(remembered),
        )

    async def _edit_panel_view_only(self):
        if not (self.bot.panel_channel_id and self.bot.panel_message_id):
            return
        try:
            channel = self.bot.get_channel(int(self.bot.panel_channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(self.bot.panel_channel_id))
            message = await channel.fetch_message(int(self.bot.panel_message_id))
            await message.edit(view=self)
        except Exception as e:
            logger.error("Failed to update panel selection view: %s", e)

    async def generate_embed(self, guild=None):
        nodes = self.bot.load_cluster_nodes()
        self.all_nodes_cached = nodes
        known_names = {n["name"] for n in nodes}

        dcs_latest_release = await self.bot.fetch_latest_dcs_release()
        srs_latest_release = await self.bot.fetch_latest_srs_release_cached()
        current_time_str = datetime.now().strftime("%H:%M")

        embed = discord.Embed(
            description="\n```🛡️ Operational System Status for DCS World Servers```\n",
            color=discord.Color.from_rgb(26, 132, 255),
        )
        embed.set_footer(
            text=(
                f"Updated Today at {current_time_str}\n"
                f"ED Release Version: {dcs_latest_release}\n"
                f"SRS Release Version: {srs_latest_release}\n"
                f"Bot version: {CURRENT_BOT_VERSION}"
            )
        )

        if guild and guild.icon:
            embed.set_author(name="🛸 DCS Norway Live Control Panel", icon_url=guild.icon.url)
        else:
            embed.title = "🛸 DCS Norway Live Control Panel"

        options = []
        snapshots = []
        snapshots_by_name = {}
        tasks_list = [self.bot.send_socket_command(n["ip"], n["port"], "PING_STATUS") for n in nodes]
        responses = await asyncio.gather(*tasks_list)

        # Prune selection only for servers removed from the cluster config.
        # Never clear operator picks just because a refresh ran.
        live_selection = [n for n in self._selection_names() if n in known_names]
        if live_selection != list(self._selection_names()):
            self._set_selection(live_selection, persist=True)
        selected_set = set(self._selection_names())

        for idx, (node, answer) in enumerate(zip(nodes, responses)):
            classified = classify_node_answer(answer, srs_latest_release=srs_latest_release)
            status_text = classified["status_text"]
            ver_info = classified["ver_info"]
            task_info = classified["task_info"]
            needs_action = classified.get("needs_action", classified["is_outdated"])
            icon = classified["icon"]
            snap = {
                "key": f"{node.get('ip')}:{node.get('port')}",
                "ip": node.get("ip"),
                "port": node.get("port"),
                "name": node["name"],
                **classified,
            }
            snapshots.append(snap)
            snapshots_by_name[node["name"]] = snap

            if needs_action:
                options.append(
                    discord.SelectOption(
                        label=node["name"],
                        description=f"{status_text} | Port {node['port']}",
                        value=node["name"],
                        emoji=icon if icon in {"⚠️", "🛑"} else "⚠️",
                        default=node["name"] in selected_set,
                    )
                )

            boxed_value = format_server_status_box(
                status_text,
                ver_info,
                task_info,
                classified.get("srs_info", "—"),
            )

            field_name = f"{icon}\u2001{node['name']}\u2001\u2001\u2001\u2001\u2001\u2001"

            embed.add_field(name=field_name, value=boxed_value, inline=True)

            if (idx + 1) % 3 == 0 and (idx + 1) < len(nodes):
                for _ in range(3):
                    embed.add_field(name="\u2001", value="\u2001", inline=True)

        # Re-read selection after awaits above may have interleaved with user clicks.
        selected_set = set(self._selection_names())
        for opt in options:
            opt.default = opt.value in selected_set
        options = self._ensure_selected_servers_in_options(options, nodes, snapshots_by_name)
        self._rebuild_select_menu(options)

        try:
            await self.bot.notify_status_changes(snapshots, guild)
        except Exception as e:
            logger.error("Failed to send status alert DMs: %s", e)

        # Selection may have changed during notify DMs — rebuild defaults again before return.
        self._resync_select_menu_from_selection()
        return embed

    @staticmethod
    def _interaction_select_values(interaction: discord.Interaction):
        data = getattr(interaction, "data", None)
        if data is None:
            return []
        if isinstance(data, dict):
            return [str(v) for v in (data.get("values") or []) if v]
        values = getattr(data, "get", lambda _k, _d=None: None)("values")
        if values:
            return [str(v) for v in values if v]
        try:
            return [str(v) for v in data["values"] if v]
        except Exception:
            return []

    async def select_menu_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        values = self._interaction_select_values(interaction)
        if not values and self.select_menu is not None:
            values = [str(v) for v in (getattr(self.select_menu, "values", None) or []) if v]

        # Prefer writing onto the live panel view so refresh and button share state.
        target = self.bot.active_panel_view or self
        async with self.bot._panel_update_lock:
            target._set_selection(values, persist=True)
            selected = target._selection_names()
            selected_set = set(selected)
            # Rebuild option defaults on the active view's current option list.
            option_rows = []
            if target.select_menu is not None:
                for opt in target.select_menu.options:
                    option_rows.append(
                        discord.SelectOption(
                            label=opt.label,
                            value=opt.value,
                            description=opt.description or None,
                            emoji=opt.emoji if opt.emoji is not None else None,
                            default=opt.value in selected_set,
                        )
                    )
                target._rebuild_select_menu(option_rows)
            else:
                target._apply_deploy_button_state(
                    has_actionable=False,
                    selected_count=len(selected),
                )
            await target._edit_panel_view_only()

        if not selected:
            confirm = await interaction.followup.send(
                "☑️ Cleared server selection.", ephemeral=True
            )
        else:
            selected_text = ", ".join(selected)
            confirm = await interaction.followup.send(
                f"✅ Selected for deployment: **{selected_text}**.", ephemeral=True
            )
        self.bot.dismiss_status_message_later(confirm)

    async def refresh_panel(self):
        if self.bot.panel_channel_id and self.bot.panel_message_id:
            try:
                channel = self.bot.get_channel(int(self.bot.panel_channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(self.bot.panel_channel_id))
                message = await channel.fetch_message(int(self.bot.panel_message_id))
                # Ping nodes outside the lock so dropdown selection can be saved mid-refresh.
                new_embed = await self.generate_embed(guild=channel.guild)
                async with self.bot._panel_update_lock:
                    # User may have changed selection while generate_embed awaited; re-apply now.
                    self._resync_select_menu_from_selection()
                    await message.edit(embed=new_embed, view=self)
                return True
            except Exception as e:
                logger.error("Failed to auto-edit persistent message frame: %s", e)
                return False
        return False

    @discord.ui.button(
        label="🔄 Refresh Server Status",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="dcs_panel:refresh",
    )
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok = await self.refresh_panel()
        if not ok:
            await self.bot.restore_or_recreate_panel()

    @discord.ui.button(
        label="Select Actions",
        style=discord.ButtonStyle.secondary,
        row=0,
        disabled=True,
        custom_id="dcs_panel:deploy",
    )
    async def btn_deploy_selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prefer the live panel instance; persistent custom_id routing can hit a stale view.
        view = self.bot.active_panel_view or self
        selected = view._resolve_selection(interaction)
        if not selected:
            await interaction.response.send_message(
                "⚠️ You must select at least one server from the menu dropdown!",
                ephemeral=True,
            )
            try:
                self.bot.dismiss_status_message_later(await interaction.original_response())
            except Exception:
                pass
            return

        nodes_source = view.all_nodes_cached or self.all_nodes_cached or self.bot.load_cluster_nodes()
        matched = []
        for server_name in selected:
            node = next((n for n in nodes_source if n["name"] == server_name), None)
            if node:
                matched.append(node)
        if not matched:
            await interaction.response.send_message(
                "⚠️ No matching servers found for the current selection.",
                ephemeral=True,
            )
            return

        view = PanelActionPickerView(self.bot, matched, interaction.channel)
        await interaction.response.send_message(
            f"Choose an action for **{', '.join(n['name'] for n in matched)}**:",
            view=view,
            ephemeral=True,
        )


# ==================== INITIALIZATION COMMAND ====================


@bot.tree.command(
    name="dcs-panel-wiki",
    description="Show status icon explanations and traffic-light logic (auto-dismisses).",
)
@has_dcs_management_permission()
async def dcs_panel_wiki(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await bot.dismiss_wiki_for_user(interaction.user.id, "replaced")

    embed = discord.Embed(
        title="DCS Norway — status wiki",
        description=(
            "Traffic-light logic for the live panel.\n"
            "This message is removed when you switch channel, go offline, press **Close**, "
            "or after 15 minutes."
        ),
        color=discord.Color.from_rgb(26, 132, 255),
    )
    embed.add_field(
        name="🟢 Green",
        value="**UP TO DATE** — node answers, no pending DCS/SRS update, SRS running when configured.",
        inline=False,
    )
    embed.add_field(
        name="⚠️ Yellow (Caution)",
        value=(
            "**UPDATE READY** / **SRS OUTDATED** — version mismatch\n"
            "**SRS DOWN** — SRS not running (DCS OK)\n"
            "**SRS + DCS DOWN** — both down, but DCS never started"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛑 Red",
        value=(
            "**DCS DOWN** — DCS was running, then UNHEALTHY/DEAD\n"
            "**SRS + DCS DOWN** — both down after DCS had been running"
        ),
        inline=False,
    )
    embed.add_field(
        name="Other icons",
        value=(
            "⏸️ **NOT STARTED** — DCS never started (idle; Action required — no auto-boot)\n"
            "⏳ **STARTING** — process up, port not ready yet (Port pending)\n"
            "🔐 **UNAUTHORIZED** — auth token mismatch\n"
            "🔴 **OFFLINE** — node did not answer"
        ),
        inline=False,
    )
    embed.add_field(
        name="Priority",
        value=(
            "Updates → SRS+DCS DOWN → SRS DOWN → STARTING → "
            "NOT STARTED → DCS crash → UP TO DATE"
        ),
        inline=False,
    )
    embed.add_field(
        name="Panel actions (yellow/red)",
        value=(
            "Use **Select server(s)**, then **Select Actions**:\n"
            "• **Start/Restart DCS** / **Start/Restart SRS**\n"
            "• **Update DCS** / **Update SRS**\n"
            "• **Reboot Server** (confirmation required)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Status box",
        value="ℹ️ status · ⚙️ DCS version · 📻 SRS version · 🖥️ task detail",
        inline=False,
    )
    embed.set_footer(text=f"Bot v{CURRENT_BOT_VERSION}")

    view = WikiDismissView(bot, interaction.user.id)
    message = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
    bot.track_wiki_session(interaction.user.id, interaction.channel_id, message)


@bot.tree.command(name="dcs-panel-init", description="Pins the permanent live dashboard into this channel.")
@has_dcs_management_permission()
async def dcs_panel_init(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await bot.purge_old_bot_messages(interaction.channel)
    await bot.ensure_panel_channel_guide(interaction.channel)

    view = LiveControlPanelView(bot)
    bot.add_view(view)
    embed = await view.generate_embed(guild=interaction.guild)

    message = await interaction.followup.send(embed=embed, view=view)

    bot.panel_channel_id = interaction.channel_id
    bot.panel_message_id = message.id
    bot.active_panel_view = view
    bot.persist_panel_ids()
    await bot.sync_slash_commands(guild=interaction.guild)
    logger.info("Persistent dashboard frame spawned and locked onto Message ID: %s", message.id)


@bot.tree.command(
    name="dcs-panel-update",
    description="Force an immediate GitHub check and install a newer Discord Bot if available.",
)
@has_dcs_management_permission()
async def dcs_panel_update(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await bot.check_github_self_update(apply=False) or {
        "ok": False,
        "message": "Update check returned no result.",
    }
    prefix = "✅" if result.get("ok") else "❌"
    status = await interaction.followup.send(f"{prefix} {result.get('message')}", ephemeral=True)
    downloads = result.get("downloads")
    if result.get("ok") and result.get("updated") and downloads:
        # Delete before exit — delayed dismiss never runs after os._exit / NSSM restart.
        try:
            await status.delete()
        except Exception:
            pass
        await bot._apply_self_update(downloads)
        return
    bot.dismiss_status_message_later(status)


if __name__ == "__main__":
    ensure_single_bot_instance()
    token = get_discord_bot_token()
    if not token:
        raise SystemExit(
            "Missing Discord bot token. Set environment variable DISCORD_BOT_TOKEN "
            "(regenerate the old token in Discord Developer Portal — it was previously hardcoded)."
        )

    async def main():
        async with bot:
            await bot.start(token)

    asyncio.run(main())
