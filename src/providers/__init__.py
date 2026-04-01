"""
Provider Plugin System
======================

House-v3 uses a simple registry for LLM providers.
OpenRouter is the default (covers 200+ models via one API).
Additional providers can be added by subclassing BaseProvider
and adding them to the registry dict in registry.py.

Usage:
    from src.providers import create_provider, get_provider_registry

    # Default: OpenRouter
    provider = create_provider("openrouter", model="anthropic/claude-sonnet-4", api_key="...")

    # Or via config
    provider = create_provider_from_config(config)
"""

from .base import BaseProvider, ProviderConfig, GenerationResult
from .registry import create_provider, create_provider_from_config, create_tiered_provider, get_provider_registry

__all__ = [
    "BaseProvider",
    "ProviderConfig",
    "GenerationResult",
    "create_provider",
    "create_provider_from_config",
    "create_tiered_provider",
    "get_provider_registry",
]
