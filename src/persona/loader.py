"""
Persona File Loader
===================

Loads persona prompt files from disk with multi-path search and caching.
This is the lower-level loader; PersonaAssembler is the primary interface.

Search order for persona "elvira":
    1. Path specified in config (personas.elvira.prompt_file)
    2. data/personas/voice/elvira.txt
    3. data/personas/voice/elvira.md
"""

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_persona_prompts: Dict[str, str] = {}


def load_persona_prompt(
    persona_name: str,
    config: Optional[dict] = None,
    cache: bool = True,
) -> str:
    """
    Load a persona's voice/personality prompt from file.

    Args:
        persona_name: Name of the persona (e.g. "elvira")
        config: Optional config dict with persona file paths
        cache: Whether to cache the loaded prompt

    Returns:
        The persona prompt text, or a minimal default if not found.
    """
    if cache and persona_name in _persona_prompts:
        return _persona_prompts[persona_name]

    prompt = None

    # Try config-specified path first
    if config:
        personas_config = config.get("personas", {})
        persona_config = personas_config.get(persona_name, {})
        config_path = persona_config.get("prompt_file")
        if config_path:
            path = Path(config_path)
            if path.exists():
                prompt = path.read_text(encoding="utf-8").strip()
                logger.debug(f"Loaded {persona_name} prompt from config path: {path}")

    # Search standard locations
    if prompt is None:
        from ..utils.paths import get_project_root
        project_root = get_project_root()
        search_paths = [
            project_root / "data" / "personas" / "voice" / f"{persona_name}.txt",
            project_root / "data" / "personas" / "voice" / f"{persona_name}.md",
        ]

        for path in search_paths:
            if path.exists():
                prompt = path.read_text(encoding="utf-8").strip()
                logger.debug(f"Loaded {persona_name} prompt from: {path}")
                break

    # Fallback
    if prompt is None:
        prompt = f"You are {persona_name.capitalize()}, a helpful assistant."
        logger.warning(f"No prompt file found for {persona_name}; using default")

    if cache:
        _persona_prompts[persona_name] = prompt

    return prompt


def clear_persona_cache() -> None:
    """Clear the persona prompt cache."""
    _persona_prompts.clear()


def get_cached_personas() -> Dict[str, str]:
    """Return a copy of the current cache."""
    return dict(_persona_prompts)
