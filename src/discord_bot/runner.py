"""
Discord Bot Runner
==================

Starts all six bots (Watcher + 5 persona clients) on a single asyncio event loop.
Single process, shared HouseOrchestrator, shared TTSService.

Usage:
    python -m src.discord_bot.runner

Or from code:
    from src.discord_bot.runner import run_house
    asyncio.run(run_house())
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from typing import Optional

logger = logging.getLogger(__name__)

# ── Token mapping ────────────────────────────────────────────────────

def _token_key_for(persona_name: str) -> str:
    """Derive the .env variable name for a persona's Discord token."""
    return f"DISCORD_TOKEN_{persona_name.upper()}"
WATCHER_TOKEN_KEY = "DISCORD_TOKEN_WATCHER"


def _load_env(env_path: Optional[str] = None) -> None:
    """Load .env file."""
    from dotenv import load_dotenv

    env_file = Path(env_path) if env_path else Path.cwd() / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)
        print(f"[runner] Loaded .env from {env_file}")
    else:
        print(f"[runner] WARNING: .env not found at {env_file}")


def _load_config() -> dict:
    """Load the app config (default.yaml + local.yaml overrides + env).

    Delegates to utils.config.load_config so config/local.yaml is actually
    merged in — the previous implementation read default.yaml only, silently
    ignoring every local override. Fails fast if missing or incomplete.
    """
    from ..utils.config import load_config

    config = load_config()
    if not config:
        logger.error("No config loaded — is config/default.yaml present?")
        sys.exit(1)

    required = ["personas", "provider"]
    missing = [k for k in required if k not in config]
    if missing:
        logger.error(f"Config missing required keys: {missing}")
        sys.exit(1)

    return config


async def _run_reflection_catchup(config: dict, watcher=None) -> None:
    """Backfill any missed daily reflections on startup.

    Because the bot isn't guaranteed to be running at midnight, reflections
    can't rely on a timer. Instead, on every startup we look for exchanges
    from past dates that were never reflected on, and generate those summaries
    now. Today's exchanges are left alone (the day may still be in progress).

    When there's work to do, the cycle announces itself in watched channels —
    it competes with live messages for the embedding model and CPU, so
    responses are slower until it finishes. This task runs once per process,
    so reconnects never re-announce.
    """
    announced = False
    try:
        from datetime import datetime, timezone
        from ..memory.store import get_store
        from ..services.daily_reflection_service import DailyReflectionService

        store = await get_store(config)
        unreflected = await store.get_unreflected_exchanges()
        if not unreflected:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        past_dates = sorted({
            ex.get("timestamp", "")[:10]
            for ex in unreflected
            if len(ex.get("timestamp", "")) >= 10 and ex.get("timestamp", "")[:10] < today
        })

        if not past_dates:
            logger.info("Reflection catch-up: nothing to backfill (only today's exchanges)")
            return

        service = DailyReflectionService(config)
        await service.initialize()
        logger.info(f"Reflection catch-up: backfilling {len(past_dates)} date(s): {past_dates}")

        if watcher is not None:
            days = f"{len(past_dates)} past day" + ("s" if len(past_dates) != 1 else "")
            announced = await watcher.announce_system(
                f"🪞 The House is reflecting on {days} — "
                f"responses may be slower until it finishes.",
                wait_seconds=120,
            )

        total_written = 0
        for date in past_dates:
            results = await service.run_for_all_personas(date=date)
            written = sum(1 for r in results.values() if r.get("reflection_id"))
            total_written += written
            logger.info(f"Reflection catch-up {date}: {written} reflection(s) written")

        if announced:
            await watcher.announce_system(
                f"🪞 Reflection cycle finished — "
                f"{total_written} reflection(s) written."
            )
    except Exception as e:
        logger.error(f"Reflection catch-up failed: {e}", exc_info=True)
        if announced:
            try:
                await watcher.announce_system(
                    "🪞 Reflection cycle stopped on an error — check the logs."
                )
            except Exception:
                pass


