"""Unit tests for Section B — Clone Prosody Drift Phase 1A + Phase 1B (2026-11-24).

Pins:
1. Chunk budget & packing: with a 10 s sliced reference + English control instruction,
   `chunk_char_budget()` returns >= 400 chars; `MAX_CHUNK_CHARS = 425` packs whole sentences
   and never splits mid-sentence except an over-cap single sentence.
2. Reference conditioning & dead-code fix:
   - Steady ~10 s slice selector picks the lowest-F0-IQR window.
   - Custom clone with plausible ASR (`8-22 chars/s`) passes `prompt_text` to `engine.synthesize`.
   - ASR failure / implausible rate falls back to `prompt_text=""` AND passes
     `DEFAULT_CLONE_FIDELITY_CONTROL` to `engine.synthesize` (fixing `worker.py:964`).
   - Cross-gender flip (`m2f` / `f2m`) does NOT force locked-timbre `prompt_text` mode.
3. Space pinning & v1 exclusion: v2 failover never includes `yeyee2609/voxcpm`; first accepting
   v2 space is pinned for subsequent chunks; tokens rotate on failure while pinned space holds.
4. Mastering once + boundary pauses: per-chunk calls pass `skip_mastering=True`;
   `concat_wav_files(..., boundary_pause_sec=0.135)` inserts silence pauses; post-stitch
   `master_voxcpm_audio` runs once with `speed_preset` preserved.
5. Prosody gate & rubberband micro-correction: planted +11 st outlier chunk triggers 1 retry,
   respects `<=1/chunk` & `<=2/job` retry caps, keeps closer candidate; rubberband clamps
   `±2.0 st` (deadband `<=0.5 st`) and `[0.90, 1.12]` tempo; cross-gender anchor = job median.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np

from app import worker as worker_mod
from app.services import audio as audio_mod
from app.services import clone_prosody as cp
from app.services import voice_chunking as vc
from app.services import voxcpm_engine as vox_mod


def _write_tone_wav(
    path: Path,
    segments_hz_sec: list[tuple[float, float]],
    sample_rate: int = 24000,
) -> Path:
    parts: list[np.ndarray] = []
    for hz, dur in segments_hz_sec:
        n = max(1, int(round(dur * sample_rate)))
        if hz <= 0.0:
            parts.append(np.zeros(n, dtype=np.int16))
        else:
            t = np.arange(n, dtype=np.float64) / sample_rate
            sig = 0.65 * np.sin(2.0 * np.pi * hz * t) + 0.25 * np.sin(4.0 * np.pi * hz * t)
            parts.append((sig * 14000).astype(np.int16))
    full = np.concatenate(parts) if parts else np.zeros(sample_rate, dtype=np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(full.tobytes())
    return path


def _wav_duration_sec(path: Path) -> float:
    samples, sr = cp.read_wav_mono_float32(path)
    return samples.size / float(sr) if sr > 0 else 0.0


def test_chunk_budget_fix_and_whole_sentence_packing():
    """Pin #1: 10s sliced reference + English control yields >= 400 char budget; MAX_CHUNK_CHARS=425 respects sentences."""
    budget = vc.chunk_char_budget(
        reference_seconds=10.0,
        control_instruction=vox_mod.DEFAULT_CLONE_FIDELITY_CONTROL,
    )
    assert budget >= 400
    assert vc.MAX_CHUNK_CHARS == 425

    s1 = "မောင်မောင်သည် မနက်စောစော အိပ်ရာမှ ထပြီး ရွာဦးဘုန်းကြီးကျောင်းသို့ လမ်းလျှောက်သွားခဲ့ပါသည်။"
    s2 = "လမ်းတစ်လျှောက်တွင် ငှက်ကလေးများ၏ သာယာနာပျော်ဖွယ် တေးသီသံများကို ကြားနေရပါသည်။"
    s3 = "ရာသီဥတုမှာ အေးမြကြည်လင်နေသဖြင့် စိတ်နှလုံး အေးချမ်းလှပါသည်။"
    narration = " ".join([s1, s2, s3] * 8)  # ~1,900+ chars

    chunks = vc.plan_text_chunks(
        narration,
        reference_seconds=10.0,
        control_instruction=vox_mod.DEFAULT_CLONE_FIDELITY_CONTROL,
    )
    assert 4 <= len(chunks) <= 7
    for ch in chunks:
        assert len(ch) <= vc.MAX_CHUNK_CHARS
        assert ch.endswith("။")  # never split mid-sentence when sentences <= 425 chars!

    # Over-cap single sentence splits cleanly at clause boundary (`၊`)
    long_clause = "ဒါက အရမ်းရှည်လျားတဲ့ ဝါကျခွဲ တစ်ခု ဖြစ်ပြီး စမ်းသပ်ဖို့ ရေးထားတာပါ၊ "
    giant_sentence = (long_clause * 10).rstrip("၊ ") + "။"
    assert len(giant_sentence) > vc.MAX_CHUNK_CHARS
    split_giant = vc.plan_text_chunks(giant_sentence)
    assert len(split_giant) >= 2
    assert all(len(c) <= vc.MAX_CHUNK_CHARS for c in split_giant)


