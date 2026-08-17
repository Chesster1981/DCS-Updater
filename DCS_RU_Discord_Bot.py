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
    save_master_config,
    wrap_command,
)

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DCS_Discord_Bot")

CURRENT_BOT_VERSION = "2.1.41"
GITHUB_REPO = "Chesster1981/DCS-Updater"
URL_GITHUB_API = "https://api.github.com/repos/"
BOT_SELF_UPDATE_FILES = ("DCS_RU_Discord_Bot.py", "dcs_ru_common.py")
BOT_GITHUB_CHECK_DELAY_SECONDS = 20
BOT_GITHUB_CHECK_MINUTES = 5
# Set to a Discord username to DM only that person while testing.
# None = page panel-channel members one at a time (online first).
STATUS_ALERT_TEST_USERNAME = None
STATUS_ALERT_TEST_NAME_ALIASES = ("Chesster", "Chesster1981")
STATUS_ALERT_DELAY_SECONDS = 300
ATTENTION_REPLY_TIMEOUT_SECONDS = 300
ATTENTION_QUESTION = "Are you available to attend the issue ?"
ATTENTION_YES_REPLIES = {"ja", "yes"}
ATTENTION_NO_REPLIES = {"no", "nei"}
STATUS_UP_TO_DATE = "UP TO DATE"
STATUS_RUNNING = {"UP TO DATE", "UPDATE READY"}
STATUS_DOWN = {"DCS DOWN", "OFFLINE"}
STATUS_BOOT = {"DCS STARTING", "DCS NOT STARTED"}
HEALTH_CRASHED = {"DEAD", "UNHEALTHY"}


class DCSClusterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(command_prefix="!", intents=intents)

        self.config_path = "master_config.json"
        self.deployment_queue = asyncio.Queue()
        self.is_processing_queue = False
        self.socket_timeout = 4.0

        self.active_panel_view = None
        self.panel_channel_id = None
        self.panel_message_id = None
        self.cached_dcs_version = "Unknown"
        self.last_cache_time = 0
        self.auth_token = ""
        self._restoring_panel = False
        self._self_updating = False
        self._last_node_status = {}
        self._pending_status_alerts = {}
        self._status_alert_lock = asyncio.Lock()
        self._attention_task = None
        self._attention_wait = None
        self._attention_issue_body = ""
        self._attention_stop = asyncio.Event()

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

    def load_cluster_nodes(self):
        data = self.load_cluster_config()
        return data.get("servers", [])

    def persist_panel_ids(self):
        data = load_master_config(self.config_path)
        data["discord"] = {
            "panel_channel_id": self.panel_channel_id,
            "panel_message_id": self.panel_message_id,
        }
        save_master_config(data, self.config_path)
        logger.info(
            "Persisted Discord panel IDs (channel=%s message=%s)",
            self.panel_channel_id,
            self.panel_message_id,
        )

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
        argv = [sys.executable, script, *sys.argv[1:]]
        cwd = install_dir or os.getcwd()
        logger.info("Relaunching Discord Bot from %s", script)
        try:
            await self.close()
        except Exception:
            pass

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

    async def setup_hook(self):
        await self.sync_slash_commands()
        self.queue_processor_loop.start()
        self.persistent_panel_refresh_loop.start()
        self.github_self_update_loop.start()
        self.load_cluster_config()
        logger.info("Discord Bot v%s running. Auto panel restore enabled.", CURRENT_BOT_VERSION)

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
        try:
            async for message in channel.history(limit=50):
                if message.author.id == self.user.id:
                    if message.embeds or message.components:
                        try:
                            await message.delete()
                            await asyncio.sleep(0.2)
                        except Exception:
                            pass
        except Exception as e:
            logger.error("Error purging historic channel messages: %s", e)

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

    async def _start_attention_round(self, body, guild):
        self._attention_issue_body = body
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
                    await member.send("No reply received — contacting the next person.")
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
                await message.channel.send("Thanks — the alert round is closed.")
            elif reply in ATTENTION_NO_REPLIES:
                wait["answer"] = "no"
                wait["event"].set()
                await message.channel.send("Understood — contacting the next person.")
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

                if pending and curr_status == STATUS_UP_TO_DATE:
                    logger.info(
                        "Cancelling delayed status alert for %s — recovered to %s",
                        name,
                        STATUS_UP_TO_DATE,
                    )
                    self._pending_status_alerts.pop(key, None)
                    continue

                if pending:
                    pending["latest_snap"] = snap
                    elapsed = now - pending["started_at"]
                    if elapsed < STATUS_ALERT_DELAY_SECONDS:
                        continue
                    text = self._describe_status_alert(pending["from_snap"], snap)
                    self._pending_status_alerts.pop(key, None)
                    if text:
                        problems.append(text)
                    continue

                text = self._describe_status_alert(prev, snap)
                if not text:
                    continue
                self._pending_status_alerts[key] = {
                    "started_at": now,
                    "from_snap": prev,
                    "latest_snap": snap,
                }
                logger.info(
                    "Delaying status alert for %s by %ss (%s -> %s)",
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

    async def execute_node_deployment(self, task_data):
        node = task_data["node"]
        channel = task_data["channel"]
        name = node["name"]

        status_msg = await channel.send(f"⏳ **[QUEUE]** Connecting to `{name}` for DCS update...")
        ans = await self.send_socket_command(node["ip"], node["port"], "TRIGGER_DCS_UPDATE")

        if not ans:
            await status_msg.edit(content=f"❌ **[{name}]** Connection timeout. Node did not respond.")
            return

        try:
            res_json = json.loads(ans)
            if res_json.get("status") == "UNAUTHORIZED":
                await status_msg.edit(
                    content=f"🔐 **[{name}]** Unauthorized — check shared auth_token on Bot and Node."
                )
                return
            if res_json.get("status") == "REJECTED_BUSY":
                await status_msg.edit(content=f"⚠️ **[{name}]** Server busy executing: `{res_json.get('task')}`.")
                return
            if res_json.get("status") != "OK_STARTING":
                await status_msg.edit(content=f"❌ **[{name}]** Rejected with status: `{res_json.get('status')}`.")
                return
        except Exception:
            await status_msg.edit(content=f"❌ **[{name}]** Error parsing JSON response payload.")
            return

        await status_msg.edit(content=f"🚀 **[{name}]** DCS update authorized! Downloading patch payload...")
        await asyncio.sleep(15)

        start_time = asyncio.get_event_loop().time()
        while True:
            if asyncio.get_event_loop().time() - start_time > 900:
                await status_msg.edit(content=f"⏰ **[{name}]** Deployment exceeded 15 minutes (Timeout reached).")
                break

            chk = await self.send_socket_command(node["ip"], node["port"], "PING_STATUS")
            if chk is None:
                await status_msg.edit(content=f"🎉 **[{name}]** Connection closed. Server completed sequence!")
                break

            if chk.startswith("{"):
                try:
                    res = json.loads(chk)
                    if res.get("active_task", "Idle") in ["Rebooting", "Idle"]:
                        await status_msg.edit(
                            content=f"🎉 **[{name}]** DCS update successfully completed and verified!"
                        )
                        break
                except Exception:
                    pass
            await asyncio.sleep(4)

        if self.active_panel_view:
            try:
                await self.active_panel_view.refresh_panel()
            except Exception:
                pass

    @tasks.loop(seconds=30)
    async def persistent_panel_refresh_loop(self):
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
            return False
        return True

    return app_commands.check(predicate)


PANEL_BOX_LINE_WIDTH = 13
PANEL_STATUS_SHORT = {
    "DCS NOT STARTED": "NOT STARTED",
    "DCS STARTING": "STARTING",
}
PANEL_TASK_SHORT = {
    "Awaiting server boot": "Boot pending",
    "Awaiting DCS port": "Port pending",
    "Port not responding": "Port down",
    "Server stopped/crashed": "Crashed",
    "DCS_server stopped": "Stopped",
}


def _panel_line(text: str, width: int = PANEL_BOX_LINE_WIDTH) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= width:
        return cleaned
    return cleaned[: width - 1] + "…"


def format_server_status_box(status_text: str, ver_info: str, task_info: str) -> str:
    """Fixed three-line status block so every server tile is the same height."""
    status_text = PANEL_STATUS_SHORT.get(status_text, status_text)
    task_info = PANEL_TASK_SHORT.get(task_info, task_info)
    rows = [
        f"ℹ️ {_panel_line(status_text)}",
        f"⚙️ {_panel_line(ver_info)}",
        f"🖥️ {_panel_line(task_info)}",
    ]
    return "```yaml\n" + "\n".join(rows) + "\n```"


def classify_node_answer(answer):
    status_text = STATUS_UP_TO_DATE
    ver_info = "Unknown"
    task_info = "Ready"
    is_outdated = False
    icon = "🟢"
    dcs_health = ""
    dcs_running = None

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

                if dcs_health == "STARTING":
                    status_text = "DCS STARTING"
                    ver_info = f"{installed_ver}"
                    icon = "⏳"
                    task_info = "Awaiting DCS port"
                elif dcs_health == "NEVER_STARTED":
                    status_text = "DCS NOT STARTED"
                    ver_info = f"{installed_ver}"
                    icon = "⏸️"
                    task_info = (
                        "Restarting..."
                        if active_task == "Restarting DCS"
                        else "Awaiting server boot"
                    )
                elif dcs_running is False or dcs_health in HEALTH_CRASHED:
                    status_text = "DCS DOWN"
                    ver_info = f"{installed_ver}"
                    icon = "🛑"
                    if active_task == "Restarting DCS":
                        task_info = "Restarting..."
                    elif dcs_health == "UNHEALTHY":
                        task_info = "Port not responding"
                    elif dcs_health == "DEAD":
                        task_info = "Server stopped/crashed"
                    else:
                        task_info = "DCS_server stopped"
                elif str(installed_ver).strip() != str(latest_ver).strip() and latest_ver != "Unknown":
                    status_text = "UPDATE READY"
                    ver_info = f"{installed_ver}"
                    icon = "⚠️"
                    is_outdated = True
                    task_info = "Ready" if active_task == "Idle" else active_task
                else:
                    status_text = STATUS_UP_TO_DATE
                    ver_info = f"{installed_ver}"
                    icon = "🟢"
                    task_info = "Ready" if active_task == "Idle" else active_task
        except Exception:
            status_text = "OFFLINE"
            icon = "🔴"
    else:
        status_text = "OFFLINE"
        icon = "🔴"

    return {
        "status_text": status_text,
        "ver_info": ver_info,
        "task_info": task_info,
        "is_outdated": is_outdated,
        "icon": icon,
        "dcs_health": dcs_health,
        "dcs_running": dcs_running,
    }


# ==================== INTERACTIVE MULTI-SELECT PANEL ENGINE ====================


class LiveControlPanelView(discord.ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.all_nodes_cached = []
        self.select_menu = None
        self.currently_selected_server_names = []

    async def generate_embed(self, guild=None):
        nodes = self.bot.load_cluster_nodes()
        self.all_nodes_cached = nodes

        dcs_latest_release = await self.bot.fetch_latest_dcs_release()
        current_time_str = datetime.now().strftime("%H:%M")

        embed = discord.Embed(
            description="\n```🛡️ Operational System Status for DCS World Servers```\n",
            color=discord.Color.from_rgb(26, 132, 255),
        )
        embed.set_footer(
            text=(
                f"Updated Today at {current_time_str}\n"
                f"ED Release Version: {dcs_latest_release}\n"
                f"Bot version: {CURRENT_BOT_VERSION}"
            )
        )

        if guild and guild.icon:
            embed.set_author(name="🛸 DCS Norway Live Control Panel", icon_url=guild.icon.url)
        else:
            embed.title = "🛸 DCS Norway Live Control Panel"

        options = []
        snapshots = []
        tasks_list = [self.bot.send_socket_command(n["ip"], n["port"], "PING_STATUS") for n in nodes]
        responses = await asyncio.gather(*tasks_list)

        for idx, (node, answer) in enumerate(zip(nodes, responses)):
            classified = classify_node_answer(answer)
            status_text = classified["status_text"]
            ver_info = classified["ver_info"]
            task_info = classified["task_info"]
            is_outdated = classified["is_outdated"]
            icon = classified["icon"]
            snapshots.append(
                {
                    "key": f"{node.get('ip')}:{node.get('port')}",
                    "name": node["name"],
                    **classified,
                }
            )

            if is_outdated:
                options.append(
                    discord.SelectOption(
                        label=node["name"],
                        description=f"Port {node['port']} | Select for deployment queue",
                        value=node["name"],
                        emoji="⚠️",
                    )
                )

            boxed_value = format_server_status_box(status_text, ver_info, task_info)

            field_name = f"{icon}\u2001{node['name']}\u2001\u2001\u2001\u2001\u2001\u2001"

            embed.add_field(name=field_name, value=boxed_value, inline=True)

            if (idx + 1) % 3 == 0 and (idx + 1) < len(nodes):
                for _ in range(3):
                    embed.add_field(name="\u2001", value="\u2001", inline=True)

        if self.select_menu in self.children:
            self.remove_item(self.select_menu)

        if options:
            self.select_menu = discord.ui.Select(
                placeholder="Check one or multiple servers to queue for update...",
                min_values=1,
                max_values=len(options),
                options=options,
                row=1,
                custom_id="dcs_panel:select",
            )
            self.select_menu.callback = self.select_menu_callback
            self.add_item(self.select_menu)

            self.btn_deploy_selected.disabled = False
            self.btn_deploy_selected.label = f"🚀 Execute {len(options)} Selected Update(s)"
            self.btn_deploy_selected.style = discord.ButtonStyle.danger
        else:
            self.btn_deploy_selected.disabled = True
            self.btn_deploy_selected.label = "✅ All Servers Up To Date"
            self.btn_deploy_selected.style = discord.ButtonStyle.secondary
            self.select_menu = None

        try:
            await self.bot.notify_status_changes(snapshots, guild)
        except Exception as e:
            logger.error("Failed to send status alert DMs: %s", e)

        return embed

    async def select_menu_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        self.currently_selected_server_names = self.select_menu.values
        selected_text = ", ".join(self.currently_selected_server_names)
        await interaction.followup.send(f"✅ Selected for deployment: **{selected_text}**.", ephemeral=True)

    async def refresh_panel(self):
        if self.bot.panel_channel_id and self.bot.panel_message_id:
            try:
                channel = self.bot.get_channel(int(self.bot.panel_channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(self.bot.panel_channel_id))
                message = await channel.fetch_message(int(self.bot.panel_message_id))
                new_embed = await self.generate_embed(guild=channel.guild)
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
        label="🚀 Execute Selected Updates",
        style=discord.ButtonStyle.secondary,
        row=0,
        disabled=True,
        custom_id="dcs_panel:deploy",
    )
    async def btn_deploy_selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not self.currently_selected_server_names:
            await interaction.followup.send(
                "⚠️ You must select at least one server from the menu dropdown!",
                ephemeral=True,
            )
            return

        await interaction.channel.send(
            f"🚨 **[DEPLOYMENT LOG]** Initiating sequential cluster updates for: "
            f"**{', '.join(self.currently_selected_server_names)}**."
        )

        for server_name in self.currently_selected_server_names:
            matched_node = next((n for n in self.all_nodes_cached if n["name"] == server_name), None)
            if matched_node:
                await self.bot.deployment_queue.put({"node": matched_node, "channel": interaction.channel})

        self.currently_selected_server_names = []
        await self.refresh_panel()


# ==================== INITIALIZATION COMMAND ====================


@bot.tree.command(name="dcs-panel-init", description="Pins the permanent live dashboard into this channel.")
@has_dcs_management_permission()
async def dcs_panel_init(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    await bot.purge_old_bot_messages(interaction.channel)

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
    name="check-bot-update",
    description="Force an immediate GitHub check and install a newer Discord Bot if available.",
)
@has_dcs_management_permission()
async def check_bot_update(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await bot.check_github_self_update(apply=False) or {
        "ok": False,
        "message": "Update check returned no result.",
    }
    prefix = "✅" if result.get("ok") else "❌"
    await interaction.followup.send(f"{prefix} {result.get('message')}", ephemeral=True)
    downloads = result.get("downloads")
    if result.get("ok") and result.get("updated") and downloads:
        await bot._apply_self_update(downloads)


if __name__ == "__main__":
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