async def run_house(env_path: Optional[str] = None) -> None:
    """
    Main entry point. Initializes all services and starts the bot fleet.

    This function runs forever (until interrupted).
    """
    # ── Load environment and config ──────────────────────────────
    _load_env(env_path)
    config = _load_config()

    # File logging + wire tap (config-driven, so it can't start earlier)
    from ..utils.wire_log import setup_logging
    setup_logging(config)

    # ── Validate tokens ──────────────────────────────────────────
    watcher_token = os.environ.get(WATCHER_TOKEN_KEY)
    if not watcher_token:
        logger.error(f"Missing {WATCHER_TOKEN_KEY} in environment")
        sys.exit(1)

    persona_tokens = {}
    personas = config.get("personas", [])
    if not personas:
        logger.error("No personas configured in config")
        sys.exit(1)
    for persona_name in personas:
        key = _token_key_for(persona_name)
        token = os.environ.get(key)
        if not token:
            logger.error(f"Missing {key} in environment for persona '{persona_name}'")
            sys.exit(1)
        persona_tokens[persona_name] = token

    logger.info(f"Tokens loaded: watcher + {len(persona_tokens)} personas")

    # ── Initialize TTS ───────────────────────────────────────────
    tts_service = None
    tts_config = config.get("tts", {})
    if tts_config.get("provider") == "kokoro":
        try:
            from ..services.tts_service import TTSService
            tts_service = TTSService(config)
            tts_service.initialize()
            logger.info("TTS service initialized (Kokoro)")
        except Exception as e:
            logger.warning(f"TTS initialization failed — TTS disabled: {e}")
            tts_service = None

    # ── Validate embedding model loads ─────────────────────────
    # Fail fast at startup instead of silently failing on first message
    from ..services.embedding_service import EmbeddingService
    model_path = os.path.join(
        config.get("memory", {}).get("data_dir", "./data"),
        "models",
        config.get("memory", {}).get("embedding_model", "nomic-embed-text-v1.5-onnx"),
    )
    embed = await EmbeddingService.get_instance(model_path)
    logger.info(f"Embedding model loaded ({embed.DIMENSIONS}d)")

    # ── Initialize UnifiedOrchestrator ──────────────────────────
    from ..unified_orchestrator import UnifiedOrchestrator
    house = UnifiedOrchestrator(config)
    await house.initialize()
    logger.info("UnifiedOrchestrator initialized")

    # ── Create persona clients ───────────────────────────────────
    from .persona_client import PersonaClient
    persona_clients = {}
    for persona_name in personas:
        client = PersonaClient(
            persona_name=persona_name,
            tts_service=tts_service,
        )
        persona_clients[persona_name] = client

    # ── Create watcher ───────────────────────────────────────────
    from .watcher import Watcher
    watcher = Watcher(
        house_orchestrator=house,
        persona_clients=persona_clients,
        config=config,
    )

    # ── Start all bots concurrently ──────────────────────────────
    logger.info("Starting bot fleet...")

    async def _start_bot(client, token, name):
        """Start a single bot with error handling."""
        try:
            await client.start(token)
        except discord.LoginFailure:
            logger.error(f"[{name}] Invalid token — cannot connect")
        except Exception as e:
            logger.error(f"[{name}] Unexpected error: {e}", exc_info=True)

    tasks = []

    # Start persona bots first
    for persona_name, client in persona_clients.items():
        tasks.append(
            asyncio.create_task(
                _start_bot(client, persona_tokens[persona_name], persona_name)
            )
        )

    # Start watcher last (it needs persona bots to be connectable)
    tasks.append(
        asyncio.create_task(
            _start_bot(watcher, watcher_token, "watcher")
        )
    )

    # Dashboard (in-process web UI — it needs the live watcher state)
    from ..dashboard.server import start_dashboard
    dashboard = await start_dashboard(config, watcher=watcher, house=house)

    # Backfill any missed daily reflections in the background so it doesn't
    # delay the bot fleet coming online. The watcher is passed so the cycle
    # can announce its start/finish in watched channels.
    catchup_task = asyncio.create_task(_run_reflection_catchup(config, watcher))
    catchup_task.add_done_callback(
        lambda t: t.cancelled() or (
            t.exception() and logger.error(f"Reflection catch-up task error: {t.exception()}")
        )
    )

    # Wait for all bots (they run forever until interrupted)
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down bot fleet...")
    finally:
        # Graceful shutdown
        if dashboard:
            await dashboard.stop()
        shutdown_tasks = []
        for name, client in persona_clients.items():
            if not client.is_closed():
                shutdown_tasks.append(client.close())
        if not watcher.is_closed():
            shutdown_tasks.append(watcher.close())

        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        # Drain pending memory writes before the loop tears down
        await house.shutdown()

        # Shutdown TTS
        if tts_service:
            tts_service.shutdown()

        logger.info("Bot fleet shut down")


# ── CLI Entry Point ──────────────────────────────────────────────────

def main():
    """CLI entry point: python -m src.discord_bot.runner"""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Quiet noisy libraries
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    try:
        asyncio.run(run_house())
    except KeyboardInterrupt:
        print("\nShutdown complete.")


if __name__ == "__main__":
    main()
