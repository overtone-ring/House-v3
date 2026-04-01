"""
Discord Bot Layer
=================

Six-bot fleet architecture:
    Watcher        — Listens to all messages, runs arbitration, dispatches responses
    PersonaClient  — One per persona (x5), sends messages and handles TTS reactions

The Watcher owns the HouseOrchestrator. PersonaClients are thin senders.
All six run on the same asyncio event loop in a single process via runner.py.
"""
