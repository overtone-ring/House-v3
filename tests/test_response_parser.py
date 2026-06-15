"""
Tests for src/response_parser.py — the scene parser and its fallback chain.

Run:  python -m unittest tests.test_response_parser
(also discovered by pytest if installed)
"""

import json
import unittest

from src.response_parser import (
    parse_house_turns,
    MAX_RESPONSE_CHARS,
    MAX_TURNS,
)

PERSONAS = ["elvira", "frank", "zagna", "vireline", "ellie"]
DEFAULT = "elvira"


def parse(raw):
    return parse_house_turns(raw, valid_personas=PERSONAS, default_persona=DEFAULT)


class TestTurnsFormat(unittest.TestCase):
    def test_basic_turns_array(self):
        raw = json.dumps({"turns": [
            {"speaker": "frank", "text": "yeah no"},
            {"speaker": "zagna", "text": "BOSS."},
        ]})
        turns = parse(raw)
        self.assertEqual(
            turns,
            [{"persona": "frank", "text": "yeah no"},
             {"persona": "zagna", "text": "BOSS."}],
        )

    def test_persona_key_alias(self):
        # "persona" is accepted as an alias for "speaker"
        raw = json.dumps({"turns": [{"persona": "ellie", "text": "i am here"}]})
        self.assertEqual(parse(raw), [{"persona": "ellie", "text": "i am here"}])

    def test_same_persona_multiple_turns_preserved(self):
        raw = json.dumps({"turns": [
            {"speaker": "frank", "text": "one"},
            {"speaker": "zagna", "text": "two"},
            {"speaker": "frank", "text": "three"},
        ]})
        turns = parse(raw)
        self.assertEqual([t["persona"] for t in turns], ["frank", "zagna", "frank"])

    def test_speaker_case_and_whitespace_normalized(self):
        raw = json.dumps({"turns": [{"speaker": "  FRANK  ", "text": "hi"}]})
        self.assertEqual(parse(raw), [{"persona": "frank", "text": "hi"}])

    def test_bare_array(self):
        raw = json.dumps([{"speaker": "vireline", "text": "Confirmed."}])
        self.assertEqual(parse(raw), [{"persona": "vireline", "text": "Confirmed."}])

    def test_invalid_speaker_dropped(self):
        raw = json.dumps({"turns": [
            {"speaker": "gandalf", "text": "you shall not pass"},
            {"speaker": "frank", "text": "who?"},
        ]})
        self.assertEqual(parse(raw), [{"persona": "frank", "text": "who?"}])

    def test_max_turns_cap(self):
        raw = json.dumps({"turns": [
            {"speaker": "frank", "text": f"turn {i}"} for i in range(MAX_TURNS + 5)
        ]})
        self.assertEqual(len(parse(raw)), MAX_TURNS)


class TestLegacyFormat(unittest.TestCase):
    def test_legacy_dict_key_order_is_speaking_order(self):
        # Legacy {persona: text} — dict insertion order is the speaking order
        raw = '{"zagna": "first", "frank": "second"}'
        turns = parse(raw)
        self.assertEqual([t["persona"] for t in turns], ["zagna", "frank"])

    def test_legacy_dict_filters_unknown_keys(self):
        raw = '{"frank": "ok", "not_a_persona": "ignored"}'
        self.assertEqual(parse(raw), [{"persona": "frank", "text": "ok"}])


class TestExtractionFallbacks(unittest.TestCase):
    def test_markdown_fenced_json(self):
        raw = "Here you go:\n```json\n" + json.dumps(
            {"turns": [{"speaker": "frank", "text": "fenced"}]}
        ) + "\n```"
        self.assertEqual(parse(raw), [{"persona": "frank", "text": "fenced"}])

    def test_outermost_brace_slice(self):
        raw = "blah blah " + json.dumps(
            {"turns": [{"speaker": "ellie", "text": "sliced"}]}
        ) + " trailing noise"
        self.assertEqual(parse(raw), [{"persona": "ellie", "text": "sliced"}])

    def test_unparseable_prose_goes_to_default_persona(self):
        raw = "I think the weather is nice today and nothing here is JSON."
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["persona"], DEFAULT)
        self.assertEqual(turns[0]["text"], raw)


class TestSilenceAndEmpty(unittest.TestCase):
    def test_empty_input_yields_placeholder(self):
        turns = parse("   ")
        self.assertEqual(turns, [{"persona": DEFAULT, "text": "[No response generated]"}])

    def test_valid_json_no_usable_turns_is_silence(self):
        # Real JSON, but nothing usable → silence (never post raw braces)
        self.assertEqual(parse(json.dumps({"turns": []})), [])
        self.assertEqual(parse(json.dumps({"turns": [{"speaker": "nope", "text": "x"}]})), [])

    def test_empty_text_turn_dropped(self):
        raw = json.dumps({"turns": [
            {"speaker": "frank", "text": "   "},
            {"speaker": "zagna", "text": "real"},
        ]})
        self.assertEqual(parse(raw), [{"persona": "zagna", "text": "real"}])

    def test_non_string_text_dropped(self):
        raw = json.dumps({"turns": [
            {"speaker": "frank", "text": {"nested": "dict"}},
            {"speaker": "zagna", "text": "real"},
        ]})
        self.assertEqual(parse(raw), [{"persona": "zagna", "text": "real"}])


class TestRepetitionAndTruncation(unittest.TestCase):
    def test_repetition_loop_discarded(self):
        loop = "I am stuck in a loop. " * 60
        raw = json.dumps({"turns": [
            {"speaker": "frank", "text": loop},
            {"speaker": "zagna", "text": "fine"},
        ]})
        turns = parse(raw)
        self.assertEqual(turns, [{"persona": "zagna", "text": "fine"}])

    def test_long_legitimate_text_not_falsely_flagged(self):
        # ~6000 chars of varied prose must survive (length-scaled guard)
        import hashlib
        words = [hashlib.md5(str(i).encode()).hexdigest()[:8] for i in range(900)]
        long_text = " ".join(words)
        raw = json.dumps({"turns": [{"speaker": "vireline", "text": long_text}]})
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["persona"], "vireline")

    def test_runaway_text_truncated(self):
        # Varied content (not repetitive) so it's truncated, not rep-flagged.
        import hashlib
        huge = " ".join(hashlib.md5(str(i).encode()).hexdigest() for i in range(1000))
        self.assertGreater(len(huge), MAX_RESPONSE_CHARS)
        raw = json.dumps({"turns": [{"speaker": "frank", "text": huge}]})
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertLessEqual(len(turns[0]["text"]), MAX_RESPONSE_CHARS)


if __name__ == "__main__":
    unittest.main()
