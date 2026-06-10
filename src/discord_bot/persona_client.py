"""
Persona Client
===============

Lightweight Discord client for a single persona.
Responsibilities:
    - Send text responses in channels (dispatched by Watcher)
    - Handle 🔊 reactions on its own messages → TTS synthesis → send audio

No routing logic. No orchestrator awareness. Just a face and a voice.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, Optional

import discord

logger = logging.getLogger(__name__)

# The reaction emoji that triggers TTS
TTS_EMOJI = "🔊"

# Abuse guards for the reaction handler: each 🔊 is a CPU-bound synthesis on
# the host (same machine that runs embeddings) plus an audio upload, and
# react/unreact re-fires the event endlessly without these.
TTS_USER_COOLDOWN_SECONDS = 15.0
TTS_MAX_CHARS = 800


class PersonaClient(discord.Client):
    """
    Discord client for a single persona bot.

    Args:
        persona_name: e.g. "elvira", "frank"
        tts_service: Shared TTSService instance (or None to disable TTS)
    """

    def __init__(
        self,
        persona_name: str,
        tts_service=None,
        **kwargs,
    ):
        # We need message_content intent + reactions intent
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.guilds = True

        super().__init__(intents=intents, **kwargs)

        self.persona_name = persona_name
        self._tts_service = tts_service

        # TTS dedup/throttle state: messages already voiced (or in flight)
        # never re-synthesize; users get a cooldown between requests.
        self._tts_in_flight: set = set()
        self._tts_served: Dict[int, None] = {}  # insertion-ordered for trimming
        self._tts_last_by_user: Dict[int, float] = {}

    # ── Events ───────────────────────────────────────────────────

    async def on_ready(self):
        logger.info(f"[{self.persona_name}] Connected as {self.user} (ID: {self.user.id})")

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle 🔊 reactions on this bot's messages → TTS."""
        # Ignore reactions from bots
        if payload.member and payload.member.bot:
            return

        # Only handle the TTS emoji
        if str(payload.emoji) != TTS_EMOJI:
            return

        # Don't process if TTS is disabled
        if self._tts_service is None or not self._tts_service.is_ready():
            return

        # Dedup: a message is voiced at most once. Removing and re-adding the
        # reaction re-fires this event every time — without this, one user
        # scripting react/unreact pins the CPU and floods the channel.
        message_id = payload.message_id
        if message_id in self._tts_served or message_id in self._tts_in_flight:
            return
        self._tts_in_flight.add(message_id)

        try:
            # Fetch the message to check authorship
            channel = self.get_channel(payload.channel_id)
            if channel is None:
                channel = await self.fetch_channel(payload.channel_id)

            message = await channel.fetch_message(payload.message_id)

            # Only handle reactions on messages THIS bot sent
            # This check works across restarts — no in-memory tracking needed
            if message.author.id != self.user.id:
                return

            text = message.content
            if not text or not text.strip():
                return

            # Per-user cooldown (checked after authorship so reactions on
            # other people's messages don't burn anyone's cooldown)
            now = time.monotonic()
            last = self._tts_last_by_user.get(payload.user_id)
            if last is not None and (now - last) < TTS_USER_COOLDOWN_SECONDS:
                logger.info(
                    f"[{self.persona_name}] TTS throttled for user {payload.user_id}"
                )
                return
            self._tts_last_by_user[payload.user_id] = now

            if len(text) > TTS_MAX_CHARS:
                logger.info(
                    f"[{self.persona_name}] TTS text capped "
                    f"({len(text)} → {TTS_MAX_CHARS} chars)"
                )
                text = text[:TTS_MAX_CHARS]

            logger.info(
                f"[{self.persona_name}] TTS requested for message {payload.message_id} "
                f"({len(text)} chars)"
            )

            # Synthesize audio
            audio_path = await asyncio.to_thread(
                self._tts_service.synthesize,
                self.persona_name,
                text,
            )

            # Send audio as attachment
            audio_file = discord.File(
                str(audio_path),
                filename=f"{self.persona_name}_tts.wav",
            )
            await channel.send(file=audio_file)

            # Clean up temp file
            try:
                Path(audio_path).unlink(missing_ok=True)
            except OSError as e:
                logger.debug(f"Failed to clean up TTS temp file {audio_path}: {e}")

            self._tts_served[message_id] = None
            if len(self._tts_served) > 1000:
                for mid in list(self._tts_served)[:500]:
                    del self._tts_served[mid]

            logger.info(f"[{self.persona_name}] TTS audio sent for message {payload.message_id}")

        except Exception as e:
            logger.error(f"[{self.persona_name}] TTS failed: {e}", exc_info=True)
        finally:
            self._tts_in_flight.discard(message_id)

    # ── Sending ──────────────────────────────────────────────────

    async def send_response(
        self,
        channel: discord.TextChannel,
        text: str,
    ) -> Optional[discord.Message]:
        """
        Send a text response in a channel.

        Called by the Watcher after arbitration decides this persona should respond.

        Returns:
            The sent Message object, or None on failure.
        """
        try:
            message = await channel.send(text)
            logger.debug(f"[{self.persona_name}] Sent message {message.id} in #{channel.name}")
            return message

        except discord.HTTPException as e:
            logger.error(f"[{self.persona_name}] Failed to send in #{channel.name}: {e}")
            return None

    async def send_long_response(
        self,
        channel: discord.TextChannel,
        text: str,
        max_length: int = 2000,
    ) -> list:
        """
        Send a response that may exceed Discord's 2000 char limit.
        Splits on sentence boundaries when possible.

        Returns:
            List of sent Message objects.
        """
        if len(text) <= max_length:
            msg = await self.send_response(channel, text)
            return [msg] if msg else []

        messages = []
        chunks = self._split_text(text, max_length)

        for chunk in chunks:
            msg = await self.send_response(channel, chunk)
            if msg:
                messages.append(msg)
            # Small delay between chunks to maintain order
            await asyncio.sleep(0.3)

        return messages

    # ── Internals ────────────────────────────────────────────────

    @staticmethod
    def _split_text(text: str, max_length: int) -> list:
        """Split text into chunks, preferring sentence boundaries."""
        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            # Try to split at a sentence boundary
            split_at = max_length
            for sep in [". ", "! ", "? ", "\n", ", ", " "]:
                idx = remaining.rfind(sep, 0, max_length)
                if idx > max_length // 2:  # Don't split too early
                    split_at = idx + len(sep)
                    break

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        return chunks

    def __repr__(self) -> str:
        status = "connected" if self.is_ready() else "disconnected"
        return f"PersonaClient({self.persona_name}, {status})"
