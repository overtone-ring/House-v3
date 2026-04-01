# House-v3 Architecture

## Overview

House is a multi-persona conversational AI framework with flat RAG memory, daily reflections, and pluggable LLM providers. v3 is a simplified reorganization — RAPTOR hierarchy removed in favor of direct vector search over turn-pair exchanges with per-persona daily summaries.

## Design Principles

1. **Finish before expanding.** Every feature ships complete or doesn't ship.
2. **Modular boundaries.** Each subsystem can be understood, tested, and modified independently.
3. **OpenRouter first.** One provider covers 200+ models. Add direct providers via the plugin system only when needed.
4. **Config over constants.** Anything that was hardcoded in v2 now lives in `config/default.yaml`.
5. **Simplicity over abstraction.** One level of memory (exchanges + daily reflections). No hierarchical clustering.

## Directory Structure

```
House-v3/
├── config/
│   ├── default.yaml          # Shipped defaults (committed)
│   └── local.yaml            # User overrides (.gitignored)
│
├── data/
│   ├── models/               # Embedding models (nomic-embed ONNX)
│   ├── personas/
│   │   ├── soul/             # {name}_soul.txt — core identity
│   │   ├── agent/            # {name}_agent.txt — behavioral rules
│   │   ├── voice/            # {name}.txt — personality/tone
│   │   └── shared/           # mother.txt, thresholds.txt
│   └── shared/               # Runtime: shared memory JSONL + indexes
│       ├── memory/           # exchanges.jsonl
│       ├── relationships/    # {user_id}.json
│       └── indexes/          # exchanges_vectors.npz
│
├── src/
│   ├── providers/            # LLM provider plugin system
│   │   ├── __init__.py
│   │   ├── base.py           # BaseProvider ABC + retry logic
│   │   ├── registry.py       # Provider registry + factory
│   │   └── openrouter_provider.py  # Default provider
│   │
│   ├── memory/               # Flat RAG memory
│   │   ├── __init__.py
│   │   ├── models.py         # Data models (Exchange, DailyReflection, ...)
│   │   ├── vector_index.py   # Numpy cosine similarity search
│   │   └── store.py          # JSONL persistence + singleton management
│   │
│   ├── persona/              # Identity and prompt assembly
│   │   ├── __init__.py
│   │   ├── loader.py         # File-based persona prompt loading
│   │   ├── assembler.py      # Multi-section prompt composition
│   │   └── memory_instructions.py  # Per-persona memory-use templates
│   │
│   ├── conversation/         # Conversation state management
│   │   ├── __init__.py
│   │   └── buffer.py         # Sliding window buffer + persistence
│   │
│   ├── context/              # Context retrieval and formatting
│   │   ├── __init__.py
│   │   ├── manager.py        # "The Librarian" — orchestrates retrieval
│   │   └── formatters.py     # Memory/state → prompt string formatters
│   │
│   ├── services/             # Business logic services
│   │   ├── __init__.py
│   │   ├── embedding_service.py       # ONNX text embedding (nomic-embed)
│   │   ├── raptor_memory_service.py   # MemoryService — flat RAG API
│   │   ├── daily_reflection_service.py # Per-persona daily summaries
│   │   ├── state_manager.py           # Affective state + time decay
│   │   └── query_inference_service.py # "Should I search memory?" decisions
│   │
│   ├── orchestrator.py       # Main pipeline: message → response
│   │
│   └── utils/                # Shared utilities
│       ├── __init__.py
│       ├── config.py         # YAML config loader with env overrides
│       └── token_counter.py  # Token counting (tiktoken or heuristic)
│
├── filtered_conversations/   # Legacy GPT logs (6M tokens, not yet embedded)
├── tests/                    # Test suite
└── ARCHITECTURE.md           # This file
```

## Memory Architecture

### The Simplification (v2 → v3)

v2 used RAPTOR: Exchange → Leaf → Branch → Tree via GMM clustering. This was removed because:
- GMM clustering destroys temporal information (memories from different dates get merged by topic)
- Branch/tree reflections freeze one interpretation at one moment; the persona's prompt already provides dynamic interpretation at inference time
- The complexity wasn't earning its keep for the actual conversation volumes

v3 uses flat RAG: exchanges are embedded and searched directly by vector similarity.

