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
    5. Parse JSON into an ordered list of turns (personas may speak
       multiple times — the response is a scene, not five slots)
    6. Post-process: record exchanges, update state per responding persona
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .providers import create_provider_from_config
from .providers.base import BaseProvider
from .conversation.buffer import ConversationBuffer
from .context.unified_manager import UnifiedContextManager
from .context.formatters import format_unified_context
from .services.memory_service import MemoryService
from .services.state_manager import get_state_manager, StateManager
from .services.query_inference_service import create_query_inference_service
from .response_parser import parse_house_turns

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

        # Tracks fire-and-forget post-process tasks so shutdown() can drain
        # them — otherwise a Ctrl+C right after a message can lose the
        # exchange before it's written to memory.
        self._pending_tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        """Initialize all components."""
        # Load unified system prompt
        self._unified_prompt = self._load_prompt()

        # Create provider
        provider_config = dict(self.config.get("provider", {}))
        self._provider = create_provider_from_config(provider_config)

        # State manager
        state_path = str(Path(self.config.get("memory", {}).get("data_dir", "./data")) / "state")
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
        forced_personas: Optional[set] = None,
    ) -> List[Dict[str, str]]:
        """
        Process a user message through the unified pipeline.

        Args:
            forced_personas: If set, only these personas may respond — used when
                specific personas are @mentioned. None means the whole house is
                summoned (@Girls) and the model decides who speaks.

        Returns:
            Ordered list of turns: [{"persona": name, "text": str}, ...].
            Personas may appear more than once (back-and-forth). An empty
            list means the House stays silent.
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
        # exclude_current: the watcher adds the user's message to the buffer
        # before calling us, and we already pass it separately as the prompt —
        # without this the model sees the same message twice every turn.
        conversation_history = None
        if conversation_buffer:
            conversation_history = conversation_buffer.get_history_for_unified_llm(
                limit=self.config.get("conversation", {}).get("max_turns", 50),
                exclude_current=True,
            )

        # Step 3: Build contextual primer from unified context
        contextual_primer = format_unified_context(
            memories=context.get("memories", []),
            user_context=context.get("user_context"),
        )

        # Routing directive: when specific personas were @mentioned, tell the
        # model only they were addressed so it doesn't waste tokens (or break
        # character) on the others. The output is also filtered in Step 5 as a
        # hard guarantee.
        if forced_personas:
            names = ", ".join(sorted(p.capitalize() for p in forced_personas))
            was = "was" if len(forced_personas) == 1 else "were"
            directive = (
                f"[Routing: only {names} {was} directly addressed this turn. "
                f"Only they should respond; everyone else stays silent.]"
            )
            contextual_primer = (
                f"{contextual_primer}\n\n{directive}" if contextual_primer else directive
            )

        # Step 4: Single LLM call
        response_text = await self._generate(
            user_input=user_input,
            contextual_primer=contextual_primer,
            conversation_history=conversation_history,
        )

        # Step 5: Parse response into an ordered scene
        if self._json_mode:
            turns = parse_house_turns(
                response_text,
                valid_personas=self.personas,
                default_persona=self._fallback_persona,
            )
        else:
            # Plain-text mode: entire response goes to the fallback persona.
            # Intended for single-persona configurations where JSON shaping
            # is dead weight on the model.
            text = response_text.strip()
            turns = [{"persona": self._fallback_persona, "text": text}] if text else []

        # Visibility: who the model actually chose to speak as, before any
        # forced-persona filtering. This is the ground truth of the generation.
        spoke_before = list(dict.fromkeys(t["persona"] for t in turns))
        logger.info(
            f"Model produced {len(turns)} turn(s) from: {spoke_before or 'NONE'}"
            + (f" | forced={sorted(forced_personas)}" if forced_personas else "")
        )

        # Hard guarantee: when specific personas were addressed, silence anyone
        # else the model may have let speak anyway.
        if forced_personas:
            filtered = [t for t in turns if t["persona"] in forced_personas]
            # Never discard a real generation silently. If the model produced
            # output but spoke only as wrong/unaddressed persona(s), the filter
            # just blanked everything — the silent-dead-air bug. Reroute the
            # discarded text to an addressed persona instead of going quiet.
            # This also covers error placeholders, which always land on the
            # fallback persona and would otherwise vanish here.
            if turns and not filtered:
                target = sorted(forced_personas)[0]
                source_turns = (
                    [t for t in turns if t["persona"] == self._fallback_persona]
                    or turns
                )
                logger.warning(
                    "Forced-persona filter would discard the ENTIRE generation: "
                    f"model spoke as {spoke_before} but only {sorted(forced_personas)} "
                    f"{'was' if len(forced_personas) == 1 else 'were'} addressed. "
                    f"Rerouting {len(source_turns)} turn(s) to {target} instead of dead air."
                )
                filtered = [
                    {"persona": target, "text": t["text"]} for t in source_turns
                ]
            turns = filtered

        # Step 6: Post-process (async, don't block response delivery)
        task = asyncio.create_task(
            self._post_process(
                user_input=user_input,
                turns=turns,
                session_id=session_id,
                user_id=user_id,
                conversation_buffer=conversation_buffer,
            )
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._on_post_process_done)

        return turns

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
        turns: List[Dict[str, str]],
        session_id: Optional[str],
        user_id: Optional[str],
        conversation_buffer: Optional[ConversationBuffer] = None,
    ) -> None:
        """
        Record exchanges and update state for each responding persona.

        A persona may take several turns in one scene — they're joined into
        a single exchange so memory keeps one record per persona per message.

        Runs async after responses are dispatched to Discord.
        """
        combined: Dict[str, List[str]] = {}
        for turn in turns:
            combined.setdefault(turn["persona"], []).append(turn["text"])

        participants = list(combined.keys())

        for persona_name, texts in combined.items():
            try:
                # Record exchange in memory
                memory_service = self._memory_services.get(persona_name)
                if memory_service:
                    await memory_service.add_exchange(
                        session_id=session_id or "",
                        user_msg=user_input,
                        assistant_response="\n\n".join(texts),
                        participants=participants,
                        metadata={"user_id": user_id} if user_id else None,
                    )

                # Update engagement
                self._state_manager.record_interaction(persona_name)

            except Exception as e:
                logger.error(
                    f"Post-processing failed for {persona_name}: {e}",
                    exc_info=True,
                )

    def _on_post_process_done(self, task: asyncio.Task) -> None:
        """Log exceptions from fire-and-forget post-processing tasks."""
        self._pending_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Post-processing failed: {exc}", exc_info=exc)

    # ── Lifecycle ────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Drain pending memory writes so Ctrl+C doesn't lose exchanges."""
        if self._pending_tasks:
            pending = list(self._pending_tasks)
            logger.info(f"Draining {len(pending)} pending memory writes...")
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"{len(self._pending_tasks)} memory writes did not finish in 5s"
                )
        logger.info("UnifiedOrchestrator shut down")


# ── Factory ──────────────────────────────────────────────────────────

async def create_unified_house(config: Optional[Dict] = None) -> UnifiedOrchestrator:
    """Create and initialize a UnifiedOrchestrator."""
    house = UnifiedOrchestrator(config)
    await house.initialize()
    return house
