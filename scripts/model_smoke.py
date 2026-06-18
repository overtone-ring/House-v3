"""
Model smoke test
================

Hit a model through the REAL unified prompt + JSON parser, so you can see
exactly what the House would produce on a given backend — plus latency,
token usage, and a cost projection at paid rates.

Usage:
    python scripts/model_smoke.py                         # default :free model
    python scripts/model_smoke.py google/gemma-4-31b-it   # paid variant
    python scripts/model_smoke.py qwen/qwen3-235b-a22b-2507

Reads OPENROUTER_API_KEY from .env. Does not touch memory/state — this is a
pure generation + parsing check.
"""

import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Repo root on path so `import src...` works regardless of cwd
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.providers import create_provider_from_config
from src.response_parser import parse_house_turns, parse_house_transcript

# Paid OpenRouter pricing for gemma-4-31b-it, $/1M tokens (in, out)
PRICE_IN, PRICE_OUT = 0.12, 0.37

# Mirrors production input: the watcher tags the current speaker [name]:
# and prepends a [replying to ...] anchor when the message is a Discord reply.
TEST_MESSAGES = [
    "[Locke]: @Girls I've been staring at the same problem for three days and I feel like I'm losing my mind. Tell me something real.",
    "[DieselDave]: elvira, what are you thinking about right now?",
    '[replying to Frank: "You don\'t have to thank me. Just don\'t fall apart when I\'m not looking."]\n[Sarah_K]: wait, do you actually care about us or is this all an act?',
]


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "google/gemma-4-31b-it:free"
    load_dotenv(ROOT / ".env")

    config = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    personas = config["personas"]
    unified_cfg = config.get("unified", {})
    fallback = unified_cfg.get("fallback_persona", personas[0])
    output_format = str(
        unified_cfg.get("output_format", "json" if unified_cfg.get("json_mode", True) else "plain")
    ).lower()

    prov_cfg = dict(config["provider"])
    prov_cfg["model"] = model
    provider = create_provider_from_config(prov_cfg)

    system_prompt = (ROOT / "data" / "personas" / "unified_house.md").read_text().strip()

    print(f"\n=== Smoke test: {model} ===")
    print(f"System prompt: {len(system_prompt)} chars | temp={prov_cfg.get('temperature')}\n")

    total_cost = 0.0
    for i, msg in enumerate(TEST_MESSAGES, 1):
        print(f"\n{'─' * 70}\n[{i}] USER: {msg}\n{'─' * 70}")
        start = time.monotonic()
        try:
            result = provider.generate(
                prompt=msg,
                system_prompt=system_prompt,
                json_mode=(output_format == "json"),
            )
        except Exception as e:
            print(f"  !! generation failed: {type(e).__name__}: {e}")
            continue
        latency = time.monotonic() - start

        parse = parse_house_transcript if output_format == "transcript" else parse_house_turns
        turns = parse(
            result.text, valid_personas=personas, default_persona=fallback
        )

        spoke = list(dict.fromkeys(t["persona"] for t in turns))
        print(f"  routed→ {result.model} | {latency:.1f}s | "
              f"{len(turns)} turn(s), spoke: {', '.join(spoke) or 'NONE'}")
        if result.usage:
            u = result.usage
            cost = u["prompt_tokens"] / 1e6 * PRICE_IN + u["completion_tokens"] / 1e6 * PRICE_OUT
            total_cost += cost
            print(f"  tokens: {u['prompt_tokens']} in / {u['completion_tokens']} out "
                  f"| paid-rate cost: ${cost:.5f}")
        print()
        for turn in turns:
            print(f"  ▸ {turn['persona'].upper()}: {turn['text']}\n")

        if not spoke:
            print(f"  RAW (parse produced nothing):\n  {result.text[:500]}\n")

        time.sleep(3)  # be gentle with :free rate limits

    if total_cost:
        print(f"\n{'=' * 70}\nTotal paid-rate cost for {len(TEST_MESSAGES)} msgs: "
              f"${total_cost:.5f}  (~${total_cost / len(TEST_MESSAGES):.5f}/msg)")
        print(f"At this rate, $37 ≈ {int(37 / (total_cost / len(TEST_MESSAGES))):,} messages")

    # ── Addressed (solo) path ────────────────────────────────────
    # Exercises per-persona generation: a single ping (one isolated call) and a
    # 2-persona sequential scene where the second persona sees the first's turn
    # (cross-talk). Solo-call cost isn't tallied here — this is a qualitative
    # check (clean single voice, Frank never misgenders himself).
    if not unified_cfg.get("per_persona_when_addressed"):
        return

    solo_dir = ROOT / unified_cfg.get("solo_prompt_dir", "data/personas/solo")
    print(f"\n\n{'#' * 70}\n# ADDRESSED PATH (per-persona solo calls)\n{'#' * 70}")

    def solo_call(name, user_msg, prior_block=None):
        path = solo_dir / f"{name}.md"
        if not path.exists():
            print(f"  !! no solo prompt for {name} at {path}")
            return None, 0.0
        sp = path.read_text().strip()
        start = time.monotonic()
        try:
            r = provider.generate(
                prompt=user_msg, system_prompt=sp,
                contextual_primer=prior_block, json_mode=False,
            )
        except Exception as e:
            print(f"  !! {name} solo failed: {type(e).__name__}: {e}")
            return None, time.monotonic() - start
        return r.text.strip(), time.monotonic() - start

    # 1) Single ping — one isolated call, only Frank's identity in context
    single_msg = "[DieselDave]: frank, you doing okay man?"
    print(f"\n[single] USER: {single_msg}")
    text, lat = solo_call("frank", single_msg)
    if text:
        print(f"  frank | {lat:.1f}s\n  ▸ FRANK: {text}\n")
        low = f" {text.lower()} "
        if any(w in low for w in (" she ", " her ", "i'm a girl", "i am a girl")):
            print("  ⚠️  Frank may be misgendering himself — check the output above")

    # 2) Two-persona sequential — Zagna's call sees Frank's turn
    time.sleep(3)
    multi_msg = (
        "[Locke]: frank, zagna — talk me down, I want to rewrite "
        "everything from scratch"
    )
    print(f"\n[sequential] USER: {multi_msg}")
    f_text, f_lat = solo_call("frank", multi_msg)
    prior, total_lat = None, f_lat
    if f_text:
        print(f"  frank | {f_lat:.1f}s\n  ▸ FRANK: {f_text}\n")
        prior = f"[In the room just now:]\nFrank: {f_text}"
    time.sleep(3)
    z_text, z_lat = solo_call("zagna", multi_msg, prior_block=prior)
    total_lat += z_lat
    if z_text:
        print(f"  zagna | {z_lat:.1f}s (sees Frank's turn)\n  ▸ ZAGNA: {z_text}\n")
    print(f"  sequential total latency: {total_lat:.1f}s")


if __name__ == "__main__":
    main()
