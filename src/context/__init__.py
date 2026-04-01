"""
Context Subsystem
=================

Handles context retrieval and formatting for prompt injection.
The "librarian" — decides what memories and state to include in prompts.

Components:
    - ContextManager: Orchestrates memory search + state retrieval
    - formatters: Formats memories, affective state, and relational data for prompts
"""

from .manager import ContextManager
from .formatters import format_memories, format_affective_primer, format_relational_primer

__all__ = [
    "ContextManager",
    "format_memories",
    "format_affective_primer",
    "format_relational_primer",
]
