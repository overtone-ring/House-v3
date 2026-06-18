"""
Tests for the solo / per-persona generation path — _generate_persona_scene.

Each addressed persona is generated in its own call, sequentially, with the
prior personas' turns fed in so cross-talk survives. These tests stub the
provider so no network/model is touched.

Run:  python -m unittest tests.test_persona_scene
"""
import asyncio
import unittest
from types import SimpleNamespace

from src.unified_orchestrator import UnifiedOrchestrator, HouseUnavailableError
from src.providers.base import ErrorCategory


class FakeProvider:
    """Records each generate() call and returns scripted responses in order.

    A scripted entry may be a string (returned as result.text) or an Exception
    instance (raised on that call).
    """

    def __init__(self, responses, error_category=ErrorCategory.UNKNOWN):
        self._responses = list(responses)
        self._error_category = error_category
        self.calls = []

    def generate(self, prompt, system_prompt=None, contextual_primer=None,
                 conversation_history=None, json_mode=False):
        self.calls.append({
            "prompt": prompt,
            "system_prompt": system_prompt,
            "contextual_primer": contextual_primer,
            "json_mode": json_mode,
        })
        item = self._responses[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, model="fake", usage=None)

    def classify_error(self, exc):
        return self._error_category


def make_orchestrator(provider, solo_prompts):
    """Build an orchestrator with just the fields the solo path touches."""
    house = UnifiedOrchestrator({"personas": list(solo_prompts.keys())})
    house._provider = provider
    house._solo_prompts = solo_prompts
    return house


SOLO = {
    "elvira": "ELVIRA PROMPT",
    "frank": "FRANK PROMPT",
    "zagna": "ZAGNA PROMPT",
}


def run(coro):
    return asyncio.run(coro)


class TestSoloScene(unittest.TestCase):
    def test_single_persona_one_call_one_turn(self):
        provider = FakeProvider(["darling, hello."])
        house = make_orchestrator(provider, SOLO)

        turns = run(house._generate_persona_scene(
            personas_ordered=["elvira"],
            user_input="[Locke]: hey elvira",
        ))

        self.assertEqual(turns, [{"persona": "elvira", "text": "darling, hello."}])
        self.assertEqual(len(provider.calls), 1)
        # Used Elvira's solo prompt, plain (no json_mode).
        self.assertEqual(provider.calls[0]["system_prompt"], "ELVIRA PROMPT")
        self.assertFalse(provider.calls[0]["json_mode"])

    def test_two_personas_sequential_second_sees_first(self):
        provider = FakeProvider(["frank says hi", "zagna ESCALATES"])
        house = make_orchestrator(provider, SOLO)

        turns = run(house._generate_persona_scene(
            personas_ordered=["frank", "zagna"],
            user_input="[Locke]: hey you two",
        ))

        self.assertEqual([t["persona"] for t in turns], ["frank", "zagna"])
        self.assertEqual(len(provider.calls), 2)
        # First call has no prior-scene block; uses Frank's prompt.
        self.assertEqual(provider.calls[0]["system_prompt"], "FRANK PROMPT")
        self.assertNotIn("In the room just now", provider.calls[0]["contextual_primer"] or "")
        # Second call uses Zagna's prompt AND sees Frank's output.
        self.assertEqual(provider.calls[1]["system_prompt"], "ZAGNA PROMPT")
        primer2 = provider.calls[1]["contextual_primer"] or ""
        self.assertIn("In the room just now", primer2)
        self.assertIn("frank says hi", primer2)

    def test_order_is_preserved(self):
        provider = FakeProvider(["z", "e", "f"])
        house = make_orchestrator(provider, SOLO)
        turns = run(house._generate_persona_scene(
            personas_ordered=["zagna", "elvira", "frank"],
            user_input="[Locke]: hi",
        ))
        self.assertEqual([t["persona"] for t in turns], ["zagna", "elvira", "frank"])

    def test_empty_solo_output_dropped(self):
        # Frank returns whitespace -> dropped; Zagna still speaks.
        provider = FakeProvider(["   ", "zagna real"])
        house = make_orchestrator(provider, SOLO)
        turns = run(house._generate_persona_scene(
            personas_ordered=["frank", "zagna"],
            user_input="[Locke]: hi",
        ))
        self.assertEqual(turns, [{"persona": "zagna", "text": "zagna real"}])

    def test_dropped_persona_not_shown_to_next(self):
        # Frank's empty turn must not appear in Zagna's prior-scene block.
        provider = FakeProvider(["", "zagna real"])
        house = make_orchestrator(provider, SOLO)
        run(house._generate_persona_scene(
            personas_ordered=["frank", "zagna"],
            user_input="[Locke]: hi",
        ))
        primer2 = provider.calls[1]["contextual_primer"] or ""
        self.assertNotIn("In the room just now", primer2)

    def test_generic_failure_drops_one_others_survive(self):
        # First call raises a generic error (classified UNKNOWN) -> "" -> dropped.
        provider = FakeProvider([RuntimeError("boom"), "zagna real"],
                                error_category=ErrorCategory.UNKNOWN)
        house = make_orchestrator(provider, SOLO)
        turns = run(house._generate_persona_scene(
            personas_ordered=["frank", "zagna"],
            user_input="[Locke]: hi",
        ))
        self.assertEqual(turns, [{"persona": "zagna", "text": "zagna real"}])

    def test_rate_limit_raises_house_unavailable(self):
        provider = FakeProvider([RuntimeError("429")],
                                error_category=ErrorCategory.RATE_LIMIT)
        house = make_orchestrator(provider, SOLO)
        with self.assertRaises(HouseUnavailableError):
            run(house._generate_persona_scene(
                personas_ordered=["frank"],
                user_input="[Locke]: hi",
            ))

    def test_contextual_primer_preserved_in_first_call(self):
        provider = FakeProvider(["ok"])
        house = make_orchestrator(provider, SOLO)
        run(house._generate_persona_scene(
            personas_ordered=["elvira"],
            user_input="[Locke]: hi",
            contextual_primer="MEMORIES HERE",
        ))
        self.assertIn("MEMORIES HERE", provider.calls[0]["contextual_primer"])


class TestFormatSceneSoFar(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(UnifiedOrchestrator._format_scene_so_far([]))

    def test_renders_capitalized_labels(self):
        block = UnifiedOrchestrator._format_scene_so_far([
            {"persona": "frank", "text": "hey"},
            {"persona": "zagna", "text": "YO"},
        ])
        self.assertIn("[In the room just now:]", block)
        self.assertIn("Frank: hey", block)
        self.assertIn("Zagna: YO", block)


if __name__ == "__main__":
    unittest.main()