def test_steady_slice_and_worker_custom_clone_conditioning_and_flip_carveout(tmp_path, monkeypatch):
    """Pin #2 & #4: steady ~10s slice picks lowest IQR window; plausible ASR -> prompt_text; empty ASR -> fidelity control; m2f -> no prompt_text; mastering once."""
    # First 12s wildly oscillating F0 (110Hz <-> 260Hz), last 12s rock-steady 150Hz!
    noisy_then_steady = _write_tone_wav(
        tmp_path / "ref_raw.wav",
        [(110.0, 3.0), (260.0, 3.0), (115.0, 3.0), (255.0, 3.0), (150.0, 12.0)],
    )
    sliced_wav, dur_s, slice_stats = cp.select_steady_reference_slice(
        noisy_then_steady, tmp_path / "work", target_sec=10.0
    )
    assert sliced_wav.exists()
    assert 8.5 <= dur_s <= 10.5
    assert slice_stats.f0_median_hz is not None and 140.0 <= slice_stats.f0_median_hz <= 160.0
    assert slice_stats.f0_iqr_st < 1.0

    synth_calls: list[dict] = []
    master_calls: list[tuple] = []

    def _fake_synth(**kwargs):
        synth_calls.append(dict(kwargs))
        _write_tone_wav(Path(kwargs["output_wav"]), [(150.0, 2.5)])
        return Path(kwargs["output_wav"])

    def _fake_master(raw_wav, out_wav, *, speed="+0%", preset_voice=None, ffmpeg_bin=None):
        master_calls.append((str(raw_wav), str(out_wav), speed))
        Path(out_wav).write_bytes(Path(raw_wav).read_bytes())
        return Path(out_wav)

    fake_engine = SimpleNamespace(
        synthesize=_fake_synth,
        reset_pinned_space=lambda: None,
    )
    monkeypatch.setattr(vox_mod, "master_voxcpm_audio", _fake_master)
    monkeypatch.setattr(cp, "apply_rubberband_micro_correction", lambda wav_path, **kw: False)

    # 2-chunk text (> 425 chars) so multi-chunk path (skip_mastering=True + single post-stitch master) runs!
    multi_text = (
        "မောင်မောင်သည် မနက်စောစော အိပ်ရာမှ ထပြီး ရွာဦးဘုန်းကြီးကျောင်းသို့ လမ်းလျှောက်သွားခဲ့ပါသည်။ " * 8
    )
    plausible_asr = "မင်္ဂလာပါရှင့် ဒီနေ့မှာတော့ အသံပွားစနစ်အကြောင်း ရှင်းပြပေးသွားမှာ ဖြစ်ပါတယ်ရှင့်။ " * 2  # ~150 chars / 10s = 15 c/s

    # Case (a): Custom clone + plausible ASR -> prompt_text reaches engine.synthesize; per-chunk skip_mastering=True; 1 master pass!
    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=AsyncMock(return_value=plausible_asr)):
        worker_mod._synthesize_voxcpm_voice_job(
            fake_engine,
            job_id="job_custom_asr_ok",
            text=multi_text,
            output_wav=tmp_path / "out_a.wav",
            reference_wav=noisy_then_steady,
            control_instruction="",
            preset_voice=None,
            speed="+12%",
            progress_cb=None,
            gender_preset="m2m",
        )
    assert len(synth_calls) >= 2
    assert all(c["prompt_text"] == plausible_asr.strip() for c in synth_calls)
    assert all(c["skip_mastering"] is True for c in synth_calls)
    assert len(master_calls) == 1
    assert master_calls[0][2] == "+12%"

    # Case (b): Custom clone + ASR returns None -> prompt_text=="" AND DEFAULT_CLONE_FIDELITY_CONTROL reaches engine!
    synth_calls.clear()
    master_calls.clear()
    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=AsyncMock(return_value=None)):
        worker_mod._synthesize_voxcpm_voice_job(
            fake_engine,
            job_id="job_custom_asr_empty",
            text=multi_text,
            output_wav=tmp_path / "out_b.wav",
            reference_wav=noisy_then_steady,
            control_instruction="",
            preset_voice=None,
            speed="+0%",
            progress_cb=None,
            gender_preset="m2m",
        )
    assert len(synth_calls) >= 2
    assert all(c["prompt_text"] == "" for c in synth_calls)
    assert all(c["control_instruction"] == vox_mod.DEFAULT_CLONE_FIDELITY_CONTROL for c in synth_calls)
    assert len(master_calls) == 1

    # Case (c): Cross-gender flip (`m2f`) -> does NOT force prompt_text mode!
    synth_calls.clear()
    asr_spy = AsyncMock(return_value=plausible_asr)
    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=asr_spy):
        worker_mod._synthesize_voxcpm_voice_job(
            fake_engine,
            job_id="job_custom_flip_m2f",
            text="ဒါက အသံပြောင်း စမ်းသပ် စာကြောင်း ဖြစ်ပါတယ်။",
            output_wav=tmp_path / "out_c.wav",
            reference_wav=noisy_then_steady,
            control_instruction="Transform into a clear female voice.",
            preset_voice=None,
            speed="+0%",
            progress_cb=None,
            gender_preset="m2f",
        )
    assert asr_spy.await_count == 0
    assert "prompt_text" not in synth_calls[0] or not synth_calls[0].get("prompt_text")
    assert "female" in synth_calls[0]["control_instruction"].lower()


