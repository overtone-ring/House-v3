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
from src.response_parser import parse_house_response

# Paid OpenRouter pricing for gemma-4-31b-it, $/1M tokens (in, out)
PRICE_IN, PRICE_OUT = 0.12, 0.37

TEST_MESSAGES = [
    "@Girls I've been staring at the same problem for three days and I feel like I'm losing my mind. Tell me something real.",
    "elvira, what are you thinking about right now?",
    "Okay strategists — if I want to ship this bot to a 100-person server next week, what's the one thing I absolutely cannot skip?",
]


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "google/gemma-4-31b-it:free"
    load_dotenv(ROOT / ".env")

    config = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    personas = config["personas"]
    fallback = config.get("unified", {}).get("fallback_persona", personas[0])

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
                json_mode=True,
            )
        except Exception as e:
            print(f"  !! generation failed: {type(e).__name__}: {e}")
            continue
        latency = time.monotonic() - start

        responses = parse_house_response(
            result.text, valid_personas=personas, default_persona=fallback
        )

        spoke = [p for p, r in responses.items() if r]
        print(f"  routed→ {result.model} | {latency:.1f}s | spoke: {', '.join(spoke) or 'NONE'}")
        if result.usage:
            u = result.usage
            cost = u["prompt_tokens"] / 1e6 * PRICE_IN + u["completion_tokens"] / 1e6 * PRICE_OUT
            total_cost += cost
            print(f"  tokens: {u['prompt_tokens']} in / {u['completion_tokens']} out "
                  f"| paid-rate cost: ${cost:.5f}")
        print()
        for persona in personas:
            text = responses.get(persona)
            if text:
                print(f"  ▸ {persona.upper()}: {text}\n")

        if not spoke:
            print(f"  RAW (parse produced nothing):\n  {result.text[:500]}\n")

        time.sleep(3)  # be gentle with :free rate limits

    if total_cost:
        print(f"\n{'=' * 70}\nTotal paid-rate cost for {len(TEST_MESSAGES)} msgs: "
              f"${total_cost:.5f}  (~${total_cost / len(TEST_MESSAGES):.5f}/msg)")
        print(f"At this rate, $37 ≈ {int(37 / (total_cost / len(TEST_MESSAGES))):,} messages")


if __name__ == "__main__":
    main()
