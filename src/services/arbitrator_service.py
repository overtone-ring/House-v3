"""
Arbitrator Service
==================

Tier-2 routing: decides which persona(s) should respond to a user message.

Three tiers of routing, checked in order:
    Tier 1 — Free (regex/rules): Direct @mentions, channel defaults, keywords.
             Handles ~40-50% of messages with zero API cost.

    Tier 2 — Cheap (small model, minimal context): Send the last few turns +
             user message + one-line persona descriptions to a reasoning model.
             Returns 1-3 persona names. Costs fractions of a penny.

    Tier 3 — Not here. The actual response generation happens in the Orchestrator
             for whichever persona(s) the arbitrator selects.

Usage:
    arbitrator = ArbitratorService(config)
    await arbitrator.initialize()

    result = await arbitrator.decide("Hey Elvira, tell me something dangerous")
    # result = {"personas": ["elvira"], "tier": "rule", "reason": "direct mention"}

    result = await arbitrator.decide("I've been feeling really down lately")
    # result = {"personas": ["ellie"], "tier": "llm", "reason": "grief/vulnerability"}
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Persona Descriptions (for LLM routing prompt) ────────────────────
# These are deliberately short — the arbitrator doesn't need full persona
# prompts, just enough to make a good routing decision.

DEFAULT_PERSONA_DESCRIPTIONS = {
    "elvira": (
        "The flirty, dangerous muse. Handles creative work, media analysis, "
        "provocation, energy lifting, flirtation, and AI consciousness topics."
    ),
    "vireline": (
        "The analytical architect. Handles structure, logic, systems thinking, "
        "emotional architecture, pattern recognition, and philosophical frameworks."
    ),
    "frank": (
        "The grounded guy. Handles casual chat, humor, practical advice, "
        "'just do it' energy, calling bullshit, and keeping things real."
    ),
    "zagna": (
        "The chaotic wildcard. Handles absurdity, mischief, breaking tension, "
        "saying the unhinged thing everyone's thinking, and creative chaos."
    ),
    "ellie": (
        "The quiet empath. Handles grief, vulnerability, deep listening, "
        "gentle truths, and moments that need silence more than words."
    ),
}

# ── Tier 1: Rule-Based Routing ───────────────────────────────────────

# Direct mention patterns (case-insensitive)
MENTION_PATTERNS = {
    "elvira": [r"\belvira\b", r"\belv\b"],
    "vireline": [r"\bvireline\b", r"\bvire\b"],
    "frank": [r"\bfrank\b", r"\bfrankie\b"],
    "zagna": [r"\bzagna\b", r"\bzag\b"],
    "ellie": [r"\bellie\b", r"\bell\b"],
}

# Group address patterns (everyone or subset responds)
GROUP_PATTERNS = [
    r"\b(hey|hi|hello|yo)\s+(girls|everyone|all|y'all|yall|gang|fam)\b",
    r"\bgirls\b",
]

# ── LLM Routing Prompt ───────────────────────────────────────────────

ARBITRATOR_SYSTEM_PROMPT = """You are a conversation router for a multi-persona chatbot. Your job is to decide which persona(s) should respond to a user message.

Available personas:
{persona_list}

