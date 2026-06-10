# House-v3 Architecture

## Overview

House is a multi-persona Discord bot. Five personas (Elvira, Frank, Zagna, Vireline, Ellie) share **one** LLM call: a single unified system prompt asks the model to inhabit all five voices at once and return a **scene** — an ordered JSON array of turns (`{"turns": [{"speaker", "text"}, ...]}`). Personas may take multiple turns in one scene, reacting to each other, and the turns are dispatched to Discord in order. There is no arbitrator and no per-persona routing pass — the model decides who responds and in what order, and the orchestrator parses and dispatches.

Memory is flat RAG over turn-pair exchanges, stored in a single SQLite database with on-disk vector search (sqlite-vec) and keyword search (FTS5), combined via Reciprocal Rank Fusion. One optional layer of abstraction sits on top: per-persona daily reflections.

## Design Principles

1. **One model call, many voices.** The unified prompt replaces the old arbitrator + N per-persona calls. Lower latency, lower cost, more coherent cross-talk.
2. **Single source of memory.** One SQLite file (`data/memory.db`) holds every persona's exchanges and reflections. Retrieval filters by `persona_name`.
3. **OpenRouter only.** One OpenAI-compatible endpoint fronts 200+ models behind a single key. The model is a config value, not a code change.
4. **Config over constants.** Behavior lives in `config/default.yaml`, overridable per-environment in `config/local.yaml` (gitignored).
5. **Fail fast, degrade gracefully.** Startup validates tokens, config, and the embedding model. At runtime, a corrupt buffer or a failed memory write never takes the bot down.

## Request Flow

```
User message in a watched channel
  └─ Watcher bot (on_message)
       ├─ trigger gating: only an @Girls role ping (whole house) or a
       │   specific persona ping (that persona) is answered; else ignored
       ├─ append to per-channel ConversationBuffer
       ├─ speaker attribution: current message tagged [name]:; Discord
       │   replies get a [replying to name: "…"] anchor from the reference
       └─ UnifiedOrchestrator.process_message()
            ├─ query inference: "does this need a memory search?"
            ├─ unified context retrieval (parallel memory search, all personas)
            ├─ format contextual primer (memories + optional routing directive)
            ├─ single LLM call (json_mode, unified system prompt)
            ├─ response parser (ordered turns + fallback chain + repetition
            │   guard + 6000-char runaway cap; Discord layer splits >2000)
            └─ fire-and-forget post-process (record exchanges, bump engagement)
  └─ Dispatch turns in scene order via each persona's own PersonaClient bot,
      with a short typing beat between turns
```

## Process Model

A single process runs six `discord.py` clients on one asyncio loop (`src/discord_bot/runner.py`):

- **Watcher** — no persona identity. Listens across watched channels, gates triggers, owns the conversation buffers and slash commands, calls the orchestrator, and dispatches responses.
- **Five PersonaClients** — one bot per persona. Each only sends its own messages and handles 🔊 reactions for TTS. No routing logic.

On startup the runner also kicks off a background **reflection catch-up** (see below).

## Memory Architecture

### Exchange model

An exchange is a **turn pair**: one user message + one persona's response. In a multi-persona reply, the same user message produces several exchanges — one per responding persona — so each embedding ties user intent to a specific persona's voice, and retrieval can filter by `persona_name` ("my memories") or leave it open ("things I witnessed").

### Storage (SQLite, single file)

