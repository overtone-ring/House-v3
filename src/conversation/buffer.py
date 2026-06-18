"""
Conversation Buffer
===================

Sliding-window conversation buffer with JSON persistence.
Provider-agnostic history formatting for LLM context injection.

Ported from v2 ConversationBuffer with identical interface.
Improvements:
    - Removed unused 'interface' field complexity
    - Cleaner persistence (atomic writes)
    - Type hints throughout
"""

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ConversationTurn:
    """A single turn in the conversation."""
    role: str = ""             # "user" or "assistant"
    content: str = ""
    timestamp: str = ""
    persona: str = ""          # Which persona responded (for assistant turns)
    speaker_name: str = ""     # Human-readable name (for user turns)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationTurn":
        """Resilient deserialization — ignores unknown fields, provides defaults."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class ConversationBuffer:
    """
    Sliding-window conversation buffer with persistence.

    Maintains a rolling window of recent turns. When turns expire,
    they can be summarized and prepended as context.
    """

    def __init__(
        self,
        max_turns: int = 50,
        max_chars: int = 50_000,
        session_id: Optional[str] = None,
    ):
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.session_id = session_id
        self._turns: Deque[ConversationTurn] = deque()
        self._summary_prefix: Optional[str] = None
        self._dirty = False

    # ── Adding Turns ──────────────────────────────────────────────

    def add_user_message(
        self,
        content: str,
        speaker_name: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        """Add a user message to the buffer."""
        turn = ConversationTurn(
            role="user",
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            speaker_name=speaker_name,
            metadata=metadata or {},
        )
        self._turns.append(turn)
        self._dirty = True

    def add_assistant_response(
        self,
        content: str,
        persona: str = "",
        metadata: Optional[dict] = None,
    ) -> None:
        """Add an assistant response to the buffer."""
        turn = ConversationTurn(
            role="assistant",
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            persona=persona,
            metadata=metadata or {},
        )
        self._turns.append(turn)
        self._dirty = True

    def drop_last_user_message(self) -> bool:
        """Remove the most recent turn iff it's a user message.

        Undoes a user turn whose generation produced no response (e.g. the
        model emitted malformed output the parser rejected as silence, or the
        provider was unavailable), so a resend doesn't leave the failed
        message dangling in context with no reply after it. Safe because
        per-channel processing is serialized — on a failed generation the user
        turn just added is still the last turn. Returns True if one was removed.
        """
        if self._turns and self._turns[-1].role == "user":
            self._turns.pop()
            self._dirty = True
            return True
        return False

    # ── Retrieval ─────────────────────────────────────────────────

    def get_history_for_llm(
        self,
        limit: Optional[int] = None,
        exclude_current: bool = False,
        for_persona: Optional[str] = None,
    ) -> List[Dict]:
        """
        Get conversation history formatted for LLM consumption.

        Returns:
            List of {"role": str, "content": str} dicts.
            Provider-agnostic format compatible with OpenAI/Anthropic/etc.
        """
        turns = list(self._turns)

        if exclude_current and turns:
            turns = turns[:-1]

        if limit:
            turns = turns[-limit:]

        history = []
        for turn in turns:
            if for_persona and turn.role == "assistant" and turn.persona:
                if turn.persona != for_persona:
                    # OTHER personas' responses must NOT be role:assistant —
                    # the model interprets assistant messages as its own prior
                    # output, causing identity confusion and name leakage.
                    # Instead, fold them into the conversation as a "user" turn
                    # with clear third-party attribution.
                    history.append({
                        "role": "user",
                        "content": f"[{turn.persona.capitalize()} said]: {turn.content}",
                    })
                    continue

            history.append({"role": turn.role, "content": turn.content})

        return history

    def get_history_for_unified_llm(
        self,
        limit: Optional[int] = None,
        exclude_current: bool = False,
    ) -> List[Dict]:
        """
        Get conversation history for the unified multi-persona model.

        All assistant turns include persona attribution in the content so
        the model knows which persona said what. Since the unified model
        IS all personas, every assistant turn is role: "assistant".

        exclude_current drops the most recent user turn — for callers that
        pass the current message separately as the prompt, so the model
        doesn't see it twice.

        Returns:
            List of {"role": str, "content": str} dicts.
        """
        turns = list(self._turns)
        if exclude_current and turns and turns[-1].role == "user":
            turns = turns[:-1]
        if limit:
            turns = turns[-limit:]

        history = []
        for turn in turns:
            if turn.role == "assistant" and turn.persona:
                content = f"[{turn.persona}]: {turn.content}"
            elif turn.role == "user" and turn.speaker_name:
                content = f"[{turn.speaker_name}]: {turn.content}"
            else:
                content = turn.content
            history.append({"role": turn.role, "content": content})

        return history

    def get_history_for_query_inference(self, limit: int = 10) -> List[Dict]:
        """Get recent history formatted for query inference decisions."""
        turns = list(self._turns)[-limit:]
        return [{"role": t.role, "content": t.content[:200]} for t in turns]

    def get_summary_prefix(self) -> Optional[str]:
        """Get the summary of expired turns, if any."""
        return self._summary_prefix

    def get_formatted_context(
        self,
        exclude_persona: Optional[str] = None,
        include_speaker_names: bool = True,
    ) -> str:
        """Get conversation as a human-readable string."""
        lines = []
        for turn in self._turns:
            if turn.role == "user":
                name = turn.speaker_name or "User"
                prefix = f"{name}: " if include_speaker_names else "User: "
            else:
                if exclude_persona and turn.persona == exclude_persona:
                    continue
                prefix = f"{turn.persona or 'Assistant'}: "
            lines.append(f"{prefix}{turn.content}")
        return "\n".join(lines)

    def get_recent_turns(self, limit: int = 3) -> List[Dict]:
        """
        Get recent turns formatted for the arbitrator.

        Returns:
            List of {"role": str, "content": str, "persona": str} dicts.
            The arbitrator uses these for routing context.
        """
        turns = list(self._turns)[-limit:]
        return [
            {
                "role": t.role,
                "content": t.content,
                "persona": t.persona,
            }
            for t in turns
        ]

    def get_last_user_message(self) -> Optional[str]:
        """Get the most recent user message."""
        for turn in reversed(self._turns):
            if turn.role == "user":
                return turn.content
        return None

    def get_last_response(self) -> Optional[str]:
        """Get the most recent assistant response."""
        for turn in reversed(self._turns):
            if turn.role == "assistant":
                return turn.content
        return None

    # ── Sliding Window ────────────────────────────────────────────

    def trim(self, max_turns: Optional[int] = None) -> List[ConversationTurn]:
        """
        Trim buffer to max_turns. Returns expired turns.

        Expired turns can be fed to a summarizer for context preservation.
        """
        limit = max_turns or self.max_turns
        expired = []

        while len(self._turns) > limit:
            expired.append(self._turns.popleft())

        if expired:
            self._dirty = True

        return expired

    def set_summary_prefix(self, summary: str) -> None:
        """Set a summary of expired turns as context prefix."""
        self._summary_prefix = summary

    def format_expired_turns_for_summary(
        self,
        expired_turns: List[ConversationTurn],
        existing_summary: Optional[str] = None,
    ) -> str:
        """
        Format expired turns for summarization.

        Returns:
            Text suitable for passing to an LLM summarizer.
        """
        parts = []
        if existing_summary:
            parts.append(f"Previous summary:\n{existing_summary}")

        parts.append("Recent expired turns:")
        for turn in expired_turns:
            speaker = turn.speaker_name or turn.persona or turn.role
            parts.append(f"  {speaker}: {turn.content}")

        return "\n".join(parts)

    # ── Persistence ───────────────────────────────────────────────

    def save(self, path: str) -> str:
        """
        Save buffer to a JSON file (atomic write).

        Returns:
            The path the buffer was saved to.
        """
        data = {
            "session_id": self.session_id,
            "max_turns": self.max_turns,
            "max_chars": self.max_chars,
            "summary_prefix": self._summary_prefix,
            "turns": [t.to_dict() for t in self._turns],
        }

        from ..utils.io import atomic_write_json
        atomic_write_json(Path(path), data)

        self._dirty = False
        return path

    @classmethod
    def load(cls, path: str) -> "ConversationBuffer":
        """Load a buffer from a JSON file."""
        with open(path) as f:
            data = json.load(f)

        buffer = cls(
            max_turns=data.get("max_turns", 50),
            max_chars=data.get("max_chars", 50_000),
            session_id=data.get("session_id"),
        )
        buffer._summary_prefix = data.get("summary_prefix")

        for turn_data in data.get("turns", []):
            buffer._turns.append(ConversationTurn.from_dict(turn_data))

        buffer._dirty = False
        return buffer

    @classmethod
    def load_or_create(
        cls,
        session_id: str,
        base_dir: str = "./data",
        max_turns: int = 50,
    ) -> "ConversationBuffer":
        """Load existing session or create a new buffer.

        Falling back to an empty buffer is always safer than crashing the
        watcher: a corrupt session file (truncated mid-write, schema drift,
        non-dict root, OS-level read error) loses recent history but keeps
        the channel responsive instead of taking the bot down on startup.
        """
        path = cls.session_file_path(session_id, base_dir)
        if os.path.exists(path):
            try:
                return cls.load(path)
            except Exception as e:
                logger.warning(
                    f"Failed to load session {session_id} ({type(e).__name__}: {e}); "
                    f"starting fresh buffer"
                )

        return cls(max_turns=max_turns, session_id=session_id)

    @staticmethod
    def session_file_path(session_id: str, base_dir: str = "./data") -> str:
        """Get the file path for a session's conversation buffer."""
        return os.path.join(base_dir, "sessions", f"{session_id}_conversation.json")

    @staticmethod
    def archive_file_path(session_id: str, base_dir: str = "./data") -> str:
        """Get the append-only archive path for a session's evicted turns.

        When the active buffer is capped, older turns are appended here (one
        JSON object per line) instead of being discarded, so the full history
        is preserved without bloating the live buffer file.
        """
        return os.path.join(base_dir, "sessions", f"{session_id}_archive.jsonl")

    # ── Utilities ─────────────────────────────────────────────────

    def clear(self) -> None:
        """Clear all turns and summary."""
        self._turns.clear()
        self._summary_prefix = None
        self._dirty = True

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def approx_token_count(self) -> int:
        """Rough token estimate (chars / 4)."""
        total = sum(len(t.content) for t in self._turns)
        if self._summary_prefix:
            total += len(self._summary_prefix)
        return total // 4

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def __len__(self) -> int:
        return len(self._turns)

    def __repr__(self) -> str:
        return (
            f"ConversationBuffer(session={self.session_id}, "
            f"turns={len(self._turns)}, max={self.max_turns})"
        )
