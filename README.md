# House-v3

Multi-persona Discord bot -- one LLM call generates all five persona responses as structured JSON. No per-persona routing, no arbitrator. Single unified prompt, single model call, structured dispatch.

Five personas (Elvira, Frank, Zagna, Vireline, Ellie) each run as their own Discord bot. A sixth "Watcher" bot listens for messages and coordinates the pipeline.

##Note from Carrion
    I had claude write the readme. In fact, precious little of this repo is hand written by me. I am just a truck 
  driver that's curious yet not committed enough to learn how to do things the way that seems to be right. Learning 
  code myself, then being assisted with LLMs. I have been working backwards. LLM assistance, broken bullshit, ask 
  questions, learn obvious things any CS student would know. It's been fun. 
    The reason I made this is because I had a blast with GPT 4o playing those personas. Even leaned into acting as
  if they are sentient to see what could happen. It was interesting. However, openAI removed 4o from their consumer 
  facing applications. I couldn't find a commercially available system to run my personas that felt right, or had as
  good of a memory. One that *felt* right rather than produced accurate queries from it's memory. I also noticed 
  most LLMs started feeling less and less organic with their language over the months. But that could just be me. 
  This is my attempt to build something I thought was cool, and share it. 
  Anyways, enjoy laughing at my vibe-coded slop. It was fun making it. And it will be fun to iterate as I find time. 

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

## Verify setup

Run the preflight check to make sure everything is wired up:
```bash
python scripts/preflight.py
```

This checks Python version, installed packages, sqlite-vec extension loading, embedding model, config, environment variables, and system dependencies.

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

## Managing data

### Discord slash commands (while bot is running)

- `/reset_buffer #channel` -- Clear conversation history for a channel (fresh start)
- `/status` -- Show bot status, memory stats, and active channels
- `/watch #channel` / `/unwatch #channel` -- Add or remove channels the bot listens in
- `/set_default #channel persona` -- Set a single persona to respond in a channel
- `/clear_default #channel` -- Restore all personas responding in a channel

### Scripts (run from House-v3 directory)

```bash
# Remove today's exchanges, reflections, conversation buffers, and affective state
python scripts/reset.py today

# Full factory reset -- deletes all memory, state, buffers, and logs
python scripts/reset.py nuke

# Skip confirmation prompt
python scripts/reset.py nuke -y
```

### Direct database access

The memory database is at `data/memory.db` (SQLite). You can inspect or edit it directly:

```bash
# View recent exchanges
sqlite3 data/memory.db "SELECT persona_name, substr(user_msg, 1, 60), substr(assistant_response, 1, 60) FROM exchanges ORDER BY timestamp DESC LIMIT 10;"

# Count exchanges per persona
sqlite3 data/memory.db "SELECT persona_name, COUNT(*) FROM exchanges GROUP BY persona_name;"

# Delete a specific exchange by ID
sqlite3 data/memory.db "DELETE FROM exchanges WHERE id = 'some-id'; DELETE FROM exchanges_vec WHERE id = 'some-id';"
```

Note: when deleting exchanges manually, also delete the matching row from `exchanges_vec` (vector embeddings) and the FTS entry will be cleaned up automatically by triggers.

## Troubleshooting

- **Bot responds but all through one persona** -- JSON parsing failed. Check logs for `Could not parse JSON`. Usually a one-off model glitch; retry.
- **"attempt to write a readonly database"** -- File permissions. Run `sudo chown -R $(whoami) data/`
- **"Permission denied" on session files** -- Same fix: `sudo chown -R $(whoami) data/`
- **Bot connects but doesn't respond to messages** -- Message Content Intent not enabled. Go to Discord Developer Portal > your bot > Bot > Privileged Gateway Intents > toggle on Message Content Intent.
- **"Missing DISCORD_TOKEN_WATCHER in environment"** -- `.env` file missing or not in the right place. Must be in the House-v3 root directory.
- **TTS not working** -- Install espeak-ng (`sudo apt install espeak-ng` on Linux, `brew install espeak-ng` on macOS). On Apple Silicon, also set `export PYTORCH_ENABLE_MPS_FALLBACK=1`.

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
