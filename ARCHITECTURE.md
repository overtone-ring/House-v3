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

An exchange is a **turn pair**: one user message + one persona's response. In a multi-persona reply, the same user message produces several exchanges — one per responding persona — so each embedding ties user intent to a specific persona's voice, and retrieval can filter by `persona_name` ("my memories") or leave it open ("things I witnessed"). Because that storage fan-out means one `@Girls` line lives as several rows sharing the same `user_msg`, the **render** layer (`formatters._format_memory_block`) regroups retrieved exchanges by `(user_msg, date)` so the prompt shows the user line once with each persona's reply beneath it — the duplication is collapsed at display time, never in storage (collapsing storage would break per-persona recall).

### Storage (SQLite, single file)

`data/memory.db`, opened with APSW (stdlib `sqlite3` on macOS can't load extensions):

- `exchanges` + `exchanges_vec` (sqlite-vec, 768-d) + `exchanges_fts` (FTS5, trigger-synced)
- `reflections` + `reflections_vec` + `reflections_fts`
- `relationships`, `sessions`
- WAL mode; writes serialized through a process-level lock since all worker threads share one connection. Reads are not locked — SQLite's serialized threading mode protects the data, but a single shared connection under heavy concurrent multi-channel load is the pattern that can surface intermittent "library used incorrectly"/locked errors. Not observed in practice; if it appears in the error log under real load, move to a per-thread connection or a single serialized DB worker.

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

## Observability

`utils/wire_log.py` writes two rotating logs under `logging.log_dir` (midnight rotation, `logging.retention_days` kept): `house.log` mirrors everything the console prints, and `wire.jsonl` (when `logging.wire_tap` is on) records one JSON line per pipeline event — `llm_call` (the exact API request payload and full raw model response), `llm_error`, `memory_search` (whether search ran, the inferred query, and every memory returned), and `scene` (the parsed turns as dispatched). One message's events share a `request_id` (a contextvar set at the top of `process_message`, which survives the `asyncio.to_thread` hop into the provider). Any exchange can be reconstructed after the fact: what was sent, what came back, and what the parser made of it.

`dashboard/server.py` is an in-process aiohttp web UI (config block `dashboard`, default port 8765) with five tabs: Live (watched channels, queue depth, locks, cooldowns, buffer sizes — state that only exists inside the running process, which is why the dashboard is embedded rather than a separate program), Exchanges (wire.jsonl events grouped by request_id, paginated, expandable to full JSON), Stats (messages/day from memory.db — permanent — plus search rate, latency, and tokens from the wire-log window), Users (per-user message counts from exchange metadata), and Health (recent llm_error events, reflection status, unreflected backlog). Read-only; failure-soft (the bot runs fine if the port is taken).

## What's Dead Code (intentionally kept)

- `context/manager.py` — legacy per-persona context manager, superseded by `unified_manager.py`.
- Relationship tracking — `relationships` table, `save_relationship`, and the relational primer exist but nothing writes relationships yet. Slated for a future build-out; harmless until then.
- Session-state methods on `StateManager` — present but not wired into the unified pipeline.
- `utils/token_counter.py` — token counting helpers, zero callers.
- `format_affective_primer()` in `context/formatters.py` — leftover from the deprecated affective subsystem.

## Tests

Stdlib `unittest` (no extra deps; pytest discovers them too). Run: `python -m unittest discover -s tests`. Current coverage is the high-leverage pure-logic paths: `response_parser` (every fallback, repetition guard, truncation, silence, MAX_TURNS), `apply_forced_personas` (the addressed-persona filter + dead-air reroute, extracted from `process_message` into a pure function for testability), and `formatters._format_memory_block` (the render-side dedup). DB/provider-backed paths are not yet covered.

## What's Next

- Build out the relationship/familiarity system (write path + richer primer). The `relationships` table and primer exist; nothing writes to them yet.
- Buffer-archive summarization (the evicted-turn archive exists; summarizing it back into context is not wired up).
- Extend tests to the DB/provider-backed paths: hybrid search + RRF ranking, buffer attribution tagging, and a full orchestrator→dispatch roundtrip (these need DB/provider fixtures).

## Known limitations (watched, not yet fixed)

- **JSON extraction is heuristic.** `response_parser._try_parse_json` tries direct parse → markdown-fence extract → outermost-brace slice. There's no `json-repair`/trailing-comma layer. With `json_mode` on (current models) this hasn't bitten; the dead-air path it used to feed is closed by the `apply_forced_personas` reroute.
- **Conversation history trims by turn count, not characters.** `get_history_for_unified_llm` keeps the last N turns regardless of length, so very long messages could bloat context. `max_chars`/`token_counter` exist but aren't wired in. Add a char/token budget if context overflow ever shows up.
- **Single shared DB connection across threads** — see the Storage note above.
- **Personas under-cross-talk under a deep buffer.** Diagnosed 2026-06-16 from the wire log: live `@Girls` scenes come back as *parallel monologues* — each persona answers the user once, no one takes a second turn or addresses another persona by name (0/8 scenes had a second beat). Cold smoke runs (no buffer) cross-talk fine, so the suspected cause is **in-context imitation**: the live buffer is full of single-voice/parallel exchanges, and the model copies that established rhythm over the system prompt's cross-talk instruction — a self-reinforcing loop (each parallel-monologue scene gets recorded and reinforces the next). Note the gate compounds it: a single-persona ping filters the scene to that persona (`apply_forced_personas`), so cross-talk can *only* surface on `@Girls` or a multi-persona ping.
  - **2026-06-16 mitigation (insufficient alone):** strengthened `unified_house.md` (de-hedged the multi-turn rule into an engagement mandate, added a group-scale cross-talk few-shot, added an explicit parallel-monologue ❌ / conversation ✅ contrast). Side effect: turns got ~50% shorter (the new few-shots were terse and DeepSeek imitated them) *without* producing cross-talk — the worst trade.
  - **2026-06-17 follow-up (the lever that worked in smoke):** DeepSeek is a roleplay-tuned model, and the prompt was **banning physical action / asterisk emotes** — the exact RP idiom the model is strongest at, and the cleanest way one persona engages another (acting *on* them). Changes: (1) lifted the action ban → action/gesture now allowed and encouraged, first-person voice preserved; (2) added a user-agency rule (personas control only themselves + the room, never narrate the user); (3) replaced the permissive length line with a concrete floor (developed 2–4-paragraph turns are normal for the talkers — DeepSeek optimizes toward brevity when instructions are permissive, where Gemma rambled regardless); (4) lengthened the few-shots + added action beats so they stop teaching terseness; (5) dropped `frequency_penalty`/`presence_penalty` in config (limited effect on DeepSeek's API; a frequency penalty mildly suppresses length). Smoke result: length restored, action beats landed in-voice, and a 2–3-persona scene produced a real second beat (Ellie reacting to Frank, Frank firing back at Ellie). **Still open:** the 5-voice `@Girls` fan-out stayed parallel even in smoke, and none of this is yet proven under a deep live buffer. Next levers if the buffer wins: surface scene boundaries in history so past multi-voice moments read as such; or let un-addressed personas contribute reaction turns on a single ping.
