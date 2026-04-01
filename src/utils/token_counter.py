"""
Token Counter
=============

Token counting utilities for prompt budget management.
Uses tiktoken when available, falls back to chars/4 heuristic.
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Try to load tiktoken for precise counting
_tokenizer = None
try:
    import tiktoken
    _tokenizer = tiktoken.get_encoding("cl100k_base")
    logger.debug("Using tiktoken for token counting")
except ImportError:
    logger.debug("tiktoken not available; using chars/4 heuristic")


def count_tokens(text: str) -> int:
    """
    Count tokens in a text string.

    Uses tiktoken (cl100k_base) if available, otherwise chars/4.
    """
    if not text:
        return 0
    if _tokenizer:
        return len(_tokenizer.encode(text))
    return len(text) // 4


def check_prompt_budget(
    static_prompt: str,
    max_static_tokens: int = 12000,
    warn_threshold: int = 10000,
) -> Tuple[int, Optional[str]]:
    """
    Check if a static prompt is within budget.

    Args:
        static_prompt: The static (persona) prompt to check
        max_static_tokens: Hard limit
        warn_threshold: Soft limit that triggers a warning

    Returns:
        Tuple of (token_count, warning_message_or_None)
    """
    tokens = count_tokens(static_prompt)

    if tokens > max_static_tokens:
        return tokens, (
            f"Static prompt ({tokens} tokens) exceeds budget ({max_static_tokens}). "
            f"This may cause context window issues."
        )

    if tokens > warn_threshold:
        return tokens, (
            f"Static prompt is {tokens} tokens (warn threshold: {warn_threshold}). "
            f"Consider trimming to leave room for context."
        )

    return tokens, None
