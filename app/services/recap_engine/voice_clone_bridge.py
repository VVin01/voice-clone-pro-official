"""Voice-clone bridge for the Recap Engine — Voice v2 (VoxCPM2 only).

The legacy trained-model (RVC) bridge was removed: there is no local
conversion engine, no .pth model scanning, and no fallback to it.
Recap voices are VoxCPM2 curated personas / reference audio, or Edge TTS.
Callers that ask for a legacy "clone_" voice get a clear error here and
fall back to the base TTS audio in recap_engine/core.py instead of a
silent wrong voice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


def get_available_clone_models() -> Dict[str, Dict[str, Any]]:
    """No legacy trained models are available in Voice v2."""
    return {}


def resolve_clone_model(model_tag: str) -> Optional[Dict[str, Any]]:
    """Legacy model tags no longer resolve to a model."""
    return None


def convert_voice_rvc(
    input_wav: Path | str,
    output_wav: Path | str,
    model_tag: str,
    pitch: int = 0,
    f0_method: str = "rmvpe",
    timeout_seconds: int = 600,
    log_fn: Optional[Any] = None,
) -> Path:
    """Kept as a named entry point so old callers fail clearly, not silently."""
    raise RuntimeError(
        "Legacy trained voice model is no longer supported. "
        "Use a VoxCPM2 curated persona or reference audio instead."
    )
