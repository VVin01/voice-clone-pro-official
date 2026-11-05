"""Batch AJ / M11 (engine side) — Edge TTS calls carry a timeout.

Both Edge TTS save paths (app/services/audio.py for the voice worker
and recap_engine/core.py's EdgeTTSEngine) awaited
``communicate.save(...)`` unbounded: a stalled third-party socket
pinned the worker / recap slot forever. Both are now wrapped in
asyncio.wait_for under a named per-call constant.

NOT covered here (parent's files): curated_voices.py's illusory
30 s bound and the worker-level job deadline in app/worker.py.
"""
from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

from app.services import audio as audio_mod
from app.services.recap_engine import core as core_mod


class _SlowCommunicate:
    """edge_tts.Communicate stand-in whose save() hangs."""

    def __init__(self, *args, **kwargs):
        pass

    async def save(self, _path):
        await asyncio.sleep(5)


def _patch_edge(monkeypatch, module, timeout_attr_owner, seconds):
    fake_edge = SimpleNamespace(Communicate=_SlowCommunicate)
    monkeypatch.setitem(sys.modules, "edge_tts", fake_edge)
    if module is not None:
        monkeypatch.setattr(module, "edge_tts", fake_edge, raising=False)
    monkeypatch.setattr(timeout_attr_owner, "EDGE_TTS_TIMEOUT_SECONDS", seconds, raising=False)


@pytest.mark.asyncio
async def test_audio_generate_edge_tts_times_out(monkeypatch, tmp_path):
    _patch_edge(monkeypatch, audio_mod, audio_mod, 0.2)
    started = time.monotonic()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await audio_mod.generate_edge_tts(
            "Hello", tmp_path / "out.mp3", voice="en-US-GuyNeural"
        )
    assert time.monotonic() - started < 10
    assert 0 < audio_mod.EDGE_TTS_TIMEOUT_SECONDS <= 240


@pytest.mark.asyncio
async def test_core_edge_engine_save_times_out(monkeypatch, tmp_path):
    _patch_edge(monkeypatch, None, core_mod, 0.2)
    engine = core_mod.EdgeTTSEngine.__new__(core_mod.EdgeTTSEngine)
    engine.rate = "+0%"
    engine.pitch = "+0Hz"
    started = time.monotonic()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await engine._save_mp3_async("Hello", "en-US-GuyNeural", tmp_path / "x.mp3")
    assert time.monotonic() - started < 10
    assert 0 < core_mod.EDGE_TTS_TIMEOUT_SECONDS <= 240
