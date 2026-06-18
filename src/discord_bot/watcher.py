"""
Watcher Bot
============

The sixth bot — infrastructure only, no persona identity.
Responsibilities:
    - Listen to all messages across watched channels
    - Feed user messages to HouseOrchestrator for arbitration + response
    - Dispatch responses to the correct PersonaClient(s)
    - Manage the shared ConversationBuffer per channel
    - Ignore bot messages (including from the five persona bots)
    - Slash commands for runtime configuration

The Watcher never speaks in character. It's the invisible coordinator.

Slash Commands:
    /watch #channel          — Start watching a channel
    /unwatch #channel        — Stop watching a channel
    /channels                — List currently watched channels
    /status                  — Show bot fleet status
    /reset_buffer #channel   — Clear conversation buffer for a channel
    /set_default #channel persona — Set a default persona for a channel
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import discord
from discord import app_commands

from ..conversation.buffer import ConversationBuffer
from ..providers.base import ErrorCategory
from ..unified_orchestrator import HouseUnavailableError

logger = logging.getLogger(__name__)


class Watcher(discord.Client):
    """
    Coordinator bot that listens, arbitrates, and dispatches.
    Owns the slash command tree for runtime configuration.
    """

    def __init__(
        self,
        house_orchestrator,
        persona_clients: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(intents=intents, **kwargs)

        self._house = house_orchestrator
        self._persona_clients = persona_clients
        self._config = config or {}

        # Per-channel conversation buffers
        self._buffers: Dict[str, ConversationBuffer] = {}
        self._buffer_max_turns = self._config.get("conversation", {}).get("max_turns", 50)
        # The active buffer holds more turns than the LLM window (headroom),
        # but is still capped so the live file can't grow without bound. Turns
        # evicted past this cap are archived, not dropped.
        self._buffer_active_cap = self._buffer_max_turns * 2

        # Set of all persona bot user IDs (populated on_ready)
        self._persona_bot_ids: Set[int] = set()

        # Cached map of persona bot user ID → persona name (populated on_ready).
        # Used to resolve @mentions without reading live client.user, which can
        # be None mid-reconnect and silently drop a ping.
        self._persona_id_to_name: Dict[int, str] = {}

        # Processing lock per channel to prevent concurrent arbitration
        self._channel_locks: Dict[int, asyncio.Lock] = {}

        # ── Watched channels ─────────────────────────────────────
        # Channel IDs that the watcher actively monitors
        # Empty set = watch nothing (must /watch to start)
        self._watched_channel_ids: Set[int] = set()

        # Channel-specific default personas (skip arbitrator)
        self._channel_defaults: Dict[int, str] = {}

        # Load channel defaults from config
        discord_config = self._config.get("discord", {})
        self._config_channel_names: List[str] = discord_config.get("channels", [])

        # Name of the shared role that summons the whole house (case-insensitive).
        # Pinging this role runs the full house; pinging an individual persona
        # bot summons only that persona; a message with neither is ignored.
        self._girls_role_name: str = discord_config.get("girls_role", "Girls").lower()

        # Abuse guards for public servers: per-user trigger cooldown, and a cap
        # on how many messages may wait behind a channel's processing lock.
        # Throttled messages get a 🕐 reaction instead of queueing an LLM call.
        self._user_cooldown_seconds = float(
            discord_config.get("user_cooldown_seconds", 8)
        )
        self._max_queued_per_channel = int(
            discord_config.get("max_queued_per_channel", 3)
        )
        self._last_trigger_at: Dict[int, float] = {}  # user_id → time.monotonic()
        self._queued_count: Dict[int, int] = {}  # channel_id → waiting messages

        # Slash-command operators. On a server where you aren't an admin you
        # can't use the admin-gated commands, so list your Discord user ID(s)
        # here to authorize them by identity instead of by server role. A
        # server Administrator is always authorized too.
        self._owner_ids: set = {
            int(uid) for uid in discord_config.get("owner_ids", []) if str(uid).strip()
        }

        # Prefix for text-based admin commands (e.g. "!house watch"). Text
        # commands work with only the bot scope + message-content intent, so
        # they function on servers where the slash-command scope
        # (applications.commands) wasn't granted. Same admin/owner gating.
        self._cmd_prefix: str = str(
            discord_config.get("command_prefix", "!house")
        ).strip().lower()

        # on_ready re-fires on every new gateway session; one-time setup
        # (watch state load, command sync) must not re-run on reconnect.
        self._ready_initialized = False
        # Set once first-ready setup finishes (watch state loaded) — lets
        # announce_system() wait until the channel list actually exists.
        self._setup_done: asyncio.Event = asyncio.Event()

        # Persistence file for watched channels
        data_dir = self._config.get("memory", {}).get("data_dir", "./data")
        self._watch_state_path = Path(data_dir) / "discord_watch_state.json"

        # ── Slash command tree ───────────────────────────────────
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    # ── Slash Commands ───────────────────────────────────────────

    def _register_commands(self):
        """Register all slash commands on the command tree."""

        def is_authorized(interaction: discord.Interaction) -> bool:
            # Runtime gate. Commands are visible to everyone (no
            # default_permissions), so this is the real authorization check:
            # an operator listed in discord.owner_ids, OR a server admin.
            if interaction.user.id in self._owner_ids:
                return True
            perms = getattr(interaction.user, "guild_permissions", None)
            return bool(perms and perms.administrator)

        @self.tree.error
        async def on_command_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ):
            if isinstance(error, app_commands.CheckFailure):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Not authorized.", ephemeral=True
                    )
                return
            logger.error(f"[Watcher] Slash command error: {error}", exc_info=error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Command failed — check the logs.", ephemeral=True
                )

        @self.tree.command(
            name="watch",
            description="Start watching a channel — the personas will respond to messages here",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        @app_commands.describe(channel="The channel to start watching")
        async def watch(interaction: discord.Interaction, channel: discord.TextChannel):
            self._watched_channel_ids.add(channel.id)
            self._save_watch_state()
            await interaction.response.send_message(
                f"Now watching **#{channel.name}**. Personas will respond to messages there.",
                ephemeral=True,
            )
            logger.info(f"[Watcher] Now watching #{channel.name} ({channel.id})")

        @self.tree.command(
            name="unwatch",
            description="Stop watching a channel — personas will no longer respond here",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        @app_commands.describe(channel="The channel to stop watching")
        async def unwatch(interaction: discord.Interaction, channel: discord.TextChannel):
            self._watched_channel_ids.discard(channel.id)
            self._channel_defaults.pop(channel.id, None)
            self._save_watch_state()
            await interaction.response.send_message(
                f"Stopped watching **#{channel.name}**.",
                ephemeral=True,
            )
            logger.info(f"[Watcher] Stopped watching #{channel.name} ({channel.id})")

        @self.tree.command(
            name="channels",
            description="List all currently watched channels",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        async def channels(interaction: discord.Interaction):
            if not self._watched_channel_ids:
                await interaction.response.send_message(
                    "Not watching any channels. Use `/watch` to add one.",
                    ephemeral=True,
                )
                return

            lines = []
            for cid in sorted(self._watched_channel_ids):
                ch = self.get_channel(cid)
                name = f"#{ch.name}" if ch else f"(unknown: {cid})"
                default = self._channel_defaults.get(cid)
                suffix = f" → **{default}**" if default else ""
                lines.append(f"• {name}{suffix}")

            await interaction.response.send_message(
                "**Watched channels:**\n" + "\n".join(lines),
                ephemeral=True,
            )

        @self.tree.command(
            name="status",
            description="Show the bot fleet status — who's online, what's being watched",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        async def status(interaction: discord.Interaction):
            lines = ["**Bot Fleet Status**\n"]

            # Persona bots
            for name, client in self._persona_clients.items():
                if client.is_ready() and client.user:
                    lines.append(f"• **{name.capitalize()}**: online ({client.user})")
                else:
                    lines.append(f"• **{name.capitalize()}**: offline")

            # Watched channels
            lines.append(f"\n**Watched channels:** {len(self._watched_channel_ids)}")
            lines.append(f"**Active buffers:** {len(self._buffers)}")

            # Generation mode
            unified = self._config.get("unified", {})
            json_mode = unified.get("json_mode", True)
            lines.append(f"**Mode:** unified generation (json_mode={'on' if json_mode else 'off'})")

            # TTS
            tts_provider = self._config.get("tts", {}).get("provider", "none")
            lines.append(f"**TTS:** {tts_provider}")

            await interaction.response.send_message(
                "\n".join(lines),
                ephemeral=True,
            )

        @self.tree.command(
            name="set_default",
            description="Set a default persona for a channel — skips the arbitrator",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        @app_commands.describe(
            channel="The channel to set a default for",
            persona="The persona who always responds in this channel",
        )
        @app_commands.choices(
            persona=[
                app_commands.Choice(name=p.capitalize(), value=p)
                for p in self._config.get("personas", [])
            ]
        )
        async def set_default(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
            persona: app_commands.Choice[str],
        ):
            self._channel_defaults[channel.id] = persona.value
            # Also make sure the channel is watched
            self._watched_channel_ids.add(channel.id)
            self._save_watch_state()
            await interaction.response.send_message(
                f"**#{channel.name}** → **{persona.name}** will always respond "
                f"(arbitrator bypassed).",
                ephemeral=True,
            )
            logger.info(f"[Watcher] #{channel.name} default set to {persona.value}")

        @self.tree.command(
            name="clear_default",
            description="Remove the default persona for a channel — use the arbitrator again",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        @app_commands.describe(channel="The channel to clear the default for")
        async def clear_default(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            removed = self._channel_defaults.pop(channel.id, None)
            self._save_watch_state()
            if removed:
                await interaction.response.send_message(
                    f"Cleared default persona for **#{channel.name}**. "
                    f"Arbitrator will decide who responds.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"**#{channel.name}** had no default persona set.",
                    ephemeral=True,
                )

        @self.tree.command(
            name="reset_buffer",
            description="Clear the conversation history for a channel — fresh start",
        )
        @app_commands.guild_only()
        @app_commands.check(is_authorized)
        @app_commands.describe(channel="The channel to reset")
        async def reset_buffer(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            # Take the channel's processing lock so an in-flight message
            # (holding the old buffer object) can't re-save it after the
            # reset, silently undoing it. The lock may be held by an LLM
            # call for several seconds — defer to stay within Discord's
            # 3-second interaction window.
            await interaction.response.defer(ephemeral=True)
            async with self._channel_locks.setdefault(channel.id, asyncio.Lock()):
                if channel.id in self._buffers:
                    self._buffers[channel.id].clear()
                    del self._buffers[channel.id]

                # Delete the persisted buffer file too
                data_dir = self._config.get("memory", {}).get("data_dir", "./data")
                buf_path = Path(
                    ConversationBuffer.session_file_path(
                        f"discord_{channel.id}", data_dir
                    )
                )
                if buf_path.exists():
                    buf_path.unlink()

            await interaction.followup.send(
                f"Conversation buffer for **#{channel.name}** has been cleared.",
                ephemeral=True,
            )
            logger.info(f"[Watcher] Buffer cleared for #{channel.name} ({channel.id})")

    # ── Events ───────────────────────────────────────────────────

    async def on_ready(self):
        logger.info(f"[Watcher] Connected as {self.user} (ID: {self.user.id})")

        # Collect persona bot user IDs for filtering and mention resolution
        for name, client in self._persona_clients.items():
            if client.user:
                self._persona_bot_ids.add(client.user.id)
                self._persona_id_to_name[client.user.id] = name
                logger.info(f"[Watcher] Tracking persona bot: {name} → {client.user.id}")

        # One-time setup only: on_ready re-fires whenever discord.py opens a
        # new session (resume failure, extended outage). Re-running the steps
        # below on reconnect could wipe watch state while a guild is briefly
        # unavailable, and would re-sync slash commands needlessly.
        if self._ready_initialized:
            logger.info("[Watcher] Reconnected — keeping existing watch state")
            return
        self._ready_initialized = True

        # Load persisted watch state. Channels that no longer resolve are kept
        # (a stale ID never matches a message, so it's harmless) rather than
        # pruned — pruning here destroyed the watch list when a guild was
        # merely unavailable at READY time.
        self._load_watch_state()
        unresolved = [
            cid for cid in self._watched_channel_ids if self.get_channel(cid) is None
        ]
        if unresolved:
            logger.warning(
                f"[Watcher] {len(unresolved)} watched channel(s) not currently "
                f"resolvable (guild unavailable or channel deleted): {unresolved}"
            )

        # Resolve config channel names → IDs (first time setup)
        if self._config_channel_names and not self._watched_channel_ids:
            for guild in self.guilds:
                for ch in guild.text_channels:
                    if ch.name in self._config_channel_names:
                        self._watched_channel_ids.add(ch.id)
                        logger.info(f"[Watcher] Auto-watching #{ch.name} from config")
            if self._watched_channel_ids:
                self._save_watch_state()

        # Sync slash commands with Discord
        try:
            synced = await self.tree.sync()
            logger.info(f"[Watcher] Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"[Watcher] Failed to sync slash commands: {e}")

        watched_names = []
        for cid in self._watched_channel_ids:
            ch = self.get_channel(cid)
            watched_names.append(f"#{ch.name}" if ch else f"({cid})")

        logger.info(
            f"[Watcher] Monitoring: {', '.join(watched_names) if watched_names else 'no channels (use /watch)'}"
        )
        self._setup_done.set()

    async def announce_system(self, text: str, wait_seconds: float = 0) -> bool:
        """Send a small status line to every watched channel, as the Watcher.

        Used for process-startup events (e.g. the reflection cycle). Waits up
        to wait_seconds for first-ready setup so the watched-channel list is
        loaded; returns False if setup didn't finish in time or nothing was
        sent. Failure-soft — never raises.
        """
        if wait_seconds:
            try:
                await asyncio.wait_for(self._setup_done.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                logger.warning(
                    "[Watcher] Announce skipped — setup not finished "
                    f"within {wait_seconds}s: {text!r}"
                )
                return False
        elif not self._setup_done.is_set():
            return False

        sent = False
        for cid in list(self._watched_channel_ids):
            channel = self.get_channel(cid)
            if channel is None:
                # Cache miss — READY can fire before every guild's channels
                # are cached (announce runs at startup), so fetch directly
                # instead of silently skipping the channel.
                try:
                    channel = await self.fetch_channel(cid)
                except Exception as e:
                    logger.warning(
                        f"[Watcher] Announce skipped — channel {cid} "
                        f"unresolvable: {e}"
                    )
                    continue
            try:
                await channel.send(text)
                sent = True
            except discord.HTTPException as e:
                logger.warning(f"[Watcher] Announce failed in channel {cid}: {e}")
        return sent

    # ── Text commands (!house ...) ───────────────────────────────
    # Mirror the slash commands, but as message-prefix commands so they work
    # on servers that didn't grant the slash-command scope. Same gating:
    # an operator in discord.owner_ids, or a server admin.

    def _is_authorized_member(self, member: discord.abc.User) -> bool:
        if member.id in self._owner_ids:
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def _command_target_channel(self, message: discord.Message):
        """Channel a command acts on: the first #mention, else the channel
        the command was sent in. Returns None if that isn't a text channel."""
        if message.channel_mentions:
            ch = message.channel_mentions[0]
        else:
            ch = message.channel
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _handle_house_command(self, message: discord.Message):
        """Parse and dispatch a `!house <subcommand> [args]` message."""
        if not self._is_authorized_member(message.author):
            await message.reply("Not authorized.", mention_author=False)
            return

        parts = message.content.split()
        sub = parts[1].lower() if len(parts) > 1 else "help"
        args = parts[2:]

        handlers = {
            "watch": self._cmd_watch,
            "unwatch": self._cmd_unwatch,
            "channels": self._cmd_channels,
            "status": self._cmd_status,
            "set_default": self._cmd_set_default,
            "clear_default": self._cmd_clear_default,
            "reset_buffer": self._cmd_reset_buffer,
            "help": self._cmd_help,
        }
        handler = handlers.get(sub)
        if handler is None:
            await message.reply(
                f"Unknown command `{sub}`. Try `{self._cmd_prefix} help`.",
                mention_author=False,
            )
            return
        try:
            await handler(message, args)
        except Exception as e:
            logger.error(f"[Watcher] !house {sub} failed: {e}", exc_info=e)
            await message.reply("Command failed — check the logs.", mention_author=False)

    async def _cmd_help(self, message: discord.Message, args):
        p = self._cmd_prefix
        await message.reply(
            "**House commands** (admin/owner only):\n"
            f"`{p} watch [#channel]` — start responding in a channel (default: here)\n"
            f"`{p} unwatch [#channel]` — stop responding there\n"
            f"`{p} channels` — list watched channels\n"
            f"`{p} status` — bot fleet status\n"
            f"`{p} set_default [#channel] <persona>` — one persona answers every message there\n"
            f"`{p} clear_default [#channel]` — remove that default\n"
            f"`{p} reset_buffer [#channel]` — clear a channel's conversation history",
            mention_author=False,
        )

    async def _cmd_watch(self, message: discord.Message, args):
        channel = self._command_target_channel(message)
        if channel is None:
            await message.reply("Pick a text channel.", mention_author=False)
            return
        self._watched_channel_ids.add(channel.id)
        self._save_watch_state()
        await message.reply(
            f"Now watching **#{channel.name}**. Personas will respond to messages there.",
            mention_author=False,
        )
        logger.info(f"[Watcher] Now watching #{channel.name} ({channel.id})")

    async def _cmd_unwatch(self, message: discord.Message, args):
        channel = self._command_target_channel(message)
        if channel is None:
            await message.reply("Pick a text channel.", mention_author=False)
            return
        self._watched_channel_ids.discard(channel.id)
        self._channel_defaults.pop(channel.id, None)
        self._save_watch_state()
        await message.reply(
            f"Stopped watching **#{channel.name}**.", mention_author=False
        )
        logger.info(f"[Watcher] Stopped watching #{channel.name} ({channel.id})")

    async def _cmd_channels(self, message: discord.Message, args):
        if not self._watched_channel_ids:
            await message.reply(
                f"Not watching any channels. Use `{self._cmd_prefix} watch` to add one.",
                mention_author=False,
            )
            return
        lines = []
        for cid in sorted(self._watched_channel_ids):
            ch = self.get_channel(cid)
            name = f"#{ch.name}" if ch else f"(unknown: {cid})"
            default = self._channel_defaults.get(cid)
            suffix = f" → **{default}**" if default else ""
            lines.append(f"• {name}{suffix}")
        await message.reply(
            "**Watched channels:**\n" + "\n".join(lines), mention_author=False
        )

    async def _cmd_status(self, message: discord.Message, args):
        lines = ["**Bot Fleet Status**\n"]
        for name, client in self._persona_clients.items():
            if client.is_ready() and client.user:
                lines.append(f"• **{name.capitalize()}**: online ({client.user})")
            else:
                lines.append(f"• **{name.capitalize()}**: offline")
        lines.append(f"\n**Watched channels:** {len(self._watched_channel_ids)}")
        lines.append(f"**Active buffers:** {len(self._buffers)}")
        unified = self._config.get("unified", {})
        json_mode = unified.get("json_mode", True)
        lines.append(f"**Mode:** unified generation (json_mode={'on' if json_mode else 'off'})")
        tts_provider = self._config.get("tts", {}).get("provider", "none")
        lines.append(f"**TTS:** {tts_provider}")
        await message.reply("\n".join(lines), mention_author=False)

    async def _cmd_set_default(self, message: discord.Message, args):
        personas = [p.lower() for p in self._config.get("personas", [])]
        persona = next((a.lower() for a in args if a.lower() in personas), None)
        if persona is None:
            await message.reply(
                f"Usage: `{self._cmd_prefix} set_default [#channel] <persona>` — "
                f"persona one of: {', '.join(personas)}",
                mention_author=False,
            )
            return
        channel = self._command_target_channel(message)
        if channel is None:
            await message.reply("Pick a text channel.", mention_author=False)
            return
        self._channel_defaults[channel.id] = persona
        self._watched_channel_ids.add(channel.id)
        self._save_watch_state()
        await message.reply(
            f"**#{channel.name}** → **{persona}** will always respond "
            f"(arbitrator bypassed).",
            mention_author=False,
        )
        logger.info(f"[Watcher] #{channel.name} default set to {persona}")

    async def _cmd_clear_default(self, message: discord.Message, args):
        channel = self._command_target_channel(message)
        if channel is None:
            await message.reply("Pick a text channel.", mention_author=False)
            return
        removed = self._channel_defaults.pop(channel.id, None)
        self._save_watch_state()
        if removed:
            await message.reply(
                f"Cleared default persona for **#{channel.name}**. "
                f"Arbitrator will decide who responds.",
                mention_author=False,
            )
        else:
            await message.reply(
                f"**#{channel.name}** had no default persona set.",
                mention_author=False,
            )

    async def _cmd_reset_buffer(self, message: discord.Message, args):
        channel = self._command_target_channel(message)
        if channel is None:
            await message.reply("Pick a text channel.", mention_author=False)
            return
        # Take the channel's processing lock so an in-flight message (holding
        # the old buffer object) can't re-save it after the reset.
        async with self._channel_locks.setdefault(channel.id, asyncio.Lock()):
            if channel.id in self._buffers:
                self._buffers[channel.id].clear()
                del self._buffers[channel.id]
            data_dir = self._config.get("memory", {}).get("data_dir", "./data")
            buf_path = Path(
                ConversationBuffer.session_file_path(
                    f"discord_{channel.id}", data_dir
                )
            )
            if buf_path.exists():
                buf_path.unlink()
        await message.reply(
            f"Conversation buffer for **#{channel.name}** has been cleared.",
            mention_author=False,
        )
        logger.info(f"[Watcher] Buffer cleared for #{channel.name} ({channel.id})")

    async def on_message(self, message: discord.Message):
        """
        Main message handler.
        Filters → buffer → orchestrate → dispatch.
        """
        # ── Filtering ────────────────────────────────────────────
        # Ignore all bots (persona bots, watcher itself, other bots)
        if message.author.bot:
            return

        # Ignore DMs (for now — could support later)
        if not message.guild:
            return

        # ── Text admin commands (!house ...) ─────────────────────
        # Handled before the watched-channel gate so `!house watch` works in a
        # channel that isn't watched yet. Authorization is checked inside.
        first = message.content.split(maxsplit=1)[0].lower() if message.content else ""
        if first == self._cmd_prefix:
            await self._handle_house_command(message)
            return

        # Only respond in watched channels
        if message.channel.id not in self._watched_channel_ids:
            return

        # ── Trigger gating ───────────────────────────────────────
        # The house only speaks when explicitly summoned: either the shared
        # @Girls role (whole house) or a specific persona bot ping (that
        # persona only). Everything else is ignored — no LLM call, no cost.
        cleaned_input, inline_personas = self._resolve_mentions(message.content)
        mentioned_personas = self._all_mentioned_personas(message, inline_personas)
        girls_triggered = self._girls_role_mentioned(message)

        # A channel with a standing default persona (/set_default) answers
        # every message there, ping or not — as that persona only. Opt-in:
        # no default set means the ping gate below still applies.
        channel_default = self._channel_defaults.get(message.channel.id)

        if not girls_triggered and not mentioned_personas and not channel_default:
            logger.info(
                f"[Watcher] #{message.channel.name} | ignored (no ping) | "
                f"{message.author.display_name}: {message.content[:60]}"
            )
            return

        # @Girls = full house (model picks who speaks). Otherwise: explicit
        # pings if any, else the channel's standing default persona. A @Girls
        # ping wins over individual pings and over the default.
        if girls_triggered:
            forced_personas = None
            trigger = "girls"
        elif mentioned_personas:
            forced_personas = set(mentioned_personas)
            trigger = f"personas={mentioned_personas}"
        else:
            forced_personas = {channel_default}
            trigger = f"default={channel_default}"

        logger.info(
            f"[Watcher] #{message.channel.name} | triggered ({trigger}) | "
            f"{message.author.display_name}: {message.content[:60]}"
        )

        if girls_triggered:
            cleaned_input = self._strip_role_mention(cleaned_input, message)

        # ── Abuse guards ─────────────────────────────────────────
        # Every trigger past this point is a paid LLM call. On a public
        # server, throttle per-user and cap the per-channel queue so a spam
        # loop can't stack up unbounded calls behind the lock.
        now = time.monotonic()
        last = self._last_trigger_at.get(message.author.id)
        if last is not None and (now - last) < self._user_cooldown_seconds:
            logger.info(
                f"[Watcher] #{message.channel.name} | throttled (cooldown) | "
                f"{message.author.display_name}"
            )
            await self._react_throttled(message)
            return

        channel_lock = self._channel_locks.setdefault(
            message.channel.id, asyncio.Lock()
        )
        if (
            channel_lock.locked()
            and self._queued_count.get(message.channel.id, 0)
            >= self._max_queued_per_channel
        ):
            logger.info(
                f"[Watcher] #{message.channel.name} | dropped (queue full) | "
                f"{message.author.display_name}"
            )
            await self._react_throttled(message)
            return

        self._last_trigger_at[message.author.id] = now

        # ── Get channel lock ─────────────────────────────────────
        self._queued_count[message.channel.id] = (
            self._queued_count.get(message.channel.id, 0) + 1
        )
        try:
            async with channel_lock:
                await self._process_message(message, cleaned_input, forced_personas)
        finally:
            self._queued_count[message.channel.id] -= 1

    async def _react_throttled(self, message: discord.Message) -> None:
        """Mark a dropped message with 🕐 so the user knows it wasn't missed."""
        try:
            await message.add_reaction("\U0001f552")
        except discord.HTTPException:
            pass

    async def _process_message(
        self,
        message: discord.Message,
        cleaned_input: str,
        forced_personas: Optional[Set[str]],
    ):
        """Process a single user message through the full pipeline."""
        channel_name = message.channel.name
        user_name = message.author.display_name

        logger.info(
            f"[Watcher] #{channel_name} | {user_name}: {cleaned_input[:80]}"
            + ("..." if len(cleaned_input) > 80 else "")
        )

        # ── Signal: thinking ─────────────────────────────────────
        try:
            await message.add_reaction("\U0001f9e0")  # 🧠
        except discord.HTTPException:
            pass

        # ── Conversation buffer ──────────────────────────────────
        channel_id = message.channel.id
        buffer = self._get_or_create_buffer(channel_id)
        buffer.add_user_message(
            content=cleaned_input,
            speaker_name=user_name,
        )

        # ── Attribute the current message ────────────────────────
        # History turns carry [name]: tags via the buffer, but the current
        # message is passed to the model separately — tag it the same way so
        # the model knows who is speaking *now* (and memory stores who said
        # what). If this is a Discord reply, anchor it with a quote of the
        # replied-to message, which may be old, unpinged, or pre-restart —
        # none of which the buffer would have.
        attributed_input = f"[{user_name}]: {cleaned_input}"
        reply_context = await self._reply_context(message)
        if reply_context:
            attributed_input = f"{reply_context}\n{attributed_input}"

        # ── Orchestrate (single unified call) ────────────────────
        try:
            turns = await self._house.process_message(
                user_input=attributed_input,
                session_id=f"discord_{channel_id}",
                user_id=str(message.author.id),
                channel_name=channel_name,
                conversation_buffer=buffer,
                forced_personas=forced_personas,
            )
        except HouseUnavailableError as e:
            # Say WHY the House went quiet so users can react (and tell
            # Locke when the credits run dry) instead of assuming it broke.
            if e.category is ErrorCategory.INSUFFICIENT_CREDITS:
                notice = (
                    "⚠️ The House is out of model credits — "
                    "someone let Locke know."
                )
            else:  # rate limit, retries exhausted
                notice = (
                    "⚠️ The model provider is rate-limiting the House — "
                    "give it a minute and ping again."
                )
            try:
                await message.channel.send(notice)
            except discord.HTTPException:
                pass
            try:
                await message.remove_reaction("\U0001f9e0", self.user)
            except discord.HTTPException:
                pass
            return
        except Exception as e:
            logger.error(f"[Watcher] Orchestration failed: {e}", exc_info=True)
            try:
                await message.remove_reaction("\U0001f9e0", self.user)
            except discord.HTTPException:
                pass
            return

        # ── Dispatch turns in scene order ────────────────────────
        dispatched = 0
        for turn in turns:
            persona_name = turn["persona"]
            response_text = turn["text"]
            if not response_text.strip():
                continue

            # Find the persona client
            client = self._persona_clients.get(persona_name)
            if client is None:
                logger.error(f"[Watcher] No client for persona '{persona_name}' — skipping")
                continue

            # Resolve the channel from the persona client's perspective
            persona_channel = client.get_channel(message.channel.id)
            if persona_channel is None:
                try:
                    persona_channel = await client.fetch_channel(message.channel.id)
                except discord.HTTPException:
                    logger.error(
                        f"[Watcher] {persona_name} can't access #{channel_name}"
                    )
                    continue

            # A short beat between turns so the scene reads like a room
            # talking, not a burst — scaled to the turn's length. The first
            # turn goes out immediately.
            beat = 0.0 if dispatched == 0 else min(1.0 + len(response_text) / 150, 4.0)

            # Show typing indicator from the persona bot while sending
            try:
                async with persona_channel.typing():
                    if beat:
                        await asyncio.sleep(beat)
                    sent_messages = await client.send_long_response(
                        persona_channel, response_text
                    )
            except discord.HTTPException:
                sent_messages = await client.send_long_response(
                    persona_channel, response_text
                )

            # Record in buffer
            if sent_messages:
                dispatched += 1
                buffer.add_assistant_response(
                    content=response_text,
                    persona=persona_name,
                )

            logger.info(
                f"[Watcher] #{channel_name} | {persona_name} spoke "
                f"({len(response_text)} chars)"
            )

        # ── Signal: done ─────────────────────────────────────────
        try:
            await message.remove_reaction("\U0001f9e0", self.user)
            await message.add_reaction("\u2705")  # ✅
        except discord.HTTPException:
            pass

        # ── Cap active buffer; archive evicted turns ─────────────
        expired = buffer.trim(self._buffer_active_cap)
        if expired:
            self._archive_turns(channel_id, expired)

        # ── Persist buffer ───────────────────────────────────────
        if buffer.is_dirty:
            try:
                data_dir = self._config.get("memory", {}).get("data_dir", "./data")
                # Off the event loop — sync JSON write would otherwise stall
                # every other channel's processing for the duration.
                await asyncio.to_thread(
                    buffer.save,
                    ConversationBuffer.session_file_path(
                        f"discord_{channel_id}", data_dir
                    ),
                )
            except Exception as e:
                logger.warning(f"[Watcher] Buffer save failed: {e}")

    # ── Buffer Management ────────────────────────────────────────

    def _get_or_create_buffer(self, channel_id: int) -> ConversationBuffer:
        """Get or create a conversation buffer for a channel (keyed by ID for multi-server safety)."""
        if channel_id not in self._buffers:
            session_id = f"discord_{channel_id}"
            data_dir = self._config.get("memory", {}).get("data_dir", "./data")

            self._buffers[channel_id] = ConversationBuffer.load_or_create(
                session_id=session_id,
                base_dir=data_dir,
                max_turns=self._buffer_max_turns,
            )

        return self._buffers[channel_id]

    def _archive_turns(self, channel_id: int, turns: list) -> None:
        """Append evicted buffer turns to the channel's archive (one JSON per line)."""
        data_dir = self._config.get("memory", {}).get("data_dir", "./data")
        path = Path(
            ConversationBuffer.archive_file_path(f"discord_{channel_id}", data_dir)
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for turn in turns:
                    f.write(json.dumps(turn.to_dict()) + "\n")
            logger.info(
                f"[Watcher] Archived {len(turns)} evicted turn(s) for channel {channel_id}"
            )
        except OSError as e:
            logger.warning(f"[Watcher] Failed to archive turns: {e}")

    # ── Watch State Persistence ──────────────────────────────────

    def _save_watch_state(self) -> None:
        """Persist watched channel IDs and defaults to disk."""
        state = {
            "watched_channel_ids": list(self._watched_channel_ids),
            "channel_defaults": {
                str(k): v for k, v in self._channel_defaults.items()
            },
        }
        # Atomic write (temp + rename) so a crash mid-write can't truncate or
        # corrupt the watch list — losing it would silently unwatch every
        # channel on next start.
        from ..utils.io import atomic_write_json
        atomic_write_json(self._watch_state_path, state)

    def _load_watch_state(self) -> None:
        """Load persisted watch state from disk."""
        if not self._watch_state_path.exists():
            return
        try:
            with open(self._watch_state_path) as f:
                state = json.load(f)
            self._watched_channel_ids = set(state.get("watched_channel_ids", []))
            self._channel_defaults = {
                int(k): v for k, v in state.get("channel_defaults", {}).items()
            }
            logger.info(
                f"[Watcher] Loaded watch state: "
                f"{len(self._watched_channel_ids)} channels, "
                f"{len(self._channel_defaults)} defaults"
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[Watcher] Failed to load watch state: {e}")

    def _persona_id_map(self) -> Dict[int, str]:
        """ID → persona name map for mention resolution.

        Refreshes the cache from live client.user on every call: all six bots
        connect concurrently, so personas that ready *after* the watcher must
        still get picked up, or their pings silently stop resolving. The cache
        keeps a persona resolvable while it's mid-reconnect (client.user is
        None during a reconnect).
        """
        for name, client in self._persona_clients.items():
            if client.user:
                self._persona_bot_ids.add(client.user.id)
                self._persona_id_to_name[client.user.id] = name
        return self._persona_id_to_name

    def _resolve_mentions(self, text: str) -> tuple[str, List[str]]:
        """
        Parse Discord @mentions (<@BOT_ID>) and resolve them to persona names.

        Returns:
            (cleaned_text, mentioned_personas)
            - cleaned_text: Message with <@ID> replaced by the persona's name
              so the model can see who was addressed ("elvira", "frank", etc.).
            - mentioned_personas: De-duplicated list (in mention order) of every
              persona whose bot was pinged. Empty if none were. The caller uses
              this to force-route the response to exactly those personas.
        """
        # Reverse map: bot user ID → persona name (cached, reconnect-safe)
        id_to_persona = self._persona_id_map()

        if not id_to_persona:
            return text, []

        mentioned_personas: List[str] = []

        def replace_mention(match):
            uid = int(match.group(1))
            persona = id_to_persona.get(uid)
            if persona:
                mentioned_personas.append(persona)
                return persona  # Replace <@123> with "elvira"
            return match.group(0)  # Leave non-persona mentions unchanged

        # Discord mention formats: <@ID> or <@!ID> (nickname mention)
        cleaned = re.sub(r"<@!?(\d+)>", replace_mention, text)

        # De-duplicate while preserving mention order
        seen: Set[str] = set()
        ordered = [p for p in mentioned_personas if not (p in seen or seen.add(p))]

        return cleaned, ordered

    def _all_mentioned_personas(
        self, message: discord.Message, inline_personas: List[str]
    ) -> List[str]:
        """All personas addressed by a message — inline pings, reply-pings, or
        a reply to a persona's own message.

        `inline_personas` are the <@ID> tokens already pulled from the text.
        On top of those:
        - `message.mentions` is discord.py's parsed mention list, which also
          includes the author of a replied-to message when the reply has its
          ping toggle on — the case a content-only regex scan misses.
        - A reply to one of the bot's own messages addresses that persona even
          if the ping toggle was turned off (uses the cached/resolved reference,
          no API fetch).

        Returns a de-duplicated list in discovery order.
        """
        id_map = self._persona_id_map()
        found = list(inline_personas)

        for user in message.mentions:
            persona = id_map.get(user.id)
            if persona:
                found.append(persona)

        ref = message.reference
        resolved = getattr(ref, "resolved", None) if ref else None
        ref_author = getattr(resolved, "author", None)
        if ref_author is not None:
            persona = id_map.get(ref_author.id)
            if persona:
                found.append(persona)

        seen: Set[str] = set()
        return [p for p in found if not (p in seen or seen.add(p))]

    async def _reply_context(self, message: discord.Message) -> Optional[str]:
        """If the message is a Discord reply, return a compact quote of the
        replied-to message for the model: [replying to name: "snippet"].

        Uses the reference Discord attaches to the incoming message
        (`reference.resolved`), falling back to one API fetch for uncached
        messages. Returns None for non-replies, deleted targets, or fetch
        failures — the reply still processes, just without the anchor.
        """
        ref = message.reference
        if ref is None:
            return None

        resolved = getattr(ref, "resolved", None)
        ref_msg = resolved if isinstance(resolved, discord.Message) else None
        if ref_msg is None and resolved is None and ref.message_id:
            try:
                ref_msg = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                logger.info("[Watcher] Couldn't fetch replied-to message — skipping anchor")
        if ref_msg is None:  # deleted target or failed fetch
            return None

        # Persona messages get their persona name; humans their display name
        persona = self._persona_id_map().get(ref_msg.author.id)
        name = persona.capitalize() if persona else ref_msg.author.display_name

        snippet = " ".join(ref_msg.content.split())
        if not snippet:
            return None
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        return f'[replying to {name}: "{snippet}"]'

    def _girls_role_mentioned(self, message: discord.Message) -> bool:
        """True if the message pings the shared house role (@Girls)."""
        return any(
            role.name.lower() == self._girls_role_name
            for role in message.role_mentions
        )

    def _strip_role_mention(self, text: str, message: discord.Message) -> str:
        """Replace the @Girls role mention (<@&ID>) with a readable token."""
        for role in message.role_mentions:
            if role.name.lower() == self._girls_role_name:
                text = text.replace(f"<@&{role.id}>", "the House")
        return text

    # ── Utilities ────────────────────────────────────────────────

    @property
    def active_channels(self) -> list:
        """List of channels with active conversation buffers."""
        return list(self._buffers.keys())

    def __repr__(self) -> str:
        status = "connected" if self.is_ready() else "disconnected"
        return (
            f"Watcher({status}, "
            f"personas={len(self._persona_clients)}, "
            f"watching={len(self._watched_channel_ids)}, "
            f"buffers={len(self._buffers)})"
        )
