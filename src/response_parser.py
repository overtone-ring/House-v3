"""
Response Parser
===============

Parses the unified multi-persona JSON output from the model into an
ordered list of turns — a "scene". Each turn is {"persona": name,
"text": str}. Personas may take multiple turns (back-and-forth).

Expected model output:
    {"turns": [{"speaker": "frank", "text": "..."}, ...]}

Fallback chain:
    1. Direct json.loads()
    2. Extract JSON from ```json ... ``` blocks
    3. Find first { ... } in text
    4. Legacy format: {persona: text, ...} dict → one turn per persona
       in key order (the model's chosen speaking order)
    5. Non-JSON text: treat entire response as one default-persona turn
       (graceful degradation for "model wrote prose instead of JSON")

Validates:
    - Speakers must be valid persona names
    - Texts must be non-empty strings (repetition loops discarded)
    - Valid JSON that yields no usable turn is treated as silence —
      never posted raw
    - Per-turn length cap is a runaway guard only; the Discord layer
      splits anything over 2000 chars into multiple messages
"""

import json
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Runaway guard per turn — NOT the Discord limit. send_long_response()
# splits long turns into multiple 2000-char Discord messages.
MAX_RESPONSE_CHARS = 6000
# Runaway guard on scene length.
MAX_TURNS = 12


def parse_house_turns(
    raw_text: str,
    valid_personas: List[str],
    default_persona: str = "elvira",
) -> List[Dict[str, str]]:
    """
    Parse the model's JSON output into an ordered list of turns.

    Args:
        raw_text: Raw model output text
        valid_personas: List of valid persona names
        default_persona: Fallback persona if parsing fails entirely

    Returns:
        List of {"persona": name, "text": str} in speaking order.
        Empty list means the House stays silent this turn.
    """
    raw_text = raw_text.strip()

    if not raw_text:
        logger.warning("Empty response from model, falling back to default persona")
        return [{"persona": default_persona, "text": "[No response generated]"}]

    parsed = _try_parse_json(raw_text)

    if parsed is not None:
        turns = _extract_turns(parsed, valid_personas)
        if turns:
            return turns
        # The model produced real JSON, but it validated to nothing usable
        # (no valid speakers, all-empty texts, wrong shape). The plain-text
        # fallback below would post raw JSON braces to Discord — silence is
        # the lesser evil. Log the full output so it can be diagnosed.
        logger.warning(
            "Model returned JSON with no usable turns — treating as silence. "
            f"Raw output: {raw_text[:500]!r}"
        )
        return []

    # All JSON parsing failed — treat entire text as a default-persona turn
    logger.warning(
        f"Could not parse JSON from model output, assigning to {default_persona}. "
        f"Raw text starts with: {raw_text[:100]!r}"
    )
    return _default_turn(default_persona, raw_text)


def _try_parse_json(text: str):
    """Try multiple strategies to extract JSON from text."""

    # Strategy 1: Direct parse (object or bare array)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from markdown code block
    match = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: Try the outermost braces
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace:last_brace + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _detect_repetition(text: str, min_phrase_len: int = 20, max_repeats: int = 3) -> bool:
    """Detect if text contains a repeating phrase loop.

    Bound the start window so the slice is always exactly `min_phrase_len`
    chars — without this, slices near the end of the string shrink and
    eventually become empty, and `text.count("")` returns len(text)+1,
    falsely flagging every response > 60 chars.
    """
    if len(text) < min_phrase_len * max_repeats:
        return False
    # Longer texts get more headroom — at 6000 chars a persona can reuse a
    # phrase legitimately; true degeneration repeats dozens of times.
    max_repeats = max(max_repeats, len(text) // 1500)
    last_start = len(text) - min_phrase_len
    for start in range(0, min(200, last_start + 1)):
        phrase = text[start:start + min_phrase_len]
        if text.count(phrase) > max_repeats:
            return True
    return False


def _truncate(text: str) -> str:
    """Cap runaway text at MAX_RESPONSE_CHARS, preferring a sentence boundary."""
    truncated = text[:MAX_RESPONSE_CHARS]
    last_period = truncated.rfind(".")
    if last_period > MAX_RESPONSE_CHARS // 2:
        truncated = truncated[:last_period + 1]
    return truncated


def _clean_text(persona: str, value) -> Optional[str]:
    """Validate and clean one turn's text. None means the turn is dropped."""
    if not isinstance(value, str):
        if value is not None:
            # Dict/list/number values are malformed output, not usable text —
            # str() would post a Python repr into chat. Drop the turn.
            logger.warning(
                f"Non-string text for {persona} ({type(value).__name__}), discarding"
            )
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if _detect_repetition(cleaned):
        logger.warning(f"Repetition loop detected in {persona}'s turn, discarding")
        return None
    if len(cleaned) > MAX_RESPONSE_CHARS:
        logger.warning(
            f"{persona}'s turn too long ({len(cleaned)} chars), "
            f"truncating to {MAX_RESPONSE_CHARS}"
        )
        cleaned = _truncate(cleaned)
    return cleaned


def _extract_turns(parsed, valid_personas: List[str]) -> List[Dict[str, str]]:
    """
    Pull an ordered turn list out of whatever JSON shape the model produced.

    Accepts:
        {"turns": [{"speaker"/"persona": name, "text": str}, ...]}
        [{"speaker": ..., "text": ...}, ...]          (bare array)
        {persona: text, ...}                          (legacy one-slot format)
    """
    valid_set = set(valid_personas)

    if isinstance(parsed, dict) and isinstance(parsed.get("turns"), list):
        raw_turns = parsed["turns"]
    elif isinstance(parsed, list):
        raw_turns = parsed
    elif isinstance(parsed, dict):
        # Legacy {persona: text} shape — key order is the speaking order
        raw_turns = [
            {"speaker": k, "text": v} for k, v in parsed.items()
        ]
    else:
        return []

    turns: List[Dict[str, str]] = []
    for entry in raw_turns:
        if not isinstance(entry, dict):
            continue
        speaker = entry.get("speaker", entry.get("persona"))
        if not isinstance(speaker, str):
            continue
        speaker = speaker.lower().strip()
        if speaker not in valid_set:
            continue
        text = _clean_text(speaker, entry.get("text"))
        if text is None:
            continue
        turns.append({"persona": speaker, "text": text})
        if len(turns) >= MAX_TURNS:
            logger.warning(
                f"Scene exceeded {MAX_TURNS} turns — dropping the rest"
            )
            break

    return turns


def _default_turn(default_persona: str, text: str) -> List[Dict[str, str]]:
    """Build a fallback single-turn scene for unparseable plain text."""
    # Guard against degeneration loops in fallback path too
    if _detect_repetition(text):
        logger.warning("Repetition loop in fallback response, replacing with error message")
        return [{"persona": default_persona,
                 "text": "[Response got tangled — try again?]"}]
    if len(text) > MAX_RESPONSE_CHARS:
        text = _truncate(text)
    return [{"persona": default_persona, "text": text}]
