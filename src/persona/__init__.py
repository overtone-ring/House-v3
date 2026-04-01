"""
Persona Subsystem
=================

Handles persona identity: prompt loading, multi-section assembly,
memory instruction templates, and evolution tracking.

Usage:
    from src.persona import PersonaAssembler

    assembler = PersonaAssembler(config)
    prompt = assembler.get_static_prompt("elvira")
    sections = assembler.get_sections_for_caching("elvira")
"""

from .assembler import PersonaAssembler
from .loader import load_persona_prompt, clear_persona_cache
from .memory_instructions import get_memory_instructions

__all__ = [
    "PersonaAssembler",
    "load_persona_prompt",
    "clear_persona_cache",
    "get_memory_instructions",
]
