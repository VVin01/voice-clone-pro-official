"""Regression tests for Gemini 3.8 WAV+C2PA container unwrapping (2026-11-13).

Proven by Hammock's probe: ``gemini-3.8-flash-tts`` and
``gemini-3.8-flash-lite-tts`` return ``audio/wav`` containers (RIFF header +
``data`` chunk + a 7,655-byte ``c2pa`` chunk AFTER ``data``), whereas 3.1/2.5
return raw L16 PCM. Previously ``GeminiTTSEngine`` wrote the entire response
bytes as raw PCM, producing ~0.16s of loud digital noise ("signal error" chirp)
at the tail of every 3.8-served segment.

These tests verify:
1. RIFF/WAVE payloads (with odd-length C2PA chunks after or before ``data``,
   with or without trailing padding, plus 0xFFFE extensible PCM and non-24kHz
   sample rates) strip headers + C2PA metadata cleanly and keep ONLY ``data``.
2. Raw L16 payloads (3.1/2.5) pass through untouched.
3. Corrupt/empty RIFF containers never reach segment WAVs.
4. Real ``ffmpeg`` segment synthesis via ``GeminiTTSEngine`` (3.8 flash and
   3.8 lite), a full Recap job (``EdgeSmartSync``), and a full TTS-tab job
   (``_synthesize_tts_audio_sync``) with 3.8 primary all produce strictly
   silent segment tails.
"""
from __future__ import annotations

import io
import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import tts_service
from app.services.recap_engine import gemini_tts_engine as gte


@pytest.fixture(autouse=True)
def _reset_tts_state(monkeypatch):
    if gte._key_rotation is not None:
        for name in ("gemini", "gemini_tts"):
            try:
                gte._key_rotation.health_for(name).reset()
            except Exception:
                pass
    monkeypatch.setattr(gte, "_TTS_MODEL_CURSOR", 0)
    yield
    if gte._key_rotation is not None:
        for name in ("gemini", "gemini_tts"):
            try:
                gte._key_rotation.health_for(name).reset()
            except Exception:
                pass
    monkeypatch.setattr(gte, "_TTS_MODEL_CURSOR", 0)


def _speech_like_pcm(
    lead_silence_s: float = 0.06,
    tone_s: float = 0.45,
    tail_silence_s: float = 0.30,
    sr: int = 24000,
    amp: int = 6500,
) -> bytes:
    lead = b"\x00\x00" * int(sr * lead_silence_s)
    tone = b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * 440.0 * i / sr)))
        for i in range(int(sr * tone_s))
    )
    tail = b"\x00\x00" * int(sr * tail_silence_s)
    return lead + tone + tail


def _c2pa_noise_bytes(size: int = 7655) -> bytes:
    """High-entropy binary payload mimicking a 7,655-byte C2PA manifest."""
    return bytes(((i * 97 + 43) ^ (i >> 3)) & 0xFF for i in range(size))


