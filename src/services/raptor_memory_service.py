"""
Memory Service
==============

Flat RAG memory interface. Primary API for all memory operations.

Handles:
    - Exchange ingestion (embed + store turn pairs)
    - Hybrid search over exchanges (vector + keyword via RRF)
    - Recency-weighted scoring
    - Daily reflection search

This is the primary interface other subsystems use to interact with memory.

Usage:
    memory = MemoryService("elvira", config)
    await memory.initialize()

    # Ingest a turn pair
    await memory.add_exchange(session_id, user_msg, response)

    # Search
    results = await memory.search_memory("philosophy")
"""

import logging
from typing import Any, Dict, List, Optional

from ..memory.store import MemoryStore, get_store
from ..memory.models import Exchange
from .embedding_service import EmbeddingService

logger = logging.getLogger(__name__)


class MemoryService:
    """
    Flat RAG memory interface for a single persona.

    Stores turn pairs as exchanges with embeddings. Search returns
    exchanges ranked by hybrid score (vector similarity + keyword match
    via Reciprocal Rank Fusion).
    """

    def __init__(self, persona_name: str, config: Optional[Dict] = None):
        self.persona_name = persona_name
        self.config = config or {}
        self._store: Optional[MemoryStore] = None
        self._embed: Optional[EmbeddingService] = None

        # Search config
        search_cfg = self.config.get("memory", {}).get("search", {})
        self.default_top_k = search_cfg.get("top_k", 15)

    async def initialize(self) -> None:
        """Initialize store and embedding service."""
        self._store = await get_store(self.config)
        self._embed = await EmbeddingService.get_instance()
        logger.info(f"[{self.persona_name}] MemoryService initialized")

    async def _ensure_store(self) -> None:
        if self._store is None:
            await self.initialize()

    # ── Ingestion ─────────────────────────────────────────────────

    async def add_exchange(
        self,
        session_id: str,
        user_msg: str,
        assistant_response: str,
        participants: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Add a turn pair to memory.

        Each exchange is one user message + one persona's response.
        The combined text is embedded for retrieval. The exchange, its
        embedding, and its FTS entry are written atomically to SQLite.

        Returns:
            The stored exchange dict.
        """
        await self._ensure_store()

        exchange = Exchange(
            session_id=session_id,
            user_msg=user_msg,
            assistant_response=assistant_response,
            persona_name=self.persona_name,
            participants=participants or [self.persona_name],
            metadata=metadata or {},
        )

        # Embed the combined turn pair
        try:
            embedding = await self._embed.embed_document(exchange.content_for_embedding)
            exchange.embedding = embedding
        except Exception as e:
            logger.warning(f"Failed to embed exchange: {e}")

        await self._store.append_exchange(exchange)
        return exchange.to_dict()

    # ── Search ────────────────────────────────────────────────────

    async def search_memory(
        self,
        query: str,
        top_k: Optional[int] = None,
        persona_filter: Optional[str] = None,
        include_reflections: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search memory via hybrid search (vector + keyword).

        Args:
            query: Search query text
            top_k: Max results to return
            persona_filter: If set, only return exchanges from this persona.
                           If None, returns all exchanges (the persona can
                           see conversations it wasn't part of).
            include_reflections: Also search daily reflections

        Returns:
            List of {type, content, score, id, persona_name, timestamp, ...}
            sorted by score descending.
        """
        await self._ensure_store()

        if top_k is None:
            top_k = self.default_top_k

        # Embed query
        query_embedding = await self._embed.embed_query(query)

        results = []
        seen_ids = set()

        # Search exchanges (hybrid: vector + keyword via RRF)
        exchange_results = await self._store.search_exchanges(
            query_embedding=query_embedding,
            query_text=query,
            top_k=top_k,
            persona_filter=persona_filter,
            hybrid=True,
        )

        for record, score in exchange_results:
            record_id = record.get("id", "")
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)

            persona = record.get("persona_name", "unknown")
            user_msg = record.get("user_msg", "")
            response = record.get("assistant_response", "")
            timestamp = record.get("timestamp", "")

            results.append({
                "type": "exchange",
                "content": f"User: {user_msg}\n{persona}: {response}",
                "score": score,
                "id": record_id,
                "persona_name": persona,
                "timestamp": timestamp,
                "record": record,
            })

        # Search reflections (hybrid)
        if include_reflections:
            reflection_results = await self._store.search_reflections(
                query_embedding=query_embedding,
                query_text=query,
                top_k=min(top_k, 5),
                persona_filter=self.persona_name,
                hybrid=True,
            )

            for record, score in reflection_results:
                record_id = record.get("id", "")
                if record_id in seen_ids:
                    continue
                seen_ids.add(record_id)

                results.append({
                    "type": "reflection",
                    "content": record.get("summary", ""),
                    "score": score,
                    "id": record_id,
                    "persona_name": record.get("persona_name", ""),
                    "timestamp": record.get("date", ""),
                    "record": record,
                })

        # Sort by score descending
        results.sort(key=lambda r: r.get("score", 0), reverse=True)

        return results

    # ── Stats ─────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, int]:
        """Get memory statistics."""
        await self._ensure_store()
        return {
            "exchanges": self._store.count("exchanges"),
            "reflections": self._store.count("reflections"),
        }

    async def get_unreflected_count(self) -> int:
        """Count exchanges not yet covered by a daily reflection."""
        await self._ensure_store()
        unreflected = await self._store.get_unreflected_exchanges(self.persona_name)
        return len(unreflected)


# Keep backward-compatible alias during migration
RaptorMemoryService = MemoryService
