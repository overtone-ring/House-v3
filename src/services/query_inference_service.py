"""
Query Inference Service
=======================

Decides whether a user message requires a memory search.
Uses a lightweight LLM call (or regex pre-filter) to avoid
unnecessary memory searches for greetings, reactions, etc.

Can be disabled via config (query_inference.enabled: false).

Usage:
    service = await create_query_inference_service(config)
    if service:
        decision = await service.decide_and_generate_query("What did we talk about?")
        # {"should_search": True, "search_query": "past discussions topics"}
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Pre-filter Patterns ───────────────────────────────────────────────
# Messages matching these never trigger a search (pure reactions, greetings)

NO_SEARCH_PATTERNS = [
    r"^(hey|hi|hello|sup|yo|hola|heya|what's up|whats up)\b",
    r"^(ok|okay|sure|yep|yeah|yea|yes|no|nah|nope|k|kk)\b",
    r"^(lol|lmao|haha|hah|ha|rofl|xd|omg|bruh)\b",
    r"^(thanks|thx|ty|thank you|appreciate it)\b",
    r"^(good morning|good night|gn|gm|morning|night)\b",
    r"^(bye|goodbye|see ya|later|cya|ttyl)\b",
    r"^(wow|whoa|damn|dang|nice|cool|awesome|great|sweet)\b",
    r"^[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\u2600-\u26FF\u2700-\u27BF\s]+$",  # Emoji-only
]

NO_SEARCH_REGEX = [re.compile(p, re.IGNORECASE) for p in NO_SEARCH_PATTERNS]

# ── System Prompt ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a memory search decision system.

Your job is to analyze user messages and decide if searching long-term memories is needed.

Guidelines:
- Simple responses (greetings, acknowledgments, "lol", "ok"): NO SEARCH
- Continuation of current topic without reference to past: NO SEARCH
- Direct questions about past conversations: YES, SEARCH
- Requests involving "remember", "we talked about", "you said": YES, SEARCH
- Questions that would benefit from context about preferences/history: YES, SEARCH

If search is needed, generate a semantic search query that captures what to look for.
The query should be more specific than the user's message - extract the key concepts.

Examples:
User: "lol that's funny"
{"needs_search": false}

User: "What did we discuss about philosophy last week?"
{"needs_search": true, "search_query": "philosophy discussion existentialism meaning"}

User: "Can you help me with that project we talked about?"
{"needs_search": true, "search_query": "project collaboration plans goals"}

User: "I'm feeling anxious today"
{"needs_search": false}

User: "I'm still anxious about that thing we discussed"
{"needs_search": true, "search_query": "anxiety concerns worries discussion"}

Respond with ONLY a JSON object. No other text."""


class QueryInferenceService:
    """
    Decides whether to search memory for a given user message.

    Two-stage filtering:
        1. Regex pre-filter catches obvious non-search messages
        2. LLM-based inference for ambiguous cases
    """

    def __init__(self, provider: Any, config: Optional[Dict] = None):
        self.provider = provider
        self.config = config or {}
        self.temperature = self.config.get("query_inference", {}).get("temperature", 0.3)

    def _quick_no_search_check(self, user_input: str) -> bool:
        """
        Fast regex check for messages that definitely don't need search.

        Returns:
            True if the message definitely does NOT need a search.
        """
        text = user_input.strip()

        # Very short messages
        if len(text) < 4:
            return True

        # Pattern matching
        for pattern in NO_SEARCH_REGEX:
            if pattern.match(text):
                return True

        return False

    def _validate_search_query(self, user_input: str, search_query: str) -> str:
        """
        Validate that the generated search query has semantic overlap with user input.
        Prevents hallucinated queries.
        """
        if not search_query:
            return user_input

        # Check for at least some word overlap
        user_words = set(user_input.lower().split())
        query_words = set(search_query.lower().split())

        overlap = user_words & query_words
        if not overlap and len(query_words) > 3:
            # Query seems unrelated; fall back to user input
            logger.debug(f"Search query '{search_query}' has no overlap with input; using raw input")
            return user_input

        return search_query

    async def decide_and_generate_query(
        self,
        user_input: str,
        recent_context: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Decide whether to search memory and generate a search query.

        Args:
            user_input: The user's message
            recent_context: Recent conversation turns for context

        Returns:
            {
                "should_search": bool,
                "search_query": str,        # Only if should_search is True
                "reason": str,              # "pre_filter" or "llm_inference"
            }
        """
        # Stage 1: Quick regex pre-filter
        if self._quick_no_search_check(user_input):
            return {
                "should_search": False,
                "reason": "pre_filter",
            }

        # Stage 2: LLM inference
        try:
            # Build context if available
            context_str = ""
            if recent_context:
                context_lines = []
                for turn in recent_context[-5:]:
                    role = turn.get("role", "user")
                    content = turn.get("content", "")[:200]
                    context_lines.append(f"{role}: {content}")
                context_str = f"\n\nRecent conversation:\n" + "\n".join(context_lines)

            prompt = f"User message: \"{user_input}\"{context_str}\n\nDecide:"

            result = await asyncio.to_thread(
                self.provider.generate,
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                temperature=self.temperature,
                max_tokens=200,
            )

            parsed = self._parse_response(result.text)

            if parsed.get("should_search"):
                query = parsed.get("search_query", user_input)
                query = self._validate_search_query(user_input, query)
                return {
                    "should_search": True,
                    "search_query": query,
                    "reason": "llm_inference",
                }
            else:
                return {
                    "should_search": False,
                    "reason": "llm_inference",
                }

        except Exception as e:
            logger.warning(f"Query inference failed: {e}; defaulting to search")
            return {
                "should_search": True,
                "search_query": user_input,
                "reason": "fallback_on_error",
            }

    def _parse_response(self, response_text: str) -> Dict[str, Any]:
        """Parse JSON from LLM response and normalize to expected shape."""
        text = response_text.strip()
        parsed = None

        # Try direct JSON parse
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from markdown code block
        if parsed is None and "```" in text:
            match = re.search(r"```(?:json)?\s*({.*?})\s*```", text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass

        # Try finding JSON object in text
        if parsed is None:
            match = re.search(r"\{[^}]+\}", text)
            if match:
                try:
                    parsed = json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        # Keyword fallback
        if parsed is None:
            lower = text.lower()
            should = "true" in lower or "yes" in lower or "search" in lower
            return {"should_search": should, "search_query": ""}

        # Normalize parsed JSON to expected shape
        if not isinstance(parsed, dict):
            return {"should_search": False, "search_query": ""}

        return {
            "should_search": parsed.get("should_search", parsed.get("needs_search", False)),
            "search_query": parsed.get("search_query", ""),
        }


# ── Factory ───────────────────────────────────────────────────────────

async def create_query_inference_service(
    config: Dict,
) -> Optional[QueryInferenceService]:
    """
    Create a QueryInferenceService if enabled in config.

    Returns:
        QueryInferenceService or None if disabled.
    """
    qi_config = config.get("query_inference", {})
    if not qi_config.get("enabled", True):
        logger.info("Query inference disabled by config")
        return None

    try:
        from ..providers import create_tiered_provider

        # Use reflection provider for query inference (cheaper/faster)
        provider = create_tiered_provider(config, ["reflection_provider"])

        return QueryInferenceService(provider, config)

    except Exception as e:
        logger.warning(f"Failed to create query inference service: {e}")
        return None
