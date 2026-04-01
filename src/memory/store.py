"""
Memory Store
=============

SQLite-backed persistent storage with sqlite-vec vector search and FTS5
full-text search. Single database file replaces the old JSONL + numpy system.

Features:
    - Atomic writes via SQLite transactions (no crash-safety gap)
    - On-disk vector index via sqlite-vec (no full RAM load)
    - Hybrid search: vector similarity + keyword via Reciprocal Rank Fusion
    - SQL filtering by persona, date, reflected status, etc.

Database file: {data_dir}/memory.db
"""

import asyncio
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import apsw
import sqlite_vec

from .models import Exchange, DailyReflection, UserRelationship, SessionState

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 768

# ── Vector serialization ─────────────────────────────────────────────

def _serialize_embedding(embedding: List[float]) -> bytes:
    """Pack a float list into bytes for sqlite-vec."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _deserialize_embedding(data: bytes) -> List[float]:
    """Unpack bytes from sqlite-vec into a float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


# ── Schema ───────────────────────────────────────────────────────────

_SCHEMA_SQL = """
-- Exchanges: turn pairs from all personas
CREATE TABLE IF NOT EXISTS exchanges (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    user_msg TEXT NOT NULL DEFAULT '',
    assistant_response TEXT NOT NULL DEFAULT '',
    persona_name TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT '',
    reflected INTEGER DEFAULT 0,
    participants TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_exchanges_persona ON exchanges(persona_name);
CREATE INDEX IF NOT EXISTS idx_exchanges_timestamp ON exchanges(timestamp);
CREATE INDEX IF NOT EXISTS idx_exchanges_reflected ON exchanges(reflected, persona_name);

-- Vector embeddings for exchanges
CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_vec USING vec0(
    id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);

-- Full-text search for exchanges
CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_fts USING fts5(
    user_msg,
    assistant_response,
    content=exchanges,
    content_rowid=rowid
);

-- Triggers to keep FTS in sync with exchanges table
CREATE TRIGGER IF NOT EXISTS exchanges_ai AFTER INSERT ON exchanges BEGIN
    INSERT INTO exchanges_fts(rowid, user_msg, assistant_response)
    VALUES (new.rowid, new.user_msg, new.assistant_response);
END;
CREATE TRIGGER IF NOT EXISTS exchanges_ad AFTER DELETE ON exchanges BEGIN
    INSERT INTO exchanges_fts(exchanges_fts, rowid, user_msg, assistant_response)
    VALUES ('delete', old.rowid, old.user_msg, old.assistant_response);
END;
CREATE TRIGGER IF NOT EXISTS exchanges_au AFTER UPDATE ON exchanges BEGIN
    INSERT INTO exchanges_fts(exchanges_fts, rowid, user_msg, assistant_response)
    VALUES ('delete', old.rowid, old.user_msg, old.assistant_response);
    INSERT INTO exchanges_fts(rowid, user_msg, assistant_response)
    VALUES (new.rowid, new.user_msg, new.assistant_response);
END;

-- Daily reflections
CREATE TABLE IF NOT EXISTS reflections (
    id TEXT PRIMARY KEY,
    persona_name TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    exchange_count INTEGER DEFAULT 0,
    exchange_ids TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT '',
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reflections_persona ON reflections(persona_name);
CREATE INDEX IF NOT EXISTS idx_reflections_date ON reflections(date);

CREATE VIRTUAL TABLE IF NOT EXISTS reflections_vec USING vec0(
    id TEXT PRIMARY KEY,
    embedding FLOAT[768]
);

CREATE VIRTUAL TABLE IF NOT EXISTS reflections_fts USING fts5(
    summary,
    content=reflections,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS reflections_ai AFTER INSERT ON reflections BEGIN
    INSERT INTO reflections_fts(rowid, summary)
    VALUES (new.rowid, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS reflections_ad AFTER DELETE ON reflections BEGIN
    INSERT INTO reflections_fts(reflections_fts, rowid, summary)
    VALUES ('delete', old.rowid, old.summary);
END;

-- User relationships
CREATE TABLE IF NOT EXISTS relationships (
    user_id TEXT PRIMARY KEY,
    id TEXT NOT NULL DEFAULT '',
    display_name TEXT DEFAULT '',
    total_exchanges INTEGER DEFAULT 0,
    trust_level REAL DEFAULT 0.0,
    relationship_type TEXT DEFAULT 'stranger',
    first_seen TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT '',
    metadata TEXT DEFAULT '{}'
);

-- Session state
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    id TEXT NOT NULL DEFAULT '',
    persona_name TEXT NOT NULL DEFAULT '',
    exchange_count INTEGER DEFAULT 0,
    topics_discussed TEXT DEFAULT '[]',
    emotional_arc TEXT DEFAULT '[]',
    started_at TEXT NOT NULL DEFAULT '',
    last_activity TEXT NOT NULL DEFAULT '',
    metadata TEXT DEFAULT '{}'
);
"""