def test_space_pinning_excludes_v1_and_rotates_tokens_on_pinned_space(tmp_path, monkeypatch):
    """Pin #3: v2 failover never includes v1 `yeyee2609/voxcpm`; tokens rotate while pinned space stays fixed."""
    monkeypatch.setenv("VOXCPM_SPACES", "yeyee2609/voxcpm,owner/voxcpm2-a,owner/voxcpm2-b")
    monkeypatch.setenv("HF_TOKENS", "hf_tok_1,hf_tok_2")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.delenv("HF_RESERVE_TOKENS", raising=False)

    spaces = vox_mod.get_configured_spaces()
    assert "yeyee2609/voxcpm" not in spaces
    assert spaces[:2] == ["owner/voxcpm2-a", "owner/voxcpm2-b"]

    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    monkeypatch.setattr(vox_mod.VoxCPMEngine, "_ensure_cooldowns_restored", classmethod(lambda cls, tokens=None: None))
    monkeypatch.setattr(vox_mod, "space_supports_clone", lambda sp: True)

    ref_wav = _write_tone_wav(tmp_path / "ref.wav", [(150.0, 2.0)])
    gen_wav = _write_tone_wav(tmp_path / "gen_src.wav", [(150.0, 1.5)])
    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "work")

    attempts_seen: list[tuple[str, str | None]] = []
    chunk_num = {"n": 0}

    class _FakeJob:
        def __init__(self, out_file: Path):
            self._out = str(out_file)

        def done(self) -> bool:
            return True

        def status(self):
            return SimpleNamespace(eta=None, rank=None)

        def result(self, timeout=None):
            return self._out

    class _FakeGradioClient:
        def __init__(self, space_name: str, hf_token: str | None):
            self.space_name = space_name
            self.hf_token = hf_token

        def submit(self, *args, **kwargs):
            attempts_seen.append((self.space_name, self.hf_token))
            if chunk_num["n"] == 1:
                if self.space_name == "owner/voxcpm2-a":
                    raise RuntimeError("GPU unavailable on space A")
                return _FakeJob(gen_wav)
            # Chunk 2: pinned to space B! First token tried fails transiently, second token succeeds on space B!
            if len(attempts_seen) == 1:
                raise RuntimeError("HTTP 429 rate limit on first token")
            return _FakeJob(gen_wav)

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeGradioClient(sp, hf_token)),
    )
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)

    chunk_num["n"] = 1
    eng.synthesize(
        "ပထမ အပိုင်း။",
        tmp_path / "c1.wav",
        reference_wav=ref_wav,
        prompt_text="နမူနာ စာသား။",
        skip_mastering=True,
        allow_fallback=False,
    )
    assert eng.pinned_space == "owner/voxcpm2-b"

    attempts_seen.clear()
    chunk_num["n"] = 2
    eng.synthesize(
        "ဒုတိယ အပိုင်း။",
        tmp_path / "c2.wav",
        reference_wav=ref_wav,
        prompt_text="နမူနာ စာသား။",
        skip_mastering=True,
        allow_fallback=False,
    )
    # Chunk 2 NEVER touched space A or v1; it rotated tokens strictly on pinned space B!
    assert len(attempts_seen) == 2
    assert all(sp == "owner/voxcpm2-b" for sp, _ in attempts_seen)
    assert attempts_seen[0][1] != attempts_seen[1][1]

    eng.reset_pinned_space()
    assert eng.pinned_space is None


