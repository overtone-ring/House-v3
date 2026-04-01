#!/usr/bin/env python3
"""
House-v3 Data Reset
====================

Two modes:
    python scripts/reset.py nuke     — wipe everything (memory, state, buffers, logs)
    python scripts/reset.py today    — wipe only today's data (exchanges, reflections, buffers)

Run from the House-v3 directory.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_config() -> dict:
    """Load config to find data paths."""
    try:
        import yaml
        config_path = Path.cwd() / "config" / "default.yaml"
        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
    except ImportError:
        pass
    return {}


def get_paths(config: dict) -> dict:
    """Resolve all data directories."""
    data_dir = Path(config.get("memory", {}).get("data_dir", "./data"))
    log_dir = Path(config.get("logging", {}).get("log_dir", "./logs"))
    personas = config.get("personas", ["elvira", "vireline", "frank", "zagna", "ellie"])

    return {
        "data_dir": data_dir,
        "log_dir": log_dir,
        "personas": personas,
    }


def confirm(action: str) -> bool:
    """Ask for confirmation."""
    response = input(f"\n⚠️  This will {action}. Type 'yes' to confirm: ")
    return response.strip().lower() == "yes"


def nuke(config: dict, skip_confirm: bool = False) -> None:
    """Delete everything — full factory reset."""
    paths = get_paths(config)
    data_dir = paths["data_dir"]
    log_dir = paths["log_dir"]

    print("🔥 NUKE — Full data reset")
    print(f"   Data dir:  {data_dir.resolve()}")
    print(f"   Log dir:   {log_dir.resolve()}")

    targets = []

    if data_dir.exists():
        targets.append(("Data directory", data_dir))
    if log_dir.exists():
        targets.append(("Log directory", log_dir))

    if not targets:
        print("\n   Nothing to delete — directories don't exist yet.")
        return

    print("\n   Will delete:")
    for label, path in targets:
        file_count = sum(1 for _ in path.rglob("*") if _.is_file())
        print(f"     {label}: {path} ({file_count} files)")

    if not skip_confirm and not confirm("permanently delete ALL data, memory, state, and logs"):
        print("   Cancelled.")
        return

    for label, path in targets:
        shutil.rmtree(path)
        print(f"   ✓ Deleted {label}: {path}")

    print("\n✅ Full reset complete. House-v3 is a blank slate.")


def reset_today(config: dict, skip_confirm: bool = False) -> None:
    """Delete only today's data — exchanges, reflections, and buffers from today."""
    paths = get_paths(config)
    data_dir = paths["data_dir"]
    personas = paths["personas"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"🗓️  RESET TODAY — Removing data from {today}")
    print(f"   Data dir: {data_dir.resolve()}")

    removed = {"exchanges": 0, "reflections": 0, "buffers": 0, "state": 0}

    # ── Preview ──────────────────────────────────────────────
    # Count exchanges from today
    exchanges_file = data_dir / "shared" / "memory" / "exchanges.jsonl"
    today_exchange_count = 0
    if exchanges_file.exists():
        with open(exchanges_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    ts = record.get("timestamp", "")
                    if ts.startswith(today):
                        today_exchange_count += 1
                except json.JSONDecodeError:
                    continue
    print(f"   Exchanges from today: {today_exchange_count}")

    # Count reflections from today
    today_reflection_count = 0
    for persona in personas:
        reflections_file = data_dir / persona / "memory" / "reflections.jsonl"
        if reflections_file.exists():
            with open(reflections_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("date", "") == today:
                            today_reflection_count += 1
                    except json.JSONDecodeError:
                        continue
    print(f"   Reflections from today: {today_reflection_count}")

    # Conversation buffers
    sessions_dir = data_dir / "sessions"
    buffer_count = len(list(sessions_dir.glob("*_conversation.json"))) if sessions_dir.exists() else 0
    print(f"   Active conversation buffers: {buffer_count}")

    if today_exchange_count == 0 and today_reflection_count == 0 and buffer_count == 0:
        print("\n   Nothing to reset for today.")
        return

    if not skip_confirm and not confirm(f"remove today's data ({today})"):
        print("   Cancelled.")
        return

    # ── Remove today's exchanges ─────────────────────────────
    if exchanges_file.exists() and today_exchange_count > 0:
        kept_lines = []
        with open(exchanges_file) as f:
            for line in f:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    record = json.loads(line_stripped)
                    ts = record.get("timestamp", "")
                    if ts.startswith(today):
                        removed["exchanges"] += 1
                        continue
                except json.JSONDecodeError:
                    pass
                kept_lines.append(line_stripped)

        with open(exchanges_file, "w") as f:
            for line in kept_lines:
                f.write(line + "\n")

        # Invalidate exchange vector cache (will rebuild on next search)
        _clear_vector_cache(data_dir / "shared" / "indexes", "exchanges")

    # ── Remove today's reflections ───────────────────────────
    for persona in personas:
        reflections_file = data_dir / persona / "memory" / "reflections.jsonl"
        if not reflections_file.exists():
            continue

        kept_lines = []
        with open(reflections_file) as f:
            for line in f:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    record = json.loads(line_stripped)
                    if record.get("date", "") == today:
                        removed["reflections"] += 1
                        continue
                except json.JSONDecodeError:
                    pass
                kept_lines.append(line_stripped)

        with open(reflections_file, "w") as f:
            for line in kept_lines:
                f.write(line + "\n")

        # Invalidate persona vector cache
        _clear_vector_cache(data_dir / persona / "indexes", "reflections")

    # ── Clear conversation buffers ───────────────────────────
    if sessions_dir.exists():
        for buf_file in sessions_dir.glob("*_conversation.json"):
            buf_file.unlink()
            removed["buffers"] += 1

    # ── Clear today's state ──────────────────────────────────
    state_dir = data_dir / "state"
    if state_dir.exists():
        for persona in personas:
            persona_state = state_dir / persona
            if persona_state.exists():
                # Reset affective and engagement to defaults
                for state_file in ["affective.json", "engagement.json"]:
                    sf = persona_state / state_file
                    if sf.exists():
                        sf.unlink()
                        removed["state"] += 1

    print(f"\n   ✓ Removed {removed['exchanges']} exchanges")
    print(f"   ✓ Removed {removed['reflections']} reflections")
    print(f"   ✓ Cleared {removed['buffers']} conversation buffers")
    print(f"   ✓ Reset {removed['state']} state files")
    print(f"\n✅ Today's data cleared. Historical data preserved.")


def _clear_vector_cache(index_dir: Path, collection_name: str) -> None:
    """Remove vector cache files so they rebuild on next search."""
    for suffix in ["_vectors.npz", "_manifest.json"]:
        cache_file = index_dir / f"{collection_name}{suffix}"
        if cache_file.exists():
            cache_file.unlink()


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="House-v3 Data Reset Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python scripts/reset.py nuke       # Full factory reset
    python scripts/reset.py today      # Remove only today's data
    python scripts/reset.py nuke -y    # Skip confirmation
        """,
    )
    parser.add_argument(
        "mode",
        choices=["nuke", "today"],
        help="'nuke' = delete everything, 'today' = delete only today's data",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()
    config = load_config()

    if args.mode == "nuke":
        nuke(config, skip_confirm=args.yes)
    elif args.mode == "today":
        reset_today(config, skip_confirm=args.yes)


if __name__ == "__main__":
    main()
