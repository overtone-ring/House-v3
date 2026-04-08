"""
Unified Orchestrator
====================

Single-call multi-persona orchestrator. One LLM call generates all
persona responses simultaneously via structured JSON output.

Flow:
    1. Query inference: "Does this need memory search?"
    2. Unified context retrieval: memories + affective state for all personas
    3. Prompt assembly: unified system prompt + context + history + user input
    4. Single LLM call with json_mode
    5. Parse JSON into per-persona responses
    6. Post-process: record exchanges, update state per responding persona
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .providers import create_provider_from_config
from .providers.base import BaseProvider
from .conversation.buffer import ConversationBuffer
from .context.unified_manager import UnifiedContextManager
from .context.formatters import format_unified_context
from .services.raptor_memory_service import MemoryService
from .services.state_manager import get_state_manager, StateManager
from .services.query_inference_service import create_query_inference_service
from .response_parser import parse_house_response

logger = logging.getLogger(__name__)


class UnifiedOrchestrator:
    """
    Single-call multi-persona orchestrator.

    One model inhabits all personas simultaneously. It outputs structured
    JSON indicating who speaks and what they say. The orchestrator parses
    the JSON and dispatches per-persona responses.
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.personas = self.config.get("personas", [])
        self.default_persona = self.config.get("default_persona", "elvira")

        # Unified config
        unified_cfg = self.config.get("unified", {})
        self._prompt_file = unified_cfg.get(
            "system_prompt_file", "data/personas/unified_house.md"
        )
        self._json_mode = unified_cfg.get("json_mode", True)
        self._fallback_persona = unified_cfg.get("fallback_persona", self.default_persona)

        # Components (initialized in initialize())
        self._provider: Optional[BaseProvider] = None
        self._memory_services: Dict[str, MemoryService] = {}
        self._context_manager: Optional[UnifiedContextManager] = None
        self._state_manager: Optional[StateManager] = None
        self._query_inference = None
        self._unified_prompt: Optional[str] = None

    async def initialize(self) -> None:
        """Initialize all components."""
        # Load unified system prompt
        self._unified_prompt = self._load_prompt()

        # Create provider
        provider_config = dict(self.config.get("provider", {}))
        self._provider = create_provider_from_config(provider_config)

        # State manager
        state_path = self.config.get("memory", {}).get("data_dir", "./data") + "/state"
        self._state_manager = get_state_manager(state_path, self.config)

        # Memory service per persona (they share the same SQLite store)
        for persona in self.personas:
            ms = MemoryService(persona, self.config)
            await ms.initialize()
            self._memory_services[persona] = ms

        # Query inference (optional)
        self._query_inference = await create_query_inference_service(self.config)

        # Unified context manager
        self._context_manager = UnifiedContextManager(
            memory_services=self._memory_services,
            state_manager=self._state_manager,
            query_inference=self._query_inference,
            config=self.config,
        )

        logger.info(
            f"UnifiedOrchestrator initialized: {len(self.personas)} personas, "
            f"prompt={self._prompt_file}"
        )

    def _load_prompt(self) -> str:
        """Load the unified system prompt from file."""
        path = Path(self._prompt_file)
        if not path.is_absolute():
            from .utils.paths import get_project_root
            path = get_project_root() / path

        if not path.exists():
            raise FileNotFoundError(
                f"Unified system prompt not found: {path}. "
                f"Expected at {self._prompt_file}"
            )

        prompt = path.read_text(encoding="utf-8").strip()
        logger.info(f"Loaded unified prompt: {len(prompt)} chars from {path}")
        return prompt

    # ── Main Entry Point ─────────────────────────────────────────

    async def process_message(
        self,
        user_input: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        channel_name: Optional[str] = None,
        conversation_buffer: Optional[ConversationBuffer] = None,
    ) -> Dict[str, Optional[str]]:
        """
        Process a user message through the unified pipeline.

        Returns:
            Dict mapping persona names to response text.
            None values indicate the persona stayed silent.
            Example: {"elvira": "response...", "frank": null, "zagna": "text"}
        """
        # Step 1: Retrieve unified context
        recent_context = None
        if conversation_buffer:
            recent_context = conversation_buffer.get_history_for_query_inference()

        context = await self._context_manager.retrieve_context(
            query=user_input,
            session_id=session_id,
            user_id=user_id,
            recent_context=recent_context,
        )

        # Step 2: Build conversation history
        conversation_history = None
        if conversation_buffer:
            conversation_history = conversation_buffer.get_history_for_unified_llm(
                limit=self.config.get("conversation", {}).get("max_turns", 50)
            )

        # Step 3: Build contextual primer from unified context
        contextual_primer = format_unified_context(
            memories=context.get("memories", []),
            affective_states=context.get("affective_states", {}),
            user_context=context.get("user_context"),
        )

        # Step 4: Single LLM call
        response_text = await self._generate(
            user_input=user_input,
            contextual_primer=contextual_primer,
            conversation_history=conversation_history,
        )

        # Step 5: Parse JSON response
        responses = parse_house_response(
            response_text,
            valid_personas=self.personas,
            default_persona=self._fallback_persona,
        )

        # Step 6: Post-process (async, don't block response delivery)
        task = asyncio.create_task(
            self._post_process(
                user_input=user_input,
                responses=responses,
                session_id=session_id,
                user_id=user_id,
                conversation_buffer=conversation_buffer,
            )
        )
        task.add_done_callback(self._on_post_process_done)

        return responses

    # ── Generation ───────────────────────────────────────────────

    async def _generate(
        self,
        user_input: str,
        contextual_primer: Optional[str] = None,
        conversation_history: Optional[List[Dict]] = None,
    ) -> str:
        """Run the unified LLM call."""
        try:
            result = await asyncio.to_thread(
                self._provider.generate,
                prompt=user_input,
                system_prompt=self._unified_prompt,
                contextual_primer=contextual_primer,
                conversation_history=conversation_history,
                json_mode=self._json_mode,
            )
            return result.text
        except Exception as e:
            logger.error(f"Unified generation failed: {e}", exc_info=True)
            return ""

    # ── Post-Processing ──────────────────────────────────────────

    async def _post_process(
        self,
        user_input: str,
        responses: Dict[str, Optional[str]],
        session_id: Optional[str],
        user_id: Optional[str],
        conversation_buffer: Optional[ConversationBuffer] = None,
    ) -> None:
        """
        Record exchanges and update state for each responding persona.

        Runs async after responses are dispatched to Discord.
        """
        for persona_name, response_text in responses.items():
            if response_text is None:
                continue

            try:
                # Record exchange in memory
                memory_service = self._memory_services.get(persona_name)
                if memory_service:
                    await memory_service.add_exchange(
                        session_id=session_id or "",
                        user_msg=user_input,
                        assistant_response=response_text,
                        participants=[
                            p for p, r in responses.items() if r is not None
                        ],
                        metadata={"user_id": user_id} if user_id else None,
                    )

                # Update engagement
                self._state_manager.record_interaction(persona_name)

            except Exception as e:
                logger.error(
                    f"Post-processing failed for {persona_name}: {e}",
                    exc_info=True,
                )

    @staticmethod
    def _on_post_process_done(task: asyncio.Task) -> None:
        """Log exceptions from fire-and-forget post-processing tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Post-processing failed: {exc}", exc_info=exc)

    # ── Lifecycle ────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Shutdown all components."""
        logger.info("UnifiedOrchestrator shutting down")


# ── Factory ──────────────────────────────────────────────────────────

async def create_unified_house(config: Optional[Dict] = None) -> UnifiedOrchestrator:
    """Create and initialize a UnifiedOrchestrator."""
    house = UnifiedOrchestrator(config)
    await house.initialize()
    return house
