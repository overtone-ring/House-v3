"""
Orchestrator
=============

Two levels:
    Orchestrator      — Single-persona pipeline (message → response)
    HouseOrchestrator — Multi-persona coordinator (arbitrator → persona pipelines)

Single-persona pipeline:
    1. Query inference: "Does this need memory search?"
    2. Context retrieval: Search exchanges + reflections, load affective/relational state
    3. Prompt assembly: System prompt + contextual primer + history + memories + user input
    4. LLM generation: Send to provider, get response
    5. Post-processing: Record exchange, update state

Multi-persona pipeline:
    1. Arbitrator: "Who should respond?" (tier 1 rules → tier 2 Qwen LLM)
    2. For each selected persona, run the single-persona pipeline
    3. Return all responses with persona attribution

This is the ONLY module that imports from all subsystems.
Every other module stays within its own domain.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .providers import create_provider_from_config
from .providers.base import BaseProvider, GenerationResult
from .persona.assembler import PersonaAssembler
from .conversation.buffer import ConversationBuffer
from .context.manager import ContextManager
from .context.formatters import format_memories, format_affective_primer, format_relational_primer
from .services.raptor_memory_service import MemoryService
from .services.state_manager import get_state_manager, StateManager
from .services.query_inference_service import create_query_inference_service
from .services.arbitrator_service import ArbitratorService
from .utils.config import get_config

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Single-Persona Orchestrator
# ══════════════════════════════════════════════════════════════════════

class Orchestrator:
    """
    Main conversation pipeline for a single persona.

    Usage:
        orch = Orchestrator("elvira", provider, config)
        await orch.initialize()
        response = await orch.process_user_input("Hello!", session_id="s1")
    """

    def __init__(
        self,
        persona_name: str,
        provider: BaseProvider,
        config: Optional[Dict] = None,
    ):
        self.persona_name = persona_name
        self.provider = provider
        self.config = config or get_config()

        # Components (initialized lazily or in initialize())
        self._assembler: Optional[PersonaAssembler] = None
        self._memory: Optional[MemoryService] = None
        self._context_manager: Optional[ContextManager] = None
        self._state_manager: Optional[StateManager] = None
        self._query_inference = None  # Optional
        self._static_prompt: Optional[str] = None
        self._session_id: Optional[str] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize all subsystems."""
        if self._initialized:
            return

        # Persona prompt assembly
        self._assembler = PersonaAssembler(self.config)
        assembly = self._assembler.assemble(self.persona_name)
        self._static_prompt = assembly["static_prompt"]
        logger.info(
            f"[{self.persona_name}] Prompt assembled: "
            f"{len(assembly['sections'])} sections, ~{assembly['token_estimate']} tokens"
        )

        # Memory service (flat RAG)
        self._memory = MemoryService(self.persona_name, self.config)
        await self._memory.initialize()

        # State manager
        self._state_manager = get_state_manager(
            base_path=self.config.get("memory", {}).get("data_dir", "./data") + "/state",
            config=self.config,
        )

        # Query inference (optional)
        self._query_inference = await create_query_inference_service(self.config)

        # Context manager
        self._context_manager = ContextManager(
            persona_name=self.persona_name,
            memory_service=self._memory,
            query_inference=self._query_inference,
            config=self.config.get("context", {}),
        )

        self._initialized = True
        logger.info(f"[{self.persona_name}] Orchestrator initialized")

    def set_session(self, session_id: str) -> None:
        """Set the active session ID."""
        self._session_id = session_id

    # ── Main Pipeline ─────────────────────────────────────────────

    async def process_user_input(
        self,
        user_input: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        stream: bool = False,
        stream_callback: Optional[Callable[[str], None]] = None,
        conversation_buffer: Optional[ConversationBuffer] = None,
    ) -> str:
        """
        Process a user message through the full pipeline.

        Returns:
            The assistant's response text.
        """
        if not self._initialized:
            await self.initialize()

        session_id = session_id or self._session_id or "default"

        # Step 1: Retrieve context (memory search + state)
        recent_context = None
        conversation_history = None
        summary_prefix = None

        if conversation_buffer:
            recent_context = conversation_buffer.get_history_for_query_inference()
            conversation_history = conversation_buffer.get_history_for_llm(
                limit=self.config.get("conversation", {}).get("max_turns", 50),
                for_persona=self.persona_name,  # Labels other personas' messages
            )
            summary_prefix = conversation_buffer.get_summary_prefix()

        context = await self._context_manager.retrieve_context(
            query=user_input,
            session_id=session_id,
            user_id=user_id,
            recent_context=recent_context,
        )

        # Step 2: Build prompt parts
        prompt_parts = self._build_prompt_parts(
            user_input=user_input,
            context=context,
            conversation_history=conversation_history,
            summary_prefix=summary_prefix,
        )

        # Step 3: Generate response
        if stream and stream_callback:
            response_text = await self._generate_streaming(prompt_parts, stream_callback)
        else:
            response_text = await self._generate(prompt_parts)

        # Step 4: Post-processing (async, don't block response)
        task = asyncio.create_task(
            self._post_process(
                user_input=user_input,
                response=response_text,
                session_id=session_id,
                user_id=user_id,
                conversation_buffer=conversation_buffer,
            )
        )
        task.add_done_callback(self._on_post_process_done)

        return response_text

    # ── Prompt Building ───────────────────────────────────────────

    def _build_prompt_parts(
        self,
        user_input: str,
        context: Dict,
        conversation_history: Optional[List[Dict]] = None,
        summary_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assemble all prompt components."""
        # Format memories with persona attribution
        formatted_memories = ""
        if context.get("memories"):
            formatted_memories = format_memories(
                context["memories"],
                current_persona=self.persona_name,
            )

        # Build contextual primer (affective state + relationship)
        primer_parts = []

        # Affective state
        affective_state = self._state_manager.load_affective_state(self.persona_name)
        affective_primer = format_affective_primer(affective_state)
        if affective_primer:
            primer_parts.append(affective_primer)

        # Relational context
        if context.get("user_context"):
            relational_primer = format_relational_primer(context["user_context"])
            if relational_primer:
                primer_parts.append(relational_primer)

        # Summary of expired turns
        if summary_prefix:
            primer_parts.append(f"[Previous conversation summary: {summary_prefix}]")

        contextual_primer = "\n".join(primer_parts) if primer_parts else ""

        return {
            "prompt": user_input,
            "system_prompt": self._static_prompt,
            "contextual_primer": contextual_primer,
            "conversation_history": conversation_history,
            "formatted_memories": formatted_memories,
        }

    # ── Generation ────────────────────────────────────────────────

    async def _generate(self, prompt_parts: Dict[str, Any]) -> str:
        """Generate a complete response."""
        try:
            # Run in thread — provider.generate() is synchronous and would
            # block the event loop (starving Discord's heartbeat)
            result = await asyncio.to_thread(
                self.provider.generate,
                prompt=prompt_parts["prompt"],
                system_prompt=prompt_parts["system_prompt"],
                contextual_primer=prompt_parts["contextual_primer"],
                conversation_history=prompt_parts["conversation_history"],
                formatted_memories=prompt_parts["formatted_memories"],
            )
            return result.text

        except Exception as e:
            logger.error(f"[{self.persona_name}] Generation failed: {e}")
            return "[I'm having trouble responding right now. Please try again.]"

    async def _generate_streaming(
        self,
        prompt_parts: Dict[str, Any],
        callback: Callable[[str], None],
    ) -> str:
        """Generate a streaming response."""
        try:
            full_response = ""
            for chunk in self.provider.generate_stream(
                prompt=prompt_parts["prompt"],
                system_prompt=prompt_parts["system_prompt"],
                contextual_primer=prompt_parts["contextual_primer"],
            ):
                full_response += chunk
                callback(chunk)
            return full_response

        except Exception as e:
            logger.error(f"[{self.persona_name}] Streaming failed: {e}")
            error_msg = "[I'm having trouble responding right now. Please try again.]"
            callback(error_msg)
            return error_msg

    @staticmethod
    def _on_post_process_done(task: asyncio.Task) -> None:
        """Log exceptions from fire-and-forget post-processing tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Post-processing failed: {exc}", exc_info=exc)

    # ── Post-Processing ───────────────────────────────────────────

    async def _post_process(
        self,
        user_input: str,
        response: str,
        session_id: str,
        user_id: Optional[str],
        conversation_buffer: Optional[ConversationBuffer] = None,
    ) -> None:
        """
        Post-processing tasks (runs async after response is sent).

        - Records the turn pair as a single exchange
        - Updates engagement metrics
        - Updates conversation buffer
        """
        try:
            # Record as a single turn pair exchange
            await self._memory.add_exchange(
                session_id=session_id,
                user_msg=user_input,
                assistant_response=response,
                metadata={"user_id": user_id} if user_id else None,
            )

            # Update engagement
            self._state_manager.record_interaction(self.persona_name)
            self._state_manager.increment_session_exchange(self.persona_name, session_id)

            # NOTE: Do NOT update conversation_buffer here.
            # The caller (e.g. Watcher) owns the buffer and already records
            # user messages and assistant responses. Adding here would double-record.

        except Exception as e:
            logger.error(f"[{self.persona_name}] Post-processing error: {e}")

    # ── Lifecycle ─────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Clean shutdown of all subsystems."""
        logger.info(f"[{self.persona_name}] Shutting down orchestrator")


# ══════════════════════════════════════════════════════════════════════
# Multi-Persona House Orchestrator
# ══════════════════════════════════════════════════════════════════════

class HouseOrchestrator:
    """
    Multi-persona coordinator.

    Manages a pool of single-persona Orchestrators and an ArbitratorService.
    When a message comes in:
        1. Arbitrator decides who responds (1-3 personas)
        2. Selected persona orchestrators run their pipelines
        3. Responses returned with attribution

    Usage:
        house = HouseOrchestrator(config)
        await house.initialize()

        # Returns list of (persona_name, response_text)
        responses = await house.process_message("Hey girls!", session_id="s1")

        # Or with a specific persona (bypasses arbitrator)
        responses = await house.process_message("Hi", persona="elvira", session_id="s1")
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or get_config()
        self.personas = self.config.get("personas", [])

        self._arbitrator: Optional[ArbitratorService] = None
        self._orchestrators: Dict[str, Orchestrator] = {}
        self._provider = None
        self._initialized = False

    async def initialize(self, preload_personas: Optional[List[str]] = None) -> None:
        """
        Initialize the house.

        Args:
            preload_personas: Personas to initialize eagerly. If None, orchestrators
                             are created lazily on first use (saves startup time).
        """
        if self._initialized:
            return

        # Create shared provider
        provider_config = dict(self.config.get("provider", {}))
        self._provider = create_provider_from_config(provider_config)

        # Initialize arbitrator
        self._arbitrator = ArbitratorService(self.config)
        await self._arbitrator.initialize()

        # Pre-load specific personas if requested
        if preload_personas:
            for name in preload_personas:
                await self._get_orchestrator(name)

        self._initialized = True
        logger.info(
            f"House initialized: {len(self.personas)} personas, "
            f"arbitrator={'enabled' if self._arbitrator.enabled else 'disabled'}"
        )

    async def _get_orchestrator(self, persona_name: str) -> Orchestrator:
        """Get or create an orchestrator for a persona (lazy init)."""
        if persona_name not in self._orchestrators:
            orch = Orchestrator(persona_name, self._provider, self.config)
            await orch.initialize()
            self._orchestrators[persona_name] = orch
        return self._orchestrators[persona_name]

    # ── Main Entry Point ──────────────────────────────────────────

    async def process_message(
        self,
        user_input: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        channel_name: Optional[str] = None,
        persona: Optional[str] = None,
        conversation_buffer: Optional[ConversationBuffer] = None,
        stream: bool = False,
        stream_callback: Optional[Callable[[str, str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Process a user message through the full multi-persona pipeline.

        Args:
            user_input: The user's message
            session_id: Session identifier
            user_id: User identifier
            channel_name: Channel/room name (for rule-based routing)
            persona: If set, skip arbitrator and use this persona directly
            conversation_buffer: Shared conversation buffer
            stream: Whether to stream responses
            stream_callback: Callback(persona_name, chunk) for streaming

        Returns:
            List of response dicts:
            [
                {
                    "persona": "elvira",
                    "response": "Oh darling...",
                    "routing": {"tier": "llm", "reason": "..."},
                },
                ...
            ]
        """
        if not self._initialized:
            await self.initialize()

        session_id = session_id or "default"

        # ── Step 1: Decide who responds ───────────────────────────
        if persona:
            # Direct persona specified — skip arbitrator
            selected = [persona]
            routing = {"personas": [persona], "tier": "direct", "reason": "Persona specified by caller"}
        else:
            # Build recent turns for arbitrator context
            recent_turns = None
            if conversation_buffer:
                recent_turns = conversation_buffer.get_recent_turns(
                    limit=self._arbitrator.max_context_turns
                )

            routing = await self._arbitrator.decide(
                user_message=user_input,
                channel_name=channel_name,
                recent_turns=recent_turns,
            )
            selected = routing.get("personas", [self.config.get("default_persona", "elvira")])

        logger.info(
            f"Routing: {', '.join(selected)} "
            f"(tier={routing.get('tier', '?')}, reason={routing.get('reason', '?')[:60]})"
        )

        # ── Step 2: Run selected persona pipelines ────────────────
        # If multiple personas, run them concurrently
        async def _run_persona(name: str) -> Dict[str, Any]:
            try:
                orch = await self._get_orchestrator(name)

                # Per-persona stream callback wrapper
                persona_stream_cb = None
                if stream and stream_callback:
                    persona_stream_cb = lambda chunk, _name=name: stream_callback(_name, chunk)

                response = await orch.process_user_input(
                    user_input=user_input,
                    session_id=session_id,
                    user_id=user_id,
                    stream=stream,
                    stream_callback=persona_stream_cb,
                    conversation_buffer=conversation_buffer,
                )

                return {
                    "persona": name,
                    "response": response,
                    "routing": routing,
                }

            except Exception as e:
                logger.error(f"[{name}] Pipeline failed: {e}")
                return {
                    "persona": name,
                    "response": f"[{name} is having trouble responding right now.]",
                    "routing": routing,
                    "error": str(e),
                }

        if len(selected) == 1:
            # Single persona — no need for gather
            results = [await _run_persona(selected[0])]
        else:
            # Multiple personas — run concurrently
            tasks = [_run_persona(name) for name in selected]
            results = await asyncio.gather(*tasks)

        return results

    async def shutdown(self) -> None:
        """Shutdown all orchestrators."""
        for name, orch in self._orchestrators.items():
            await orch.shutdown()
        self._orchestrators.clear()
        logger.info("House shut down")


# ── Factories ─────────────────────────────────────────────────────────

async def create_orchestrator(
    persona_name: str,
    config: Optional[Dict] = None,
) -> Orchestrator:
    """
    Create and initialize a single-persona Orchestrator.

    Use this for direct single-persona access (testing, specific channels).
    For multi-persona routing, use create_house() instead.
    """
    if config is None:
        config = get_config()

    provider_config = dict(config.get("provider", {}))
    provider = create_provider_from_config(provider_config)

    orch = Orchestrator(persona_name, provider, config)
    await orch.initialize()
    return orch


async def create_house(
    config: Optional[Dict] = None,
    preload_personas: Optional[List[str]] = None,
) -> HouseOrchestrator:
    """
    Create and initialize the multi-persona HouseOrchestrator.

    Args:
        config: Configuration dict (loads default if not provided)
        preload_personas: Personas to initialize eagerly (None = lazy init)

    Returns:
        Initialized HouseOrchestrator ready for process_message()
    """
    if config is None:
        config = get_config()

    house = HouseOrchestrator(config)
    await house.initialize(preload_personas=preload_personas)
    return house
