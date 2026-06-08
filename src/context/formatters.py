"""
Context Formatters
==================

Converts raw memory results, affective state, and relational data
into prompt-ready strings for injection into LLM context.

v3 simplified: No RAPTOR level labels. Exchanges are tagged with
persona attribution and timestamp. Reflections are marked as summaries.
"""

from typing import Any, Dict, List, Optional

# Memory type labels for prompt formatting
MEMORY_LABELS = {
    "exchange": "[Past Conversation]",
    "reflection": "[Daily Summary]",
}

# Default thresholds (can be overridden via config)
DEFAULT_AFFECTIVE_INTENSITY_THRESHOLD = 0.3
DEFAULT_MAX_PREFERRED_TOPICS = 3


def format_memories(
    memories: List[Dict],
    max_chars: int = 8000,
    include_scores: bool = False,
    current_persona: Optional[str] = None,
) -> str:
    """
    Format retrieved memories into a prompt-ready string.

    Adds attribution context so the persona knows whether a memory
    is from their own conversation or one they witnessed.

    Args:
        memories: List of memory dicts with 'type', 'content', 'persona_name', 'timestamp'
        max_chars: Maximum total character budget
        include_scores: Whether to include similarity scores
        current_persona: The persona reading these memories (for attribution)

    Returns:
        Formatted string, or empty string if no memories.
    """
    if not memories:
        return ""

    lines = []
    char_count = 0

    for mem in memories:
        mem_type = mem.get("type", "exchange")
        label = MEMORY_LABELS.get(mem_type, "[Memory]")
        content = mem.get("content", "").strip()
        timestamp = mem.get("timestamp", "")
        mem_persona = mem.get("persona_name", "")

        if not content:
            continue

        # Add attribution context
        date_str = ""
        if timestamp:
            # Extract date from ISO timestamp or YYYY-MM-DD
            date_str = timestamp[:10] if len(timestamp) >= 10 else timestamp

        # Tag whether this is the persona's own memory or observed
        if current_persona and mem_persona and mem_persona != current_persona:
            attribution = f" (from another conversation, {date_str})"
        elif date_str:
            attribution = f" ({date_str})"
        else:
            attribution = ""

        line = f"{label}{attribution} {content}"
        if include_scores and "score" in mem:
            line += f" (relevance: {mem['score']:.2f})"

        # Check budget
        if char_count + len(line) > max_chars:
            break

        lines.append(line)
        char_count += len(line)

    return "\n".join(lines)


def _intensity_to_word(intensity: float) -> str:
    """Convert a 0-1 intensity to a natural language descriptor."""
    if intensity < 0.3:
        return "faintly"
    elif intensity < 0.5:
        return "noticeably"
    elif intensity < 0.7:
        return "strongly"
    elif intensity < 0.9:
        return "deeply"
    else:
        return "overwhelmingly"


def format_affective_primer(
    affective_state: Optional[Dict],
    intensity_threshold: float = DEFAULT_AFFECTIVE_INTENSITY_THRESHOLD,
) -> str:
    """
    Format affective state into a natural language primer string.

    Uses descriptive language instead of numerical values to avoid
    biasing the model toward clinical/analytical output.
    """
    if not affective_state:
        return ""

    state = affective_state.get("state", "neutral")
    intensity = affective_state.get("intensity", 0.0)

    if state == "neutral" or intensity < intensity_threshold:
        return ""

    word = _intensity_to_word(intensity)
    return f"[Currently feeling {word} {state}]"


def format_unified_context(
    memories: List[Dict],
    user_context: Optional[Dict] = None,
    max_memory_chars: int = 6000,
) -> str:
    """
    Format all context for the unified multi-persona prompt.

    Combines retrieved memories (tagged by persona) and user relationship
    context into a single block injected between system prompt and user message.
    """
    sections = []

    # Memories
    if memories:
        mem_lines = []
        char_count = 0
        for mem in memories:
            content = mem.get("content", "").strip()
            if not content:
                continue
            mem_type = mem.get("type", "exchange")
            label = MEMORY_LABELS.get(mem_type, "[Memory]")
            persona = mem.get("persona_name", "")
            timestamp = mem.get("timestamp", "")[:10]
            tag = f" ({persona}, {timestamp})" if persona else ""
            line = f"{label}{tag} {content}"
            if char_count + len(line) > max_memory_chars:
                break
            mem_lines.append(line)
            char_count += len(line)
        if mem_lines:
            sections.append("## Relevant Memories\n" + "\n".join(mem_lines))

    # User relationship
    if user_context:
        rel_line = format_relational_primer(user_context)
        if rel_line:
            sections.append("## User Context\n" + rel_line)

    return "\n\n".join(sections)


def format_relational_primer(
    user_context: Optional[Dict],
    affective_state: Optional[Dict] = None,
    max_topics: int = DEFAULT_MAX_PREFERRED_TOPICS,
) -> str:
    """
    Format user relationship context into a relational primer.

    Returns:
        Primer string with relationship context, or empty string.
    """
    if not user_context:
        return ""

    parts = []

    # Familiarity
    display_name = user_context.get("display_name", "")
    familiarity = user_context.get("relationship_type", "stranger")
    if display_name:
        parts.append(f"[Speaking with: {display_name} — {familiarity}]")

    # Relationship notes
    notes = user_context.get("notes", "")
    if notes:
        parts.append(f"[Notes: {notes}]")

    # Preferred topics
    topics = user_context.get("preferred_topics", [])
    if topics:
        topic_str = ", ".join(topics[:max_topics])
        parts.append(f"[Their interests: {topic_str}]")

    # Affective context
    if affective_state:
        affective_line = format_affective_primer(affective_state)
        if affective_line:
            parts.append(affective_line)

    return "\n".join(parts)
