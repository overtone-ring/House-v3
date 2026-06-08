"""
Services Layer
==============

Business logic services that operate on the core subsystems.
Each service has a single responsibility and explicit dependencies.

Services:
    - EmbeddingService        : ONNX text embedding (nomic-embed)
    - MemoryService           : Flat RAG over turn-pair exchanges
    - DailyReflectionService  : Per-persona daily summaries (startup catch-up)
    - StateManager            : Engagement + session state (file-based)
    - QueryInferenceService   : "Should I search memory?" decisions
    - TTSService              : Text-to-speech via Kokoro (per-persona voices)

Note: who-responds routing happens inside the single unified LLM call,
not a separate arbitrator service.

Future services (add when needed):
    - RelationshipService     : User relationship tracking
"""

# Services are imported individually where needed to avoid
# heavy dependency loading at package import time.
# Example: from src.services.memory_service import MemoryService
