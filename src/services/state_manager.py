"""
State Manager
=============

File-based persistence for affective state, engagement metrics,
and session state.

Implements automatic time-based decay: novelty recovers, tension decays,
fatigue recovers. All state is stored as JSON files per persona.

Singleton accessed via get_state_manager().

Directory structure:
    data/state/{persona}/
        affective.json
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

DEFAULT_AFFECTIVE_STATE = {
    "state": "neutral",
    "intensity": 0.0,
    "novelty": 0.5,
    "resonance": 0.0,
    "tension": 0.0,
    "fatigue": 0.0,
    "temperature": 0.5,
    "intellectual_depth": 0.0,
    "last_updated": None,
}

# Per-dimension decay config: rate multiplier relative to base, and target value
DEFAULT_DIMENSION_CONFIG = {
    "novelty":            {"rate": 1.0,  "target": 0.5},
    "tension":            {"rate": 1.33, "target": 0.0},
    "fatigue":            {"rate": 1.67, "target": 0.0},
    "resonance":          {"rate": 0.67, "target": 0.0},
    "temperature":        {"rate": 0.67, "target": 0.5},
    "intellectual_depth": {"rate": 0.53, "target": 0.0},
}

DEFAULT_ENGAGEMENT_STATE = {
    "total_interactions": 0,
    "session_count": 0,
    "avg_session_length": 0.0,
    "last_interaction": None,
    "streak_days": 0,
}


class StateManager:
    """
    File-based state persistence with automatic time decay.

    All writes are atomic (write to .tmp, then rename).
    """

    def __init__(self, base_path: str = "data/state", config: Optional[Dict] = None):
        self.base_path = Path(base_path)
        self.config = config or {}

        # Decay config: base rate with per-dimension modifiers
        affective_cfg = self.config.get("affective", {})
        self.base_decay_rate = affective_cfg.get("base_decay_rate", 0.15)
        self.dimension_config = affective_cfg.get("dimensions", DEFAULT_DIMENSION_CONFIG)

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

    # ── Affective State ───────────────────────────────────────────

    def load_affective_state(self, persona: str) -> Dict[str, Any]:
        """Load affective state, applying time decay if needed."""
        d = self._ensure_persona_dir(persona)
        state = self._read_json(d / "affective.json", DEFAULT_AFFECTIVE_STATE)

        # Apply time decay
        last = state.get("last_updated")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                state = self.apply_time_decay(state, last_dt)
            except (ValueError, TypeError):
                pass

        return state

    def save_affective_state(self, persona: str, state: Dict[str, Any]) -> None:
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        d = self._ensure_persona_dir(persona)
        self._atomic_write(d / "affective.json", state)

    def update_affective_dimension(
        self, persona: str, dimension: str, value: float
    ) -> Dict[str, Any]:
        """Update a single affective dimension."""
        state = self.load_affective_state(persona)
        if dimension in state:
            state[dimension] = max(0.0, min(1.0, value))
        self.save_affective_state(persona, state)
        return state

    def apply_time_decay(self, state: Dict[str, Any], since: datetime) -> Dict[str, Any]:
        """
        Apply time-based decay to all affective dimensions.

        Each dimension decays toward its target value at a rate derived
        from base_decay_rate * dimension-specific multiplier.
        """
        now = datetime.now(timezone.utc)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        hours = (now - since).total_seconds() / 3600.0

        if hours <= 0:
            return state

        for dim, cfg in self.dimension_config.items():
            rate = self.base_decay_rate * cfg.get("rate", 1.0)
            target = cfg.get("target", 0.0)
            current = state.get(dim, target)

            decay = rate * hours
            if current > target:
                state[dim] = max(target, current - decay)
            elif current < target:
                state[dim] = min(target, current + decay)

        # Update intensity from active dimensions
        active_dims = [state.get(d, 0.0) for d in ["resonance", "tension", "intellectual_depth"]]
        state["intensity"] = max(active_dims) if active_dims else 0.0

        return state

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
