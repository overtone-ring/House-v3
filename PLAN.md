# Unified Generation Architecture Plan

## What's Changing

Replace multi-call architecture (ArbitratorService LLM call + N separate per-persona Orchestrator calls) with a single LLM call where one model inhabits all five personas simultaneously and outputs structured JSON.

**Current:** User msg → Watcher → Arbitrator (LLM call #1) → N × Orchestrator (LLM calls #2..N+1) → Dispatch
**New:** User msg → Watcher → UnifiedOrchestrator (single LLM call) → JSON parse → Dispatch

---

## Part 1: Build the Unified System Prompt

### Create `data/personas/unified_house.md`

Single ~8-10K token prompt combining all persona definitions, voice examples, interaction dynamics, and output format. Structure:

```
# THE HOUSE — Unified Multi-Persona System

## You Are The House
Framing: you are a collective of five personas, respond as JSON.

## Output Format
{"elvira": "response or null", "frank": "response or null", ...}
Rules: null = silent, at least one must speak, 1-3 typical, all five rare.

## Routing Intuition
When each persona speaks — merge arbitrator rules + persona activation patterns.

## Persona Definitions (5 sections)
Each: core traits, tone signature, voice quotes from NotebookLM report, behavioral rules.

## Interaction Dynamics
From General_report.txt + Interplay Framework — how they play off each other.

## Emotional Range
When personas break type — Zagna tender, Vireline sarcastic, etc.

## Hard Rules
Frank is male. User is Locke. No asterisk emotes. Tone over theatrics.
```

**Source material:**
- `house-archive/Persona Summaries.txt` — deep character studies
- `house-archive/Interplay and Expansion Framework.txt` — persona gates, quotas
- `house-archive/FractalMindEngine - 3.0.txt` — identity binding, symbolic compression
- `NotebookLM_reports/General_report.txt` — voice quotes, interaction dynamics
- `data/personas/*.md` — existing persona definitions
- `src/services/arbitrator_service.py` — routing rules to absorb

**Token budget:**
- 5 persona defs + quotes: ~3,000
- Interaction dynamics + emotional range: ~1,500
- Output format + routing: ~800
- Hard rules + framing: ~500
- Memory instructions: ~800
- Buffer: ~1,400
- **Total: ~8,000 tokens**

---

## Part 2: Code Refactor

### Step 1: Foundation (no dependencies)

**1a. Write `data/personas/unified_house.md`**
Pure content work. Can iterate independently.

**1b. Create `src/response_parser.py`**
Standalone JSON parser for model output.

Fallback chain:
1. Direct `json.loads()`
2. Extract from markdown code blocks
3. Find first `{...}` in text
4. Treat entire response as default persona (graceful degradation)

Validates: keys are real persona names, values are str or null, at least one non-null.

### Step 2: Core Infrastructure

**2a. Update `src/providers/base.py` + `openrouter_provider.py`**
Add `json_mode: bool = False` parameter to `generate()`.
In OpenRouter: set `response_format: {"type": "json_object"}` when enabled.

**2b. Create `src/context/unified_manager.py`**
New context manager that retrieves context for ALL personas in one pass:
- Searches memory across all personas in parallel (`asyncio.gather`)
- Aggregates affective state for all personas
- Returns unified context dict

**2c. Add unified formatters to `src/context/formatters.py`**
- `format_unified_memories()` — memories tagged by persona
- `format_unified_affective_primer()` — all personas' states in one block

### Step 3: The Orchestrator

**3a. Create `src/unified_orchestrator.py`**
The centerpiece. Single class:

```python
class UnifiedOrchestrator:
    async def process_message(self, user_input, session_id, user_id,
                              channel_name, conversation_buffer) -> Dict[str, Optional[str]]:
        # 1. Retrieve unified context (memories, state, relationships)
        # 2. Build prompt (unified system prompt + context + history)
        # 3. Single LLM call with json_mode=True
        # 4. Parse JSON response via response_parser
        # 5. Post-process per responding persona (record exchange, update state)
        # Return {"elvira": "response...", "frank": null, ...}
```

**3b. Add `get_history_for_unified_llm()` to `src/conversation/buffer.py`**
New method (doesn't break existing). Assistant turns include persona attribution:
`role: "assistant", content: "[elvira]: response text"`
So the model sees multi-voice history naturally.

### Step 4: Integration

**4a. Update `src/discord_bot/watcher.py`**
Swap orchestrator. Change dispatch loop:
```python
# Old: for result in results: persona = result["persona"]
# New: for persona, text in responses.items(): if text: dispatch
```

**4b. Update `src/discord_bot/runner.py`**
Swap `HouseOrchestrator` → `UnifiedOrchestrator` in initialization.

**4c. Update `config/default.yaml`**
Add unified section, deprecate arbitrator section:
```yaml
unified:
  system_prompt_file: data/personas/unified_house.md
  json_mode: true
  fallback_persona: elvira
```

### Step 5: Memory Recording

In `UnifiedOrchestrator._post_process()`:
```python
for persona_name, response_text in responses.items():
    if response_text is None:
        continue
    await memory_services[persona_name].add_exchange(...)
    state_manager.record_interaction(persona_name)
```
Each responding persona gets their exchange recorded separately for future retrieval.

### Step 6: Deprecate (don't delete)

- `src/services/arbitrator_service.py` — add deprecation note
- `src/orchestrator.py` — keep `Orchestrator` for single-persona testing, deprecate `HouseOrchestrator`

---

## What Stays Unchanged

- `src/memory/store.py` — SQLite store
- `src/memory/models.py` — Exchange/Reflection models
- `src/services/embedding_service.py`
- `src/services/tts_service.py`
- `src/services/state_manager.py`
- `src/services/daily_reflection_service.py`
- `src/discord_bot/persona_client.py` — still posts per persona
- `src/persona/loader.py` — individual persona files kept for reference
- Discord bot fleet — still 5 persona bots + watcher

---

## Files Summary

| File | Action | Complexity |
|------|--------|------------|
| `data/personas/unified_house.md` | CREATE | High (content) |
| `src/response_parser.py` | CREATE | Medium |
| `src/unified_orchestrator.py` | CREATE | High |
| `src/context/unified_manager.py` | CREATE | Medium |
| `src/context/formatters.py` | MODIFY (add funcs) | Low |
| `src/conversation/buffer.py` | MODIFY (add method) | Low |
| `src/providers/base.py` | MODIFY (add param) | Low |
| `src/providers/openrouter_provider.py` | MODIFY (json_mode) | Medium |
| `src/discord_bot/watcher.py` | MODIFY (swap orch) | Medium |
| `src/discord_bot/runner.py` | MODIFY (swap init) | Low |
| `config/default.yaml` | MODIFY | Low |
| `src/services/arbitrator_service.py` | DEPRECATE | Low |
| `src/orchestrator.py` | DEPRECATE HouseOrch | Low |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Malformed JSON from model | Multi-layer fallback parser + OpenRouter response-healing + fallback to default persona |
| Voice blending | Strong voice examples + quotes in prompt; test with different models |
| System prompt too large | Target 8K; measure; trim examples if needed |
| Model ignores JSON format | json_mode API param + strong instruction + fallback parser |
| Routing quality regression | Unified model has MORE context than arbitrator did; should be better |