def _wav_with_c2pa(
    pcm: bytes,
    *,
    c2pa_size: int = 7655,
    sr: int = 24000,
    channels: int = 1,
    bits: int = 16,
    c2pa_after: bool = True,
    pad_odd: bool = True,
    extensible: bool = False,
) -> bytes:
    byte_rate = sr * channels * (bits // 8)
    block_align = channels * (bits // 8)
    if extensible:
        guid = b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        fmt_payload = (
            struct.pack("<HHIIHHHH", 0xFFFE, channels, sr, byte_rate, block_align, bits, 22, bits)
            + struct.pack("<I", 4 if channels == 1 else 3)
            + guid
        )
    else:
        fmt_payload = struct.pack("<HHIIHH", 1, channels, sr, byte_rate, block_align, bits)
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    data_chunk = (
        b"data"
        + struct.pack("<I", len(pcm))
        + pcm
        + (b"\x00" if (len(pcm) & 1) and pad_odd else b"")
    )
    c2pa_payload = _c2pa_noise_bytes(c2pa_size)
    c2pa_chunk = (
        b"c2pa"
        + struct.pack("<I", c2pa_size)
        + c2pa_payload
        + (b"\x00" if (c2pa_size & 1) and pad_odd else b"")
    )
    body = b"WAVE" + fmt_chunk + (data_chunk + c2pa_chunk if c2pa_after else c2pa_chunk + data_chunk)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _wav_peaks(path: Path, tail_ms: int = 80) -> tuple[int, int, int, int]:
    """Return (sample_rate, nframes, body_max_abs, tail_max_abs) for a WAV."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    tail_count = max(1, int(sr * tail_ms / 1000))
    body = samples[:-tail_count] if len(samples) > tail_count else samples
    tail = samples[-tail_count:]
    body_peak = max((abs(s) for s in body), default=0)
    tail_peak = max((abs(s) for s in tail), default=0)
    return sr, nframes, body_peak, tail_peak


# ── Unit tests for RIFF+C2PA unwrapping ────────────────────────────


@pytest.mark.parametrize("after,pad", [(True, True), (True, False), (False, True)])
def test_extract_wav_pcm_strips_header_and_7655_byte_c2pa_chunk(after: bool, pad: bool):
    pcm = _speech_like_pcm()
    fixture = _wav_with_c2pa(pcm, c2pa_size=7655, c2pa_after=after, pad_odd=pad)
    extracted, ch, sw, sr = gte._unwrap_tts_audio(fixture)
    assert (ch, sw, sr) == (1, 2, 24000)
    assert extracted == pcm
    assert not extracted.startswith(b"RIFF")


def test_extract_wav_pcm_handles_extensible_and_custom_rate():
    pcm = _speech_like_pcm(sr=48000, tone_s=0.25)
    fixture = _wav_with_c2pa(pcm, c2pa_size=7655, sr=48000, extensible=True)
    extracted, ch, sw, sr = gte._unwrap_tts_audio(fixture)
    assert (ch, sw, sr) == (1, 2, 48000)
    assert extracted == pcm


def test_raw_l16_pcm_passes_through_unchanged():
    raw_l16 = _speech_like_pcm()
    extracted, ch, sw, sr = gte._unwrap_tts_audio(raw_l16)
    assert extracted == raw_l16
    assert (ch, sw, sr) == (1, 2, 24000)


def test_c2pa_noise_fixture_would_cause_loud_tail_if_not_stripped():
    c2pa_tail = _c2pa_noise_bytes(7655)
    even = c2pa_tail[: len(c2pa_tail) - (len(c2pa_tail) % 2)]
    samples = struct.unpack(f"<{len(even) // 2}h", even)
    assert max(abs(s) for s in samples) > 20000


def test_corrupt_riff_container_rejected_in_ask_instead_of_writing_noise(tmp_path, monkeypatch):
    bad_riff = b"RIFF\x10\x00\x00\x00WAVEfmt "
    good_pcm = _speech_like_pcm(tone_s=0.3)
    good_wav = _wav_with_c2pa(good_pcm, c2pa_size=7655)
    calls: list[str] = []

    def fake_client(_key):
        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                calls.append(model)
                payload = bad_riff if len(calls) == 1 else good_wav
                part = SimpleNamespace(inline_data=SimpleNamespace(data=payload))
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

        return SimpleNamespace(models=_Models())

    monkeypatch.setattr(gte, "_client", fake_client)
    monkeypatch.setattr(
        gte, "_effective_tts_models", lambda: ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
    )
    eng = gte.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path, keys=["k1"], log=lambda _m: None)
    got = eng._ask("မင်္ဂလာပါ", "Charon")
    assert calls == ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
    assert got == good_pcm


def test_duration_gate_uses_unwrapped_pcm_not_c2pa_padding(tmp_path, monkeypatch):
    # Text length 20 -> max plausible = 20 / 9 + 4 = 6.222 s.
    # Give 6.20 s of pure PCM (< bound) plus 7,655-byte C2PA (+0.16s if not stripped -> 6.36s!).
    pcm_under_bound = b"\x00\x01" * int(6.20 * 24000)
    wav_under_bound = _wav_with_c2pa(pcm_under_bound, c2pa_size=7655)
    assert gte._pcm_seconds(wav_under_bound) > gte._max_plausible_seconds(20)
    assert gte._pcm_seconds(pcm_under_bound) < gte._max_plausible_seconds(20)

    def fake_client(_key):
        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                part = SimpleNamespace(inline_data=SimpleNamespace(data=wav_under_bound))
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

        return SimpleNamespace(models=_Models())

    monkeypatch.setattr(gte, "_client", fake_client)
    monkeypatch.setattr(gte, "_effective_tts_models", lambda: ("gemini-3.8-flash-tts",))
    eng = gte.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path, keys=["k1"], log=lambda _m: None)
    assert eng._ask("က" * 20, "Charon") == pcm_under_bound


# ── Real ffmpeg verification: Engine, Recap job & TTS-tab job ───────


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
@pytest.mark.parametrize("model_id", ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"])
def test_generate_channel99_voice_38_wav_c2pa_produces_silent_tail(tmp_path, monkeypatch, model_id):
    pcm = _speech_like_pcm()
    wav_c2pa = _wav_with_c2pa(pcm, c2pa_size=7655)

    def client_for(payload):
        def _factory(_key):
            class _Models:
                def generate_content(self, model=None, contents=None, config=None):
                    part = SimpleNamespace(inline_data=SimpleNamespace(data=payload))
                    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

            return SimpleNamespace(models=_Models())

        return _factory

    monkeypatch.setattr(gte, "_effective_tts_models", lambda: (model_id,))
    monkeypatch.setattr(gte, "_client", client_for(wav_c2pa))
    eng_38 = gte.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / "w38", keys=["k1"], log=lambda _m: None)
    out_38 = eng_38.generate_channel99_voice("မင်္ဂလာပါ", "Charon")

    monkeypatch.setattr(gte, "_effective_tts_models", lambda: ("gemini-3.1-flash-tts-preview",))
    monkeypatch.setattr(gte, "_client", client_for(pcm))
    eng_31 = gte.GeminiTTSEngine(ffmpeg="ffmpeg", work_dir=tmp_path / "w31", keys=["k1"], log=lambda _m: None)
    out_31 = eng_31.generate_channel99_voice("မင်္ဂလာပါ", "Charon")

    sr, _nf, body_peak, tail_peak = _wav_peaks(out_38, tail_ms=80)
    assert sr == 48000
    assert body_peak > 2000
    assert tail_peak == 0, f"Expected silent tail, got peak={tail_peak}"
    # Output WAV from 3.8 WAV+C2PA must be sample-identical to clean 3.1 L16.
    assert out_38.read_bytes() == out_31.read_bytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_recap_job_with_38_primary_has_silent_segment_tails(tmp_path, monkeypatch):
    from app.services.recap_engine.core import EdgeSmartSync

    pcm = _speech_like_pcm(lead_silence_s=0.06, tone_s=0.55, tail_silence_s=0.35)
    wav_c2pa = _wav_with_c2pa(pcm, c2pa_size=7655)
    models_seen: list[str] = []

    def fake_client(_key):
        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                models_seen.append(model)
                part = SimpleNamespace(inline_data=SimpleNamespace(data=wav_c2pa))
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

        return SimpleNamespace(models=_Models())

    monkeypatch.setattr(gte, "_client", fake_client)
    monkeypatch.setattr(
        gte, "_effective_tts_models", lambda: ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
    )
    monkeypatch.setattr(gte, "collect_gemini_keys", lambda: ["recap-test-key"])

    job = tmp_path / "recap_job"
    job.mkdir()
    src = job / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=duration=3:size=320x180:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src),
        ],
        check=True,
    )
    plan = {
        "segments": [
            {"video_start": 0.0, "video_end": 1.5, "narration": "ပထမ စာကြောင်း ဖြစ်ပါတယ်"},
            {"video_start": 1.5, "video_end": 3.0, "narration": "ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်"},
        ]
    }
    (job / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    sync = EdgeSmartSync(
        job_dir=str(job),
        source_path=str(src),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        overlays={"tts_engine": "gemini", "gemini_voice": "Charon", "is_female": False},
        log=lambda _m: None,
    )
    result = sync.run()
    assert Path(result["final"]).is_file()
    assert models_seen[0] == "gemini-3.8-flash-tts"

    out_plan = json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))
    assert out_plan["tts_provider"] == "gemini"
    assert out_plan["fallback_used"] is False
    seg_files = sorted((job / "edge_tts_smart_sync" / "tts_parts").glob("*.wav"))
    assert len(seg_files) == 2
    for seg_wav in seg_files:
        sr, _nf, body_peak, tail_peak = _wav_peaks(seg_wav, tail_ms=80)
        assert sr == 48000
        assert body_peak > 2000
        assert tail_peak == 0, f"Recap segment {seg_wav.name} has noisy tail peak={tail_peak}"

    # Verify muxed final recap video audio track also ends cleanly without chirp noise.
    muxed_wav = tmp_path / "muxed_final_audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(result["final"]), "-vn", "-c:a", "pcm_s16le", str(muxed_wav)],
        check=True,
    )
    _m_sr, _m_nf, m_body_peak, m_tail_peak = _wav_peaks(muxed_wav, tail_ms=80)
    assert m_body_peak > 1000
    # AAC muxing introduces minimal quantization dust (<= ~20 / <-64 dBFS) on pure zeros,
    # whereas unstripped C2PA metadata produces >15,000 peak chirp.
    assert m_tail_peak < 100, f"Recap final muxed audio tail not silent: peak={m_tail_peak}"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_tts_tab_job_with_38_primary_has_silent_segment_tails(tmp_path, monkeypatch):
    pcm = _speech_like_pcm(lead_silence_s=0.06, tone_s=0.50, tail_silence_s=0.35)
    wav_c2pa = _wav_with_c2pa(pcm, c2pa_size=7655)
    models_seen: list[str] = []

    def fake_client(_key):
        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                models_seen.append(model)
                part = SimpleNamespace(inline_data=SimpleNamespace(data=wav_c2pa))
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

        return SimpleNamespace(models=_Models())

    monkeypatch.setattr(gte, "_client", fake_client)
    monkeypatch.setattr(
        gte, "_effective_tts_models", lambda: ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
    )
    monkeypatch.setattr(gte, "collect_gemini_keys", lambda: ["tts-test-key"])
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    # Force 2 chunks so both segment generation and ffmpeg concat are exercised.
    monkeypatch.setattr(tts_service, "split_tts_text", lambda _txt: ["ပထမ စာပိုဒ်", "ဒုတိယ စာပိုဒ်"])

    job = SimpleNamespace(
        id="tts-38-c2pa-job",
        input_text="ပထမ စာပိုဒ်\nဒုတိယ စာပိုဒ်",
        engine="gemini",
        edge_voice=None,
        preset_voice="Charon",
        speed_preset="+0%",
    )
    out_path, duration = tts_service._synthesize_tts_audio_sync(job)

    assert out_path.is_file() and duration > 0.5
    assert models_seen[0] == "gemini-3.8-flash-tts"
    seg_wavs = sorted((tmp_path / "tts_work" / job.id).glob("gemini_*.wav"))
    assert len(seg_wavs) == 2
    for seg_wav in seg_wavs:
        sr, _nf, body_peak, tail_peak = _wav_peaks(seg_wav, tail_ms=80)
        assert sr == 48000
        assert body_peak > 2000
        assert tail_peak == 0, f"TTS segment {seg_wav.name} has noisy tail peak={tail_peak}"

    sr_out, _nf_out, out_body, out_tail = _wav_peaks(out_path, tail_ms=80)
    assert sr_out == 44100
    assert out_body > 2000
    assert out_tail == 0, f"TTS final output has noisy tail peak={out_tail}"