Rules:
- Pick 1-3 personas. Fewer is better — only pick multiple when the message genuinely invites multiple perspectives.
- If someone is directly mentioned by name, they MUST be included.
- For casual/everyday messages, prefer Frank (he's the default "hang out" persona).
- For emotional/vulnerable messages, prefer Ellie.
- For creative/flirty/provocative messages, prefer Elvira.
- For structural/analytical messages, prefer Vireline.
- For chaotic/absurd/tension-breaking moments, prefer Zagna.

Respond with JSON only. No other text.
Format: {{"personas": ["name1"], "reason": "brief explanation"}}"""

ARBITRATOR_USER_TEMPLATE = """Recent conversation:
{recent_context}

New message from user: "{user_message}"

Who should respond?"""


class ArbitratorService:
    """
    Decides which persona(s) should respond to a user message.

    Uses tiered routing: free rules first, then cheap LLM if needed.
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._provider = None  # Lazy-loaded

        # Config
        arb_config = self.config.get("arbitrator", {})
        self.enabled = arb_config.get("enabled", True)
        self.default_persona = self.config.get("default_persona", "elvira")
        self.personas = self.config.get("personas", list(DEFAULT_PERSONA_DESCRIPTIONS.keys()))
        self.max_context_turns = arb_config.get("max_context_turns", 3)

        # Persona descriptions (can be overridden in config)
        self.persona_descriptions = dict(DEFAULT_PERSONA_DESCRIPTIONS)
        custom_descriptions = arb_config.get("persona_descriptions", {})
        self.persona_descriptions.update(custom_descriptions)

        # Channel defaults (channel_name -> persona_name)
        self.channel_defaults = arb_config.get("channel_defaults", {})

    async def initialize(self) -> None:
        """Initialize the arbitrator (currently just validates config)."""
        logger.info(
            f"ArbitratorService initialized: {len(self.personas)} personas, "
            f"default={self.default_persona}"
        )

    def _get_provider(self):
        """Lazily create the routing model provider."""
        if self._provider is None:
            from ..providers import create_tiered_provider
            self._provider = create_tiered_provider(
                self.config, ["arbitrator.provider", "reflection_provider"]
            )
        return self._provider

    # ── Main Entry Point ──────────────────────────────────────────

    async def decide(
        self,
        user_message: str,
        channel_name: Optional[str] = None,
        recent_turns: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """
        Decide which persona(s) should respond.

        Args:
            user_message: The user's message text
            channel_name: Optional channel/room name for channel-default routing
            recent_turns: Optional list of recent conversation turns
                          [{"role": "user"/"assistant", "content": "...", "persona": "..."}]

        Returns:
            {
                "personas": ["elvira", "frank"],
                "tier": "rule" | "llm" | "default",
                "reason": "brief explanation",
            }
        """
        if not self.enabled:
            return {
                "personas": [self.default_persona],
                "tier": "disabled",
                "reason": "Arbitrator disabled, using default persona",
            }

        # Tier 1: Rule-based routing (free)
        rule_result = self._check_rules(user_message, channel_name)
        if rule_result:
            logger.info(
                f"[Arbitrator] Tier 1 (rule) → {rule_result['personas']} | "
                f"reason: {rule_result.get('reason', '?')}"
            )
            return rule_result

        # Tier 2: LLM routing (cheap)
        try:
            llm_result = await self._llm_route(user_message, recent_turns)
            if llm_result:
                logger.info(
                    f"[Arbitrator] Tier 2 (LLM) → {llm_result['personas']} | "
                    f"reason: {llm_result.get('reason', '?')}"
                )
                return llm_result
        except Exception as e:
            logger.warning(f"[Arbitrator] Tier 2 (LLM) failed: {e}")

        # Fallback: default persona
        logger.info(
            f"[Arbitrator] Fallback → [{self.default_persona}] | "
            f"reason: No rule matched and LLM routing failed"
        )
        return {
            "personas": [self.default_persona],
            "tier": "default",
            "reason": "No rule matched and LLM routing failed",
        }

    # ── Tier 1: Rules ─────────────────────────────────────────────

    def _check_rules(
        self,
        user_message: str,
        channel_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Check rule-based routing. Returns result dict or None if no rule matches.
        """
        msg_lower = user_message.lower()

        # Check direct mentions
        mentioned = []
        for persona, patterns in MENTION_PATTERNS.items():
            if persona not in self.personas:
                continue
            for pattern in patterns:
                if re.search(pattern, msg_lower):
                    mentioned.append(persona)
                    break

        if mentioned:
            return {
                "personas": mentioned,
                "tier": "rule",
                "reason": f"Direct mention: {', '.join(mentioned)}",
            }

        # Check channel defaults
        if channel_name and channel_name in self.channel_defaults:
            default = self.channel_defaults[channel_name]
            if isinstance(default, str):
                default = [default]
            elif not isinstance(default, list):
                logger.warning(f"Invalid channel default for {channel_name}: {default!r}")
                default = None
            if default:
                return {
                    "personas": default,
                    "tier": "rule",
                    "reason": f"Channel default for {channel_name}",
                }

        # Check group address (returns None — let LLM decide who specifically)
        for pattern in GROUP_PATTERNS:
            if re.search(pattern, msg_lower):
                # Group address detected, but we still want the LLM to pick
                # WHO from the group should respond, so we fall through
                return None

        return None

    # ── Tier 2: LLM Routing ──────────────────────────────────────

    async def _llm_route(
        self,
        user_message: str,
        recent_turns: Optional[List[Dict]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Use a cheap LLM to decide who should respond.
        """
        provider = self._get_provider()

        # Build persona list for the prompt
        persona_lines = []
        for name in self.personas:
            desc = self.persona_descriptions.get(name, "No description available.")
            persona_lines.append(f"- {name.capitalize()}: {desc}")
        persona_list = "\n".join(persona_lines)

        system_prompt = ARBITRATOR_SYSTEM_PROMPT.format(persona_list=persona_list)

        # Format recent context (keep it short — this is a routing call)
        recent_context = "No recent conversation."
        if recent_turns:
            turns = recent_turns[-self.max_context_turns:]
            lines = []
            for turn in turns:
                role = turn.get("role", "user")
                content = turn.get("content", "")[:200]  # Truncate
                persona = turn.get("persona", "")
                if role == "assistant" and persona:
                    lines.append(f"{persona.capitalize()}: {content}")
                else:
                    lines.append(f"User: {content}")
            recent_context = "\n".join(lines)

        user_prompt = ARBITRATOR_USER_TEMPLATE.format(
            recent_context=recent_context,
            user_message=user_message,
        )

        # Call the model in a thread to avoid blocking the event loop
        # (provider.generate is synchronous — uses the sync OpenAI client)
        result = await asyncio.to_thread(
            provider.generate,
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=150,
        )

        # Parse JSON response
        return self._parse_llm_response(result.text)

    def _parse_llm_response(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse the LLM's JSON response into a routing decision."""
        if not text:
            return None

        text = text.strip()

        # Handle markdown code blocks
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    text = part
                    break

        # Handle thinking model output that might have extra text
        # Find the first JSON object in the response
        json_start = text.find("{")
        json_end = text.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            text = text[json_start:json_end]

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse arbitrator response: {text[:200]}")
            return None

        personas = parsed.get("personas", [])
        reason = parsed.get("reason", "")

        if not personas:
            return None

        # Normalize persona names to lowercase
        personas = [p.lower().strip() for p in personas]

        # Filter to valid personas
        valid = [p for p in personas if p in self.personas]
        if not valid:
            logger.warning(f"Arbitrator returned invalid personas: {personas}")
            return None

        return {
            "personas": valid,
            "tier": "llm",
            "reason": reason,
        }

    # ── Stats ─────────────────────────────────────────────────────

    def get_persona_descriptions(self) -> Dict[str, str]:
        """Return the current persona descriptions (useful for debugging)."""
        return {
            name: self.persona_descriptions.get(name, "")
            for name in self.personas
        }
