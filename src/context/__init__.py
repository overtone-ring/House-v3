"""
Context Subsystem
=================

Handles context retrieval and formatting for prompt injection.
The "librarian" — decides what memories to include in prompts.

Components:
    - UnifiedContextManager: parallel memory retrieval for all personas (the
      live path; see unified_manager.py)
    - ContextManager: legacy per-persona retriever, kept as dead code
    - formatters: format memories and relational data for prompts
"""

from .manager import ContextManager
from .formatters import format_memories, format_affective_primer, format_relational_primer

__all__ = [
    "ContextManager",
    "format_memories",
    "format_affective_primer",
    "format_relational_primer",
]
