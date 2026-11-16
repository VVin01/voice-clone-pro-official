"""VoxCPM segment-seam glitch fix verification (2026-11-16).

Verifies the resolution of production seam clipping and stutter glitches:
1. Production Evidence Job 5e5f9c64 (348 chars):
   - Word "နည်းနည်း" was severed at char 167 into "နည်း" / "နည်း", causing "နှိ-နှီး" stutter
     with two 0.44s gaps around a 0.26s burst between 8.00-9.13s.
   - Word "ခက်ခဲ" was severed into "ခက်" / "ခဲ".
2. Production Evidence Job ed4acbb1 (421 chars):
   - Word "စာရိုက်" was severed at char 167 into "စာ" / "ရိုက်" (~9.7s, 0.46s gap).
   - Word "မျိုးရဲ့" had dependent vowel/tone "ဲ့" severed into an orphaned mark.

This suite tests:
- Safe boundary chunking preserving Burmese words, syllables, and combining clusters.
- Waveform burst-between-gaps seam analysis.
- Edge-clip audio detection (`is_wav_edge_clipped`).
- Worker single-retry on edge-clipped synthesis.
- Duration and billing accounting accuracy.
"""
from __future__ import annotations

import math
import shutil
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.audio import (
    concat_wav_files,
    get_wav_duration_seconds,
    is_wav_edge_clipped,
)
from app.services.curated_voices import get_persona_reference
from app.services.voice_chunking import (
    BURMESE_SYLLABLE_BREAK,
    _is_combining_or_stack,
    chunk_char_budget,
    plan_text_chunks,
)
from app.worker import _synthesize_voxcpm_voice_job


# ---------------------------------------------------------------------------
# Test Fixtures: Production Evidence Texts
# ---------------------------------------------------------------------------

TEXT_5E5F9C64 = (
    "မင်္ဂလာပါအကို Voice Clone Pro Bot ရဲ့ Mini App မှ Persona အသံ 12 မျိုးကို "
    "အစမ်းနားထောင်ကြည့်ဖို့ အတွက် အသံဖန်တီး ဆိုတဲ့ Tab ထဲမှာဝင်ရောက်ပြီး အောက်လေးကို "
    "နည်းနည်း ဆင်းလိုက်ရင် Persona အသံ 12 မျိုးဆိုပြီး ရွေးထားသောအသံ ရဲ့ ဘေးမှာ "
    "ပြောင်းမည်ဆိုတဲ့ ခလုတ်လေးရှိပါတယ် Screenshot လေးလဲပို့ပေးထားပါတယ် "
    "အဆင်မပြေ အသုံးပြုရခက်ခဲသွားစေလို့ တောင်းပန်ပါတယ်နော်။"
)

TEXT_ED4ACBB1 = (
    "မင်္ဂလာပါအကို Voice Clone Pro Bot ရဲ့ Mini App မှာ Persona အသံ 12 မျိုးကို "
    "အစမ်းနားထောင်နည်းလေးပြောပြမယ်နော် ပထမဆုံး အသံဖန်တီး ဆိုတဲ့အထဲဝင်လိုက်ပါ "
    "ပြီးရင်  စာရိုက်တဲ့နေရာရဲ့ အောက်နားလေးမှာ Persona အသံ 12မျိုး နဲ့ ကိုယ်ပိုင်အသံဆိုတာရှိပါတယ် "
    "ကိုယ်ပိုင်အသံလဲ ထည့်သွင်းအသုံးပြုလို့လဲ ရသလို Persona အသံ 12 မျိုးရဲ့ နမူနာအသံတွေကိုလဲ "
    "ပြောင်းမည် ဆိုတဲ့ ခလုတ်လေးကို နှိပ်ပြီး အသံ 12 မျိုးလုံးရဲ့ အသံတွေကို နားထောင်နိုင်ပါတယ်နော်။"
)


# ---------------------------------------------------------------------------
# Waveform Analysis Helpers
# ---------------------------------------------------------------------------

