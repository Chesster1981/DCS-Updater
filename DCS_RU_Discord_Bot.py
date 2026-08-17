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
from datetime import datetime

from dcs_ru_common import (
    DCS_UPDATE_URLS,
    VERSION_PATTERNS,
    get_discord_bot_token,
    load_master_config,
    save_master_config,
    wrap_command,
)

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DCS_Discord_Bot")

CURRENT_BOT_VERSION = "2.1.19"
GITHUB_REPO = "Chesster1981/DCS-Updater"
URL_GITHUB_API = "https://api.github.com/repos/"
BOT_SELF_UPDATE_FILES = ("DCS_RU_Discord_Bot.py", "dcs_ru_common.py")
BOT_GITHUB_CHECK_DELAY_SECONDS = 20
BOT_GITHUB_CHECK_HOURS = 1


class DCSClusterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
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

    async def check_github_self_update(self):
        """Compare GitHub latest release and install source files if newer."""
        if self._self_updating or getattr(sys, "frozen", False):
            return
        headers = {
            "User-Agent": "DCS-Norway-Discord-Bot",
            "Accept": "application/vnd.github.v3+json",
        }
        url = f"{URL_GITHUB_API}{GITHUB_REPO}/releases/latest"
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        logger.warning("GitHub bot update check HTTP %s", response.status)
                        return
                    data = await response.json()
            latest = str(data.get("tag_name", "")).lstrip("v").strip()
            tag = str(data.get("tag_name", "")).strip() or f"v{latest}"
            logger.info(
                "Discord Bot v%s | GitHub latest v%s",
                CURRENT_BOT_VERSION,
                latest or "?",
            )
            if not latest:
                return
            if self._version_tuple(latest) <= self._version_tuple(CURRENT_BOT_VERSION):
                return

            downloads = {}
            assets = {str(a.get("name", "")): a.get("browser_download_url") for a in data.get("assets") or []}
            async with aiohttp.ClientSession(headers={"User-Agent": "DCS-Norway-Discord-Bot"}) as session:
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
                        return
                    downloads[filename] = content

            logger.info("Newer Discord Bot v%s available — applying self-update.", latest)
            await self._apply_self_update(downloads)
        except Exception as e:
            logger.error("GitHub bot update check failed: %s", e)

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

    async def setup_hook(self):
        await self.tree.sync()
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

    @tasks.loop(hours=BOT_GITHUB_CHECK_HOURS)
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
        embed.set_footer(text=f"Updated Today at {current_time_str}\nED Release Version: {dcs_latest_release}")

        if guild and guild.icon:
            embed.set_author(name="🛸 DCS Norway Live Control Panel", icon_url=guild.icon.url)
        else:
            embed.title = "🛸 DCS Norway Live Control Panel"

        options = []
        tasks_list = [self.bot.send_socket_command(n["ip"], n["port"], "PING_STATUS") for n in nodes]
        responses = await asyncio.gather(*tasks_list)

        for idx, (node, answer) in enumerate(zip(nodes, responses)):
            status_text = "UP TO DATE"
            ver_info = "Unknown"
            task_info = "Ready"
            is_outdated = False
            icon = "🟢"

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
                            task_info = "Waiting for DCS port after boot"
                        elif dcs_health == "NEVER_STARTED":
                            status_text = "DCS NOT STARTED"
                            ver_info = f"{installed_ver}"
                            icon = "⏸️"
                            task_info = (
                                "Restarting..."
                                if active_task == "Restarting DCS"
                                else "Waiting for manual/server boot start"
                            )
                        elif dcs_running is False or dcs_health in ("DEAD", "UNHEALTHY"):
                            status_text = "DCS DOWN"
                            ver_info = f"{installed_ver}"
                            icon = "🛑"
                            if active_task == "Restarting DCS":
                                task_info = "Restarting..."
                            elif dcs_health == "UNHEALTHY":
                                task_info = "Process up, port not responding"
                            elif dcs_health == "DEAD":
                                task_info = "Server stopped/crashed"
                            else:
                                task_info = "DCS_server.exe stopped"
                        elif str(installed_ver).strip() != str(latest_ver).strip() and latest_ver != "Unknown":
                            status_text = "UPDATE READY"
                            ver_info = f"{installed_ver}"
                            icon = "⚠️"
                            is_outdated = True
                            task_info = "Ready" if active_task == "Idle" else active_task
                        else:
                            status_text = "UP TO DATE"
                            ver_info = f"{installed_ver}"
                            icon = "🟢"
                            task_info = "Ready" if active_task == "Idle" else active_task
                except Exception:
                    status_text = "OFFLINE"
                    icon = "🔴"
            else:
                status_text = "OFFLINE"
                icon = "🔴"

            if is_outdated:
                options.append(
                    discord.SelectOption(
                        label=node["name"],
                        description=f"Port {node['port']} | Select for deployment queue",
                        value=node["name"],
                        emoji="⚠️",
                    )
                )

            boxed_value = (
                f"```yaml\n"
                f"ℹ️ {status_text:<18}\n"
                f"⚙️ {ver_info:<18}\n"
                f"🖥️ {task_info:<18}\n"
                f"```"
            )

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
    logger.info("Persistent dashboard frame spawned and locked onto Message ID: %s", message.id)


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
