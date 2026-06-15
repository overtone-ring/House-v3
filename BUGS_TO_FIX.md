# Bugs To Fix

Consolidated from the Opus + Grok code reviews. Every item below was verified
against the actual source (file/line checked, not parroted from the review text).
Ordered roughly by priority: confirmed functional bugs first, then robustness,
then doc/cleanup.

> **Status 2026-06-14:** Re-verified all 14 against current source and fixed the
> clear low-risk bugs. **Done:** #1, #2 (already fixed earlier), #3, #4, #6,
> #9 (partially, pre-existing), #11 wire_tap (implemented), #12 (already clean).
> **Open / needs a decision before churning:** #5, #7, #8, #10, #13, #14.
> Completed items are struck through below.

---

## Confirmed functional bugs (fix these first)

### ~~1. `/set_default` and `/clear_default` do nothing~~ ✅ FIXED 2026-06-14
- Wired `channel_default` into `on_message`: a channel with a standing default
  answers every message there as that persona (opt-in; ping gate unchanged when
  no default is set). `@Girls` and explicit pings still win over the default.
- **Where:** `src/discord_bot/watcher.py`
- **What:** `_channel_defaults` is stored, persisted, listed in `/channels`
  (`watcher.py:160`), and pruned — but **nothing reads it in the message path**.
  `on_message` gates purely on pings, and `forced_personas` (`watcher.py:376`)
  comes only from `@mentions`. The slash command tells the user the channel will
  "always respond (arbitrator bypassed)" but that default persona is never consulted.
- **Fix:** In `on_message`, when a watched channel has a default and no ping
  triggered, set `forced_personas = {default}` and proceed. (Or remove the commands.)

### ~~2. Ping a specific persona + JSON parse failure = total silence~~ ✅ ALREADY FIXED
- Fixed in an earlier session: when `forced_personas` is set and the filter would
  discard the entire generation, the orchestrator reroutes the fallback text to an
  addressed persona instead of going silent (`unified_orchestrator.py`, the
  "Forced-persona filter would discard the ENTIRE generation" reroute block).

### ~~3. `min_similarity` is plumbed but never applied~~ ✅ FIXED 2026-06-14
- Removed the dead param from `search_exchanges` / `_search_exchanges_sync` and
  deleted the `memory.search.min_similarity` config key. (RRF scores aren't raw
  similarities, so a real floor would need to filter on vector distance — removal
  was the honest fix rather than faking a threshold.)

---

## Robustness / correctness

### ~~4. Blocking file I/O on the event loop~~ ✅ FIXED 2026-06-14
- Wrapped `record_interaction` (orchestrator `_post_process`) and `buffer.save`
  (watcher) in `asyncio.to_thread`. Both were synchronous JSON writes running on
  the event loop.

### 5. Single shared APSW connection used from many threads, reads unlocked
- **Where:** `src/memory/store.py`
- **What:** One `apsw.Connection` dispatched across worker threads via `to_thread`.
  Writes take `_write_lock`; reads don't. A search can run on the shared connection
  in one thread while a write transaction runs in another. SQLite serialized mode
  protects the data, but this is the pattern that surfaces as intermittent
  "library used incorrectly" / locked errors under concurrent channels.
- **Status:** Potential, not confirmed. Verify under multi-channel load before
  trusting it. Cleaner: per-thread connection / pool, or one serialized DB worker.

### 6. Non-atomic writes for watch state and archive — ✅ watch state FIXED 2026-06-14
- **Watch state:** now uses `atomic_write_json` (a crash here would silently
  unwatch every channel on next start — the real risk). Done.
- **Archive append** (`_archive_turns`, `open(...,"a")`): left as-is. A crash
  mid-append can only corrupt the last line of a best-effort log; an atomic
  rewrite of a growing file would be wasteful. Documented trade-off, not fixed.

### 7. Response parser JSON extraction is brittle — ⏸ OPEN (partly stale)
- Re-verified: the brittle one-level brace regex described here is **gone**. Current
  `_try_parse_json` is 3 strategies: direct `json.loads`, markdown-fence extract,
  and an outermost-brace slice (`text[first_brace:last_brace+1]`). No fragile
  balanced-brace matcher remains.
- Still true: the outermost-brace slice is a heuristic, and there's no
  trailing-comma / `json-repair` layer. With `json_mode` on (Gemma) this hasn't
  bitten. **Decision needed:** add `json-repair` as a final strategy, or leave it.
  Low urgency — the #2 dead-air path it fed is already closed.

