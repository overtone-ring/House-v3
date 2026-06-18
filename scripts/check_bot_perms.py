#!/usr/bin/env python3
"""Report each bot's EFFECTIVE permissions in a given channel.

Connects briefly as each of the six bots, asks Discord to resolve the
channel permissions for that bot (base role perms + category/channel
overwrites), prints a table, and disconnects. Read-only.

Usage:
    python scripts/check_bot_perms.py [channel_id]

Default channel_id is the new server's #house-v3.
"""
import asyncio
import os
import sys

import discord

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
except Exception:
    pass

CHANNEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 1516937499426164817

BOTS = {
    "watcher": "DISCORD_TOKEN_WATCHER",
    "elvira": "DISCORD_TOKEN_ELVIRA",
    "frank": "DISCORD_TOKEN_FRANK",
    "zagna": "DISCORD_TOKEN_ZAGNA",
    "vireline": "DISCORD_TOKEN_VIRELINE",
    "ellie": "DISCORD_TOKEN_ELLIE",
}

# The permissions that matter for hearing/speaking in a channel.
WANT = ["view_channel", "read_message_history", "send_messages", "add_reactions"]


async def check(label: str, token: str) -> dict:
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    out = {"label": label}

    @client.event
    async def on_ready():
        try:
            ch = client.get_channel(CHANNEL_ID)
            if ch is None:
                out["error"] = "not in cache (bot can't see this channel's guild?)"
                return
            me = ch.guild.me
            perms = ch.permissions_for(me)
            out["guild"] = ch.guild.name
            out["channel"] = f"#{ch.name}"
            for p in WANT:
                out[p] = getattr(perms, p)
        except Exception as e:  # noqa: BLE001
            out["error"] = repr(e)
        finally:
            await client.close()

    try:
        await client.start(token)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"login failed: {e}"
    return out


async def main():
    print(f"Checking channel {CHANNEL_ID}\n")
    results = []
    for label, key in BOTS.items():
        token = os.environ.get(key)
        if not token:
            results.append({"label": label, "error": f"{key} not set"})
            continue
        results.append(await check(label, token))

    # Header
    hdr = f"{'bot':<9} {'view':>5} {'history':>8} {'send':>5} {'react':>6}  where / error"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        if "error" in r:
            print(f"{r['label']:<9} {'—':>5} {'—':>8} {'—':>5} {'—':>6}  {r['error']}")
            continue
        def m(p):
            return "YES" if r.get(p) else "no"
        where = f"{r.get('guild','?')} {r.get('channel','?')}"
        print(
            f"{r['label']:<9} {m('view_channel'):>5} {m('read_message_history'):>8} "
            f"{m('send_messages'):>5} {m('add_reactions'):>6}  {where}"
        )


if __name__ == "__main__":
    asyncio.run(main())
