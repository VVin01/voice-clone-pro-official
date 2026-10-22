"""
voxcpm_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning.
Drop-in replacement: Powered by Fish Audio API ("s2.1-pro-free").
Maintains full backward compatibility for existing Telegram Bot handlers,
worker processes, and Movie Recap pipelines.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Any, Callable

from app.services.fish_audio_engine import (
    FishAudioEngine,
    DEFAULT_BURMESE_CONTROL,
    _synthesize_neural_tts_fallback,
)

logger = logging.getLogger(__name__)


class VoxCPMEngine(FishAudioEngine):
    """
    Drop-in compatibility adapter that routes all VoxCPM speech synthesis
    and voice cloning requests through Fish Audio's 's2.1-pro-free' engine.
    """
    _client_instance = None
    _last_init_time = 0

    def __init__(
        self,
        space_name: Optional[str] = None,
        work_dir: Optional[Path | str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        **kwargs: Any,
    ):
        super().__init__(work_dir=work_dir, log_fn=log_fn)
        self.space_name = space_name or "FishAudio-s2.1-pro-free"

    @classmethod
    def get_client(cls, space_name: Optional[str] = None):
        if cls._client_instance is None:
            cls._client_instance = cls(space_name=space_name)
        return cls._client_instance

    def synthesize(
        self,
        text: str,
        output_wav: Path | str,
        control_instruction: str = DEFAULT_BURMESE_CONTROL,
        reference_wav: Optional[Path | str] = None,
        prompt_text: str = "",
        cfg_value: float = 2.0,
        progress_cb: Optional[Callable[[str], None]] = None,
        retries: int = 2,
        **kwargs: Any,
    ) -> Path:
        """Seamlessly synthesize speech using Fish Audio s2.1-pro-free engine."""
        return super().synthesize(
            text=text,
            output_wav=output_wav,
            control_instruction=control_instruction,
            reference_wav=reference_wav,
            prompt_text=prompt_text,
            progress_cb=progress_cb,
            retries=retries,
            **kwargs,
        )


__all__ = ["VoxCPMEngine", "FishAudioEngine", "DEFAULT_BURMESE_CONTROL"]
