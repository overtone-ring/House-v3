#!/usr/bin/env python3
"""
House-v3 Preflight Check
=========================

Verifies that the environment is set up correctly before running the bot.
Run from the House-v3 directory:

    python scripts/preflight.py
"""

import os
import sys
from pathlib import Path


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = "OK" if ok else "FAIL"
    msg = f"  [{status}] {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def main():
    print("House-v3 Preflight Check\n")
    failures = 0

    # ── Python version ───────────────────────────────────────
    print("Python:")
    v = sys.version_info
    if not check(f"Python {v.major}.{v.minor}.{v.micro}", v >= (3, 11), "need 3.11+"):
        failures += 1

    # ── Required packages ────────────────────────────────────
    print("\nPackages:")
    packages = {
        "yaml": "pyyaml",
        "dotenv": "python-dotenv",
        "numpy": "numpy",
        "openai": "openai",
        "apsw": "apsw",
        "sqlite_vec": "sqlite-vec",
        "onnxruntime": "onnxruntime",
        "transformers": "transformers",
        "torch": "torch",
        "discord": "discord.py",
    }
    for import_name, pip_name in packages.items():
        try:
            __import__(import_name)
            check(pip_name, True)
        except ImportError:
            check(pip_name, False, f"pip install {pip_name}")
            failures += 1

    # Optional packages
    optional = {"kokoro": "kokoro", "soundfile": "soundfile", "anthropic": "anthropic"}
    for import_name, pip_name in optional.items():
        try:
            __import__(import_name)
            check(f"{pip_name} (optional)", True)
        except ImportError:
            print(f"  [SKIP] {pip_name} (optional) — not installed")

    # ── sqlite-vec extension loading ─────────────────────────
    print("\nSQLite:")
    try:
        import apsw
        import sqlite_vec
        conn = apsw.Connection(":memory:")
        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        conn.enable_load_extension(False)
        # Verify vec0 works
        conn.execute("CREATE VIRTUAL TABLE test_vec USING vec0(embedding float[4])")
        conn.execute("DROP TABLE test_vec")
        check("sqlite-vec extension loads", True)
        conn.close()
    except Exception as e:
        check("sqlite-vec extension loads", False, str(e))
        failures += 1

    # ── Embedding model ──────────────────────────────────────
    print("\nEmbedding model:")
    model_paths = [
        Path("data/models/nomic-embed-text-v1.5-onnx"),
        Path("data/models/nomic-embed-text-v1.5-onnx/onnx/model_quantized.onnx"),
    ]
    model_dir_ok = model_paths[0].exists()
    model_file_ok = model_paths[1].exists()
    if not check("Model directory exists", model_dir_ok, str(model_paths[0])):
        failures += 1
    if model_dir_ok:
        check("Model file exists", model_file_ok)
        if not model_file_ok:
            failures += 1

    # ── Config ───────────────────────────────────────────────
    print("\nConfig:")
    config_path = Path("config/default.yaml")
    if not check("default.yaml exists", config_path.exists()):
        failures += 1
    else:
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            required_keys = ["provider", "personas", "memory", "unified"]
            missing = [k for k in required_keys if k not in config]
            check("Required config sections", not missing,
                  f"missing: {missing}" if missing else "")
            if missing:
                failures += 1
        except Exception as e:
            check("Config parseable", False, str(e))
            failures += 1

    # ── System prompt ────────────────────────────────────────
    print("\nSystem prompt:")
    prompt_path = Path("data/personas/unified_house.md")
    if not check("unified_house.md exists", prompt_path.exists()):
        failures += 1
    else:
        size = prompt_path.stat().st_size
        check(f"Size: {size:,} bytes", size > 1000, "seems too small" if size <= 1000 else "")

    # ── Environment variables ────────────────────────────────
    print("\nEnvironment (.env):")
    env_path = Path(".env")
    if not env_path.exists():
        check(".env file exists", False, "copy .env.example to .env and fill in your keys")
        failures += 1
    else:
        check(".env file exists", True)
        # Load it to check contents
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except ImportError:
            pass

        required_vars = [
            "OPENROUTER_API_KEY",
            "DISCORD_TOKEN_WATCHER",
            "DISCORD_TOKEN_ELVIRA",
            "DISCORD_TOKEN_FRANK",
            "DISCORD_TOKEN_ZAGNA",
            "DISCORD_TOKEN_VIRELINE",
            "DISCORD_TOKEN_ELLIE",
        ]
        for var in required_vars:
            val = os.environ.get(var, "")
            is_set = bool(val) and val not in ("your-key-here", "sk-or-v1-your-key-here")
            if not check(var, is_set, "not set" if not is_set else ""):
                failures += 1

    # ── Data directories (auto-created at runtime) ───────────
    print("\nData directories:")
    for d in ["data", "data/personas", "data/models"]:
        check(d, Path(d).exists())

    # These are created at runtime, just note their status
    for d in ["data/sessions", "data/state"]:
        p = Path(d)
        if p.exists():
            print(f"  [OK]   {d} (exists)")
        else:
            print(f"  [INFO] {d} (will be created on first run)")

    # ── System dependencies ──────────────────────────────────
    print("\nSystem packages:")
    import shutil as sh
    espeak = sh.which("espeak-ng")
    if espeak:
        check("espeak-ng", True, espeak)
    else:
        print("  [SKIP] espeak-ng — not installed (TTS will be disabled)")

    # ── Summary ──────────────────────────────────────────────
    print(f"\n{'=' * 40}")
    if failures == 0:
        print("All checks passed. Ready to run:")
        print("  python -m src.discord_bot")
    else:
        print(f"{failures} check(s) failed. Fix the issues above before running.")
    print()

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
