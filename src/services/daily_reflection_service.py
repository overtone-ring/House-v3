"""
Daily Reflection Service
========================

Generates per-persona daily summaries of conversations.

Designed to run once per day (midnight trigger). For each persona:
    1. Find all unreflected exchanges for that persona
    2. Group by date
    3. Generate a narrative summary via LLM
    4. Embed and store as a DailyReflection
    5. Mark source exchanges as reflected

The reflection is written from the persona's perspective, preserving
timeline and attribution. This is the ONLY abstraction layer — there
are no hierarchical clusters or trees.

Usage:
    service = DailyReflectionService(config)
    await service.initialize()
    results = await service.run_for_all_personas()
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..memory.store import get_store
from ..memory.models import DailyReflection
from .embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

DEFAULT_REFLECTION_PROMPT = (
    "You are {persona_name}. Review today's conversations and write a brief "
    "personal reflection summarizing what happened, how you felt about it, "
    "and anything noteworthy about the user's state or your interactions.\n\n"
    "Write naturally, in first person, as a diary entry. Keep it concise "
    "(2-4 paragraphs). Preserve the timeline — mention what came first vs later.\n\n"
    "Today's conversations ({date}):\n"
    "---\n"
    "{exchanges}\n"
    "---\n\n"
    "Your reflection:"
)


class DailyReflectionService:
    """
    Generates daily per-persona summaries of conversations.

    Each persona gets one reflection per day covering all their exchanges
    from that date. The reflection is embedded for vector search.
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._embed: Optional[EmbeddingService] = None
        self._reflection_provider = None  # Lazy-loaded

        # Config
        self.prompt_template = self.config.get(
            "reflection_prompt", DEFAULT_REFLECTION_PROMPT
        )
        self.max_exchanges_per_reflection = self.config.get(
            "max_exchanges_per_reflection", 50
        )

    async def initialize(self) -> None:
        """Initialize embedding service."""
        self._embed = await EmbeddingService.get_instance()

    def _get_reflection_provider(self):
        """Lazily create the reflection model provider."""
        if self._reflection_provider is None:
            from ..providers import create_tiered_provider
            self._reflection_provider = create_tiered_provider(
                self.config, ["reflection_provider"]
            )
        return self._reflection_provider

    async def reflect_for_persona(
        self,
        persona_name: str,
        date: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate daily reflection for a single persona.

        Args:
            persona_name: Which persona to reflect as
            date: Date to reflect on (YYYY-MM-DD). Defaults to today.
            dry_run: If True, return what would be generated without storing

        Returns:
            {
                "persona": str,
                "date": str,
                "exchange_count": int,
                "reflection_id": str or None,
                "summary": str or None,
            }
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        store = await get_store(self.config)

        # Find unreflected exchanges for this persona on this date
        unreflected = await store.get_unreflected_exchanges(
            persona_name=persona_name,
            date=date,
        )

        if not unreflected:
            return {
                "persona": persona_name,
                "date": date,
                "exchange_count": 0,
                "reflection_id": None,
                "summary": None,
                "reason": "No unreflected exchanges",
            }

        # Cap to avoid massive prompts
        exchanges = unreflected[:self.max_exchanges_per_reflection]

        # Format exchanges for the prompt
        exchange_text = self._format_exchanges(exchanges)

        if dry_run:
            return {
                "persona": persona_name,
                "date": date,
                "exchange_count": len(exchanges),
                "reflection_id": None,
                "summary": None,
                "dry_run": True,
            }

        # Generate reflection via LLM
        prompt = self.prompt_template.format(
            persona_name=persona_name,
            date=date,
            exchanges=exchange_text,
        )

        try:
            provider = self._get_reflection_provider()
            result = provider.generate(prompt, temperature=0.3, max_tokens=1000)
            summary = result.text.strip()
        except Exception as e:
            logger.error(f"Failed to generate reflection for {persona_name}: {e}")
            return {
                "persona": persona_name,
                "date": date,
                "exchange_count": len(exchanges),
                "reflection_id": None,
                "summary": None,
                "error": str(e),
            }

        if not summary:
            return {
                "persona": persona_name,
                "date": date,
                "exchange_count": len(exchanges),
                "reflection_id": None,
                "summary": None,
                "reason": "Empty reflection generated",
            }

        # Embed the reflection
        try:
            embedding = await self._embed.embed_document(summary)
        except Exception as e:
            logger.warning(f"Failed to embed reflection: {e}")
            embedding = None

        # Store the reflection (atomic: text + vector + FTS in one transaction)
        exchange_ids = [ex.get("id", "") for ex in exchanges]

        reflection = DailyReflection(
            persona_name=persona_name,
            date=date,
            summary=summary,
            exchange_count=len(exchanges),
            exchange_ids=exchange_ids,
            embedding=embedding,
        )

        await store.append_reflection(reflection)

        # Mark exchanges as reflected
        for ex_id in exchange_ids:
            await store.update_exchange(ex_id, {"reflected": True})

        logger.info(
            f"[{persona_name}] Daily reflection for {date}: "
            f"{len(exchanges)} exchanges summarized"
        )

        return {
            "persona": persona_name,
            "date": date,
            "exchange_count": len(exchanges),
            "reflection_id": reflection.id,
            "summary": summary,
        }

    async def run_for_all_personas(
        self,
        personas: Optional[List[str]] = None,
        date: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Run daily reflection for all personas.

        Args:
            personas: List of persona names (defaults to config list)
            date: Date to reflect on (defaults to today)
            dry_run: Preview mode

        Returns:
            {persona_name: result_dict, ...}
        """
        if personas is None:
            personas = self.config.get("personas", [])

        results = {}
        for persona in personas:
            try:
                results[persona] = await self.reflect_for_persona(
                    persona, date=date, dry_run=dry_run
                )
            except Exception as e:
                logger.error(f"Reflection failed for {persona}: {e}")
                results[persona] = {"error": str(e)}

        return results

    def _format_exchanges(self, exchanges: List[dict]) -> str:
        """Format exchanges into readable text for the reflection prompt."""
        lines = []
        for ex in exchanges:
            timestamp = ex.get("timestamp", "")
            # Extract just the time portion if it's a full ISO timestamp
            if "T" in timestamp:
                time_part = timestamp.split("T")[1][:5]  # HH:MM
            else:
                time_part = timestamp

            user_msg = ex.get("user_msg", "")
            response = ex.get("assistant_response", "")
            persona = ex.get("persona_name", "")

            lines.append(f"[{time_part}] User: {user_msg}")
            lines.append(f"[{time_part}] {persona}: {response}")
            lines.append("")

        return "\n".join(lines)
