"""
Tests for ConversationBuffer.drop_last_user_message — the failed-turn cleanup.

A user message is added to the buffer before generation; if the generation
produces no response (malformed output → silence, or provider unavailable),
that user turn must be removed so a resend doesn't leave it dangling.

Run:  python -m unittest tests.test_buffer
"""
import unittest

from src.conversation.buffer import ConversationBuffer


class TestDropLastUserMessage(unittest.TestCase):
    def _buf(self):
        return ConversationBuffer(session_id="test")

    def test_drops_trailing_user_turn(self):
        b = self._buf()
        b.add_user_message("hello", speaker_name="Locke")
        self.assertTrue(b.drop_last_user_message())
        self.assertEqual(len(b._turns), 0)

    def test_no_op_on_empty_buffer(self):
        b = self._buf()
        self.assertFalse(b.drop_last_user_message())

    def test_does_not_drop_assistant_turn(self):
        b = self._buf()
        b.add_user_message("hi", speaker_name="Locke")
        b.add_assistant_response("hey", persona="frank")
        self.assertFalse(b.drop_last_user_message())
        self.assertEqual(len(b._turns), 2)

    def test_only_drops_one_turn(self):
        b = self._buf()
        b.add_user_message("first", speaker_name="Locke")
        b.add_assistant_response("reply", persona="frank")
        b.add_user_message("second", speaker_name="Locke")
        # only the last (failed) user turn comes off
        self.assertTrue(b.drop_last_user_message())
        self.assertEqual(len(b._turns), 2)
        self.assertFalse(b.drop_last_user_message())  # now trailing assistant
        self.assertEqual(len(b._turns), 2)

    def test_marks_dirty_so_removal_persists(self):
        b = self._buf()
        b.add_user_message("hello", speaker_name="Locke")
        b._dirty = False  # simulate a clean (just-saved) buffer
        b.drop_last_user_message()
        self.assertTrue(b.is_dirty)


if __name__ == "__main__":
    unittest.main()
