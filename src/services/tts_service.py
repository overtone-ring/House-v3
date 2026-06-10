"""
TTS Service
============

Text-to-speech synthesis using Kokoro.
Maps persona names to distinct voices and produces audio files
suitable for Discord attachments.

Provider-agnostic interface: swap Kokoro for another backend
by implementing the same synthesize() contract.
"""

import io
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class TTSService:
    """
    Text-to-speech service with per-persona voice mapping.

    Usage:
        tts = TTSService(config)
        tts.initialize()  # loads model once
        audio_path = tts.synthesize("elvira", "Hello there!")
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._pipeline = None
        self._initialized = False

        # ── Voice mapping ────────────────────────────────────────
        # Defaults — override via config["tts"]["voice_map"]
        self._default_voice_map = {
            "elvira":   "af_heart",
            "vireline": "bf_emma",
            "frank":    "am_adam",
            "zagna":    "af_bella",
            "ellie":    "af_nova",
        }

        tts_config = self.config.get("tts", {})
        self._voice_map = {
            **self._default_voice_map,
            **tts_config.get("voice_map", {}),
        }

        # ── Output settings ──────────────────────────────────────
        self._lang_code = tts_config.get("lang_code", "a")
        self._output_format = tts_config.get("output_format", "wav")
        self._sample_rate = tts_config.get("sample_rate", 24000)
        self._output_dir = Path(
            tts_config.get("output_dir", tempfile.gettempdir())
        )

    # ── Initialization ───────────────────────────────────────────

    def initialize(self) -> None:
        """
        Load the Kokoro pipeline. Call once at startup.
        Keeps the model warm in memory for fast synthesis.
        """
        if self._initialized:
            return

        # ── Check MPS fallback env var (Apple Silicon) ───────
        import os
        import platform
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            mps_fallback = os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK")
            if mps_fallback != "1":
                raise RuntimeError(
                    "Apple Silicon detected but PYTORCH_ENABLE_MPS_FALLBACK is not set.\n"
                    "Kokoro requires this for GPU acceleration on Apple Silicon.\n"
                    "Set it before running:\n\n"
                    "    export PYTORCH_ENABLE_MPS_FALLBACK=1\n\n"
                    "Or add it to your .env file."
                )

        try:
            from kokoro import KPipeline
        except ImportError:
            raise RuntimeError(
                "Kokoro is not installed. Run: pip install kokoro>=0.9.4 soundfile"
            )

        self._pipeline = KPipeline(lang_code=self._lang_code)
        self._initialized = True
        logger.info(
            f"TTS initialized — lang={self._lang_code}, "
            f"voices={list(self._voice_map.keys())}"
        )

    # ── Synthesis ────────────────────────────────────────────────

    def synthesize(
        self,
        persona: str,
        text: str,
        output_path: Optional[Path] = None,
    ) -> Path:
        """
        Synthesize speech for a persona.

        Args:
            persona: Persona name (must be in voice_map).
            text: Text to speak.
            output_path: Optional explicit output path.
                         If None, writes to a temp file.

        Returns:
            Path to the generated audio file.

        Raises:
            RuntimeError: If not initialized or synthesis fails.
            ValueError: If persona has no voice mapping.
        """
        if not self._initialized:
            raise RuntimeError("TTS service not initialized. Call initialize() first.")

        voice = self.get_voice_for_persona(persona)

        # Pick the right lang_code based on voice prefix
        lang_code = self._lang_code_for_voice(voice)

        # Generate audio chunks and concatenate
        import numpy as np

        audio_chunks = []
        generator = self._pipeline(text, voice=voice)

        for _graphemes, _phonemes, audio in generator:
            if audio is not None:
                audio_chunks.append(audio)

        if not audio_chunks:
            raise RuntimeError(f"TTS produced no audio for persona={persona}")

        # Concatenate all chunks
        full_audio = np.concatenate(audio_chunks)

        # Write to file
        if output_path is None:
            import uuid

            # uuid, not id(text): CPython reuses freed object addresses, so
            # id() can collide across concurrent syntheses — one task would
            # overwrite (or unlink) the file another is still uploading.
            self._output_dir.mkdir(parents=True, exist_ok=True)
            output_path = (
                self._output_dir
                / f"tts_{persona}_{uuid.uuid4().hex[:12]}.{self._output_format}"
            )

        self._write_audio(full_audio, output_path)

        logger.debug(
            f"TTS: {persona} ({voice}) → {output_path} "
            f"({len(full_audio) / self._sample_rate:.1f}s)"
        )
        return output_path

    def synthesize_to_bytes(
        self,
        persona: str,
        text: str,
    ) -> bytes:
        """
        Synthesize and return raw audio bytes (WAV format).
        Useful for streaming to Discord without writing to disk.
        """
        if not self._initialized:
            raise RuntimeError("TTS service not initialized. Call initialize() first.")

        voice = self.get_voice_for_persona(persona)
        lang_code = self._lang_code_for_voice(voice)

        import numpy as np

        audio_chunks = []
        generator = self._pipeline(text, voice=voice)

        for _graphemes, _phonemes, audio in generator:
            if audio is not None:
                audio_chunks.append(audio)

        if not audio_chunks:
            raise RuntimeError(f"TTS produced no audio for persona={persona}")

        full_audio = np.concatenate(audio_chunks)

        # Write to in-memory buffer
        import soundfile as sf
        buffer = io.BytesIO()
        sf.write(buffer, full_audio, self._sample_rate, format="WAV")
        buffer.seek(0)
        return buffer.read()

    # ── Voice Mapping ────────────────────────────────────────────

    def get_voice_for_persona(self, persona: str) -> str:
        """Look up the Kokoro voice ID for a persona."""
        persona_lower = persona.lower()
        if persona_lower not in self._voice_map:
            raise ValueError(
                f"No voice mapping for persona '{persona}'. "
                f"Known: {list(self._voice_map.keys())}"
            )
        return self._voice_map[persona_lower]

    def set_voice_for_persona(self, persona: str, voice: str) -> None:
        """Update the voice mapping at runtime."""
        self._voice_map[persona.lower()] = voice
        logger.info(f"Voice mapping updated: {persona} → {voice}")

    @property
    def voice_map(self) -> Dict[str, str]:
        """Current persona → voice mapping."""
        return dict(self._voice_map)

    # ── Internals ────────────────────────────────────────────────

    def _lang_code_for_voice(self, voice: str) -> str:
        """
        Infer the language code from the voice prefix.
        Kokoro voices are prefixed: af_=American Female, bf_=British Female, etc.
        The first letter IS the lang_code.
        """
        if voice and len(voice) >= 2:
            return voice[0]
        return self._lang_code

    def _write_audio(self, audio, output_path: Path) -> None:
        """Write a numpy audio array to file."""
        import soundfile as sf

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        sf.write(str(output_path), audio, self._sample_rate)

    # ── Lifecycle ────────────────────────────────────────────────

    def is_ready(self) -> bool:
        return self._initialized

    def shutdown(self) -> None:
        """Release the pipeline."""
        self._pipeline = None
        self._initialized = False
        logger.info("TTS service shut down")

    def __repr__(self) -> str:
        status = "ready" if self._initialized else "not initialized"
        return f"TTSService({status}, voices={len(self._voice_map)})"
