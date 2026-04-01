"""
Conversation Subsystem
======================

Manages conversation state: turn buffering, history formatting,
sliding window with summarization, and session persistence.

Usage:
    from src.conversation import ConversationBuffer

    buffer = ConversationBuffer(max_turns=50, session_id="session_123")
    buffer.add_user_message("Hello!", speaker_name="Carrion")
    buffer.add_assistant_response("Hey there!", persona="elvira")

    history = buffer.get_history_for_llm(limit=20)
"""

from .buffer import ConversationBuffer, ConversationTurn

__all__ = ["ConversationBuffer", "ConversationTurn"]
