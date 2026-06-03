"""
Services Layer
==============

Business logic services that operate on the core subsystems.
Each service has a single responsibility and explicit dependencies.

v3 simplified — RAPTOR hierarchy removed. Services:
    - EmbeddingService        : ONNX text embedding (nomic-embed)
    - MemoryService           : Flat RAG over turn-pair exchanges
    - DailyReflectionService  : Per-persona daily summaries (midnight trigger)
    - ArbitratorService       : Tiered routing — who responds? (rules → Qwen LLM)
    - StateManager            : Affective state persistence + time decay
    - QueryInferenceService   : "Should I search memory?" decisions
    - TTSService              : Text-to-speech via Kokoro (per-persona voices)

Future services (add when needed):
    - RelationshipService     : User relationship tracking
"""

# Services are imported individually where needed to avoid
# heavy dependency loading at package import time.
# Example: from src.services.memory_service import MemoryService
