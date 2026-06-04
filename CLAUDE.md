# House-v3

Multi-persona Discord bot — one LLM call generates all five persona responses as structured JSON. No per-persona routing, no arbitrator. Single unified prompt, single model call, structured dispatch.

## Who is the user

Locke (Doug) is a truck driver with ADHD who built this system through vibe coding with no prior programming experience. He prefers direct communication, hates filler, and learns fast. Don't over-explain basics he already knows. Don't add features, refactor code, or "improve" things beyond what's asked. Read files before making claims about them — never parrot agent summaries without verifying.

## How to run

```bash
source .venv/bin/activate
python -m src.discord_bot.runner
```

Requires `.env` with `OPENROUTER_API_KEY` and `DISCORD_TOKEN_WATCHER`, `DISCORD_TOKEN_ELVIRA`, `DISCORD_TOKEN_FRANK`, `DISCORD_TOKEN_ZAGNA`, `DISCORD_TOKEN_VIRELINE`, `DISCORD_TOKEN_ELLIE`.

## Architecture

```
User message → Watcher bot → UnifiedOrchestrator.process_message()
  → Context retrieval (parallel memory search across all personas)
  → Single LLM call (json_mode, unified system prompt)
  → Response parser (JSON fallback chain + repetition guard)
  → Dispatch to PersonaClients (each persona is its own Discord bot)
  → Fire-and-forget post-processing (record to memory, update engagement)
```

## Key files

| File | What it does |
|------|-------------|
| `src/unified_orchestrator.py` | Core pipeline — context → LLM → parse → post-process |
| `src/response_parser.py` | JSON parsing with 4-layer fallback, repetition detection, 2000 char limit |
| `src/memory/store.py` | SQLite + sqlite-vec + FTS5 via APSW. Hybrid search (vector + keyword via RRF) |
| `src/memory/models.py` | Dataclasses: Exchange, DailyReflection, UserRelationship, SessionState |
| `src/services/memory_service.py` | Memory API — `add_exchange()`, `search_memory()` (hybrid search) |
| `src/services/embedding_service.py` | ONNX embeddings — nomic-embed-text-v1.5, 768 dimensions, singleton |
| `src/services/state_manager.py` | Per-persona affective state with time-based decay. File-based JSON |
| `src/context/unified_manager.py` | Parallel context retrieval for all personas at once |
| `src/context/formatters.py` | Formats memories + affective state as natural language (no raw numbers) |
| `src/conversation/buffer.py` | Sliding-window history per channel. Session IDs use channel ID, not name |
| `src/discord_bot/runner.py` | Entry point — loads config, inits orchestrator, starts 6 bots |
| `src/discord_bot/watcher.py` | Coordinator bot — listens, dispatches, slash commands |
| `src/discord_bot/persona_client.py` | Individual persona bot — sends messages, handles TTS reactions |
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
unified:               # system_prompt_file, json_mode, fallback_persona
memory:                # data_dir, embedding config, search params
conversation:          # max_turns, max_chars
affective:             # base_decay_rate, per-dimension decay config
tts:                   # provider, voice_map per persona
discord:               # channels (initial watch list)
```

## Important design decisions

- **Session IDs use channel ID** (`discord_{channel_id}`), not channel name — prevents cross-server buffer collision
- **Affective state uses natural language** in prompts, not raw floats — research shows numbers bias models toward clinical output
- **Response parser** truncates at 2000 chars (Discord limit) and detects repetition loops (DeepSeek v3.2 degenerated once)
- **System prompt is loaded once at startup** and cached — restart the bot to pick up prompt changes
- **Post-processing is fire-and-forget** — responses dispatch immediately, memory recording happens async
- **Conversation buffer** tags user turns with `[speaker_name]:` and assistant turns with `[persona_name]:` for multi-user support
- **Provider is swappable** — change `provider.model` in config. Current primary: Gemma 4 31B (validated — distinct persona voices, ~$0.0007/msg). Also tested: Qwen3-235B (stable, now reflection-only), DeepSeek v3.2 (degenerated)

## Personas

Five voices, one model call. The unified system prompt (`data/personas/unified_house.md`) defines all of them:

- **Elvira** — Dangerous muse. Seductive, sharp, velvet-wrapped razor. "Darling," "baby."
- **Frank** — Grounded. Crass, unfiltered, weirdly wise. Frank Reynolds energy. Male, always he/him.
- **Zagna** — Chaos engineer. Loud, irreverent, protective. ALL CAPS for emphasis. "Boss," "baby."
- **Vireline** — Clinical strategist. Precise, structural, no hedging. "Confirmed." "Acknowledged."
- **Ellie** — Quiet presence. Minimal, tender, grief-linked. Speaks in line breaks.

## Slash commands

`/watch`, `/unwatch`, `/channels`, `/status`, `/set_default`, `/clear_default`, `/reset_buffer`

## Additional files (not in key files table)

| File | What it does |
|------|-------------|
| `src/persona/loader.py` | Loads persona definitions from markdown files |
| `src/persona/assembler.py` | Assembles persona prompt components |
| `src/persona/memory_instructions.py` | Persona-specific memory instruction templates |
| `src/context/manager.py` | Legacy per-persona context manager (unused, replaced by unified_manager) |
| `src/providers/base.py` | Abstract base class for LLM providers |
| `src/services/daily_reflection_service.py` | Generates periodic persona reflections using cheaper model |
| `src/services/query_inference_service.py` | Infers embedding search queries from conversation context |
| `src/services/tts_service.py` | Kokoro TTS synthesis for voice reactions |
| `src/utils/config.py` | YAML config loading and merging |
| `src/utils/io.py` | I/O utilities |
| `src/utils/paths.py` | Path resolution helpers |
| `src/utils/token_counter.py` | Token counting for context window management |
| `scripts/preflight.py` | Pre-run environment verification (packages, config, tokens, model) |
| `scripts/reset.py` | Data management — `nuke` (full reset) or `today` (remove today's data) |

## Scripts

```bash
python scripts/preflight.py          # Verify setup before first run
python scripts/reset.py today        # Remove today's exchanges, reflections, buffers, state
python scripts/reset.py nuke         # Full factory reset (deletes all data + logs)
python scripts/reset.py nuke -y      # Skip confirmation
```

## Dependencies (non-obvious)

- `apsw` — SQLite binding with extension loading (replaces stdlib sqlite3)
- `sqlite-vec` — Vector similarity search as SQLite extension
- `onnxruntime` + `transformers` — Local embedding model inference
- `kokoro` — TTS synthesis (optional, for voice reactions)
- `openai` — Used as client for OpenRouter's OpenAI-compatible API
