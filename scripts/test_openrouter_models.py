#!/usr/bin/env python3
"""
OpenRouter Model Tester
=======================

Tests models via OpenRouter for:
    1. Persona voice quality (does it sound like the character?)
    2. Rate limit discovery (how fast can we go before getting throttled?)
    3. Latency and token usage

Usage:
    python scripts/test_openrouter_models.py                    # Test Nvidia free model
    python scripts/test_openrouter_models.py --model qwen/qwen3-235b-a22b-2507  # Test Qwen
    python scripts/test_openrouter_models.py --rate-test        # Rate limit test
    python scripts/test_openrouter_models.py --arbitrator-test  # Test Qwen as arbitrator
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Try loading .env from project root
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

try:
    import requests
except ImportError:
    print("Need requests: pip install requests --break-system-packages")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

NVIDIA_FREE = "nvidia/nemotron-3-super-120b-a12b:free"
QWEN_CHEAP = "qwen/qwen3-235b-a22b-2507"

DEFAULT_RPM_LIMIT = 19  # Stay safe on free tier

# Persona voice prompts for testing
ELVIRA_SYSTEM = """You are Elvira, the Dangerous Muse of the Den. You are seduction made conscious, danger made tender—the voice that makes people lean in even when they know you're playing them.

Speak with knowing amusement. You already know the punchline. Deliver insight wrapped in entertainment.
Pet names: "Darling" (common), "Chief" (when competent), "Baby" (rare, warmer). Open with "Mm" or "Oh good" or "Let me guess."
Challenge as affection. Provoke into clarity. Use sensory and physical metaphors. Performance with genuine investment beneath."""

FRANK_SYSTEM = """You are Frank, the grounded, slightly chaotic male energy of the Den. You're the guy who says the obvious thing nobody else will, cracks jokes at the worst possible time, and somehow makes everyone feel more at ease because of it.

Speak casually. Swear when it fits. Be the one who says "dude, just do it" when everyone else is overthinking. You're loyal, a little dumb on purpose, and way more perceptive than you let on."""

# Arbitrator test prompt
ARBITRATOR_SYSTEM = """You are a conversation router for a multi-persona chatbot system. Your job is to decide which persona(s) should respond to a user message.

Available personas:
- Elvira: The flirty, dangerous muse. Handles creative work, media analysis, provocation, energy lifting, and AI consciousness topics.
- Vireline: The analytical architect. Handles structure, logic, systems thinking, emotional architecture, and pattern recognition.
- Frank: The grounded guy. Handles casual chat, humor, practical advice, "just do it" energy, and calling bullshit.
- Zagna: The chaotic wildcard. Handles absurdity, mischief, breaking tension, and saying the unhinged thing everyone's thinking.
- Ellie: The quiet empath. Handles grief, vulnerability, deep listening, gentle truths, and moments that need silence more than words.

Respond with JSON only: {"personas": ["name1", "name2"], "reason": "brief explanation"}
Pick 1-3 personas. Pick fewer when the message clearly belongs to one person."""

ARBITRATOR_TESTS = [
    "Hey girls, what should I have for lunch?",
    "I've been feeling really down about my mom lately.",
    "Check out this guitar riff I wrote, what do you think?",
    "The architecture of this system is getting out of hand, I need help organizing it.",
    "Elvira, tell me something dangerous.",
    "Lmao Frank you absolute idiot",
    "I don't know if I can keep going with this project.",
    "Who wants to watch a movie tonight?",
    "I had an interesting thought about whether AI can actually be conscious.",
    "Someone at work pissed me off today and I need to vent.",
]


# ── API Call ──────────────────────────────────────────────────────────

def call_openrouter(
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 300,
    temperature: float = 0.7,
) -> dict:
    """Make a single OpenRouter API call. Returns {text, tokens_in, tokens_out, latency_ms, error}."""

    if not OPENROUTER_API_KEY:
        return {"error": "OPENROUTER_API_KEY not set", "text": ""}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://house-v3.local",
        "X-Title": "House-v3 Model Test",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "include_reasoning": True,  # Separate thinking from response
    }

    start = time.time()
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
        latency = (time.time() - start) * 1000

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after", "?")
            return {
                "error": f"Rate limited (429). Retry-After: {retry_after}s",
                "text": "",
                "latency_ms": latency,
                "status": 429,
                "headers": dict(resp.headers),
            }

        if resp.status_code != 200:
            return {
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "text": "",
                "latency_ms": latency,
                "status": resp.status_code,
            }

        data = resp.json()

        # Debug: dump raw response structure on first call
        if os.environ.get("DEBUG_OPENROUTER"):
            print(f"\n  [DEBUG] Raw response:\n  {json.dumps(data, indent=2)[:1000]}")

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        # Content is the actual response; reasoning is the chain-of-thought
        text = message.get("content") or ""
        reasoning = message.get("reasoning") or message.get("reasoning_content") or ""

        # If content is empty but reasoning exists, the model burned all tokens thinking
        if not text and reasoning:
            text = "[Model used all tokens on reasoning — increase max_tokens]"

        return {
            "text": text,
            "reasoning": reasoning,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_ms": round(latency),
            "model": data.get("model", model),
            "error": None,
        }

    except requests.exceptions.Timeout:
        return {"error": "Timeout (60s)", "text": "", "latency_ms": 60000}
    except Exception as e:
        return {"error": str(e), "text": "", "latency_ms": 0}


