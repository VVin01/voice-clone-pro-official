"""Regression tests for the Edge-TTS persona fallback (fix 2026-10-29).

Live job 457f294a delivered only ~23 s for an 847-character Burmese
story. Root cause was NOT dropped text: the persona mastering filters
were authored for a 44100 Hz input (leading ``asetrate=44100*ratio``),
but edge_tts always emits a 24000 Hz MP3, so the mastered output was
played ~1.76x too fast / too high and a ~42 s narration shrank to
~23 s. These tests pin down two guarantees:

1. Completeness: long text is split into ordered chunks, EVERY chunk
   is synthesized, and the parts are concatenated in order
   (``_split_persona_text`` is lossless).
2. Duration honesty: mastering a 24 kHz Edge MP3 preserves its
   duration (no sample-rate squeeze).
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from app.services import curated_voices as cv

_FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(
    _FFMPEG is None, reason="ffmpeg is required for persona mastering tests"
)

# One Burmese sentence, repeated to build long synthetic texts.
_SENT = (
    "မောင်သာနိုးက အဘိုးအိုကို ဆိုင်ထဲဖိတ်ခေါ်ပြီး "
    "ပူနွေးတဲ့ လက်ဖက်ရည်တစ်ခွက်နဲ့ မုန့်တွေ ကျွေးမွေးလိုက်ပါတယ်။ "
)


def _make_sine_mp3(path: Path, duration: float, freq: int = 440) -> None:
    res = subprocess.run(
        [
            _FFMPEG, "-v", "error", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={duration:.3f}",
            "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", str(path),
        ],
        capture_output=True,
    )
    if res.returncode != 0:
        pytest.skip("ffmpeg/libmp3lame unavailable for fake Edge MP3s")


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


class _FakeCommunicate:
    """Drop-in replacement for edge_tts.Communicate (no network).

    Records every text it is asked to speak and writes a real 24 kHz
    MP3 whose duration is proportional to the text length (or a fixed
    duration when ``fixed_duration`` is set), so tests can verify that
    all chunks were synthesized and survive mastering unsqueezed.
    """

    calls: list[str] = []
    fixed_duration: float | None = None

    def __init__(self, text, voice, rate="+0%", pitch="+0Hz", **kwargs):
        self.text = text
        self.voice = voice

    async def save(self, path):
        type(self).calls.append(self.text)
        dur = type(self).fixed_duration
        if dur is None:
            dur = max(0.4, len(self.text) * 0.01)
        _make_sine_mp3(Path(path), dur, freq=300 + 40 * len(type(self).calls))


@pytest.fixture
def fake_edge(monkeypatch):
    _FakeCommunicate.calls = []
    _FakeCommunicate.fixed_duration = None
    import edge_tts

    monkeypatch.setattr(edge_tts, "Communicate", _FakeCommunicate)
    return _FakeCommunicate


# ── Chunk splitting ──────────────────────────────────────────────────────


def test_split_persona_text_lossless_and_bounded():
    story = (_SENT * 12).strip()  # ~850 chars, same scale as the live story
    assert len(story) > cv._PERSONA_TTS_CHUNK_CHARS
    chunks = cv._split_persona_text(story)
    assert len(chunks) >= 2
    assert all(len(c) <= cv._PERSONA_TTS_CHUNK_CHARS for c in chunks)
    assert "".join(chunks) == story  # nothing dropped, added, or reordered


def test_split_persona_text_respects_sentence_boundaries():
    story = (_SENT * 12).strip()
    chunks = cv._split_persona_text(story, max_chars=400)
    assert len(chunks) >= 3
    assert all(len(c) <= 400 for c in chunks)
    # Every chunk except possibly the last ends at a sentence ender,
    # because packing never splits a sentence that fits.
    assert all(c.rstrip().endswith("။") for c in chunks)
    assert "".join(chunks) == story


def test_split_persona_text_overlong_single_sentence():
    text = "က" * 2000 + "။"
    chunks = cv._split_persona_text(text)
    assert len(chunks) >= 3
    assert all(len(c) <= cv._PERSONA_TTS_CHUNK_CHARS for c in chunks)
    assert "".join(chunks) == text


def test_split_persona_text_short_and_empty():
    assert cv._split_persona_text("မင်္ဂလာပါ။") == ["မင်္ဂလာပါ။"]
    assert cv._split_persona_text("") == []


# ── Full fallback synthesis ──────────────────────────────────────────────


def test_synthesize_persona_synthesizes_all_chunks_in_order(tmp_path, fake_edge):
    story = (_SENT * 12).strip()
    out = tmp_path / "persona_out.wav"
    cv.synthesize_curated_persona(story, "komin", out)

    expected_chunks = cv._split_persona_text(story)
    assert len(expected_chunks) >= 2
    # Every chunk was sent to Edge exactly once, in order.
    assert fake_edge.calls == expected_chunks
    assert "".join(fake_edge.calls) == story

    # Fake audio totals len(chunk)*0.01 s per chunk; mastering must
    # preserve that total instead of squeezing it (~0.544x before fix).
    expected_total = sum(max(0.4, len(c) * 0.01) for c in expected_chunks)
    duration = _wav_duration(out)
    assert duration == pytest.approx(expected_total, rel=0.15)


def test_synthesize_persona_mastering_preserves_24k_duration(tmp_path, fake_edge):
    fake_edge.fixed_duration = 10.0  # Edge hands us a 10 s, 24 kHz MP3
    out = tmp_path / "persona_single.wav"
    cv.synthesize_curated_persona("မင်္ဂလာပါခင်ဗျာ။", "komin", out)
    assert len(fake_edge.calls) == 1
    # Before the sample-rate fix this came out at ~5.4 s and ~1.76x pitch.
    assert _wav_duration(out) == pytest.approx(10.0, rel=0.12)
