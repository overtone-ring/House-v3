"""
State Manager
=============

File-based persistence for engagement metrics and session state.

Singleton accessed via get_state_manager().

Note: an affective-state subsystem (per-persona emotional dimensions with
time-based decay) was deprecated. It was never written to in the unified
pipeline, so it never affected prompts. Engagement and session state remain.

Directory structure:
    data/state/{persona}/
        engagement.json
        sessions/{session_id}.json
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Default States ────────────────────────────────────────────────────

DEFAULT_ENGAGEMENT_STATE = {
    "total_interactions": 0,
    "session_count": 0,
    "avg_session_length": 0.0,
    "last_interaction": None,
    "streak_days": 0,
}


class StateManager:
    """
    File-based state persistence for engagement and sessions.

    All writes are atomic (write to .tmp, then rename).
    """

    def __init__(self, base_path: str = "data/state", config: Optional[Dict] = None):
        self.base_path = Path(base_path)
        self.config = config or {}

    # ── Path Helpers ──────────────────────────────────────────────

    def _ensure_persona_dir(self, persona: str) -> Path:
        d = self.base_path / persona
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_sessions_dir(self, persona: str) -> Path:
        d = self.base_path / persona / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── I/O ───────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write(path: Path, data: Dict) -> None:
        from ..utils.io import atomic_write_json
        atomic_write_json(path, data)

    @staticmethod
    def _read_json(path: Path, default: Dict) -> Dict:
        from ..utils.io import read_json
        return read_json(path, default) or dict(default)

    # ── Session State ─────────────────────────────────────────────

    def load_session(self, persona: str, session_id: str) -> Dict[str, Any]:
        d = self._ensure_sessions_dir(persona)
        default = {
            "session_id": session_id,
            "exchange_count": 0,
            "topics": [],
            "emotional_arc": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._read_json(d / f"{session_id}.json", default)

    def save_session(self, persona: str, session_id: str, state: Dict[str, Any]) -> None:
        d = self._ensure_sessions_dir(persona)
        state["last_activity"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write(d / f"{session_id}.json", state)

    def increment_session_exchange(self, persona: str, session_id: str) -> Dict[str, Any]:
        state = self.load_session(persona, session_id)
        state["exchange_count"] = state.get("exchange_count", 0) + 1
        self.save_session(persona, session_id, state)
        return state

    def add_session_topic(self, persona: str, session_id: str, topic: str) -> Dict[str, Any]:
        state = self.load_session(persona, session_id)
        topics = state.get("topics", [])
        if topic not in topics:
            topics.append(topic)
        state["topics"] = topics
        self.save_session(persona, session_id, state)
        return state

    def add_emotional_point(
        self, persona: str, session_id: str, emotional_point: Dict[str, Any]
    ) -> Dict[str, Any]:
        state = self.load_session(persona, session_id)
        arc = state.get("emotional_arc", [])
        arc.append(emotional_point)
        state["emotional_arc"] = arc[-20:]  # Keep last 20 points
        self.save_session(persona, session_id, state)
        return state

    # ── Engagement ────────────────────────────────────────────────

    def load_engagement(self, persona: str) -> Dict[str, Any]:
        d = self._ensure_persona_dir(persona)
        return self._read_json(d / "engagement.json", DEFAULT_ENGAGEMENT_STATE)

    def save_engagement(self, persona: str, state: Dict[str, Any]) -> None:
        d = self._ensure_persona_dir(persona)
        self._atomic_write(d / "engagement.json", state)

    def record_interaction(self, persona: str) -> Dict[str, Any]:
        state = self.load_engagement(persona)
        state["total_interactions"] = state.get("total_interactions", 0) + 1
        state["last_interaction"] = datetime.now(timezone.utc).isoformat()
        self.save_engagement(persona, state)
        return state


# ── Singleton ─────────────────────────────────────────────────────────

_state_manager: Optional[StateManager] = None


def get_state_manager(
    base_path: str = "data/state",
    config: Optional[Dict] = None,
) -> StateManager:
    """Get or create the singleton StateManager."""
    global _state_manager
    if _state_manager is None:
        _state_manager = StateManager(base_path, config)
    return _state_manager