### Exchange Model

Each exchange is a **turn pair**: one user message + one persona's response.

In multi-persona conversations (Discord), the same user message produces multiple exchanges — one per responding persona. This means:
- Each embedding captures the relationship between user intent and a specific persona's response
- Retrieval can filter by `persona_name` for "my memories" or leave unfiltered for "things I witnessed"
- Attribution is clear — each record has exactly one persona

### Daily Reflections

One level of summarization, generated daily:
- Midnight trigger checks for unreflected exchanges per persona
- LLM generates a narrative summary from that persona's perspective
- Summary is embedded for vector search
- Preserves timeline (one reflection = one date)
- Persona-scoped (each persona's reflections are private)

### Storage Layout

```
data/
├── shared/
│   ├── memory/exchanges.jsonl        # All turn pairs from all personas
│   ├── relationships/{user_id}.json  # Per-user relationship data
│   └── indexes/exchanges_vectors.npz # Shared embedding index
│
├── elvira/
│   ├── memory/reflections.jsonl      # Elvira's daily summaries
│   ├── sessions/{id}.json            # Session state
│   └── indexes/reflections_vectors.npz
│
├── frank/
│   ├── memory/reflections.jsonl
│   └── ...
```

## Subsystem Details

### 1. Provider Plugin System (`src/providers/`)

Abstracts LLM API calls. OpenRouter is the default, supporting 200+ models through one API key.

Key classes:
- `BaseProvider` — ABC with retry logic, error classification, parameter resolution
- `OpenRouterProvider` — Default implementation (OpenAI-compatible API)
- `@register_provider` decorator for adding new providers

### 2. Memory (`src/memory/`)

JSONL-backed persistence with in-memory caching and numpy vector indexes.

Key classes:
- `MemoryStore` — JSONL CRUD, vector search, shared/persona isolation
- `VectorIndex` — Numpy cosine similarity with `.npz` caching
- `Exchange` — Turn pair model (user_msg + assistant_response + persona_name)
- `DailyReflection` — Per-persona daily summary

### 3. Services (`src/services/`)

- `MemoryService` — Primary memory API: add_exchange(), search_memory()
- `DailyReflectionService` — Generates per-persona daily summaries via LLM
- `EmbeddingService` — ONNX nomic-embed singleton with async batch support
- `StateManager` — Affective state with time-based decay
- `QueryInferenceService` — Regex + LLM gating for memory search

### 4. Persona (`src/persona/`)

Prompt assembly: Soul + Agent + Voice + Memory instructions, composed per persona with caching.

### 5. Context (`src/context/`)

The "librarian" — decides what memory and state to include:
1. Query inference: "Does this message need memory search?"
2. Vector search over exchanges (filtered by persona)
3. Vector search over daily reflections
4. Relationship and affective state lookup

### 6. Orchestrator (`src/orchestrator.py`)

Full pipeline: query inference → context retrieval → prompt assembly → LLM generation → post-processing (record exchange, update state).

## Dependency Graph

```
config/default.yaml
    ↓
src/utils/config.py  ← loaded by everything
    ↓
src/memory/models.py  ← no dependencies (pure data)
src/memory/vector_index.py  ← numpy only
src/memory/store.py  ← depends on models + vector_index
    ↓
src/services/embedding_service.py  ← ONNX runtime
src/services/raptor_memory_service.py  ← depends on store + embedding
src/services/daily_reflection_service.py  ← depends on store + embedding + provider
src/services/state_manager.py  ← file I/O only
src/services/query_inference_service.py  ← depends on provider
    ↓
src/persona/assembler.py  ← depends on loader + memory_instructions
src/conversation/buffer.py  ← standalone
src/context/manager.py  ← depends on memory_service + query_inference
src/context/formatters.py  ← pure functions
    ↓
src/providers/openrouter_provider.py  ← depends on base + registry
    ↓
src/orchestrator.py  ← depends on everything above
```

No circular dependencies. All arrows point downward.

## What's Next

- **Who responds logic**: Multi-persona routing for Discord (which persona(s) should respond to a given message)
- **Legacy log import**: Tool to parse and embed the 6M tokens of GPT conversation logs
- **Discord integration**: Bot framework connecting orchestrators to Discord channels
