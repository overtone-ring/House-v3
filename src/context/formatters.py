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


def format_affective_primer(
    affective_state: Optional[Dict],
    intensity_threshold: float = DEFAULT_AFFECTIVE_INTENSITY_THRESHOLD,
) -> str:
    """
    Format affective state into a contextual primer string.

    Returns:
        Primer string like "[Current state: contemplative (intensity: 0.6)]"
        or empty string if neutral/low intensity.
    """
    if not affective_state:
        return ""

    state = affective_state.get("state", "neutral")
    intensity = affective_state.get("intensity", 0.0)

    if state == "neutral" or intensity < intensity_threshold:
        return ""

    return f"[Current emotional state: {state} (intensity: {intensity:.1f})]"


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
        parts.append(f"[Speaking with: {display_name} (familiarity: {familiarity})]")

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
