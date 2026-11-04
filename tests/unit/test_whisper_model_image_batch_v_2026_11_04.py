"""Batch V (2026-11-04) — the production image must ship the Whisper model.

Production recap jobs died at the transcription stage [1/4] with the
generic error. Mechanism (code-proven): transcription tries Groq
first; ``WhisperTimestampService._transcribe_groq`` swallows ALL Groq
errors and returns None, handing over to the LOCAL faster-whisper
fallback. The image shipped no pre-downloaded model and set no
HF_HOME, so the model cache lived in the ephemeral
~/.cache/huggingface — wiped by every deploy/restart. In a fresh
container the first Groq refusal forced ``_download_model_once`` to
pull ~139 MB from huggingface.co (3 attempts); its RuntimeError (and
the backend-missing RuntimeError, and NoUsableSpeechError) matched no
classifier needle, so users saw the generic message.

Fix pinned here: the Dockerfile pre-downloads the faster-whisper
model for the code-default ``base`` size into a FIXED HF_HOME inside
the image at build time, so the runtime cache probe
(``download_model(..., local_files_only=True)``) hits with no network
access. The build follows the WHISPER_MODEL build arg so a runtime
override can be matched at build time.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_sets_fixed_hf_home_inside_image():
    # HF_HOME must be an image path (NOT ~/.cache, which is ephemeral,
    # and NOT the persistent disk, whose mount would shadow the bake).
    assert re.search(r"ENV\s+HF_HOME=/opt/hf-cache\b", DOCKERFILE), (
        "Dockerfile must set HF_HOME=/opt/hf-cache so the baked Whisper "
        "model cache survives in the image layer and huggingface_hub "
        "resolves HF_HUB_CACHE from it at runtime."
    )


def test_dockerfile_prefetches_whisper_model_at_build_time():
    # The prefetch must run the same faster-whisper download entry
    # point the runtime uses (faster_whisper.utils.download_model),
    # driven by the WHISPER_MODEL build arg.
    assert re.search(r"ARG\s+WHISPER_MODEL=base\b", DOCKERFILE), (
        "WHISPER_MODEL build arg must default to `base` (the code "
        "default in recap_planner.py / whisper_service.py)."
    )
    prefetch = re.search(
        r"RUN\s+.*from faster_whisper\.utils import download_model.*download_model\(os\.environ\['WHISPER_MODEL'\]\)",
        DOCKERFILE,
    )
    assert prefetch, (
        "Dockerfile must pre-download the Whisper model at build time "
        "via faster_whisper.utils.download_model($WHISPER_MODEL)."
    )
    # The prefetch needs faster-whisper installed, so it must come
    # after the pip install step (and its layer stays cached across
    # app-code-only changes).
    pip_idx = DOCKERFILE.index("pip install --no-cache-dir -r requirements.txt")
    assert prefetch.start() > pip_idx, (
        "Whisper model prefetch must run after pip install."
    )


def test_ensure_model_hits_prepopulated_cache_without_downloading(monkeypatch):
    """With the model already in the HF cache (the baked image),
    _ensure_model must succeed via the local_files_only probe alone —
    the real network download must never be attempted."""
    import faster_whisper
    import faster_whisper.utils

    from app.services.recap_engine.services.whisper_service import (
        WhisperTimestampService,
    )

    calls: list = []

    def fake_download(size, **kwargs):
        calls.append((size, kwargs))
        if kwargs.get("local_files_only"):
            return "/opt/hf-cache/hub/models--Systran--faster-whisper-base/snapshots/x"
        raise AssertionError(
            "network download attempted despite a pre-populated cache"
        )

    monkeypatch.setattr(faster_whisper.utils, "download_model", fake_download)

    class _FakeModel:
        pass

    monkeypatch.setattr(
        faster_whisper, "WhisperModel", lambda *a, **k: _FakeModel()
    )

    svc = WhisperTimestampService(
        model_size="base", backend="faster", log=lambda msg: None
    )
    svc._ensure_model()

    assert svc._active_backend == "faster-whisper"
    assert isinstance(svc._model, _FakeModel)
    # Exactly one call happened: the local cache probe. No download.
    assert len(calls) == 1
    assert calls[0][1].get("local_files_only") is True
