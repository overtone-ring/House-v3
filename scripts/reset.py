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
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import apsw
except ImportError:
    print("ERROR: apsw not installed. Run: pip install apsw")
    sys.exit(1)


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

    return {
        "data_dir": data_dir,
        "log_dir": log_dir,
        "db_path": data_dir / "memory.db",
    }


def confirm(action: str) -> bool:
    """Ask for confirmation."""
    response = input(f"\nThis will {action}. Type 'yes' to confirm: ")
    return response.strip().lower() == "yes"


def nuke(config: dict, skip_confirm: bool = False) -> None:
    """Delete everything — full factory reset."""
    paths = get_paths(config)
    data_dir = paths["data_dir"]
    log_dir = paths["log_dir"]

    print("NUKE — Full data reset")
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
        print(f"   Deleted {label}: {path}")

    print("\nFull reset complete. House-v3 is a blank slate.")


def reset_today(config: dict, skip_confirm: bool = False) -> None:
    """Delete only today's data — exchanges, reflections, and buffers from today."""
    paths = get_paths(config)
    data_dir = paths["data_dir"]
    db_path = paths["db_path"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"RESET TODAY — Removing data from {today}")
    print(f"   Database: {db_path.resolve()}")

    if not db_path.exists():
        print("\n   No database found. Nothing to reset.")
        return

    conn = apsw.Connection(str(db_path))
    removed = {"exchanges": 0, "reflections": 0, "buffers": 0, "state": 0}

    # Count what we'll remove
    today_start = f"{today}T00:00:00"
    today_end = f"{today}T23:59:59"

    exchange_count = conn.execute(
        "SELECT COUNT(*) FROM exchanges WHERE timestamp BETWEEN ? AND ?",
        (today_start, today_end),
    ).fetchone()[0]

    reflection_count = conn.execute(
        "SELECT COUNT(*) FROM reflections WHERE date = ?",
        (today,),
    ).fetchone()[0]

    sessions_dir = data_dir / "sessions"
    buffer_count = len(list(sessions_dir.glob("*_conversation.json"))) if sessions_dir.exists() else 0

    print(f"   Exchanges from today: {exchange_count}")
    print(f"   Reflections from today: {reflection_count}")
    print(f"   Active conversation buffers: {buffer_count}")

    if exchange_count == 0 and reflection_count == 0 and buffer_count == 0:
        print("\n   Nothing to reset for today.")
        conn.close()
        return

    if not skip_confirm and not confirm(f"remove today's data ({today})"):
        print("   Cancelled.")
        conn.close()
        return

    # Remove today's exchanges (FTS entries cleaned up by triggers)
    if exchange_count > 0:
        # Get IDs first for vec table cleanup
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM exchanges WHERE timestamp BETWEEN ? AND ?",
            (today_start, today_end),
        ).fetchall()]

        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM exchanges WHERE timestamp BETWEEN ? AND ?",
                (today_start, today_end),
            )
            for eid in ids:
                conn.execute("DELETE FROM exchanges_vec WHERE id = ?", (eid,))
            conn.execute("COMMIT")
            removed["exchanges"] = len(ids)
        except Exception as e:
            conn.execute("ROLLBACK")
            print(f"   ERROR removing exchanges: {e}")

    # Remove today's reflections
    if reflection_count > 0:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM reflections WHERE date = ?",
            (today,),
        ).fetchall()]

        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM reflections WHERE date = ?", (today,))
            for rid in ids:
                conn.execute("DELETE FROM reflections_vec WHERE id = ?", (rid,))
            conn.execute("COMMIT")
            removed["reflections"] = len(ids)
        except Exception as e:
            conn.execute("ROLLBACK")
            print(f"   ERROR removing reflections: {e}")

    conn.close()

    # Clear conversation buffers
    if sessions_dir.exists():
        for buf_file in sessions_dir.glob("*_conversation.json"):
            buf_file.unlink()
            removed["buffers"] += 1

    # Clear affective state
    state_dir = data_dir / "state"
    if state_dir.exists():
        for state_file in state_dir.rglob("*.json"):
            state_file.unlink()
            removed["state"] += 1

    print(f"\n   Removed {removed['exchanges']} exchanges")
    print(f"   Removed {removed['reflections']} reflections")
    print(f"   Cleared {removed['buffers']} conversation buffers")
    print(f"   Reset {removed['state']} state files")
    print(f"\nToday's data cleared. Historical data preserved.")


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
