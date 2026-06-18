"""
Tests for parse_house_transcript — the labeled-transcript scene parser.

Run:  python -m unittest tests.test_transcript_parser
"""
import unittest

from src.response_parser import parse_house_transcript, MAX_TURNS, MAX_RESPONSE_CHARS

PERSONAS = ["elvira", "frank", "zagna", "vireline", "ellie"]
DEFAULT = "elvira"


def parse(raw):
    return parse_house_transcript(raw, valid_personas=PERSONAS, default_persona=DEFAULT)


class TestBasic(unittest.TestCase):
    def test_simple_two_turns(self):
        raw = "Frank: yeah no\nZagna: BOSS."
        self.assertEqual(
            parse(raw),
            [{"persona": "frank", "text": "yeah no"},
             {"persona": "zagna", "text": "BOSS."}],
        )

    def test_same_persona_multiple_turns(self):
        raw = "Frank: one\nZagna: two\nFrank: three"
        self.assertEqual([t["persona"] for t in parse(raw)], ["frank", "zagna", "frank"])

    def test_case_insensitive_label(self):
        self.assertEqual(parse("FRANK: hi"), [{"persona": "frank", "text": "hi"}])
        self.assertEqual(parse("vireline: Confirmed."),
                         [{"persona": "vireline", "text": "Confirmed."}])

    def test_action_asterisks_preserved(self):
        raw = "Elvira: *she smiles* Darling."
        self.assertEqual(parse(raw), [{"persona": "elvira", "text": "*she smiles* Darling."}])


class TestMultiLine(unittest.TestCase):
    def test_continuation_lines_join_into_turn(self):
        raw = "Ellie: You're tired.\nNot the kind sleep fixes.\nI'm here."
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["persona"], "ellie")
        self.assertEqual(turns[0]["text"], "You're tired.\nNot the kind sleep fixes.\nI'm here.")

    def test_blank_line_between_turns(self):
        raw = "Frank: first\n\nZagna: second"
        self.assertEqual(
            parse(raw),
            [{"persona": "frank", "text": "first"},
             {"persona": "zagna", "text": "second"}],
        )

    def test_leading_prose_before_first_label_ignored(self):
        raw = "Here's the scene:\nFrank: hi"
        self.assertEqual(parse(raw), [{"persona": "frank", "text": "hi"}])


class TestTolerance(unittest.TestCase):
    def test_bracketed_label_echoed_from_history(self):
        raw = "[frank]: hi\n[zagna]: yo"
        self.assertEqual(
            parse(raw),
            [{"persona": "frank", "text": "hi"}, {"persona": "zagna", "text": "yo"}],
        )

    def test_bold_label(self):
        self.assertEqual(parse("**Frank**: hi"), [{"persona": "frank", "text": "hi"}])

    def test_invalid_speaker_line_is_continuation(self):
        # "Gandalf:" isn't a persona, so it folds into the prior turn's text
        raw = "Frank: who is\nGandalf: you shall not pass"
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["persona"], "frank")
        self.assertIn("Gandalf:", turns[0]["text"])


class TestGuardsAndFallback(unittest.TestCase):
    def test_empty_turn_dropped(self):
        raw = "Frank:   \nZagna: real"
        self.assertEqual(parse(raw), [{"persona": "zagna", "text": "real"}])

    def test_max_turns_cap(self):
        raw = "\n".join(f"Frank: turn {i}" for i in range(MAX_TURNS + 5))
        self.assertEqual(len(parse(raw)), MAX_TURNS)

    def test_empty_input_placeholder(self):
        self.assertEqual(parse("   "), [{"persona": DEFAULT, "text": "[No response generated]"}])

    def test_json_fallback_when_no_labels(self):
        # Model emitted JSON anyway — should still parse via the fallback chain
        raw = '{"turns": [{"speaker": "frank", "text": "json still works"}]}'
        self.assertEqual(parse(raw), [{"persona": "frank", "text": "json still works"}])

    def test_labelless_prose_goes_to_default(self):
        raw = "just some prose with no speaker labels at all"
        turns = parse(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["persona"], DEFAULT)

    def test_repetition_loop_discarded(self):
        loop = "I am stuck in a loop. " * 60
        raw = f"Frank: {loop}\nZagna: fine"
        self.assertEqual(parse(raw), [{"persona": "zagna", "text": "fine"}])


if __name__ == "__main__":
    unittest.main()
