"""
Persona Assembler
=================

Composes persona system prompts from consolidated persona files.

v3 simplified — each persona is a single file:
    data/personas/{name}.md

Prompt structure sent to the LLM (in order):
    1. Persona identity + voice + values  (from {name}.md)
    2. Memory instructions                (from memory_instructions.py)

The contextual primer (affective state, relational context) is appended
by the provider AFTER the persona prompt, not before — so the model
reads "who I am" before "what's happening right now."

Legacy support: if {name}.md doesn't exist, falls back to the old
soul/agent/voice triple (for gradual migration).
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from .memory_instructions import get_memory_instructions

logger = logging.getLogger(__name__)

SECTION_SEPARATOR = "\n\n---\n\n"
CHARS_PER_TOKEN = 4


class PersonaAssembler:
    """
    Assembles persona system prompts with caching.

    Usage:
        assembler = PersonaAssembler(config)
        prompt = assembler.get_static_prompt("elvira")
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        from ..utils.paths import get_project_root
        self.project_root = get_project_root()
        self.persona_dir = self.project_root / "data" / "personas"

        # Legacy paths (fallback)
        self.soul_dir = self.persona_dir / "soul"
        self.agent_dir = self.persona_dir / "agent"
        self.voice_dir = self.persona_dir / "voice"

        self._cache: Dict[str, Dict] = {}

    def assemble(self, persona_name: str) -> Dict:
        """
        Assemble the system prompt for a persona.

        Returns:
            {
                "static_prompt": str,       # Full persona prompt
                "sections": [               # For introspection/debugging
                    {"name": "persona", "content": "..."},
                    {"name": "memory", "content": "..."},
                ],
                "has_soul_prot": bool,
                "token_estimate": int,
            }
        """
        if persona_name in self._cache:
            return self._cache[persona_name]

        sections = []

        # 1. Persona definition (consolidated file takes priority)
        persona_content = self._load_consolidated(persona_name)
        if persona_content:
            sections.append({"name": "persona", "content": persona_content})
        else:
            # Fallback: legacy soul + agent + voice files
            legacy = self._load_legacy(persona_name)
            if legacy:
                sections.append({"name": "persona", "content": legacy})

        # 2. Memory instructions
        memory = get_memory_instructions(persona_name)
        if memory:
            sections.append({"name": "memory", "content": memory})

        static_prompt = SECTION_SEPARATOR.join(s["content"] for s in sections)
        token_estimate = len(static_prompt) // CHARS_PER_TOKEN

        result = {
            "static_prompt": static_prompt,
            "sections": sections,
            "has_soul_prot": bool(sections),
            "token_estimate": token_estimate,
        }

        self._cache[persona_name] = result
        logger.info(
            f"[{persona_name}] Assembled {len(sections)} sections, "
            f"~{token_estimate} tokens"
        )
        return result

    def get_static_prompt(self, persona_name: str) -> str:
        """Get the full prompt string."""
        return self.assemble(persona_name)["static_prompt"]

    def get_section(self, persona_name: str, section_name: str) -> Optional[str]:
        """Get a specific section's content."""
        data = self.assemble(persona_name)
        for section in data["sections"]:
            if section["name"] == section_name:
                return section["content"]
        return None

    def get_sections_for_caching(self, persona_name: str) -> Dict[str, str]:
        """Get sections as a dict for prompt-caching strategies."""
        data = self.assemble(persona_name)
        return {s["name"]: s["content"] for s in data["sections"]}

    def clear_cache(self, persona_name: Optional[str] = None) -> None:
        """Clear cached assemblies."""
        if persona_name:
            self._cache.pop(persona_name, None)
        else:
            self._cache.clear()

    # ── Loaders ──────────────────────────────────────────────────

    def _load_consolidated(self, persona_name: str) -> Optional[str]:
        """Load the single consolidated persona file."""
        # Try .md then .txt
        for ext in [".md", ".txt"]:
            path = self.persona_dir / f"{persona_name}{ext}"
            content = self._read_file(path)
            if content:
                return content
        return None

    def _load_legacy(self, persona_name: str) -> Optional[str]:
        """Fallback: load old soul + agent + voice triple."""
        parts = []

        soul = self._read_file(self.soul_dir / f"{persona_name}_soul.txt")
        if soul:
            parts.append(soul)

        agent = self._read_file(self.agent_dir / f"{persona_name}_agent.txt")
        if agent:
            parts.append(agent)

        voice = self._read_file(self.voice_dir / f"{persona_name}.txt")
        if voice is None:
            voice = self._read_file(self.voice_dir / f"{persona_name}.md")
        if voice:
            parts.append(voice)

        if parts:
            logger.info(
                f"[{persona_name}] Using legacy soul/agent/voice files "
                f"(consider migrating to {persona_name}.md)"
            )
            return SECTION_SEPARATOR.join(parts)
        return None

    def _read_file(self, path: Path) -> Optional[str]:
        """Read a file, returning None if it doesn't exist."""
        if not path.exists():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
            return content if content else None
        except OSError as e:
            logger.warning(f"Failed to read {path}: {e}")
            return None
