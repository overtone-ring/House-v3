# Audit Fix Plan (2026-06-09)

Findings from the full-system audit, ordered for editing efficiency: grouped by file so each file is opened/reasoned-about once. Happily, the highest-severity items cluster in `watcher.py`, so file-order ≈ severity-order anyway. Check items off as they land.

## Pass 1 — `src/discord_bot/watcher.py` (launch blockers)

- [x] **1.1 Persona ID cache can be permanently partial (HIGH).** `on_ready` (watcher.py:293-297) caches persona bot IDs once; personas that connect later are never added because `_persona_id_map()` only falls back when the cache is *empty*. `update_persona_bot_ids()` (watcher.py:567) has zero callers. Fix: make `_persona_id_map()` always merge live `client.user` lookups over the cache (and delete or wire the dead repair method). Symptom prevented: a persona silently unsummonable by direct ping.
- [x] **1.2 Reconnect can wipe the watch list (MEDIUM, destructive).** `on_ready` re-fires on every new session and prunes+persists channels missing from the guild cache (watcher.py:300-307). If a guild is briefly unavailable, the watch list is pruned to disk and never recovers. Fix: run load/prune/config-resolution/tree.sync only on *first* ready (guard flag); never persist a prune triggered by guild unavailability (only prune channels whose guild is present but channel is gone).
- [x] **1.3 No rate limiting on LLM triggers (HIGH for public server).** Any user can queue unlimited LLM calls by pinging in a loop; the per-channel lock serializes but never drops (watcher.py:353-383). Fix: per-user cooldown (config `discord.user_cooldown_seconds`, default ~8s) + drop-with-reaction when a message arrives while that channel's lock is held beyond a small queue depth. Keep it simple: cooldown dict {user_id: monotonic}, silently ignore (or 🕐 react) when throttled.
- [x] **1.4 Slash commands: DM leak + no permission backstop (MEDIUM).** Commands aren't `guild_only`, so `/channels`/`/status` work in DMs and leak cross-guild channel names; `default_permissions` can be re-granted by any server's admins (watcher.py:112-285). Fix: add `@app_commands.guild_only()` and a runtime check (`interaction.user.guild_permissions.administrator` or app owner) inside each command.
- [x] **1.5 `/reset_buffer` races in-flight processing (LOW).** Command mutates/deletes buffer without the per-channel lock (watcher.py:263-285 vs :489-499); in-flight handler resurrects it. Fix: acquire `self._channel_locks` lock in the command.

## Pass 2 — double-send fix (`src/conversation/buffer.py` + `src/unified_orchestrator.py`)

- [x] **2.1 Current user message sent twice every call (HIGH, hot path).** Watcher pre-adds the message to the buffer (watcher.py:409); orchestrator builds history including it (unified_orchestrator.py:161) *and* passes it as `prompt`, which the provider appends again (openrouter_provider.py:327). Fix: add `exclude_current: bool = False` to `get_history_for_unified_llm()` (mirror the existing flag on `get_history_for_llm`, buffer.py:110-122) and pass `exclude_current=True` from the orchestrator.

## Pass 3 — dead-air + parser edge cases (`src/unified_orchestrator.py` + `src/response_parser.py`)

- [x] **3.1 Pinged persona + API failure = silent dead air (MEDIUM).** All error/fallback text routes to `fallback_persona` (elvira); forced-persona filter then blanks it (unified_orchestrator.py:217-232). Fix: when `forced_personas` is set and filtering would blank a non-empty generation (the case the new warning log catches), reroute the fallback/error text to the first forced persona instead of dropping it.
- [x] **3.2 Valid-but-wrong JSON posted verbatim (MEDIUM).** All-null or wrong-keyed JSON fails validation and falls into the "unparseable" branch, which posts the raw JSON string as elvira (response_parser.py:54-64, 174-175). Fix: if text parsed as JSON but failed validation, treat as silence (return all-None) + log raw output, instead of dumping JSON braces into chat.
- [x] **3.3 (LOW, same file) Non-string JSON values become Python repr in chat (response_parser.py:164-166).** Treat dict/list values as invalid (null) rather than `str()`-ing them.

