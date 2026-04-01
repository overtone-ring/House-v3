"""
Configuration Loader
====================

Loads and merges YAML configuration from multiple sources:
    1. config/default.yaml  (shipped defaults)
    2. config/local.yaml    (user overrides, .gitignored)
    3. Environment variables (HOUSE_ prefix)

Values are deep-merged: dicts are merged recursively, everything else
is replaced by the override.

Ported from v2 with:
    - Single-file defaults + local override (replaces 3-file merge)
    - Environment variable support
    - Explicit project root resolution
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_config: Optional[Dict[str, Any]] = None

# Config files loaded in order (later overrides earlier)
CONFIG_FILES = ["config/default.yaml", "config/local.yaml"]


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursively merge override into base. Dicts are merged; others are replaced."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_project_root() -> Path:
    """Find the project root (directory containing config/)."""
    from .paths import get_project_root
    return get_project_root()


def _apply_env_overrides(config: Dict) -> Dict:
    """
    Apply environment variable overrides.

    Convention: HOUSE_SECTION_KEY maps to config[section][key]
    Example: HOUSE_PROVIDER_API_KEY -> config["provider"]["api_key"]
    """
    prefix = "HOUSE_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue

        parts = key[len(prefix):].lower().split("_", 1)
        if len(parts) == 2:
            section, setting = parts
            if section in config and isinstance(config[section], dict):
                config[section][setting] = value
                logger.debug(f"Applied env override: {key} -> {section}.{setting}")

    # Also check for .env file
    project_root = _resolve_project_root()
    env_file = project_root / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
        except OSError:
            pass

    return config


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load configuration from YAML files.

    Args:
        path: Optional explicit path to a config file.
              If provided, loads only that file (no merging).

    Returns:
        Merged configuration dict.
    """
    global _config

    if path:
        with open(path) as f:
            _config = yaml.safe_load(f) or {}
        return _config

    project_root = _resolve_project_root()
    config = {}

    for config_file in CONFIG_FILES:
        filepath = project_root / config_file
        if filepath.exists():
            try:
                with open(filepath) as f:
                    data = yaml.safe_load(f) or {}
                config = _deep_merge(config, data)
                logger.debug(f"Loaded config: {filepath}")
            except (yaml.YAMLError, OSError) as e:
                logger.warning(f"Failed to load {filepath}: {e}")

    config = _apply_env_overrides(config)
    _config = config
    return config


def reload_config() -> Dict[str, Any]:
    """Force reload configuration."""
    global _config
    _config = None
    return load_config()


def get_config() -> Dict[str, Any]:
    """Get cached configuration (loads on first call)."""
    if _config is None:
        return load_config()
    return _config


def get_persona_names() -> List[str]:
    """Get list of configured persona names."""
    config = get_config()
    return config.get("personas", [])


def get_persona_config(persona_name: str) -> Dict:
    """Get persona-specific config overrides."""
    config = get_config()
    persona_configs = config.get("persona_config", {})
    return persona_configs.get(persona_name, {})
