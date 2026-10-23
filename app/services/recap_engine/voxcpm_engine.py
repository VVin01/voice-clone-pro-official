"""
voxcpm_engine.py - Movie Recap Engine adapter for VoxCPM 2.
Directly wraps app.services.voxcpm_engine for zero-shot multilingual TTS and voice cloning.
"""

from __future__ import annotations

from app.services.voxcpm_engine import (
    VoxCPMEngine,
    DEFAULT_VOXCPM_SPACE,
    DEFAULT_BURMESE_CONTROL,
)

__all__ = ["VoxCPMEngine", "DEFAULT_VOXCPM_SPACE", "DEFAULT_BURMESE_CONTROL"]