def _write_pcm_wav(
    path: Path,
    seconds: float,
    rate: int = 48000,
    freq: float = 440.0,
    amplitude: float = 0.5,
    envelope: str = "flat",
) -> Path:
    """Generate 16-bit mono PCM test WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(rate * seconds)
    data = bytearray()
    for i in range(n_samples):
        if envelope == "sine":
            env = math.sin(math.pi * i / max(1, n_samples))
        elif envelope == "fade_out":
            env = 1.0 - (i / max(1, n_samples))
        elif envelope == "fade_in":
            env = i / max(1, n_samples)
        else:
            env = 1.0
        sample = int(amplitude * 32767.0 * env * math.sin(2 * math.pi * freq * (i / rate)))
        sample = max(-32768, min(32767, sample))
        data.extend(struct.pack("<h", sample))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data)
    return path


def _write_silence_wav(path: Path, seconds: float, rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def find_bursts_between_gaps(
    wav_path: Path,
    frame_ms: float = 10.0,
    silence_thresh_rms: float = 0.02,
    min_gap_sec: float = 0.20,
    max_burst_sec: float = 0.15,
) -> list[tuple[float, float, float]]:
    """Detect isolated acoustic burst glitches: active bursts <= max_burst_sec
    sandwiched between silences >= min_gap_sec. Returns [(start, end, duration)].
    """
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    samples = struct.unpack(f"<{len(raw)//2}h", raw)
    frame_size = int(rate * (frame_ms / 1000.0))
    if frame_size == 0 or not samples:
        return []

    frames_active = []
    for i in range(0, len(samples), frame_size):
        chunk = samples[i:i + frame_size]
        if not chunk:
            break
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk)) / 32768.0
        frames_active.append(1 if rms >= silence_thresh_rms else 0)

    runs = []
    cur_val = frames_active[0] if frames_active else 0
    cur_count = 0
    for v in frames_active:
        if v == cur_val:
            cur_count += 1
        else:
            runs.append((cur_val, cur_count * (frame_ms / 1000.0)))
            cur_val = v
            cur_count = 1
    if cur_count > 0:
        runs.append((cur_val, cur_count * (frame_ms / 1000.0)))

    glitches = []
    t = 0.0
    for idx, (is_active, dur) in enumerate(runs):
        if is_active == 1 and dur <= max_burst_sec:
            prev_gap = runs[idx - 1][1] if idx > 0 and runs[idx - 1][0] == 0 else 0.0
            next_gap = runs[idx + 1][1] if idx + 1 < len(runs) and runs[idx + 1][0] == 0 else 0.0
            if prev_gap >= min_gap_sec and next_gap >= min_gap_sec:
                glitches.append((round(t, 3), round(t + dur, 3), round(dur, 3)))
        t += dur
    return glitches


# ---------------------------------------------------------------------------
# Unit Tests: Evidence Chunking & Burmese Boundary Safety
# ---------------------------------------------------------------------------

def test_evidence_job_5e5f9c64_preserves_words_under_machaw_budget():
    """Job 5e5f9c64 previously split at char 167, cutting 'နည်းနည်း' into
    'နည်း' and 'နည်း', and cutting 'ခက်ခဲ' into 'ခက်' and 'ခဲ'.
    Verify both compound words remain completely intact in a single chunk."""
    _, ref_text = get_persona_reference("machaw")
    budget = chunk_char_budget(
        control_instruction="A friendly, clear, natural Burmese female human creator and guide, warm articulate delivery",
        reference_transcript=ref_text,
        reference_seconds=20.0,
    )
    chunks = plan_text_chunks(
        TEXT_5E5F9C64,
        control_instruction="A friendly, clear, natural Burmese female human creator and guide, warm articulate delivery",
        reference_transcript=ref_text,
        reference_seconds=20.0,
    )

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= budget

    # Word 'နည်းနည်း' must be wholly inside one chunk
    matches_naynay = [c for c in chunks if "နည်းနည်း" in c]
    assert len(matches_naynay) == 1
    assert "နည်း" not in [c for c in chunks if c != matches_naynay[0]][0]

    # Word 'ခက်ခဲ' must be wholly inside one chunk
    matches_khetkhe = [c for c in chunks if "ခက်ခဲ" in c]
    assert len(matches_khetkhe) == 1

    # Text integrity: all words preserved
    assert "".join(chunks).replace(" ", "") == TEXT_5E5F9C64.replace(" ", "")


def test_evidence_job_ed4acbb1_preserves_words_and_combining_marks():
    """Job ed4acbb1 previously split at char 167, cutting 'စာရိုက်' into
    'စာ' and 'ရိုက်', and severed 'မျိုးရဲ့' leaving orphaned 'ဲ့'.
    Verify words remain intact and no chunk starts with a combining mark."""
    _, ref_text = get_persona_reference("machaw")
    budget = chunk_char_budget(
        control_instruction="A friendly, clear, natural Burmese female human creator and guide, warm articulate delivery",
        reference_transcript=ref_text,
        reference_seconds=20.0,
    )
    chunks = plan_text_chunks(
        TEXT_ED4ACBB1,
        control_instruction="A friendly, clear, natural Burmese female human creator and guide, warm articulate delivery",
        reference_transcript=ref_text,
        reference_seconds=20.0,
    )

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= budget
        # No chunk should start with a combining/stacking mark
        assert not _is_combining_or_stack(chunk[0])

    # Word 'စာရိုက်' must be intact wholly within one chunk
    matches_saryite = [c for c in chunks if "စာရိုက်" in c]
    assert len(matches_saryite) == 1

    # Word 'မျိုးရဲ့' must be intact wholly within one chunk
    matches_myoerae = [c for c in chunks if "မျိုးရဲ့" in c]
    assert len(matches_myoerae) == 1

    # Full text preserved
    assert "".join(chunks).replace(" ", "") == TEXT_ED4ACBB1.replace(" ", "")


def test_chunking_boundary_hierarchy():
    """Test priority order: 1) clause mark '၊', 2) space ' ', 3) syllable break."""
    # 1. Clause mark preferred when available
    clause = "အပိုင်းခွဲ စမ်းသပ်စာသား ဆက်တိုက် ရေးထားတာ ဖြစ်ပါတယ်၊ "
    sentence = (clause * 20).strip()
    chunks = plan_text_chunks(
        sentence,
        reference_transcript="x" * 200,
        reference_seconds=30.0,
    )
    assert len(chunks) >= 2
    for c in chunks[:-1]:
        assert c.endswith("၊")

    # 2. Syllable break used when no spaces or clause marks exist
    unspaced = "မြန်မာစာပေသည်အလွန်စိတ်ဝင်စားဖွယ်ကောင်းသောအလှတရားများစွာရှိပါသည်" * 5
    chunks_unspaced = plan_text_chunks(
        unspaced,
        reference_transcript="x" * 200,
        reference_seconds=30.0,
    )
    assert len(chunks_unspaced) >= 2
    for c in chunks_unspaced:
        assert not _is_combining_or_stack(c[0])


# ---------------------------------------------------------------------------
# Unit Tests: Waveform Seam & Edge Clipping
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_waveform_burst_between_gaps_detector_and_clean_seam(tmp_path):
    """Verify detector catches glitch bursts and passes clean acrossfaded seams."""
    rate = 48000
    glitch_wav = tmp_path / "glitch.wav"
    with wave.open(str(glitch_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        # 0.25s silence + 0.08s burst + 0.25s silence
        wf.writeframes(b"\x00\x00" * int(rate * 0.25))
        data = bytearray()
        for i in range(int(rate * 0.08)):
            v = int(0.6 * 32767.0 * math.sin(2 * math.pi * 440 * (i / rate)))
            data.extend(struct.pack("<h", v))
        wf.writeframes(data)
        wf.writeframes(b"\x00\x00" * int(rate * 0.25))

    glitches = find_bursts_between_gaps(glitch_wav, min_gap_sec=0.20, max_burst_sec=0.15)
    assert len(glitches) == 1
    assert glitches[0][2] <= 0.15

    # Clean multi-part join with acrossfade must have zero burst glitches
    p1 = _write_pcm_wav(tmp_path / "p1.wav", 1.2, freq=300.0, envelope="sine")
    p2 = _write_pcm_wav(tmp_path / "p2.wav", 1.2, freq=350.0, envelope="sine")
    p3 = _write_pcm_wav(tmp_path / "p3.wav", 1.2, freq=400.0, envelope="sine")
    joined_wav = tmp_path / "joined.wav"
    concat_wav_files([p1, p2, p3], joined_wav, crossfade_sec=0.04)

    assert joined_wav.exists()
    seam_glitches = find_bursts_between_gaps(joined_wav, min_gap_sec=0.20, max_burst_sec=0.15)
    assert seam_glitches == []

    # Exact duration check: 3 * 1.2s - 2 * 0.04s crossfade ~ 3.52s
    with wave.open(str(joined_wav), "rb") as wf:
        exact_dur = wf.getnframes() / float(wf.getframerate())
    assert abs(exact_dur - 3.52) < 0.05
    # Billing duration (integer ceiling/round) is exactly 4s
    assert get_wav_duration_seconds(joined_wav) == 4


def test_is_wav_edge_clipped_detection(tmp_path):
    """Test is_wav_edge_clipped on clean decaying WAV vs clipped boundaries."""
    rate = 48000
    # 1. Clean decaying WAV
    clean = _write_pcm_wav(tmp_path / "clean.wav", 0.5, envelope="sine")
    assert not is_wav_edge_clipped(clean)

    # 2. Clipped at beginning
    clip_start = tmp_path / "clip_start.wav"
    with wave.open(str(clip_start), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * 0.5)
        data = bytearray()
        for i in range(n):
            env = 1.0 - (i / n)  # start at full amplitude
            v = int(0.6 * 32767.0 * env * math.cos(2 * math.pi * 440 * (i / rate)))
            data.extend(struct.pack("<h", v))
        wf.writeframes(data)
    assert is_wav_edge_clipped(clip_start)

    # 3. Clipped at end
    clip_end = tmp_path / "clip_end.wav"
    with wave.open(str(clip_end), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * 0.5)
        data = bytearray()
        for i in range(n):
            v = int(0.6 * 32767.0 * math.sin(2 * math.pi * 440 * (i / rate)))
            data.extend(struct.pack("<h", v))
        wf.writeframes(data)
    assert is_wav_edge_clipped(clip_end)

    # 4. Zero length / missing
    missing = tmp_path / "missing.wav"
    assert not is_wav_edge_clipped(missing)


# ---------------------------------------------------------------------------
# Unit Tests: Worker Edge-Clip Retry & Duration Accounting
# ---------------------------------------------------------------------------

def test_worker_retries_edge_clipped_chunk(tmp_path):
    """When a synthesized chunk is clipped at the boundary, the worker
    retries synthesis of that chunk once before accepting it."""
    clean_wav = tmp_path / "clean_sample.wav"
    _write_pcm_wav(clean_wav, 0.5, envelope="sine")

    clipped_wav = tmp_path / "clipped_sample.wav"
    _write_pcm_wav(clipped_wav, 0.5, envelope="flat")  # flat envelope = edge clipped

    output_wav = tmp_path / "out.wav"
    calls = []

    def mock_synthesize(**kwargs):
        calls.append(kwargs)
        out = Path(kwargs["output_wav"])
        out.parent.mkdir(parents=True, exist_ok=True)
        # First call produces clipped, second call produces clean
        if len(calls) == 1:
            shutil.copy2(clipped_wav, out)
        else:
            shutil.copy2(clean_wav, out)
        return out

    engine = MagicMock()
    engine.synthesize.side_effect = mock_synthesize

    # Chunked synthesis path with 2 chunks
    with patch("app.worker.plan_text_chunks", return_value=["အပိုင်း ၁", "အပိုင်း ၂"]):
        _synthesize_voxcpm_voice_job(
            engine=engine,
            job_id="test-retry-job",
            text="စမ်းသပ်စာသား ရှည်လျားသော အပိုင်း",
            output_wav=output_wav,
            reference_wav=clean_wav,
            control_instruction="",
            preset_voice="machaw",
            speed=1.0,
            tier="free",
            progress_cb=None,
        )

    # Chunk 1 was clipped on first try, so it was called twice!
    # Chunk 2 was clean, so it was called once. Total synthesize calls: 3.
    assert len(calls) == 3
    assert engine.synthesize.call_count == 3


def test_worker_retries_edge_clipped_single_call(tmp_path):
    """When single-call output is edge-clipped, synthesize is retried once."""
    clean_wav = tmp_path / "clean_sample.wav"
    _write_pcm_wav(clean_wav, 0.5, envelope="sine")

    clipped_wav = tmp_path / "clipped_sample.wav"
    _write_pcm_wav(clipped_wav, 0.5, envelope="flat")

    output_wav = tmp_path / "out_single.wav"
    calls = []

    def mock_synthesize(**kwargs):
        calls.append(kwargs)
        out = Path(kwargs["output_wav"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1:
            shutil.copy2(clipped_wav, out)
        else:
            shutil.copy2(clean_wav, out)
        return out

    engine = MagicMock()
    engine.synthesize.side_effect = mock_synthesize

    # Single-call text
    with patch("app.worker.plan_text_chunks", return_value=["မင်္ဂလာပါ"]):
        _synthesize_voxcpm_voice_job(
            engine=engine,
            job_id="test-single-job",
            text="မင်္ဂလာပါ",
            output_wav=output_wav,
            reference_wav=clean_wav,
            control_instruction="",
            preset_voice="machaw",
            speed=1.0,
            tier="free",
            progress_cb=None,
        )

    # Retried once: total 2 calls
    assert len(calls) == 2


def test_billing_duration_accounting_matches_wav_length(tmp_path):
    """Verify that get_wav_duration_seconds matches delivered WAV duration
    and aligns with ledger seconds consumed."""
    p1 = _write_pcm_wav(tmp_path / "part1.wav", 1.8, envelope="sine")
    p2 = _write_pcm_wav(tmp_path / "part2.wav", 2.2, envelope="sine")
    joined = tmp_path / "delivered.wav"
    concat_wav_files([p1, p2], joined, crossfade_sec=0.04)

    # 1.8 + 2.2 - 0.04 = 3.96 s -> rounds to 4 s
    seconds_consumed = get_wav_duration_seconds(joined)
    assert seconds_consumed == 4