`data/memory.db`, opened with APSW (stdlib `sqlite3` on macOS can't load extensions):

- `exchanges` + `exchanges_vec` (sqlite-vec, 768-d) + `exchanges_fts` (FTS5, trigger-synced)
- `reflections` + `reflections_vec` + `reflections_fts`
- `relationships`, `sessions`
- WAL mode; writes serialized through a process-level lock since all worker threads share one connection.

### Hybrid search

Vector similarity and keyword matches are each ranked, then fused with Reciprocal Rank Fusion (`k=60`). Falls back to vector-only if the FTS query can't be sanitized.

### Daily reflections

One level of summarization. For a given date, all of a persona's unreflected exchanges are summarized by a cheaper model into a first-person diary entry, embedded, and stored; the source exchanges are marked `reflected`. Because the bot isn't guaranteed to be running at midnight, reflections are **not** on a timer — they run as a **startup catch-up**: on boot, any past date with unreflected exchanges is backfilled (today is left alone). Restarting the bot is the trigger. When there's work to do, the Watcher announces the cycle's start and finish in watched channels (once per process — reconnects don't re-announce); the cycle shares the embedding model and CPU with live messages, so responses are slower while it runs, but nothing conflicts at the data level (it only touches past dates).

## Subsystems (`src/`)

| Area | Files | Role |
|------|-------|------|
| Orchestration | `unified_orchestrator.py` | The pipeline: context → single LLM call → parse → post-process |
| Parsing | `response_parser.py` | JSON fallback chain, repetition guard, per-persona 2000-char cap |
| Providers | `providers/base.py`, `providers/openrouter_provider.py`, `providers/registry.py` | OpenRouter via OpenAI-compatible API; factory + retry/error classification |
| Memory | `memory/store.py`, `memory/models.py` | SQLite + sqlite-vec + FTS5; dataclasses (Exchange, DailyReflection, UserRelationship, SessionState) |
| Services | `services/memory_service.py`, `services/embedding_service.py`, `services/daily_reflection_service.py`, `services/query_inference_service.py`, `services/state_manager.py`, `services/tts_service.py` | Memory API, ONNX embeddings, reflections, search gating, engagement/session state, Kokoro TTS |
| Context | `context/unified_manager.py`, `context/formatters.py` | Parallel retrieval for all personas; memory/relationship → prompt strings |
| Conversation | `conversation/buffer.py` | Per-channel sliding window with JSON persistence + archive |
| Discord | `discord_bot/runner.py`, `discord_bot/watcher.py`, `discord_bot/persona_client.py` | Process entry point, coordinator, per-persona bots |
| Utils | `utils/config.py`, `utils/paths.py`, `utils/io.py` | Config merge + env, project root, atomic I/O |

## Conversation Buffer

Per channel, keyed by channel **ID** (`discord_{channel_id}`) so the same channel name on different servers can't collide. The LLM sees the last `conversation.max_turns` (default 50) turns. The active buffer is capped at twice that; turns evicted past the cap are appended to an append-only archive (`data/sessions/discord_{id}_archive.jsonl`) rather than dropped, so the live buffer file stays small without losing history.

Other personas' prior messages are folded into history as attributed `user` turns, never `assistant` turns — otherwise the model reads them as its own past output and leaks identity.

## State

`StateManager` (file-based, atomic writes) tracks **engagement** counts and per-session metadata under `data/state/{persona}/`. An earlier affective-state subsystem (emotional dimensions with time decay) was deprecated: it was never written to in the unified pipeline, so it never reached a prompt. User-projected affect — the model mirroring tone from the conversation itself — covers that ground without a background state machine.

## Configuration

`utils/config.py` deep-merges `config/default.yaml` then `config/local.yaml`, then applies `HOUSE_*` environment overrides. `local.yaml` is gitignored and is where per-environment values (model, channels, keys) belong. The Discord runner loads config through this same path, so local overrides apply.

## What's Dead Code (intentionally kept)

- `context/manager.py` — legacy per-persona context manager, superseded by `unified_manager.py`.
- Relationship tracking — `relationships` table, `save_relationship`, and the relational primer exist but nothing writes relationships yet. Slated for a future build-out; harmless until then.
- Session-state methods on `StateManager` — present but not wired into the unified pipeline.
- `utils/token_counter.py` — token counting helpers, zero callers.
- `format_affective_primer()` in `context/formatters.py` — leftover from the deprecated affective subsystem.

## What's Next

- Build out the relationship/familiarity system (write path + richer primer).
- Buffer-archive summarization (the evicted-turn archive exists; summarizing it back into context is not wired up).
- A test suite (`tests/` is currently a stub).