## Pass 4 — `src/providers/openrouter_provider.py`

- [x] **4.1 No client timeout + stacked retries (MEDIUM).** `OpenAI(...)` client has SDK defaults: 600s timeout, 2 internal retries, on top of `_call_with_retry`'s 3 (openrouter_provider.py:57-64, base.py:217-263). Fix: construct client with `timeout=120, max_retries=0` so our retry layer is the only one.

## Pass 5 — TTS (`src/discord_bot/persona_client.py` + `src/services/tts_service.py`)

- [x] **5.1 🔊 reaction is an unthrottled compute/spam vector (MEDIUM-HIGH on public server).** No dedup, no cooldown, no text length cap (persona_client.py:57-116). Fix: per-message dedup (skip if synthesis for that message id is in flight or recently done), per-user cooldown, cap synthesized text length (~800 chars).
- [x] **5.2 Temp filename collision via `id(text)` (LOW).** tts_service.py:150 — use `tempfile.mkstemp`/uuid instead.
- [ ] **5.3 (Optional) Per-voice lang routing never wired (LOW).** `lang_code` computed and dropped (tts_service.py:129, 174) — Vireline's British voice runs through the American pipeline. Needs a per-lang `KPipeline` cache.

## Pass 6 — `scripts/reset.py`

- [x] **6.1 cwd-relative deletion, no root guard (MEDIUM, operationally dangerous).** Resolves `./data`, `./logs` and config from `Path.cwd()` (reset.py:31, 42-43) — `nuke -y` from $HOME would rmtree `~/logs`. Fix: anchor paths to the script's parent project root (`Path(__file__).resolve().parents[1]`), and load `config/local.yaml` over default.
- [x] **6.2 `today` mode over-deletes silently (MEDIUM).** Deletes ALL buffers + ALL engagement state regardless of date (reset.py:213-223) and the confirmation doesn't say so. Fix: state exactly what will be deleted in the confirmation prompt (docs already corrected to match current behavior; if behavior is narrowed instead, update README + CLAUDE.md again).

## Deferred (post-launch, fix when touching these files anyway)

- Duplicate daily reflections on 50+ exchange days / crash mid-marking (`daily_reflection_service.py:128,198-202`) — loop until no unreflected exchanges remain; mark exchanges in the same transaction as the insert.
- FTS ranks by age: add `ORDER BY rank` to both FTS queries (`store.py:446-458`, `:532-542`) — **done in Pass 3.5 below if time allows; one-line each, high retrieval-quality value.**
- Persona-filtered vector search under-fetches: scale `fetch_k` by persona count when `persona_filter` set (`store.py:421, 471-485`).
- Dead config keys: wire or delete (`memory.search.min_similarity/recency_weight`, `context.*`, `conversation.max_chars/persistence`, `logging.level/wire_tap`, `embedding_dimension`); fix `max_exchanges_per_reflection` read level (`daily_reflection_service.py:66`).
- Substring error classification misfires (`openrouter_provider.py:248-276`).
- Repetition guard: scans only first 200 offsets; can false-positive on intentional repetition (`response_parser.py:107-122`).
- `atomic_write_json` lacks fsync (`utils/io.py:17-23`).
- Misleading error when ONNX file missing (`embedding_service.py:121`).
- `INSERT OR REPLACE` bypasses FTS delete triggers; `update_exchange` never re-embeds (latent, `store.py:265,296-323,357`).
- Display-name spoofing in buffer (inherent LLM-injection limitation; accepted).

## Pass 3.5 — `src/memory/store.py` (cheap, high value — do with Pass 3 if budget allows)

- [x] **Add `ORDER BY rank` to both FTS queries** (store.py:446-458 exchanges, :532-542 reflections).
- [x] **Scale `fetch_k` when `persona_filter` is set** (multiply by number of personas, store.py:421).

## Verification

After each pass: `python -m py_compile` on touched files, then `python scripts/preflight.py`. After all passes: start the bot and smoke-test ping-gating + a forced-persona ping with the network model. `python scripts/model_smoke.py` exercises prompt+parser without Discord.
