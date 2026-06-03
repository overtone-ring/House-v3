"""
Response Parser
===============

Parses the unified multi-persona JSON output from the model.

Fallback chain:
    1. Direct json.loads()
    2. Extract JSON from ```json ... ``` blocks
    3. Find first { ... } in text
    4. Treat entire response as default persona (graceful degradation)

Validates:
    - Keys must be valid persona names
    - Values must be str or null
    - At least one non-null response required
"""

import json
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def parse_house_response(
    raw_text: str,
    valid_personas: List[str],
    default_persona: str = "elvira",
) -> Dict[str, Optional[str]]:
    """
    Parse the model's JSON output into a persona -> response dict.

    Args:
        raw_text: Raw model output text
        valid_personas: List of valid persona names
        default_persona: Fallback persona if parsing fails entirely

    Returns:
        Dict mapping persona names to response text (or None for silent).
        At least one persona will have a non-None value.
    """
    raw_text = raw_text.strip()

    if not raw_text:
        logger.warning("Empty response from model, falling back to default persona")
        return _default_response(valid_personas, default_persona,
                                 "[No response generated]")

    # Try parsing JSON through fallback chain
    parsed = _try_parse_json(raw_text)

    if parsed is not None and isinstance(parsed, dict):
        result = _validate_and_clean(parsed, valid_personas)
        if result:
            return result

    # All JSON parsing failed — treat entire text as default persona response
    logger.warning(
        f"Could not parse JSON from model output, assigning to {default_persona}. "
        f"Raw text starts with: {raw_text[:100]!r}"
    )
    return _default_response(valid_personas, default_persona, raw_text)


def _try_parse_json(text: str) -> Optional[dict]:
    """Try multiple strategies to extract JSON from text."""

    # Strategy 1: Direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from markdown code block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: Find first { ... } block (greedy to capture nested content)
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 4: Try to find the outermost braces
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(text[first_brace:last_brace + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    return None


MAX_RESPONSE_CHARS = 2000  # Per-persona hard limit (Discord max is 2000 anyway)


def _detect_repetition(text: str, min_phrase_len: int = 20, max_repeats: int = 3) -> bool:
    """Detect if text contains a repeating phrase loop.

    Bound the start window so the slice is always exactly `min_phrase_len`
    chars — without this, slices near the end of the string shrink and
    eventually become empty, and `text.count("")` returns len(text)+1,
    falsely flagging every response > 60 chars.
    """
    if len(text) < min_phrase_len * max_repeats:
        return False
    last_start = len(text) - min_phrase_len
    for start in range(0, min(200, last_start + 1)):
        phrase = text[start:start + min_phrase_len]
        if text.count(phrase) > max_repeats:
            return True
    return False


def _validate_and_clean(
    parsed: dict,
    valid_personas: List[str],
) -> Optional[Dict[str, Optional[str]]]:
    """
    Validate parsed JSON and clean it into the expected format.

    Returns None if the parsed data is unusable.
    """
    result = {}
    valid_set = set(valid_personas)

    for key, value in parsed.items():
        key_lower = key.lower().strip()
        if key_lower not in valid_set:
            continue

        if value is None:
            result[key_lower] = None
        elif isinstance(value, str):
            cleaned = value.strip()
            # Detect degeneration loops
            if _detect_repetition(cleaned):
                logger.warning(f"Repetition loop detected in {key_lower}'s response, discarding")
                result[key_lower] = None
                continue
            # Truncate runaway responses
            if len(cleaned) > MAX_RESPONSE_CHARS:
                logger.warning(
                    f"{key_lower}'s response too long ({len(cleaned)} chars), "
                    f"truncating to {MAX_RESPONSE_CHARS}"
                )
                # Cut at last sentence boundary within limit
                truncated = cleaned[:MAX_RESPONSE_CHARS]
                last_period = truncated.rfind(".")
                if last_period > MAX_RESPONSE_CHARS // 2:
                    truncated = truncated[:last_period + 1]
                cleaned = truncated
            result[key_lower] = cleaned if cleaned else None
        else:
            # Coerce non-string to string
            result[key_lower] = str(value).strip() or None

    # Ensure all valid personas are represented
    for persona in valid_personas:
        if persona not in result:
            result[persona] = None

    # Check at least one persona speaks
    if not any(v is not None for v in result.values()):
        return None

    return result


def _default_response(
    valid_personas: List[str],
    default_persona: str,
    text: str,
) -> Dict[str, Optional[str]]:
    """Build a fallback response with only the default persona speaking."""
    result = {p: None for p in valid_personas}
    # Guard against degeneration loops in fallback path too
    if _detect_repetition(text):
        logger.warning("Repetition loop in fallback response, replacing with error message")
        result[default_persona] = "[Response got tangled — try again?]"
    elif len(text) > MAX_RESPONSE_CHARS:
        truncated = text[:MAX_RESPONSE_CHARS]
        last_period = truncated.rfind(".")
        if last_period > MAX_RESPONSE_CHARS // 2:
            truncated = truncated[:last_period + 1]
        result[default_persona] = truncated
    else:
        result[default_persona] = text
    return result