---

## Design improvements (not bugs — decide if they bite before churning code)

### ~~8. 5× user-message duplication in memory~~ ✅ FIXED 2026-06-14 (render-side)
- Fixed at the **render** layer, not storage. Storage keeps one row per persona
  on purpose — each embeds independently so a persona can filter recall to its own
  lines (the `Exchange` docstring); collapsing rows would break per-persona memory.
- The visible waste was in the prompt: `format_unified_context` printed the same
  `user_msg` once per persona. Now `_format_memory_block` groups exchanges by
  `(user_msg, date)` and renders the user line once with each persona's reply
  beneath it. Verified on live data: 12 retrieved rows → 3 distinct user lines.
- Covered by `tests/test_formatters.py`.

### 9. Per-persona recall can starve under a shared DB — ⏸ PARTIALLY MITIGATED
- Re-verified: `fetch_k` is now `top_k * 6 if persona_filter else top_k + 5`
  (was `*3`), with a comment about the 5-persona fan-out. So the over-fetch already
  covers the worst case (one user line → up to 5 near-identical rows). Tied to #8:
  if user-message duplication is collapsed, the fan-out pressure mostly disappears
  and this stops mattering. **Decision:** leave as-is unless #8 changes the model.

### 10. `max_chars` (50k) never enforced in the unified path
- **Where:** conversation buffer / `get_history_for_unified_llm`
- **What:** Buffer trims by turn count only (`limit=50`), not characters. Long
  messages × 50 turns could overflow context. `approx_token_count()` exists but
  nothing calls it. (Grok flagged the same: token_counter present but never wired in.)
- **Fix:** Enforce a char/token budget when assembling unified history.

---

## Dead code / doc drift (low risk, cleanup)

### 11. Unused subsystems still present — ⏸ wire_tap DONE, rest intentionally kept
- ~~`wire_tap` never implemented~~ — **implemented 2026-06-14**: full raw-payload
  logging to `wire.jsonl` via `src/utils/wire_log.py`, plus the dashboard reads it.
- `src/utils/token_counter.py`, `src/context/manager.py`, affective-state dims, and
  the relationship write path remain — all **documented dead code, intentionally
  kept** (see CLAUDE.md / ARCHITECTURE.md "What's Dead Code"). Not bugs; leave until
  a feature needs them (e.g. relationship write path when the user designs that).

### ~~12. Docs reference files that don't exist~~ ✅ ALREADY CLEAN
- Re-verified: current `CLAUDE.md` does **not** reference `src/persona/loader.py`,
  `assembler.py`, or `memory_instructions.py`, and `src/persona/` does not exist.
  The stale rows were already removed in a prior docs pass. Nothing to do.

### 13. General doc drift — ⏸ OPEN (low priority; Windows item N/A)
- Model is correctly "Gemma 4 31B" in current docs. Any lingering "arbitrator"
  mentions in docstrings (watcher header, `persona_client.send_response`) are worth
  a cleanup pass but cosmetic.
- ~~Windows setup notes~~ — **N/A**: this repo targets macOS/WSL; the reviewer's
  "native Windows" assumption was wrong (per Locke). No Windows docs needed.

### 14. No tests — ✅ STARTED 2026-06-14 (highest-leverage paths covered)
- Added stdlib-`unittest` suites (zero new deps; pytest discovers them too):
  - `tests/test_response_parser.py` — all fallback paths, legacy format, repetition
    guard, truncation, silence/empty, MAX_TURNS, invalid speakers.
  - `tests/test_forced_personas.py` — the `apply_forced_personas` filter + dead-air
    reroute (extracted from the orchestrator into a pure function for testability).
  - `tests/test_formatters.py` — the #8 memory-grouping renderer.
- Run: `python -m unittest discover -s tests` (33 tests, all passing).
- **Still uncovered** (future): hybrid search + RRF, buffer attribution, full
  orchestrator/dispatch roundtrip. These need DB/provider fixtures — left for later.

---

## ⚠️ DO NOT DELETE THIS FILE

**Remove this document ONLY after every bug above (#1–#14) is fixed.** Until then it
is the working checklist. If you fix some but not all, leave the file and strike
through / mark the completed items — do not delete the whole thing.
