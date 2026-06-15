"""
Tests for apply_forced_personas — the hard filter that guarantees only
@mentioned personas speak, with a dead-air safeguard (bug #2).

Run:  python -m unittest tests.test_forced_personas
"""

import unittest

from src.unified_orchestrator import apply_forced_personas

FALLBACK = "elvira"


class TestForcedPersonas(unittest.TestCase):
    def test_none_forced_passes_everything_through(self):
        turns = [{"persona": "frank", "text": "a"}, {"persona": "zagna", "text": "b"}]
        out, rerouted = apply_forced_personas(turns, None, FALLBACK)
        self.assertEqual(out, turns)
        self.assertIsNone(rerouted)

    def test_filters_to_addressed_persona(self):
        turns = [
            {"persona": "frank", "text": "addressed"},
            {"persona": "zagna", "text": "not addressed"},
        ]
        out, rerouted = apply_forced_personas(turns, {"frank"}, FALLBACK)
        self.assertEqual(out, [{"persona": "frank", "text": "addressed"}])
        self.assertIsNone(rerouted)

    def test_keeps_multiple_addressed_personas(self):
        turns = [
            {"persona": "frank", "text": "f"},
            {"persona": "zagna", "text": "z"},
            {"persona": "ellie", "text": "e"},
        ]
        out, _ = apply_forced_personas(turns, {"frank", "ellie"}, FALLBACK)
        self.assertEqual([t["persona"] for t in out], ["frank", "ellie"])

    def test_dead_air_reroutes_fallback_text_to_addressed(self):
        # Pinged Frank, but a parse failure routed everything to fallback
        # (elvira). A plain filter would blank it → silence. Reroute to Frank.
        turns = [{"persona": FALLBACK, "text": "the real reply"}]
        out, rerouted = apply_forced_personas(turns, {"frank"}, FALLBACK)
        self.assertEqual(out, [{"persona": "frank", "text": "the real reply"}])
        self.assertEqual(rerouted, "frank")

    def test_dead_air_reroutes_all_turns_when_no_fallback_present(self):
        # Model spoke only as unaddressed non-fallback personas — reroute all.
        turns = [
            {"persona": "zagna", "text": "one"},
            {"persona": "vireline", "text": "two"},
        ]
        out, rerouted = apply_forced_personas(turns, {"frank"}, FALLBACK)
        self.assertEqual(rerouted, "frank")
        self.assertEqual([t["persona"] for t in out], ["frank", "frank"])
        self.assertEqual([t["text"] for t in out], ["one", "two"])

    def test_reroute_target_is_deterministic_lowest_sorted(self):
        turns = [{"persona": FALLBACK, "text": "x"}]
        out, rerouted = apply_forced_personas(turns, {"zagna", "frank"}, FALLBACK)
        self.assertEqual(rerouted, "frank")  # sorted()[0]
        self.assertEqual(out[0]["persona"], "frank")

    def test_empty_generation_stays_empty_no_reroute(self):
        # No turns at all (genuine silence) must NOT trigger a reroute.
        out, rerouted = apply_forced_personas([], {"frank"}, FALLBACK)
        self.assertEqual(out, [])
        self.assertIsNone(rerouted)


if __name__ == "__main__":
    unittest.main()
