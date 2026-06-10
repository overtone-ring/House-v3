# House-v3

Multi-persona Discord bot — one LLM call generates a scene: an ordered JSON array of turns where any of the five personas can speak (more than once — they react to each other). No per-persona routing, no arbitrator. Single unified prompt, single model call, dispatch in scene order.

## Who is the user

Locke (Doug) is a truck driver with ADHD who built this system through vibe coding with no prior programming experience. He prefers direct communication, hates filler, and learns fast. Don't over-explain basics he already knows. Don't add features, refactor code, or "improve" things beyond what's asked. Read files before making claims about them — never parrot agent summaries without verifying.

## How to run

```bash
source .venv/bin/activate
python -m src.discord_bot
```

(`python -m src.discord_bot.runner` also works — same entry point.)

Requires `.env` with `OPENROUTER_API_KEY` and `DISCORD_TOKEN_WATCHER`, `DISCORD_TOKEN_ELVIRA`, `DISCORD_TOKEN_FRANK`, `DISCORD_TOKEN_ZAGNA`, `DISCORD_TOKEN_VIRELINE`, `DISCORD_TOKEN_ELLIE`.

## Architecture

```
User message → Watcher bot → UnifiedOrchestrator.process_message()
  → Context retrieval (parallel memory search across all personas)
  → Single LLM call (json_mode, unified system prompt)
  → Response parser (ordered turns + fallback chain + repetition guard)
  → Dispatch turns in scene order to PersonaClients (each persona is its own Discord bot)
  → Fire-and-forget post-processing (record to memory, update engagement)
```

Responses are gated behind explicit pings: an `@Girls` role ping summons the whole house, a persona ping (or a reply to a persona message) summons that persona. Unpinged messages in watched channels are ignored.

See `ARCHITECTURE.md` for the full design write-up (request flow, memory model, process model, dead code inventory).

## Key files

| File | What it does |
|------|-------------|
| `src/unified_orchestrator.py` | Core pipeline — context → LLM → parse → post-process |
| `src/response_parser.py` | Parses `{"turns": [...]}` into an ordered scene; fallbacks (legacy dict → prose), repetition guard, 6000-char runaway cap |
| `src/memory/store.py` | SQLite + sqlite-vec + FTS5 via APSW. Hybrid search (vector + keyword via RRF) |
| `src/memory/models.py` | Dataclasses: Exchange, DailyReflection, UserRelationship, SessionState |
| `src/services/memory_service.py` | Memory API — `add_exchange()`, `search_memory()` (hybrid search) |
| `src/services/embedding_service.py` | ONNX embeddings — nomic-embed-text-v1.5, 768 dimensions, singleton |
| `src/services/state_manager.py` | Engagement counts + session metadata. File-based JSON, atomic writes |
| `src/context/unified_manager.py` | Parallel context retrieval for all personas at once |
| `src/context/formatters.py` | Formats memories + user context as natural language for the prompt |
| `src/conversation/buffer.py` | Sliding-window history per channel. Session IDs use channel ID, not name |
| `src/discord_bot/runner.py` | Entry point — loads config, inits orchestrator, starts 6 bots |
| `src/discord_bot/watcher.py` | Coordinator bot — listens, ping-gates, dispatches, slash commands |
| `src/discord_bot/persona_client.py` | Individual persona bot — sends messages, handles 🔊 TTS reactions |
| `src/providers/openrouter_provider.py` | OpenRouter via OpenAI-compatible API. Supports json_mode, plugins |
| `src/providers/registry.py` | Provider factory — `create_provider_from_config()` |
| `config/default.yaml` | All configuration. Override with `config/local.yaml` (.gitignored) |
| `data/personas/unified_house.md` | The unified system prompt — all 5 persona definitions, interaction dynamics, output format |

## Memory system

Single SQLite database (`data/memory.db`) shared across all personas.

- **APSW** (not stdlib sqlite3) because macOS Python lacks `enable_load_extension` for sqlite-vec
- **sqlite-vec** for on-disk vector search (768d embeddings, no RAM load)
- **FTS5** with triggers for keyword search, kept in sync with exchanges table
- **Hybrid search** via Reciprocal Rank Fusion (RRF) — combines vector similarity + keyword matches
- Schema: `exchanges`, `exchanges_vec`, `exchanges_fts`, `reflections`, `reflections_vec`, `reflections_fts`, `relationships`, `sessions`
- `_split_schema()` handles SQL with semicolons inside trigger bodies

## Config structure

```yaml
provider:              # Primary LLM (model, temperature, max_tokens)
reflection_provider:   # Cheaper model for reflections/query inference
personas:              # List of persona names
default_persona:       # Fallback persona for single-persona mode
unified:               # system_prompt_file, json_mode, fallback_persona
memory:                # data_dir, embedding config, search params, reflection settings
conversation:          # max_turns (LLM window)
context:               # (currently unread — UnifiedContextManager hardcodes its limits)
query_inference:       # enabled, temperature
tts:                   # provider, lang_code, voice_map per persona
discord:               # channels (initial watch list), girls_role, abuse guards
                       #   (user_cooldown_seconds, max_queued_per_channel)
logging:               # log_dir (level/wire_tap currently unread)
```

