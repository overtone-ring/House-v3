# House-v3

Multi-persona Discord bot -- one LLM call generates all five persona responses as structured JSON. No per-persona routing, no arbitrator. Single unified prompt, single model call, structured dispatch.

Five personas (Elvira, Frank, Zagna, Vireline, Ellie) each run as their own Discord bot. A sixth "Watcher" bot listens for messages and coordinates the pipeline.

## Prerequisites

- Python 3.11+
- Linux or macOS (Windows via WSL works)
- 6 Discord bot applications (one per persona + Watcher)
- OpenRouter API key

### System packages

Linux (Debian/Ubuntu):
```bash
sudo apt install espeak-ng    # Required for TTS
```

macOS:
```bash
brew install espeak-ng
```

macOS Apple Silicon -- also set this in your shell profile:
```bash
export PYTORCH_ENABLE_MPS_FALLBACK=1
```

## Setup

```bash
git clone https://github.com/overtone-ring/House-v3.git
cd House-v3
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the environment template and fill in your keys:
```bash
cp .env.example .env
```

You need:
- `OPENROUTER_API_KEY` -- LLM provider
- 6 Discord bot tokens (see below)

### Discord bot setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Create 6 applications: Watcher, Elvira, Frank, Zagna, Vireline, Ellie
3. For each application:
   - Go to **Bot** tab
   - Copy the token into your `.env`
   - Enable **Privileged Gateway Intents**: toggle on **Message Content Intent**
4. Invite each bot to your server with the **bot** scope and **Send Messages**, **Read Message History** permissions

## Run

```bash
source .venv/bin/activate
python -m src.discord_bot
```

## Configuration

All settings are in `config/default.yaml`. To override for your environment, create `config/local.yaml` (gitignored) with just the values you want to change.

Key settings:
- `provider.model` -- which LLM to use (via OpenRouter)
- `discord.channels` -- channels to listen in (empty = all)
- `tts.provider` -- set to `kokoro` for voice, or remove to disable
- `comfyui.enabled` -- image generation (requires local ComfyUI server)

## Architecture

See `CLAUDE.md` for full architecture details, key files, and design decisions.

```
User message -> Watcher bot -> UnifiedOrchestrator.process_message()
  -> Context retrieval (parallel memory search across all personas)
  -> Single LLM call (json_mode, unified system prompt)
  -> Response parser (JSON fallback chain + repetition guard)
  -> Dispatch to PersonaClients (each persona is its own Discord bot)
  -> Fire-and-forget post-processing (record to memory, update engagement)
```
