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
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import discord
from discord import app_commands

from ..conversation.buffer import ConversationBuffer

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
        comfyui_service=None,
        **kwargs,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        super().__init__(intents=intents, **kwargs)

        self._house = house_orchestrator
        self._persona_clients = persona_clients
        self._config = config or {}
        self._comfyui = comfyui_service

        # Per-channel conversation buffers
        self._buffers: Dict[str, ConversationBuffer] = {}
        self._buffer_max_turns = self._config.get("conversation", {}).get("max_turns", 50)

        # Set of all persona bot user IDs (populated on_ready)
        self._persona_bot_ids: Set[int] = set()

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

        # Persistence file for watched channels
        data_dir = self._config.get("memory", {}).get("data_dir", "./data")
        self._watch_state_path = Path(data_dir) / "discord_watch_state.json"

        # ── Slash command tree ───────────────────────────────────
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    # ── Slash Commands ───────────────────────────────────────────

    def _register_commands(self):
        """Register all slash commands on the command tree."""

        @self.tree.command(
            name="watch",
            description="Start watching a channel — the personas will respond to messages here",
        )
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
        @app_commands.describe(channel="The channel to reset")
        async def reset_buffer(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
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

            await interaction.response.send_message(
                f"Conversation buffer for **#{channel.name}** has been cleared.",
                ephemeral=True,
            )
            logger.info(f"[Watcher] Buffer cleared for #{channel.name} ({channel.id})")

        # ── Image Generation ─────────────────────────────────────

        @self.tree.command(
            name="imagine",
            description="Generate an image using ComfyUI — choose realistic or anime style",
        )
        @app_commands.describe(
            prompt="What to generate",
            style="Image style — realistic photos or anime art",
        )
        @app_commands.choices(
            style=[
                app_commands.Choice(name="Realistic", value="realistic"),
                app_commands.Choice(name="Anime", value="anime"),
            ]
        )
        async def imagine(
            interaction: discord.Interaction,
            prompt: str,
            style: app_commands.Choice[str],
        ):
            if self._comfyui is None:
                await interaction.response.send_message(
                    "Image generation is not configured.",
                    ephemeral=True,
                )
                return

            # Check if ComfyUI is running
            available = await self._comfyui.is_available()
            if not available:
                await interaction.response.send_message(
                    "ComfyUI is not running. Start it first.",
                    ephemeral=True,
                )
                return

            # Defer — generation takes a while
            await interaction.response.defer()

            try:
                image_path = await self._comfyui.generate(
                    prompt=prompt,
                    style=style.value,
                )

                # Send the image
                file = discord.File(str(image_path), filename=f"{style.value}.png")
                await interaction.followup.send(
                    f"**{style.name}** — {prompt}",
                    file=file,
                )

                # Clean up temp file
                try:
                    Path(image_path).unlink(missing_ok=True)
                except OSError:
                    pass

                logger.info(
                    f"[Watcher] Image generated: style={style.value}, "
                    f"prompt={prompt[:60]}"
                )

            except Exception as e:
                logger.error(f"[Watcher] Image generation failed: {e}", exc_info=True)
                await interaction.followup.send(
                    f"Image generation failed: {e}",
                    ephemeral=True,
                )

    # ── Events ───────────────────────────────────────────────────

    async def on_ready(self):
        logger.info(f"[Watcher] Connected as {self.user} (ID: {self.user.id})")

        # Collect persona bot user IDs for filtering
        for name, client in self._persona_clients.items():
            if client.user:
                self._persona_bot_ids.add(client.user.id)
                logger.info(f"[Watcher] Tracking persona bot: {name} → {client.user.id}")

        # Load persisted watch state and prune inaccessible channels
        self._load_watch_state()
        stale = {cid for cid in self._watched_channel_ids if self.get_channel(cid) is None}
        if stale:
            self._watched_channel_ids -= stale
            for cid in stale:
                self._channel_defaults.pop(cid, None)
            self._save_watch_state()
            logger.warning(f"[Watcher] Pruned {len(stale)} inaccessible channels from watch list")

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

        # Only respond in watched channels
        if message.channel.id not in self._watched_channel_ids:
            return

        # ── Get channel lock ─────────────────────────────────────
        async with self._channel_locks.setdefault(message.channel.id, asyncio.Lock()):
            await self._process_message(message)

    async def _process_message(self, message: discord.Message):
        """Process a single user message through the full pipeline."""
        channel_name = message.channel.name
        user_input = message.content
        user_name = message.author.display_name

        # ── Resolve @mentions ────────────────────────────────────
        # Replace <@BOT_ID> with persona names so the model can see them
        cleaned_input, _ = self._resolve_mentions(user_input)

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

        # ── Orchestrate (single unified call) ────────────────────
        try:
            responses = await self._house.process_message(
                user_input=cleaned_input,
                session_id=f"discord_{channel_id}",
                user_id=str(message.author.id),
                channel_name=channel_name,
                conversation_buffer=buffer,
            )
        except Exception as e:
            logger.error(f"[Watcher] Orchestration failed: {e}", exc_info=True)
            try:
                await message.remove_reaction("\U0001f9e0", self.user)
            except discord.HTTPException:
                pass
            return

        # ── Dispatch responses ───────────────────────────────────
        for persona_name, response_text in responses.items():
            if response_text is None or not response_text.strip():
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

            # Show typing indicator from the persona bot while sending
            try:
                async with persona_channel.typing():
                    sent_messages = await client.send_long_response(
                        persona_channel, response_text
                    )
            except discord.HTTPException:
                sent_messages = await client.send_long_response(
                    persona_channel, response_text
                )

            # Record in buffer
            if sent_messages:
                buffer.add_assistant_response(
                    content=response_text,
                    persona=persona_name,
                )

            logger.info(
                f"[Watcher] #{channel_name} | {persona_name} responded "
                f"({len(response_text)} chars)"
            )

        # ── Signal: done ─────────────────────────────────────────
        try:
            await message.remove_reaction("\U0001f9e0", self.user)
            await message.add_reaction("\u2705")  # ✅
        except discord.HTTPException:
            pass

        # ── Persist buffer ───────────────────────────────────────
        if buffer.is_dirty:
            try:
                data_dir = self._config.get("memory", {}).get("data_dir", "./data")
                buffer.save(
                    ConversationBuffer.session_file_path(
                        f"discord_{channel_id}", data_dir
                    )
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

    # ── Watch State Persistence ──────────────────────────────────

    def _save_watch_state(self) -> None:
        """Persist watched channel IDs and defaults to disk."""
        state = {
            "watched_channel_ids": list(self._watched_channel_ids),
            "channel_defaults": {
                str(k): v for k, v in self._channel_defaults.items()
            },
        }
        self._watch_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._watch_state_path, "w") as f:
            json.dump(state, f, indent=2)

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

    def update_persona_bot_ids(self) -> None:
        """Refresh persona bot IDs (call after all bots are connected)."""
        for name, client in self._persona_clients.items():
            if client.user:
                self._persona_bot_ids.add(client.user.id)

    def _resolve_mentions(self, text: str) -> tuple[str, Optional[str]]:
        """
        Parse Discord @mentions (<@BOT_ID>) and resolve them to persona names.

        Returns:
            (cleaned_text, forced_persona)
            - cleaned_text: Message with <@ID> replaced by the persona's name
              so the arbitrator's regex rules can match "elvira", "frank", etc.
            - forced_persona: If exactly ONE persona was @mentioned, return their
              name so the watcher can bypass the arbitrator. If multiple were
              mentioned, return None (let the arbitrator handle it with the
              cleaned text).
        """
        # Build reverse map: bot user ID → persona name
        id_to_persona: Dict[int, str] = {}
        for name, client in self._persona_clients.items():
            if client.user:
                id_to_persona[client.user.id] = name

        if not id_to_persona:
            return text, None

        mentioned_personas = []

        def replace_mention(match):
            uid = int(match.group(1))
            persona = id_to_persona.get(uid)
            if persona:
                mentioned_personas.append(persona)
                return persona  # Replace <@123> with "elvira"
            return match.group(0)  # Leave non-persona mentions unchanged

        # Discord mention formats: <@ID> or <@!ID> (nickname mention)
        cleaned = re.sub(r"<@!?(\d+)>", replace_mention, text)

        # If exactly one persona was mentioned, force-route to them
        forced = None
        if len(mentioned_personas) == 1:
            forced = mentioned_personas[0]
        # If multiple were mentioned, the cleaned text now contains their names
        # so the arbitrator's Tier 1 regex will pick them up

        return cleaned, forced

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
