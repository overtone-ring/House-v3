"""
Dashboard Server
================

Small aiohttp web server that runs inside the bot process and serves a
read-only dashboard at http://<host>:<port>/ (config block: `dashboard`).

It runs in-process because the most useful state — per-channel queues,
locks, cooldowns, conversation buffers — exists only in the running
Watcher's memory. Everything else comes from the wire log (3-day detail)
and memory.db (permanent history).

Endpoints (all JSON except /):
    /            — the single-page UI
    /api/live    — watched channels, queues, cooldowns, buffers, uptime
    /api/events  — wire.jsonl events grouped per message (paginated)
    /api/stats   — messages/day from the DB + search rate, latency,
                   tokens from the wire log window
    /api/users   — per-user activity from exchange metadata
    /api/health  — recent errors, reflection status, backlog

No new dependencies: aiohttp ships with discord.py.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class Dashboard:
    """Read-only dashboard over the live watcher, wire log, and memory DB."""

    def __init__(self, config: dict, watcher: Any, house: Any):
        self._config = config or {}
        self._watcher = watcher
        self._house = house
        self._started_at = time.monotonic()
        self._runner: Optional[web.AppRunner] = None

        log_cfg = self._config.get("logging", {})
        self._log_dir = self._resolve(log_cfg.get("log_dir", "./logs"))
        self._db_path = self._resolve(
            self._config.get("memory", {}).get("data_dir", "./data")
        ) / "memory.db"

    @staticmethod
    def _resolve(p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            from ..utils.paths import get_project_root
            path = get_project_root() / path
        return path

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        dash_cfg = self._config.get("dashboard", {})
        host = dash_cfg.get("host", "127.0.0.1")
        port = int(dash_cfg.get("port", 8765))

        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/live", self._api_live)
        app.router.add_get("/api/events", self._api_events)
        app.router.add_get("/api/stats", self._api_stats)
        app.router.add_get("/api/users", self._api_users)
        app.router.add_get("/api/health", self._api_health)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=host, port=port)
        await site.start()
        logger.info(f"[Dashboard] Serving on http://{host}:{port}/")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ── Pages ────────────────────────────────────────────────────

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")

    # ── Live state (in-memory, watcher internals) ────────────────

    async def _api_live(self, request: web.Request) -> web.Response:
        w = self._watcher
        now = time.monotonic()

        channels = []
        for cid in sorted(getattr(w, "_watched_channel_ids", set())):
            ch = w.get_channel(cid)
            lock = getattr(w, "_channel_locks", {}).get(cid)
            buf = getattr(w, "_buffers", {}).get(cid)
            channels.append({
                "id": str(cid),
                "name": f"#{ch.name}" if ch else "(unresolved)",
                "guild": ch.guild.name if ch and ch.guild else None,
                "processing": bool(lock and lock.locked()),
                "queued": getattr(w, "_queued_count", {}).get(cid, 0),
                "buffer_turns": len(buf._turns) if buf else 0,
            })

        cooldown_s = getattr(w, "_user_cooldown_seconds", 0)
        cooldowns = []
        for uid, last in list(getattr(w, "_last_trigger_at", {}).items()):
            remaining = cooldown_s - (now - last)
            if remaining > 0:
                user = w.get_user(uid)
                cooldowns.append({
                    "user": user.display_name if user else str(uid),
                    "remaining_s": round(remaining, 1),
                })

        return web.json_response({
            "uptime_s": round(now - self._started_at),
            "ready": w.is_ready() if hasattr(w, "is_ready") else None,
            "channels": channels,
            "cooldowns": cooldowns,
            "pending_memory_writes": len(getattr(self._house, "_pending_tasks", [])),
        })

    # ── Wire log access ──────────────────────────────────────────

    def _read_wire_events(self) -> List[Dict]:
        """All events from wire.jsonl + rotated siblings, oldest→newest."""
        files = sorted(self._log_dir.glob("wire.jsonl*"))
        # Rotated files (wire.jsonl.YYYY-MM-DD) sort before the live file
        # alphabetically, which is also chronological. Keep that order.
        files = [f for f in files if f.name != "wire.jsonl"] + (
            [self._log_dir / "wire.jsonl"]
            if (self._log_dir / "wire.jsonl").exists() else []
        )
        events = []
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
        return events

    async def _api_events(self, request: web.Request) -> web.Response:
        page = int(request.query.get("page", 0))
        page_size = min(int(request.query.get("page_size", 20)), 100)

        events = await asyncio.to_thread(self._read_wire_events)

        # Group one message's pipeline events by request_id; events without
        # one (reflections, startup work) stand alone.
        groups: Dict[str, List[Dict]] = defaultdict(list)
        order: List[str] = []
        solo_counter = 0
        for ev in events:
            rid = ev.get("request_id")
            if not rid:
                rid = f"solo-{solo_counter}"
                solo_counter += 1
            if rid not in groups:
                order.append(rid)
            groups[rid].append(ev)

        order.reverse()  # newest message first
        total = len(order)
        page_ids = order[page * page_size:(page + 1) * page_size]

        entries = []
        for rid in page_ids:
            evs = groups[rid]
            entry: Dict[str, Any] = {
                "request_id": rid if not rid.startswith("solo-") else None,
                "ts": evs[0].get("ts"),
                "events": evs,
            }
            for ev in evs:
                if ev["event"] == "scene":
                    entry["user_input"] = ev.get("user_input")
                    entry["turns"] = len(ev.get("turns", []))
                    entry["personas"] = list(dict.fromkeys(
                        t.get("persona") for t in ev.get("turns", [])
                    ))
                    entry["session_id"] = ev.get("session_id")
                elif ev["event"] == "memory_search":
                    entry["search_skipped"] = ev.get("skipped")
                    entry["memories_found"] = len(ev.get("results") or [])
                elif ev["event"] == "llm_call":
                    entry["latency_ms"] = ev.get("latency_ms")
                    usage = (ev.get("response") or {}).get("usage") or {}
                    entry["total_tokens"] = usage.get("total_tokens")
                    entry["model"] = (ev.get("request") or {}).get("model")
                elif ev["event"] == "llm_error":
                    entry["error"] = ev.get("category")
            entries.append(entry)

        return web.json_response({
            "total": total, "page": page, "page_size": page_size,
            "entries": entries,
        })

    # ── DB access (read-only, in a thread) ───────────────────────

    def _db_query(self, sql: str, params: tuple = ()) -> List[tuple]:
        import apsw
        if not self._db_path.exists():
            return []
        con = apsw.Connection(str(self._db_path), flags=apsw.SQLITE_OPEN_READONLY)
        try:
            return list(con.cursor().execute(sql, params))
        finally:
            con.close()

    async def _api_stats(self, request: web.Request) -> web.Response:
        days = min(int(request.query.get("days", 30)), 365)

        # Permanent history from the DB. Exchanges are one row per persona
        # per message, so distinct (session, user_msg) per day ≈ messages.
        per_day = await asyncio.to_thread(
            self._db_query,
            """SELECT substr(timestamp, 1, 10) AS day,
                      COUNT(DISTINCT session_id || '|' || user_msg) AS messages,
                      COUNT(*) AS persona_responses
               FROM exchanges
               WHERE timestamp >= date('now', ?)
               GROUP BY day ORDER BY day""",
            (f"-{days} days",),
        )
        per_persona = await asyncio.to_thread(
            self._db_query,
            """SELECT persona_name, COUNT(*) FROM exchanges
               WHERE timestamp >= date('now', ?)
               GROUP BY persona_name ORDER BY 2 DESC""",
            (f"-{days} days",),
        )

        # Wire-log window (search rate, latency, tokens) — detail for the
        # retention window only; the DB carries the long history.
        events = await asyncio.to_thread(self._read_wire_events)
        searches = skips = 0
        latencies: List[float] = []
        tokens_in = tokens_out = 0
        for ev in events:
            if ev["event"] == "memory_search":
                if ev.get("skipped"):
                    skips += 1
                else:
                    searches += 1
            elif ev["event"] == "llm_call":
                if ev.get("latency_ms") is not None:
                    latencies.append(ev["latency_ms"])
                usage = (ev.get("response") or {}).get("usage") or {}
                tokens_in += usage.get("prompt_tokens") or 0
                tokens_out += usage.get("completion_tokens") or 0

        latencies.sort()
        return web.json_response({
            "per_day": [
                {"day": d, "messages": m, "persona_responses": r}
                for d, m, r in per_day
            ],
            "per_persona": [{"persona": p, "responses": n} for p, n in per_persona],
            "wire_window": {
                "memory_searches": searches,
                "search_skips": skips,
                "llm_calls": len(latencies),
                "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
                "p95_latency_ms": latencies[int(len(latencies) * 0.95)] if latencies else None,
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
            },
        })

    async def _api_users(self, request: web.Request) -> web.Response:
        rows = await asyncio.to_thread(
            self._db_query,
            """SELECT metadata, user_msg, timestamp, session_id FROM exchanges
               WHERE metadata LIKE '%user_id%' ORDER BY timestamp""",
        )
        users: Dict[str, Dict[str, Any]] = {}
        seen_msgs = set()
        for metadata, user_msg, ts, session_id in rows:
            try:
                uid = json.loads(metadata or "{}").get("user_id")
            except json.JSONDecodeError:
                uid = None
            if not uid:
                continue
            # One row per persona per message — dedupe to count messages.
            msg_key = f"{session_id}|{user_msg}"
            u = users.setdefault(uid, {"user_id": uid, "name": None,
                                       "messages": 0, "last_seen": None})
            if msg_key not in seen_msgs:
                seen_msgs.add(msg_key)
                u["messages"] += 1
            u["last_seen"] = ts
            # Stored text is "[Name]: message" — recover the display name.
            if user_msg.startswith("[") and "]:" in user_msg[:80]:
                u["name"] = user_msg[1:user_msg.index("]:")]

        ranked = sorted(users.values(), key=lambda u: u["messages"], reverse=True)
        return web.json_response({"users": ranked})

    async def _api_health(self, request: web.Request) -> web.Response:
        reflections = await asyncio.to_thread(
            self._db_query,
            "SELECT persona_name, MAX(date) FROM reflections GROUP BY persona_name",
        )
        backlog = await asyncio.to_thread(
            self._db_query,
            "SELECT COUNT(*) FROM exchanges WHERE reflected = 0",
        )

        events = await asyncio.to_thread(self._read_wire_events)
        errors = [
            {"ts": ev.get("ts"), "category": ev.get("category"),
             "error": (ev.get("error") or "")[:300]}
            for ev in events if ev["event"] == "llm_error"
        ][-20:]
        errors.reverse()

        return web.json_response({
            "reflections": [{"persona": p, "last_date": d} for p, d in reflections],
            "unreflected_exchanges": backlog[0][0] if backlog else 0,
            "recent_errors": errors,
        })


async def start_dashboard(config: dict, watcher: Any, house: Any) -> Optional[Dashboard]:
    """Start the dashboard. Failure-soft: the bot must run without it."""
    if not config.get("dashboard", {}).get("enabled", False):
        return None
    try:
        dash = Dashboard(config, watcher, house)
        await dash.start()
        return dash
    except Exception as e:
        logger.error(f"[Dashboard] Failed to start (continuing without): {e}")
        return None
