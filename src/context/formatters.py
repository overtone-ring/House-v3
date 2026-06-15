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
        mem_block = _format_memory_block(memories, max_memory_chars)
        if mem_block:
            sections.append("## Relevant Memories\n" + mem_block)

    # User relationship
    if user_context:
        rel_line = format_relational_primer(user_context)
        if rel_line:
            sections.append("## User Context\n" + rel_line)

    return "\n\n".join(sections)


def _format_memory_block(memories: List[Dict], max_chars: int) -> str:
    """Render retrieved memories into prompt text.

    Exchanges that share the same user message on the same day are collapsed
    into one block — the user line printed once, each persona's reply beneath
    it. An @Girls turn stores one row per responding persona (so each can
    recall its own line), which without this would repeat the same user text
    up to 5× in the prompt. Reflections render individually as before.
    """
    blocks: List[Dict] = []
    index_by_key: Dict[tuple, int] = {}

    for mem in memories:
        mem_type = mem.get("type", "exchange")

        if mem_type == "exchange":
            record = mem.get("record") or {}
            user_msg = (record.get("user_msg") or "").strip()
            response = (record.get("assistant_response") or "").strip()
            persona = mem.get("persona_name") or record.get("persona_name") or ""
            date = (mem.get("timestamp") or "")[:10]

            if not user_msg and not response:
                # No structured record to regroup on — keep the prebuilt
                # content as a standalone line rather than dropping it.
                content = (mem.get("content") or "").strip()
                if content:
                    blocks.append({"type": "raw", "date": date, "content": content})
                continue

            key = (user_msg, date)
            if key in index_by_key:
                blocks[index_by_key[key]]["responses"].append((persona, response))
            else:
                index_by_key[key] = len(blocks)
                blocks.append({
                    "type": "exchange",
                    "date": date,
                    "user_msg": user_msg,
                    "responses": [(persona, response)],
                })
        else:
            content = (mem.get("content") or "").strip()
            if not content:
                continue
            blocks.append({
                "type": "reflection",
                "date": (mem.get("timestamp") or "")[:10],
                "persona": mem.get("persona_name") or "",
                "content": content,
            })

    out: List[str] = []
    char_count = 0
    for block in blocks:
        text = _render_memory_block(block)
        if not text:
            continue
        if char_count + len(text) > max_chars:
            break
        out.append(text)
        char_count += len(text)
    return "\n\n".join(out)


def _render_memory_block(block: Dict) -> str:
    """Render one memory block (grouped exchange, reflection, or raw)."""
    date = block.get("date", "")
    btype = block["type"]

    if btype == "exchange":
        tag = f" ({date})" if date else ""
        lines = [f"{MEMORY_LABELS['exchange']}{tag}", f"User: {block['user_msg']}"]
        for persona, response in block["responses"]:
            if not response:
                continue
            lines.append(f"  {persona or 'house'}: {response}")
        return "\n".join(lines)

    if btype == "reflection":
        persona = block.get("persona", "")
        if persona:
            tag = f" ({persona}, {date})" if date else f" ({persona})"
        else:
            tag = f" ({date})" if date else ""
        return f"{MEMORY_LABELS['reflection']}{tag} {block['content']}"

    # raw fallback
    tag = f" ({date})" if date else ""
    return f"{MEMORY_LABELS['exchange']}{tag} {block['content']}"


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
