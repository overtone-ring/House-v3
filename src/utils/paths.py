"""
Path Utilities
==============

Centralized project root resolution. Used everywhere that needs to
locate data/, config/, or other project-relative paths.
"""

from pathlib import Path

_project_root: Path = None


def get_project_root() -> Path:
    """Find the project root (directory containing config/)."""
    global _project_root
    if _project_root is not None:
        return _project_root

    # Walk up from this file: src/utils/paths.py -> src/utils -> src -> project_root
    candidate = Path(__file__).parent.parent.parent
    if (candidate / "config").exists():
        _project_root = candidate
        return _project_root

    # Fallback: current working directory
    cwd = Path.cwd()
    if (cwd / "config").exists():
        _project_root = cwd
        return _project_root

    _project_root = candidate
    return _project_root
