"""
Context Manager
===============

The "librarian" — orchestrates memory retrieval and context assembly.
Decides whether to search memory (via query inference), retrieves relevant
memories, and assembles the context dict that feeds into prompt building.

v3 simplified: No RAPTOR levels or level weighting. Flat search over
exchanges and reflections with optional persona filtering.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContextManager:
    """
    Orchestrates memory retrieval and context assembly.

    Sits between the orchestrator and the memory/services layer.
    Decides what to retrieve and how to present it.
    """

    MAX_RESULTS = 15
    MAX_RELEVANT_CHARS = 8000

    def __init__(
        self,
        persona_name: str,
        memory_service,            # MemoryService
        query_inference=None,      # QueryInferenceService (optional)
        config: Optional[Dict] = None,
    ):
        self.persona_name = persona_name
        self.memory_service = memory_service
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
        Main entry point: retrieve all context for a user query.

        Returns:
            {
                "memories": [...],           # Retrieved memories (exchanges + reflections)
                "user_context": {...},       # Relationship data (if available)
                "search_query": str,         # The query used for memory search
                "search_skipped": bool,      # Whether memory search was skipped
            }
        """
        result = {
            "memories": [],
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
                    logger.debug(f"Query inference: skipping memory search for '{query[:50]}...'")
                else:
                    search_query = decision.get("search_query", query)
                    result["search_query"] = search_query
            except Exception as e:
                logger.warning(f"Query inference failed, using raw query: {e}")

        # Memory search
        if not result["search_skipped"]:
            try:
                # Search with persona filter — persona sees own exchanges,
                # plus reflections are already persona-scoped
                memories = await self.memory_service.search_memory(
                    search_query,
                    top_k=self.MAX_RESULTS,
                    persona_filter=self.persona_name,
                    include_reflections=True,
                )
                result["memories"] = memories
            except Exception as e:
                logger.warning(f"Memory search failed: {e}")

        # User relationship context
        if user_id:
            try:
                store = self.memory_service._store
                if store:
                    result["user_context"] = await store.get_relationship(user_id)
            except Exception as e:
                logger.warning(f"Relationship lookup failed: {e}")

        return result