def _split_schema(sql: str) -> List[str]:
    """
    Split a SQL schema string into individual statements.

    Handles CREATE TRIGGER blocks where semicolons appear inside
    BEGIN...END bodies.
    """
    statements = []
    current = []
    in_trigger = False

    for line in sql.splitlines():
        stripped = line.strip().upper()

        if stripped.startswith("CREATE TRIGGER"):
            in_trigger = True

        current.append(line)

        if in_trigger and stripped == "END;":
            statements.append("\n".join(current))
            current = []
            in_trigger = False
        elif not in_trigger and ";" in line:
            stmt = "\n".join(current).strip()
            if stmt and stmt != ";":
                statements.append(stmt)
            current = []

    # Handle any trailing content
    if current:
        stmt = "\n".join(current).strip()
        if stmt and stmt != ";":
            statements.append(stmt)

    return statements


class MemoryStore:
    """
    SQLite-backed memory store with vector + full-text search.

    Single database file holds all collections (exchanges, reflections,
    relationships, sessions) with sqlite-vec virtual tables for vector
    search and FTS5 for keyword search.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[apsw.Connection] = None
        self._initialized = False

    # ── Initialization ────────────────────────────────────────────

    async def initialize(self) -> None:
        """Open DB, load sqlite-vec extension, create schema."""
        if self._initialized:
            return
        await asyncio.to_thread(self._open_db)
        self._initialized = True

    def _open_db(self) -> None:
        """Synchronous DB setup."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        self._conn = apsw.Connection(self.db_path)

        # Load sqlite-vec extension
        self._conn.enable_load_extension(True)
        self._conn.load_extension(sqlite_vec.loadable_path())
        self._conn.enable_load_extension(False)

        # WAL mode for better concurrent read performance
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        # Create schema — executescript handles multiple statements including triggers
        for statement in _split_schema(_SCHEMA_SQL):
            self._conn.execute(statement)

        stats = self._get_stats_sync()
        logger.info(
            f"MemoryStore initialized at {self.db_path} "
            f"(exchanges={stats.get('exchanges', 0)}, "
            f"reflections={stats.get('reflections', 0)})"
        )

    # ── Exchange CRUD ─────────────────────────────────────────────

    async def append_exchange(self, exchange: Exchange) -> str:
        """Insert an exchange with its embedding and FTS entry atomically."""
        return await asyncio.to_thread(self._append_exchange_sync, exchange)

    def _append_exchange_sync(self, exchange: Exchange) -> str:
        d = exchange.to_dict()
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO exchanges
                   (id, session_id, user_msg, assistant_response, persona_name,
                    timestamp, reflected, participants, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d["id"], d["session_id"], d["user_msg"], d["assistant_response"],
                    d["persona_name"], d["timestamp"], int(d.get("reflected", False)),
                    json.dumps(d.get("participants", [])),
                    json.dumps(d.get("metadata", {})),
                ),
            )

            # Insert embedding into vec0 table
            if exchange.embedding:
                vec_data = _serialize_embedding(exchange.embedding)
                self._conn.execute(
                    "INSERT OR REPLACE INTO exchanges_vec(id, embedding) VALUES (?, ?)",
                    (d["id"], vec_data),
                )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return d["id"]

    async def update_exchange(self, record_id: str, updates: dict) -> bool:
        """Update fields on an existing exchange."""
        return await asyncio.to_thread(self._update_exchange_sync, record_id, updates)

    def _update_exchange_sync(self, record_id: str, updates: dict) -> bool:
        # Build SET clause from updates
        allowed = {
            "reflected", "user_msg", "assistant_response", "persona_name",
            "session_id", "timestamp", "participants", "metadata",
        }
        sets = []
        values = []
        for key, val in updates.items():
            if key not in allowed:
                continue
            if key in ("participants", "metadata"):
                val = json.dumps(val)
            elif key == "reflected":
                val = int(val)
            sets.append(f"{key} = ?")
            values.append(val)

        if not sets:
            return False

        values.append(record_id)
        rows_changed = self._conn.execute(
            f"UPDATE exchanges SET {', '.join(sets)} WHERE id = ?",
            tuple(values),
        )
        return self._conn.changes() > 0

    async def delete_exchange(self, record_id: str) -> bool:
        """Delete an exchange and its vector/FTS entries."""
        return await asyncio.to_thread(self._delete_exchange_sync, record_id)

    def _delete_exchange_sync(self, record_id: str) -> bool:
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM exchanges WHERE id = ?", (record_id,))
            deleted = self._conn.changes() > 0
            if deleted:
                self._conn.execute(
                    "DELETE FROM exchanges_vec WHERE id = ?", (record_id,)
                )
            self._conn.execute("COMMIT")
            return deleted
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ── Reflection CRUD ───────────────────────────────────────────

    async def append_reflection(self, reflection: DailyReflection) -> str:
        """Insert a daily reflection with embedding and FTS entry."""
        return await asyncio.to_thread(self._append_reflection_sync, reflection)

    def _append_reflection_sync(self, reflection: DailyReflection) -> str:
        d = reflection.to_dict()
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO reflections
                   (id, persona_name, date, summary, exchange_count,
                    exchange_ids, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d["id"], d["persona_name"], d["date"], d["summary"],
                    d["exchange_count"], json.dumps(d.get("exchange_ids", [])),
                    d["created_at"], json.dumps(d.get("metadata", {})),
                ),
            )

            if reflection.embedding:
                vec_data = _serialize_embedding(reflection.embedding)
                self._conn.execute(
                    "INSERT OR REPLACE INTO reflections_vec(id, embedding) VALUES (?, ?)",
                    (d["id"], vec_data),
                )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

        return d["id"]

    # ── Search ────────────────────────────────────────────────────

    async def search_exchanges(
        self,
        query_embedding: List[float],
        query_text: str = "",
        top_k: int = 15,
        persona_filter: Optional[str] = None,
        hybrid: bool = True,
        min_similarity: float = 0.0,
        exclude_ids: Optional[Set[str]] = None,
    ) -> List[Tuple[dict, float]]:
        """
        Search exchanges via vector similarity, keyword search, or both.

        When hybrid=True, combines vector + FTS5 results via Reciprocal
        Rank Fusion (RRF). Returns list of (record_dict, score) sorted
        by score descending.
        """
        return await asyncio.to_thread(
            self._search_exchanges_sync,
            query_embedding, query_text, top_k, persona_filter,
            hybrid, min_similarity, exclude_ids,
        )

    def _search_exchanges_sync(
        self,
        query_embedding: List[float],
        query_text: str,
        top_k: int,
        persona_filter: Optional[str],
        hybrid: bool,
        min_similarity: float,
        exclude_ids: Optional[Set[str]],
    ) -> List[Tuple[dict, float]]:
        exclude_ids = exclude_ids or set()
        vec_data = _serialize_embedding(query_embedding)

        # Fetch more than top_k from vector search to allow for filtering
        fetch_k = top_k * 3 if persona_filter else top_k + 5

        # Vector search
        vec_results = self._conn.execute(
            """SELECT id, distance
               FROM exchanges_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (vec_data, fetch_k),
        ).fetchall()

        # Build RRF scores from vector results
        scores: Dict[str, float] = {}
        k_rrf = 60  # RRF constant

        for rank, (rid, distance) in enumerate(vec_results):
            if rid in exclude_ids:
                continue
            scores[rid] = 1.0 / (k_rrf + rank + 1)

        # FTS keyword search (if hybrid and query_text is usable)
        if hybrid and query_text and len(query_text.strip()) > 2:
            fts_query = self._sanitize_fts_query(query_text)
            if fts_query:
                try:
                    fts_rows = self._conn.execute(
                        """SELECT e.id
                           FROM exchanges_fts f
                           JOIN exchanges e ON e.rowid = f.rowid
                           WHERE exchanges_fts MATCH ?
                           LIMIT ?""",
                        (fts_query, fetch_k),
                    ).fetchall()

                    for rank, (rid,) in enumerate(fts_rows):
                        if rid in exclude_ids:
                            continue
                        scores[rid] = scores.get(rid, 0) + 1.0 / (k_rrf + rank + 1)
                except apsw.SQLError:
                    # FTS query syntax error — fall back to vector-only
                    pass

        if not scores:
            return []

        # Sort by combined score
        ranked_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        # Hydrate with full records, applying persona filter
        results = []
        for rid in ranked_ids:
            row = self._conn.execute(
                "SELECT * FROM exchanges WHERE id = ?", (rid,)
            ).fetchone()
            if row is None:
                continue

            record = self._row_to_exchange_dict(row)

            if persona_filter and record.get("persona_name") != persona_filter:
                continue

            results.append((record, scores[rid]))
            if len(results) >= top_k:
                break

        return results

    async def search_reflections(
        self,
        query_embedding: List[float],
        query_text: str = "",
        top_k: int = 5,
        persona_filter: Optional[str] = None,
        hybrid: bool = True,
    ) -> List[Tuple[dict, float]]:
        """Search reflections via vector + optional FTS."""
        return await asyncio.to_thread(
            self._search_reflections_sync,
            query_embedding, query_text, top_k, persona_filter, hybrid,
        )

    def _search_reflections_sync(
        self,
        query_embedding: List[float],
        query_text: str,
        top_k: int,
        persona_filter: Optional[str],
        hybrid: bool,
    ) -> List[Tuple[dict, float]]:
        vec_data = _serialize_embedding(query_embedding)
        fetch_k = top_k * 3 if persona_filter else top_k + 5

        vec_results = self._conn.execute(
            """SELECT id, distance
               FROM reflections_vec
               WHERE embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (vec_data, fetch_k),
        ).fetchall()

        scores: Dict[str, float] = {}
        k_rrf = 60

        for rank, (rid, distance) in enumerate(vec_results):
            scores[rid] = 1.0 / (k_rrf + rank + 1)

        if hybrid and query_text and len(query_text.strip()) > 2:
            fts_query = self._sanitize_fts_query(query_text)
            if fts_query:
                try:
                    fts_rows = self._conn.execute(
                        """SELECT r.id
                           FROM reflections_fts f
                           JOIN reflections r ON r.rowid = f.rowid
                           WHERE reflections_fts MATCH ?
                           LIMIT ?""",
                        (fts_query, fetch_k),
                    ).fetchall()

                    for rank, (rid,) in enumerate(fts_rows):
                        scores[rid] = scores.get(rid, 0) + 1.0 / (k_rrf + rank + 1)
                except apsw.SQLError:
                    pass

        if not scores:
            return []

        ranked_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        results = []
        for rid in ranked_ids:
            row = self._conn.execute(
                "SELECT * FROM reflections WHERE id = ?", (rid,)
            ).fetchone()
            if row is None:
                continue

            record = self._row_to_reflection_dict(row)

            if persona_filter and record.get("persona_name") != persona_filter:
                continue

            results.append((record, scores[rid]))
            if len(results) >= top_k:
                break

        return results

    # ── Query Helpers ─────────────────────────────────────────────

    async def get_unreflected_exchanges(
        self,
        persona_name: Optional[str] = None,
        date: Optional[str] = None,
    ) -> List[dict]:
        """Get exchanges not yet included in a daily reflection."""
        return await asyncio.to_thread(
            self._get_unreflected_sync, persona_name, date
        )

    def _get_unreflected_sync(
        self, persona_name: Optional[str], date: Optional[str],
    ) -> List[dict]:
        sql = "SELECT * FROM exchanges WHERE reflected = 0"
        params = []

        if persona_name:
            sql += " AND persona_name = ?"
            params.append(persona_name)
        if date:
            sql += " AND timestamp LIKE ?"
            params.append(f"{date}%")

        sql += " ORDER BY timestamp"
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_exchange_dict(r) for r in rows]

    async def get_exchanges_by_persona(
        self, persona_name: str, limit: int = 100,
    ) -> List[dict]:
        """Get recent exchanges for a specific persona."""
        return await asyncio.to_thread(
            self._get_exchanges_by_persona_sync, persona_name, limit
        )

    def _get_exchanges_by_persona_sync(
        self, persona_name: str, limit: int,
    ) -> List[dict]:
        rows = self._conn.execute(
            """SELECT * FROM exchanges
               WHERE persona_name = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (persona_name, limit),
        ).fetchall()
        return [self._row_to_exchange_dict(r) for r in rows]

    async def get_reflections_by_persona(
        self, persona_name: str, limit: int = 30,
    ) -> List[dict]:
        """Get recent daily reflections for a persona."""
        return await asyncio.to_thread(
            self._get_reflections_by_persona_sync, persona_name, limit
        )

    def _get_reflections_by_persona_sync(
        self, persona_name: str, limit: int,
    ) -> List[dict]:
        rows = self._conn.execute(
            """SELECT * FROM reflections
               WHERE persona_name = ?
               ORDER BY date DESC LIMIT ?""",
            (persona_name, limit),
        ).fetchall()
        return [self._row_to_reflection_dict(r) for r in rows]

    def count(self, table: str) -> int:
        """Count records in a table."""
        if table not in ("exchanges", "reflections", "relationships", "sessions"):
            return 0
        row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0] if row else 0

    async def find_exchange_by_id(self, record_id: str) -> Optional[dict]:
        """Lookup a single exchange by ID."""
        return await asyncio.to_thread(self._find_exchange_by_id_sync, record_id)

    def _find_exchange_by_id_sync(self, record_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM exchanges WHERE id = ?", (record_id,)
        ).fetchone()
        return self._row_to_exchange_dict(row) if row else None

    # ── Relationships ─────────────────────────────────────────────

    async def get_relationship(self, user_id: str) -> Optional[dict]:
        """Load a user relationship."""
        return await asyncio.to_thread(self._get_relationship_sync, user_id)

    def _get_relationship_sync(self, user_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM relationships WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_relationship_dict(row)

    async def save_relationship(self, user_id: str, relationship) -> None:
        """Upsert a user relationship."""
        await asyncio.to_thread(self._save_relationship_sync, user_id, relationship)

    def _save_relationship_sync(self, user_id: str, relationship) -> None:
        data = relationship.to_dict() if hasattr(relationship, "to_dict") else relationship
        self._conn.execute(
            """INSERT OR REPLACE INTO relationships
               (user_id, id, display_name, total_exchanges, trust_level,
                relationship_type, first_seen, last_seen, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, data.get("id", ""), data.get("display_name", ""),
                data.get("total_exchanges", 0), data.get("trust_level", 0.0),
                data.get("relationship_type", "stranger"),
                data.get("first_seen", ""), data.get("last_seen", ""),
                json.dumps(data.get("metadata", {})),
            ),
        )

    async def get_all_relationships(self) -> List[dict]:
        """Load all user relationships."""
        return await asyncio.to_thread(self._get_all_relationships_sync)

    def _get_all_relationships_sync(self) -> List[dict]:
        rows = self._conn.execute("SELECT * FROM relationships").fetchall()
        return [self._row_to_relationship_dict(r) for r in rows]

    # ── Sessions ──────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    def _get_session_sync(self, session_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_session_dict(row)

    async def save_session(self, session_id: str, state) -> None:
        await asyncio.to_thread(self._save_session_sync, session_id, state)

    def _save_session_sync(self, session_id: str, state) -> None:
        data = state.to_dict() if hasattr(state, "to_dict") else state
        self._conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, id, persona_name, exchange_count, topics_discussed,
                emotional_arc, started_at, last_activity, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, data.get("id", ""), data.get("persona_name", ""),
                data.get("exchange_count", 0),
                json.dumps(data.get("topics_discussed", [])),
                json.dumps(data.get("emotional_arc", [])),
                data.get("started_at", ""), data.get("last_activity", ""),
                json.dumps(data.get("metadata", {})),
            ),
        )

    async def list_sessions(self, persona_name: Optional[str] = None) -> List[str]:
        return await asyncio.to_thread(self._list_sessions_sync, persona_name)

    def _list_sessions_sync(self, persona_name: Optional[str]) -> List[str]:
        if persona_name:
            rows = self._conn.execute(
                "SELECT session_id FROM sessions WHERE persona_name = ?",
                (persona_name,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT session_id FROM sessions").fetchall()
        return [r[0] for r in rows]

    # ── Lifecycle ─────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            self._initialized = False
        logger.info("MemoryStore shutdown complete")

    def stats(self) -> Dict[str, Any]:
        """Return storage statistics."""
        return self._get_stats_sync()

    def _get_stats_sync(self) -> Dict[str, Any]:
        result = {}
        for table in ("exchanges", "reflections", "relationships", "sessions"):
            try:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()
                result[table] = row[0] if row else 0
            except apsw.SQLError:
                result[table] = 0

        # Vector counts
        for table in ("exchanges_vec", "reflections_vec"):
            try:
                row = self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()
                result[f"{table}"] = row[0] if row else 0
            except apsw.SQLError:
                result[table] = 0

        return result

    # ── Row Conversion Helpers ────────────────────────────────────

    def _row_to_exchange_dict(self, row: tuple) -> dict:
        """Convert a raw SQLite row to an exchange dict."""
        # Column order matches CREATE TABLE: id, session_id, user_msg,
        # assistant_response, persona_name, timestamp, reflected, participants, metadata
        return {
            "id": row[0],
            "session_id": row[1],
            "user_msg": row[2],
            "assistant_response": row[3],
            "persona_name": row[4],
            "timestamp": row[5],
            "reflected": bool(row[6]),
            "participants": json.loads(row[7]) if row[7] else [],
            "metadata": json.loads(row[8]) if row[8] else {},
        }

    def _row_to_reflection_dict(self, row: tuple) -> dict:
        """Convert a raw SQLite row to a reflection dict."""
        return {
            "id": row[0],
            "persona_name": row[1],
            "date": row[2],
            "summary": row[3],
            "exchange_count": row[4],
            "exchange_ids": json.loads(row[5]) if row[5] else [],
            "created_at": row[6],
            "metadata": json.loads(row[7]) if row[7] else {},
        }

    def _row_to_relationship_dict(self, row: tuple) -> dict:
        """Convert a raw SQLite row to a relationship dict."""
        return {
            "user_id": row[0],
            "id": row[1],
            "display_name": row[2],
            "total_exchanges": row[3],
            "trust_level": row[4],
            "relationship_type": row[5],
            "first_seen": row[6],
            "last_seen": row[7],
            "metadata": json.loads(row[8]) if row[8] else {},
        }

    def _row_to_session_dict(self, row: tuple) -> dict:
        """Convert a raw SQLite row to a session dict."""
        return {
            "session_id": row[0],
            "id": row[1],
            "persona_name": row[2],
            "exchange_count": row[3],
            "topics_discussed": json.loads(row[4]) if row[4] else [],
            "emotional_arc": json.loads(row[5]) if row[5] else [],
            "started_at": row[6],
            "last_activity": row[7],
            "metadata": json.loads(row[8]) if row[8] else {},
        }

    @staticmethod
    def _sanitize_fts_query(text: str) -> str:
        """
        Sanitize user text for FTS5 MATCH syntax.

        Wraps each word in quotes to avoid FTS5 syntax errors from
        special characters. Drops words that are too short.
        """
        words = []
        for word in text.split():
            # Strip non-alphanumeric from edges
            cleaned = word.strip("\"'`~!@#$%^&*()[]{}|\\:;<>,./?\n\t")
            if len(cleaned) >= 2:
                # Escape any internal quotes
                cleaned = cleaned.replace('"', '""')
                words.append(f'"{cleaned}"')
        return " OR ".join(words) if words else ""


# ── Singleton Access ──────────────────────────────────────────────────

_store: Optional[MemoryStore] = None
_store_lock = asyncio.Lock()


async def get_store(config_or_persona: Any = None, base_dir: str = "./data", **kwargs) -> MemoryStore:
    """
    Get or create the singleton MemoryStore.

    Accepts either:
        get_store(config_dict)          — new style
        get_store(persona_name, base_dir)  — old style (backward compat)
    """
    global _store

    if _store is not None and _store._initialized:
        return _store

    async with _store_lock:
        if _store is not None and _store._initialized:
            return _store

        # Resolve data dir
        if isinstance(config_or_persona, dict):
            data_dir = config_or_persona.get("memory", {}).get("data_dir", "./data")
        else:
            data_dir = base_dir

        db_path = str(Path(data_dir) / "memory.db")
        _store = MemoryStore(db_path)
        await _store.initialize()
        return _store


async def shutdown_all_stores() -> None:
    """Shutdown the memory store."""
    global _store
    if _store:
        await _store.shutdown()
        _store = None
    logger.info("MemoryStore shut down")
