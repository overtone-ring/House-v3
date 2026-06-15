"""
Tests for context formatting — specifically the memory de-duplication that
collapses an @Girls fan-out (one user line stored once per persona) into a
single rendered block (bug #8).

Run:  python -m unittest tests.test_formatters
"""

import unittest

from src.context.formatters import format_unified_context


def exchange_mem(user_msg, persona, response, date="2026-06-14", score=0.5):
    """Build a memory dict shaped like memory_service.search_memory output."""
    return {
        "type": "exchange",
        "content": f"User: {user_msg}\n{persona}: {response}",
        "score": score,
        "id": f"{persona}-{user_msg}",
        "persona_name": persona,
        "timestamp": f"{date}T12:00:00+00:00",
        "record": {
            "user_msg": user_msg,
            "assistant_response": response,
            "persona_name": persona,
            "timestamp": f"{date}T12:00:00+00:00",
        },
    }


class TestMemoryDeduplication(unittest.TestCase):
    def test_fanout_collapses_user_line_to_once(self):
        # Same user message, five persona responses (the @Girls case).
        mems = [
            exchange_mem("what do you all think?", p, f"{p} says hi")
            for p in ["frank", "elvira", "zagna", "vireline", "ellie"]
        ]
        out = format_unified_context(mems)
        # The user line appears exactly once despite five rows.
        self.assertEqual(out.count("User: what do you all think?"), 1)
        # Every persona's response is still present.
        for p in ["frank", "elvira", "zagna", "vireline", "ellie"]:
            self.assertIn(f"{p}: {p} says hi", out)

    def test_distinct_user_messages_stay_separate(self):
        mems = [
            exchange_mem("first question", "frank", "answer one"),
            exchange_mem("second question", "zagna", "answer two"),
        ]
        out = format_unified_context(mems)
        self.assertEqual(out.count("User: first question"), 1)
        self.assertEqual(out.count("User: second question"), 1)

    def test_same_text_different_day_not_merged(self):
        # Identical text on different dates is a different moment — keep both.
        mems = [
            exchange_mem("hey", "frank", "monday", date="2026-06-13"),
            exchange_mem("hey", "frank", "tuesday", date="2026-06-14"),
        ]
        out = format_unified_context(mems)
        self.assertEqual(out.count("User: hey"), 2)

    def test_reflection_rendered_individually(self):
        mems = [{
            "type": "reflection",
            "content": "Frank reflected on the week.",
            "score": 0.9,
            "id": "refl-1",
            "persona_name": "frank",
            "timestamp": "2026-06-14",
        }]
        out = format_unified_context(mems)
        self.assertIn("Frank reflected on the week.", out)
        self.assertIn("[Daily Summary]", out)

    def test_record_missing_falls_back_to_content(self):
        # Defensive: a memory without a structured record still renders.
        mem = {
            "type": "exchange",
            "content": "User: legacy\nfrank: legacy reply",
            "score": 0.5,
            "id": "x",
            "persona_name": "frank",
            "timestamp": "2026-06-14",
        }
        out = format_unified_context([mem])
        self.assertIn("legacy reply", out)

    def test_empty_memories_returns_empty_string(self):
        self.assertEqual(format_unified_context([]), "")

    def test_char_budget_truncates(self):
        big_response = "x" * 500
        mems = [
            exchange_mem(f"q{i}", "frank", big_response, date="2026-06-14")
            for i in range(50)
        ]
        out = format_unified_context(mems, max_memory_chars=1000)
        # Budget keeps the section well under the full unbudgeted size.
        self.assertLess(len(out), 2000)


if __name__ == "__main__":
    unittest.main()
