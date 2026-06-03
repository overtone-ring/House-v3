"""
Provider Registry
=================

Simple registry mapping provider names to classes.
"""

import logging
import os
from typing import Dict, List, Optional, Type

from .base import BaseProvider, ProviderConfig

logger = logging.getLogger(__name__)

# ── Registry ──────────────────────────────────────────────────────────

def _build_registry() -> Dict[str, Type[BaseProvider]]:
    """Build the provider registry. Import here to avoid circular imports."""
    from .openrouter_provider import OpenRouterProvider
    return {
        "openrouter": OpenRouterProvider,
    }


_registry: Optional[Dict[str, Type[BaseProvider]]] = None


def _get_registry() -> Dict[str, Type[BaseProvider]]:
    global _registry
    if _registry is None:
        _registry = _build_registry()
    return _registry


def get_provider_registry() -> Dict[str, Type[BaseProvider]]:
    """Return a copy of the current registry."""
    return dict(_get_registry())


def create_provider(
    provider_name: str,
    model: str,
    api_key: str = "",
    base_url: Optional[str] = None,
    max_tokens: int = 8192,
    temperature: float = 0.7,
    **extra,
) -> BaseProvider:
    """
    Create a provider instance by name.

    Args:
        provider_name: Provider name (e.g. "openrouter")
        model: Model identifier (e.g. "anthropic/claude-sonnet-4")
        api_key: API key for the provider
        base_url: Optional custom endpoint URL
        max_tokens: Default max output tokens
        temperature: Default sampling temperature
        **extra: Provider-specific config passed through

    Returns:
        Configured BaseProvider instance

    Raises:
        ValueError: If provider_name is not registered
    """
    registry = _get_registry()

    if provider_name not in registry:
        available = ", ".join(sorted(registry.keys()))
        raise ValueError(
            f"Unknown provider '{provider_name}'. Available: [{available}]"
        )

    config = ProviderConfig(
        provider_name=provider_name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        temperature=temperature,
        extra=extra,
    )

    cls = registry[provider_name]
    instance = cls(config)

    # Run validation
    warnings = instance.validate_config()
    for w in warnings:
        logger.warning(w)

    return instance


def create_provider_from_config(config: dict) -> BaseProvider:
    """
    Create a provider from a config dict (e.g. loaded from YAML).

    Expected config shape:
        provider: openrouter
        model: anthropic/claude-sonnet-4
        api_key: sk-or-...
        max_tokens: 8192
        temperature: 0.7

    Any extra keys are passed through as provider-specific config.
    """
    # Copy first — this function pops keys, and callers may pass a live config.
    config = dict(config)

    # ── Env var fallback per provider ────────────────────────
    ENV_KEY_MAP = {
        "openrouter": "OPENROUTER_API_KEY",
        "anthropic":  "ANTHROPIC_API_KEY",
        "google":     "GOOGLE_API_KEY",
        "deepseek":   "DEEPSEEK_API_KEY",
        "mistral":    "MISTRAL_API_KEY",
        "openai":     "OPENAI_API_KEY",
    }

    provider_name = config.pop("provider", "openrouter")
    model = config.pop("model", "")
    api_key = config.pop("api_key", "")
    base_url = config.pop("base_url", None)

    # Fall back to environment variable if no key in config
    if not api_key:
        env_key = ENV_KEY_MAP.get(provider_name, "")
        if env_key:
            api_key = os.environ.get(env_key, "")
    max_tokens = config.pop("max_tokens", 8192)
    temperature = config.pop("temperature", 0.7)

    return create_provider(
        provider_name=provider_name,
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        temperature=temperature,
        **config,
    )


def create_tiered_provider(config: dict, tier_keys: List[str]) -> BaseProvider:
    """
    Create a provider by trying config keys in order.

    Walks through tier_keys looking for a non-empty config dict,
    falls back to config["provider"] if none found.

    Args:
        config: Full app config dict
        tier_keys: Config keys to try in order (e.g. ["arbitrator.provider", "reflection_provider"])
    """
    for key in tier_keys:
        # Support dotted keys like "arbitrator.provider"
        section = config
        for part in key.split("."):
            section = section.get(part, {}) if isinstance(section, dict) else {}
        if section and isinstance(section, dict):
            return create_provider_from_config(dict(section))

    # Final fallback: main provider
    return create_provider_from_config(dict(config.get("provider", {})))


def list_available_providers() -> List[str]:
    """List all registered provider names."""
    return sorted(_get_registry().keys())
