"""Tests for the Gemini TTS option (production-grade Feature 5).

The google-genai client is faked — no network, no real keys. What is
verified: key collection precedence (values are dummy strings), the
narrator-prompt consistency wrapper, WAV production from PCM through
the real ffmpeg trim/resample chain, quota-exhaustion behaviour with
the fail-fast circuit breaker, and API-level voice validation.
"""
import shutil
import struct
from types import SimpleNamespace

import pytest

from app.services.recap_engine import gemini_tts_engine as g


# ── key collection (dummy values only) ──────────────────────────────────────

def test_collect_keys_precedence():
    env = {
        "GEMINI_API_KEY": "single-key",
        "GEMINI_IMAGE_PROJECT_2_KEY": "img-2",
        "GEMINI_IMAGE_PROJECT_1_KEY": "img-1",
    }
    assert g.collect_gemini_keys(env) == ["img-1", "img-2"]
    assert g.collect_gemini_keys({"GEMINI_API_KEY_3": "k3", "GEMINI_API_KEY_1": "k1"}) == ["k1", "k3"]
    assert g.collect_gemini_keys({"GEMINI_API_KEYS": "a, b ,c"}) == ["a", "b", "c"]
    assert g.collect_gemini_keys({"GEMINI_API_KEY": "only"}) == ["only"]
    assert g.collect_gemini_keys({}) == []
    # Duplicates removed, order kept.
    assert g.collect_gemini_keys({"GEMINI_API_KEYS": "x,x,y"}) == ["x", "y"]


def test_narrator_prompt_consistency_and_gloss_stripping():
    p = g.narrator_prompt("မင်္ဂလာပါ (Hello) ကမ္ဘာကြီး", "Kore")
    assert "Kore" in p and "female" in p
    assert "(Hello)" not in p
    assert "မင်္ဂလာပါ" in p
    p2 = g.narrator_prompt("စာသား", "gemini:Charon")
    assert "Charon" in p2 and "male" in p2


def test_narrator_direction_is_lively_not_flat():
    # Owner verdict on job rc_1793525723 (2026-11-01): the Gemini voice read
    # the script flat and lifeless. The performed-narrator profile (v3) must
    # demand energy and must not carry the old flattening cues.
    p = g.narrator_prompt("စာသား တစ်ကြောင်း", "Charon")
    assert "never flat or robotic" in g.DEFAULT_DIRECTION
    assert "LIVELY, varied conversational pace" in p
    assert "must be" in p and "audible" in p
    assert "[expressive storytelling]" in p
    # The old flattening cues are gone.
    assert "[neutral]" not in p
    assert "steady medium conversational pace" not in p
    assert "controlled emotion" not in p
    # One-narrator guardrails stay: no shouting, no character voices.
    assert "Do not shout" in p
    assert "speaker identity" in p


def _pcm(seconds=0.5, rate=24000):
    return b"".join(struct.pack("<h", 1200) for _ in range(int(seconds * rate)))


class _FakeModels:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def generate_content(self, model=None, contents=None, config=None):
        self.calls += 1
        if self.behaviour == "quota":
            err = RuntimeError("429 RESOURCE_EXHAUSTED: quota")
            err.code = 429
            raise err
        part = SimpleNamespace(inline_data=SimpleNamespace(data=_pcm()))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


def _engine(tmp_path, monkeypatch, behaviour="ok"):
    fake = _FakeModels(behaviour)
    monkeypatch.setattr(g, "_client", lambda key: SimpleNamespace(models=fake))
    eng = g.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, rate="+12%",
        keys=["dummy-key-1", "dummy-key-2"], log=lambda m: None,
    )
    return eng, fake


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_generate_produces_48k_wav(tmp_path, monkeypatch):
    eng, fake = _engine(tmp_path, monkeypatch)
    out = eng.generate_channel99_voice("မင်္ဂလာပါ", "Charon")
    assert out.is_file() and out.stat().st_size > 1000
    probe = shutil.which("ffprobe")
    import subprocess
    r = subprocess.run([probe, "-v", "error", "-show_entries", "stream=sample_rate",
                        "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert r.stdout.strip() == "48000"
    # Cached: a second identical call makes no new API request.
    calls = fake.calls
    assert eng.generate_channel99_voice("မင်္ဂလာပါ", "Charon") == out
    assert fake.calls == calls


def test_quota_exhaustion_trips_circuit_breaker(tmp_path, monkeypatch):
    eng, fake = _engine(tmp_path, monkeypatch, behaviour="quota")
    with pytest.raises(g.GeminiTTSError):
        eng.generate_channel99_voice("စာသား တစ်ကြောင်း", "Kore")
    assert eng.exhausted is True
    calls_after_first_failure = fake.calls
    assert calls_after_first_failure > 0
    # Fail-fast: no further API calls once exhausted.
    with pytest.raises(g.GeminiTTSError):
        eng.generate_channel99_voice("နောက် တစ်ကြောင်း", "Kore")
    assert fake.calls == calls_after_first_failure


def test_unknown_voice_and_missing_keys():
    with pytest.raises(g.GeminiTTSError):
        g.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir="/tmp", keys=[], log=lambda m: None)
    eng = g.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir="/tmp", keys=["k"], log=lambda m: None)
    with pytest.raises(g.GeminiTTSError):
        eng.generate_channel99_voice("text", "NotAVoice")


def test_api_gemini_voice_validation():
    from pydantic import ValidationError

    from app.webapp.api import RecapConfigRequest

    assert RecapConfigRequest(url="https://x", gemini_voice="Charon").gemini_voice == "Charon"
    assert RecapConfigRequest(url="https://x", gemini_voice="gemini:Kore").gemini_voice == "Kore"
    assert RecapConfigRequest(url="https://x", gemini_voice="").gemini_voice is None
    with pytest.raises(ValidationError):
        RecapConfigRequest(url="https://x", gemini_voice="MorganFreeman")