def test_stitch_boundary_pause_inserts_silence(tmp_path):
    """Pin #4: concat_wav_files with boundary_pause_sec=0.135 adds silence pause vs 40ms overlap default."""
    w1 = _write_tone_wav(tmp_path / "p1.wav", [(160.0, 1.0)])
    w2 = _write_tone_wav(tmp_path / "p2.wav", [(160.0, 1.0)])

    out_xfade = tmp_path / "xfade.wav"
    out_pause = tmp_path / "pause.wav"
    audio_mod.concat_wav_files([w1, w2], out_xfade)
    audio_mod.concat_wav_files([w1, w2], out_pause, boundary_pause_sec=0.135)

    d_xfade = _wav_duration_sec(out_xfade)
    d_pause = _wav_duration_sec(out_pause)
    assert 1.93 <= d_xfade <= 1.99  # 2.0s - 40ms crossfade
    assert 2.11 <= d_pause <= 2.16  # 2.0s + 135ms boundary pause


def test_prosody_gate_outlier_retry_caps_rubberband_clamps_and_flip_anchor(tmp_path, monkeypatch):
    """Pin #5: +11 st outlier triggers 1 retry, respects <=1/chunk & <=2/job caps, clamps rubberband, flip uses job median."""
    ref_wav = _write_tone_wav(tmp_path / "ref150.wav", [(150.0, 10.5)])
    c_texts = [
        "ပထမ စာပိုဒ်တွင် မောင်မောင်သည် မြို့သို့ သွားခဲ့ပါသည်။",
        "ဒုတိယ စာပိုဒ်တွင် သူငယ်ချင်းများနှင့် ပြန်လည် ဆုံတွေ့ခဲ့ပါသည်။",
        "တတိယ စာပိုဒ်တွင် စာအုပ်ဆိုင်မှ စာအုပ်အသစ်များ ဝယ်ယူခဲ့ပါသည်။",
        "စတုတ္ထ စာပိုဒ်တွင် အိမ်သို့ ပျော်ရွှင်စွာ ပြန်လာခဲ့ပါသည်။",
    ]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(c_texts))
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)

    per_text_calls: dict[str, int] = {}

    def _outlier_synth(**kwargs):
        text = kwargs["text"]
        cnt = per_text_calls.get(text, 0) + 1
        per_text_calls[text] = cnt
        if text in (c_texts[1], c_texts[2], c_texts[3]) and cnt == 1:
            # +11 semitones above 150 Hz (~283 Hz)!
            hz = 150.0 * (2.0 ** (11.0 / 12.0))
        else:
            hz = 150.0
        _write_tone_wav(Path(kwargs["output_wav"]), [(hz, 2.6)])
        return Path(kwargs["output_wav"])

    rb_shifts_seen: list[tuple[float, float]] = []

    def _spy_rubberband(wav_path: Path, *, pitch_shift_st: float, tempo_factor: float, ffmpeg_bin=None):
        rb_shifts_seen.append((pitch_shift_st, tempo_factor))
        return abs(pitch_shift_st) > 0.0 or abs(tempo_factor - 1.0) > 0.0

    fake_engine = SimpleNamespace(synthesize=_outlier_synth, reset_pinned_space=lambda: None)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    monkeypatch.setattr(cp, "apply_rubberband_micro_correction", _spy_rubberband)

    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=AsyncMock(return_value=None)):
        out_file = worker_mod._synthesize_voxcpm_voice_job(
            fake_engine,
            job_id="job_outlier_gate",
            text=" ".join(c_texts),
            output_wav=tmp_path / "out_gate.wav",
            reference_wav=ref_wav,
            control_instruction="",
            preset_voice=None,
            speed="+0%",
            progress_cb=None,
            gender_preset="m2m",
        )

    assert out_file.exists()
    # Chunk 1 clean (1 call); Chunks 2 & 3 retried once each (2 calls each = 2 job retries max!);
    # Chunk 4 NOT retried because MAX_PROSODY_RETRIES_PER_JOB == 2 was reached, and job still succeeded!
    assert per_text_calls[c_texts[0]] == 1
    assert per_text_calls[c_texts[1]] == 2
    assert per_text_calls[c_texts[2]] == 2
    assert per_text_calls[c_texts[3]] == 1
    for st_val, tempo_val in rb_shifts_seen:
        assert -2.0 <= st_val <= 2.0
        assert 0.90 <= tempo_val <= 1.12

    # Check compute_rubberband_params deadband (<= 0.5 st -> 0.0) & clamp (±2.0 st, [0.90, 1.12])
    st_small, tp_small = cp.compute_rubberband_params(
        cp.ProsodyStats(2.0, 2.0, 2.0, 153.0, 0.4, 10, 5.05),
        anchor_f0_hz=150.0,
        anchor_rate=5.0,
    )
    assert st_small == 0.0 and tp_small == 1.0

    # Updated in Clone Prosody Phase 3 (§5.3, 2026-11-25): tempo clamp softened from [0.90, 1.12] -> [0.93, 1.07]
    st_big, tp_big = cp.compute_rubberband_params(
        cp.ProsodyStats(2.0, 2.0, 2.0, 240.0, 0.4, 10, 3.0),
        anchor_f0_hz=150.0,
        anchor_rate=5.0,
    )
    assert st_big == -2.0 and tp_big == 1.07

    # Cross-gender (`m2f`) anchor check: ignores male 120 Hz reference, anchors to female ~240 Hz job median!
    male_ref = _write_tone_wav(tmp_path / "male120.wav", [(120.0, 6.0)])
    flip_calls: dict[str, int] = {}

    def _flip_synth(**kwargs):
        t = kwargs["text"]
        flip_calls[t] = flip_calls.get(t, 0) + 1
        _write_tone_wav(Path(kwargs["output_wav"]), [(240.0, 2.4)])
        return Path(kwargs["output_wav"])

    worker_mod._synthesize_voxcpm_voice_job(
        SimpleNamespace(synthesize=_flip_synth, reset_pinned_space=lambda: None),
        job_id="job_flip_anchor",
        text=" ".join(c_texts[:2]),
        output_wav=tmp_path / "out_flip.wav",
        reference_wav=male_ref,
        control_instruction="Female voice",
        preset_voice=None,
        speed="+0%",
        progress_cb=None,
        gender_preset="m2f",
    )
    # Neither 240 Hz female chunk is treated as an outlier against the 120 Hz male reference!
    assert flip_calls[c_texts[0]] == 1
    assert flip_calls[c_texts[1]] == 1

    # Benchmark measurement helper check
    from scripts.bench_clone_prosody import evaluate_wav_prosody

    metrics = evaluate_wav_prosody(ref_wav)
    assert len(metrics["windows"]) >= 2
    assert metrics["F0_win_IQR_st"] < 0.5
    assert metrics["F0_outlier_ratio"] == 0.0
    assert "PDS" in metrics and math.isfinite(metrics["PDS"])
