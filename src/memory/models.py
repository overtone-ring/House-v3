"""
Memory Data Models
==================

Dataclass models for memory storage (SQLite-backed).

Models:
    Exchange         - A turn pair (user message + one persona's response)
    DailyReflection  - Per-persona daily summary of exchanges
    UserRelationship - Cross-persona user relationship tracking
    SessionState     - Per-session tracking

Each model provides:
    - to_dict()   : Serialize for general use (API responses, logging)
    - from_dict() : Deserialize from dict with safe defaults
    - Auto-generated id and timestamp if not provided
"""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Core Models ──────────────────────────────────────────────────────

@dataclass
class Exchange:
    """
    A single turn pair: one user message + one persona's response.

    In multi-persona conversations, the same user message produces
    multiple Exchange records — one per responding persona. Each is
    embedded independently so retrieval can filter by persona_name.
    """
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    user_msg: str = ""
    assistant_response: str = ""
    persona_name: str = ""
    embedding: Optional[List[float]] = None
    timestamp: str = field(default_factory=_now_iso)
    reflected: bool = False        # Has this been included in a daily reflection?
    participants: List[str] = field(default_factory=list)  # All personas present in this session
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = asdict(self)
        # Embedding is stored separately in vec0 table, not in the dict
        d.pop("embedding", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "Exchange":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @property
    def content_for_embedding(self) -> str:
        """Combined text used for embedding."""
        return f"User: {self.user_msg}\n{self.persona_name}: {self.assistant_response}"

    @property
    def content_hash(self) -> str:
        """Deterministic hash for dedup."""
        import hashlib
        content = f"{self.user_msg}|{self.assistant_response}|{self.persona_name}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class DailyReflection:
    """
    Per-persona daily summary of conversations.

    Generated once per day (midnight trigger) by reviewing all unreflected
    exchanges for a persona and producing a narrative summary. Preserves
    timeline and attribution — each reflection belongs to exactly one persona
    and covers exactly one date.
    """
    id: str = field(default_factory=_new_id)
    persona_name: str = ""
    date: str = ""                 # YYYY-MM-DD
    summary: str = ""
    exchange_count: int = 0        # How many exchanges were summarized
    exchange_ids: List[str] = field(default_factory=list)  # Which exchanges were included
    embedding: Optional[List[float]] = None
    created_at: str = field(default_factory=_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d.pop("embedding", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "DailyReflection":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


# ── Supporting Models ────────────────────────────────────────────────

@dataclass
class UserRelationship:
    """Tracks a user's relationship with the system."""
    id: str = field(default_factory=_new_id)
    user_id: str = ""
    display_name: str = ""
    total_exchanges: int = 0
    trust_level: float = 0.0
    relationship_type: str = "stranger"
    first_seen: str = field(default_factory=_now_iso)
    last_seen: str = field(default_factory=_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "UserRelationship":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass
class SessionState:
    """Per-session tracking (exchange count, topics, emotional arc)."""
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    exchange_count: int = 0
    topics_discussed: List[str] = field(default_factory=list)
    emotional_arc: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=_now_iso)
    last_activity: str = field(default_factory=_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "SessionState":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
