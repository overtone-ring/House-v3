"""
I/O Utilities
=============

Shared JSON read/write with atomic writes.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically via tmp + rename."""
    os.makedirs(path.parent, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


def read_json(path: Path, default: Optional[Dict] = None) -> Optional[Dict]:
    """Read JSON with error handling. Returns default if file missing or corrupt."""
    if not path.exists():
        return dict(default) if default else None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {path}: {e}")
        return dict(default) if default else None