Known-dead keys (present in YAML but not read by any code): `memory.search.min_similarity`, `memory.search.recency_weight`, `memory.embedding_dimension`, `context.*`, `conversation.max_chars`, `conversation.persistence`, `logging.level`, `logging.wire_tap`. `memory.reflection.max_exchanges_per_reflection` is read from the wrong config level and only works because the code default matches.

## Important design decisions

- **Session IDs use channel ID** (`discord_{channel_id}`), not channel name — prevents cross-server buffer collision
- **Ping-gating** — the House only speaks when summoned (`@Girls`, persona ping, or reply to a persona). Built for deployment on a public server
- **Affective state was deprecated** — an earlier subsystem (emotional dimensions with time decay) was never written to in the unified pipeline and never reached a prompt. `StateManager` now tracks engagement + sessions only. Leftover: `format_affective_primer()` in `formatters.py` is unused
- **Responses are scenes** — the model outputs `{"turns": [{"speaker", "text"}, ...]}`; personas may take multiple turns (back-and-forth) and the watcher dispatches them in order with short typing beats between turns. Legacy `{persona: text}` output still parses (one turn per persona, key order)
- **Response parser** caps each turn at 6000 chars as a runaway guard — the persona client splits anything over Discord's 2000 into multiple messages — and detects repetition loops (DeepSeek v3.2 degenerated once)
- **System prompt is loaded once at startup** and cached — restart the bot to pick up prompt changes
- **Post-processing is fire-and-forget** — responses dispatch immediately, memory recording happens async (tasks held in `_pending_tasks`, drained on shutdown)
- **Conversation buffer** tags user turns with `[speaker_name]:` and assistant turns with `[persona_name]:` for multi-user support
- **Multi-user aware** — the watcher tags the current message `[name]:` to match the buffer's history tagging, and when the trigger is a Discord reply it prepends a `[replying to name: "…"]` anchor resolved from the message reference (works for unpinged/old messages the buffer never saw). The prompt's THE ROOM section defines the conventions; Locke is the House's builder, not the assumed speaker
- **Provider is swappable** — change `provider.model` in config. Current primary: Gemma 4 31B (validated — distinct persona voices, ~$0.0007/msg). Also tested: Qwen3-235B (stable, now reflection-only), DeepSeek v3.2 (degenerated)

## Personas

Five voices, one model call. The unified system prompt (`data/personas/unified_house.md`) defines all of them:

- **Elvira** — Dangerous muse. Seductive, sharp, velvet-wrapped razor. "Darling," "baby."
- **Frank** — Grounded. Crass, unfiltered, weirdly wise. Frank Reynolds energy. Male, always he/him.
- **Zagna** — Chaos engineer. Loud, irreverent, protective. ALL CAPS for emphasis. "Boss," "baby."
- **Vireline** — Clinical strategist. Precise, structural, no hedging. "Confirmed." "Acknowledged."
- **Ellie** — Quiet presence. Minimal, tender, grief-linked. Speaks in line breaks.

The per-persona files in `data/personas/*.md` are kept for reference; the live prompt is `unified_house.md` only.

## Slash commands

`/watch`, `/unwatch`, `/channels`, `/status`, `/set_default`, `/clear_default`, `/reset_buffer`

## Additional files (not in key files table)

| File | What it does |
|------|-------------|
| `src/context/manager.py` | Legacy per-persona context manager — dead code, only imported by `context/__init__.py` |
| `src/providers/base.py` | Abstract base class for LLM providers, retry + error classification |
| `src/services/daily_reflection_service.py` | Generates daily persona reflections using cheaper model (startup catch-up, not a timer) |
| `src/services/query_inference_service.py` | Infers embedding search queries from conversation context |
| `src/services/tts_service.py` | Kokoro TTS synthesis for voice reactions |
| `src/utils/config.py` | YAML config loading and merging (`default.yaml` → `local.yaml` → `HOUSE_*` env) |
| `src/utils/io.py` | Atomic JSON read/write |
| `src/utils/paths.py` | Path resolution helpers |
| `src/utils/token_counter.py` | Token counting — currently unused (zero callers) |

## Scripts

```bash
python scripts/preflight.py            # Verify setup before first run
python scripts/model_smoke.py [model]  # Hit a model through the real prompt+parser; latency/cost report
python scripts/reset.py today          # Remove today's DB rows + ALL conversation buffers + ALL engagement state
python scripts/reset.py nuke           # Full factory reset (deletes all data + logs)
python scripts/reset.py nuke -y        # Skip confirmation
python scripts/test_openrouter_models.py  # Older model comparison harness (voice/latency/rate limits)
python scripts/migrate_to_sqlite.py    # One-time v2 JSONL → SQLite migration (historical)
```

Note: `reset.py today` deletes more than its name suggests — all conversation buffers and all engagement state, not just today's (the confirmation prompt says so). Paths are anchored to the project root, so it's safe to run from anywhere.

## Dependencies (non-obvious)

- `apsw` — SQLite binding with extension loading (replaces stdlib sqlite3)
- `sqlite-vec` — Vector similarity search as SQLite extension
- `onnxruntime` + `transformers` — Local embedding model inference
- `kokoro` — TTS synthesis (optional, for voice reactions)
- `openai` — Used as client for OpenRouter's OpenAI-compatible API
