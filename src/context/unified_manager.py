"""
Unified Context Manager
=======================

Retrieves and assembles context for the unified multi-persona call.

Unlike the per-persona ContextManager, this retrieves memories for ALL
personas in parallel and aggregates affective state across the ensemble.
The result feeds into a single unified LLM prompt.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .formatters import format_relational_primer

logger = logging.getLogger(__name__)


class UnifiedContextManager:
    """
    Retrieves context for all personas in a single pass.

    Searches each persona's memory in parallel, collects affective
    states, and assembles everything into one context dict.
    """

    MAX_RESULTS_PER_PERSONA = 5
    MAX_TOTAL_RESULTS = 15

    def __init__(
        self,
        memory_services: Dict[str, Any],  # persona_name -> MemoryService
        state_manager: Any,               # StateManager
        query_inference: Any = None,      # QueryInferenceService (optional)
        config: Optional[Dict] = None,
    ):
        self.memory_services = memory_services
        self.state_manager = state_manager
        self.query_inference = query_inference
        self.config = config or {}

    async def retrieve_context(
        self,
        query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        recent_context: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve unified context for the multi-persona prompt.

        Returns:
            {
                "memories": [...],           # Combined memories from all personas
                "affective_states": {...},   # Per-persona affective state
                "user_context": {...},       # Relationship data
                "search_query": str,
                "search_skipped": bool,
            }
        """
        result = {
            "memories": [],
            "affective_states": {},
            "user_context": None,
            "search_query": query,
            "search_skipped": False,
        }

        # Query inference: should we search memory?
        search_query = query
        if self.query_inference:
            try:
                decision = await self.query_inference.decide_and_generate_query(
                    query, recent_context
                )
                if decision.get("should_search") is False:
                    result["search_skipped"] = True
                    logger.debug(f"Query inference: skipping memory search")
                else:
                    search_query = decision.get("search_query", query)
                    result["search_query"] = search_query
            except Exception as e:
                logger.warning(f"Query inference failed, using raw query: {e}")

        # Memory search across all personas in parallel
        if not result["search_skipped"]:
            try:
                memories = await self._search_all_personas(search_query)
                result["memories"] = memories
            except Exception as e:
                logger.warning(f"Memory search failed: {e}")

        # Affective states for all personas
        for persona_name in self.memory_services:
            try:
                state = self.state_manager.load_affective_state(persona_name)
                result["affective_states"][persona_name] = state
            except Exception as e:
                logger.debug(f"Affective state load failed for {persona_name}: {e}")

        # User relationship
        if user_id:
            try:
                # Use any memory service's store (they all share the same one)
                store = next(iter(self.memory_services.values()))._store
                if store:
                    result["user_context"] = await store.get_relationship(user_id)
            except Exception as e:
                logger.warning(f"Relationship lookup failed: {e}")

        return result

    async def _search_all_personas(self, query: str) -> List[Dict]:
        """Search memory for all personas in parallel, merge and deduplicate."""
        tasks = []
        for persona_name, memory_service in self.memory_services.items():
            tasks.append(self._search_one_persona(persona_name, memory_service, query))

        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge, deduplicate, and sort by score
        seen_ids = set()
        merged = []
        for persona_results in all_results:
            if isinstance(persona_results, Exception):
                logger.warning(f"Memory search failed for a persona: {persona_results}")
                continue
            for mem in persona_results:
                mem_id = mem.get("id", "")
                if mem_id and mem_id in seen_ids:
                    continue
                seen_ids.add(mem_id)
                merged.append(mem)

        # Sort by score descending, take top results
        merged.sort(key=lambda m: m.get("score", 0), reverse=True)
        return merged[:self.MAX_TOTAL_RESULTS]

    async def _search_one_persona(
        self, persona_name: str, memory_service: Any, query: str,
    ) -> List[Dict]:
        """Search memory for a single persona."""
        return await memory_service.search_memory(
            query,
            top_k=self.MAX_RESULTS_PER_PERSONA,
            persona_filter=persona_name,
            include_reflections=True,
        )
