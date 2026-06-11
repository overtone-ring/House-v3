"""
File Logging + Wire Tap
=======================

Two rotating file logs under `logging.log_dir`, both rolled at midnight
and kept for `logging.retention_days` days:

- house.log  — mirror of everything the console prints
- wire.jsonl — one JSON object per line: the exact API request payload,
               the full raw model response, memory-search activity with
               results, and the parsed scene

The wire log exists so any exchange can be reconstructed after the fact:
what was sent, what came back, and what the pipeline made of it.

setup_logging() is called once from the runner after config loads.
wire_record() is thread-safe (the logging module locks internally) and
never raises — a logging failure must never take down a live response.
"""

import contextvars
import json
import logging
import uuid
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

_wire_logger = logging.getLogger("house.wire")
_wire_logger.propagate = False  # JSON lines stay out of console/house.log
_wire_enabled = False

# Correlates the events of one pipeline run (memory_search, llm_call,
# scene) so a log viewer can group them. Set once per incoming message;
# contextvars propagate through asyncio.to_thread, so the provider call
# inherits it without plumbing.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "wire_request_id", default=""
)


def new_request_id() -> str:
    """Start a new correlation scope. Returns the id (also auto-attached
    to every wire_record until the next call)."""
    rid = uuid.uuid4().hex[:12]
    _request_id.set(rid)
    return rid

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(config: dict) -> None:
    """Attach the rotating file handlers based on the logging config block."""
    global _wire_enabled

    log_cfg = (config or {}).get("logging", {})
    log_dir = Path(log_cfg.get("log_dir", "./logs"))
    if not log_dir.is_absolute():
        from .paths import get_project_root
        log_dir = get_project_root() / log_dir
    retention = int(log_cfg.get("retention_days", 3))
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)

    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = TimedRotatingFileHandler(
        log_dir / "house.log", when="midnight", backupCount=retention,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    if log_cfg.get("wire_tap", False):
        wire_handler = TimedRotatingFileHandler(
            log_dir / "wire.jsonl", when="midnight", backupCount=retention,
            encoding="utf-8",
        )
        wire_handler.setFormatter(logging.Formatter("%(message)s"))
        _wire_logger.addHandler(wire_handler)
        _wire_logger.setLevel(logging.INFO)
        _wire_enabled = True

    logging.getLogger(__name__).info(
        f"File logging → {log_dir} (rotate at midnight, keep {retention} days, "
        f"wire_tap={'on' if _wire_enabled else 'off'})"
    )


def wire_record(event: str, **fields: Any) -> None:
    """Write one JSON line to wire.jsonl. No-op when wire_tap is off."""
    if not _wire_enabled:
        return
    try:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
        rid = _request_id.get()
        if rid:
            record["request_id"] = rid
        record.update(fields)
        _wire_logger.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        logging.getLogger(__name__).warning("wire_record failed", exc_info=True)