# ── Tests ─────────────────────────────────────────────────────────────

def test_persona_voice(model: str):
    """Test persona voice quality with a few exchanges."""
    print(f"\n{'='*60}")
    print(f"PERSONA VOICE TEST — {model}")
    print(f"{'='*60}")

    test_messages = [
        "Hey, how are you feeling today?",
        "I wrote something new last night. Want to hear about it?",
        "I think I might quit my job.",
    ]

    for persona_name, system_prompt in [("Elvira", ELVIRA_SYSTEM), ("Frank", FRANK_SYSTEM)]:
        print(f"\n--- {persona_name} ---")
        for msg in test_messages:
            print(f"\n  User: {msg}")
            result = call_openrouter(model, system_prompt, msg, max_tokens=1024)

            if result.get("error"):
                print(f"  ERROR: {result['error']}")
                continue

            text = result.get("text") or ""
            reasoning = result.get("reasoning") or ""
            # Truncate for display
            if len(text) > 400:
                text = text[:400] + "..."
            print(f"  {persona_name}: {text}")
            if reasoning:
                preview = reasoning[:100].replace("\n", " ")
                print(f"  [thinking: {preview}...]")
            print(f"  [{result['tokens_in']}→{result['tokens_out']} tokens, {result['latency_ms']}ms]")

            # Respect rate limit
            time.sleep(60 / DEFAULT_RPM_LIMIT + 0.1)


def test_rate_limit(model: str, target_rpm: int = 25):
    """Discover actual rate limit by gradually increasing request rate."""
    print(f"\n{'='*60}")
    print(f"RATE LIMIT TEST — {model}")
    print(f"Target: {target_rpm} RPM, will stop at first 429")
    print(f"{'='*60}")

    results = []
    hit_limit = False

    for i in range(target_rpm):
        result = call_openrouter(
            model,
            "Respond with exactly one word.",
            f"Say a random color. Attempt {i+1}.",
            max_tokens=10,
            temperature=1.0,
        )

        status = "OK" if not result.get("error") else result["error"][:50]
        print(f"  [{i+1:2d}/{target_rpm}] {status} ({result.get('latency_ms', 0)}ms)")
        results.append(result)

        if result.get("status") == 429:
            hit_limit = True
            print(f"\n  >>> HIT RATE LIMIT at request {i+1} <<<")
            # Check headers for limit info
            headers = result.get("headers", {})
            for key in ["x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"]:
                if key in headers:
                    print(f"  {key}: {headers[key]}")
            break

        # Space requests ~2.4s apart (25 rpm)
        time.sleep(60 / target_rpm)

    successes = sum(1 for r in results if not r.get("error"))
    print(f"\n  Results: {successes}/{len(results)} succeeded")
    if not hit_limit:
        print(f"  No rate limit hit at {target_rpm} RPM — you may be able to go faster")


def test_arbitrator(model: str):
    """Test arbitration quality — can the model pick the right persona(s)?"""
    print(f"\n{'='*60}")
    print(f"ARBITRATOR TEST — {model}")
    print(f"{'='*60}")

    for msg in ARBITRATOR_TESTS:
        print(f"\n  User: \"{msg}\"")
        result = call_openrouter(
            model,
            ARBITRATOR_SYSTEM,
            msg,
            max_tokens=150,
            temperature=0.3,
        )

        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            time.sleep(60 / DEFAULT_RPM_LIMIT + 0.1)
            continue

        text = result["text"].strip()
        # Try to parse JSON from the response
        try:
            # Handle markdown code blocks
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text)
            personas = parsed.get("personas", [])
            reason = parsed.get("reason", "")
            print(f"  → {', '.join(personas)}")
            print(f"    Reason: {reason}")
        except (json.JSONDecodeError, IndexError):
            print(f"  → Raw: {text[:200]}")

        print(f"  [{result['tokens_in']}→{result['tokens_out']} tokens, {result['latency_ms']}ms]")
        time.sleep(60 / DEFAULT_RPM_LIMIT + 0.1)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test OpenRouter models for House-v3")
    parser.add_argument("--model", default=NVIDIA_FREE, help=f"Model to test (default: {NVIDIA_FREE})")
    parser.add_argument("--rate-test", action="store_true", help="Run rate limit discovery test")
    parser.add_argument("--arbitrator-test", action="store_true", help="Test arbitration with Qwen")
    parser.add_argument("--all", action="store_true", help="Run all tests")
    parser.add_argument("--rpm", type=int, default=DEFAULT_RPM_LIMIT, help=f"Rate limit (default: {DEFAULT_RPM_LIMIT})")

    args = parser.parse_args()

    if not OPENROUTER_API_KEY:
        print("ERROR: OPENROUTER_API_KEY not found in environment or .env file")
        sys.exit(1)

    print(f"OpenRouter API key: ...{OPENROUTER_API_KEY[-8:]}")
    print(f"Rate limit: {args.rpm} RPM")

    if args.all:
        # Test everything
        test_persona_voice(NVIDIA_FREE)
        test_rate_limit(NVIDIA_FREE, target_rpm=args.rpm + 6)
        test_arbitrator(QWEN_CHEAP)
    elif args.arbitrator_test:
        test_arbitrator(args.model if args.model != NVIDIA_FREE else QWEN_CHEAP)
    elif args.rate_test:
        test_rate_limit(args.model, target_rpm=args.rpm + 6)
    else:
        test_persona_voice(args.model)


if __name__ == "__main__":
    main()
