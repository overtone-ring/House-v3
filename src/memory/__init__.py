"""
Memory Subsystem
================

SQLite-backed memory with sqlite-vec vector search and FTS5 full-text search.

Tables:
    - exchanges: Turn pairs from all personas (with vec0 + FTS5)
    - reflections: Daily summaries per persona (with vec0 + FTS5)
    - relationships: Per-user relationship tracking
    - sessions: Per-session state

Usage:
    from src.memory import get_store, MemoryStore
    from src.memory.models import Exchange, DailyReflection

    store = await get_store(config)
"""

from .models import (
    Exchange,
    DailyReflection,
    UserRelationship,
    SessionState,
)
from .store import MemoryStore, get_store, shutdown_all_stores

__all__ = [
    # Models
    "Exchange",
    "DailyReflection",
    "UserRelationship",
    "SessionState",
    # Store
    "MemoryStore",
    "get_store",
    "shutdown_all_stores",
]
