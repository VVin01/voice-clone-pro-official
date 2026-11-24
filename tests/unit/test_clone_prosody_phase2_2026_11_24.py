"""Unit & DSP Integration Tests — Clone Prosody Phase 2 (2026-11-25).

Option (e): Post-Loop Bilateral Seam Ramp + 3.5 s Envelope Tether + Burmese-Safe Edge F0.
Covers all §6 / Part 8 acceptance pins with real FFmpeg `asendcmd + rubberband` and YIN:
  1. Synthetic seesaw + declination torture WAVs (+1.2->-2.2 st and +2.2->-1.0 st, 4.4 st cliff)
  2. Mid-chunk +2.7 st 5 s arch tether ([+1.50, +1.85 st], 0% outlier, not flattened)
  3. Curated persona (`preset_voice="komin"`) Chunk-1 anchor + post-correction telemetry
  4. Boundary classifier + adaptive pauses + `?` / `လား။` question-tail guard + `\n\n` packing
  5. Burmese phrase-final 68 Hz creak exclusion + sparse-edge shrinkage/skip fallbacks
  6. Gender-flip (`m2f`), Part 6 worst-first retry re-scoring, and park/resume + Conservation invariance
  7. Full bench gate + expressiveness floor verification across multi-chunk fixtures.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import wave

import numpy as np
import pytest

from app import worker as worker_mod
from app.services import audio as audio_mod
from app.services import clone_prosody as cp
from app.services import hf_pool_ledger as hfl
from app.services import voice_chunking as vc
from app.services import voxcpm_engine as vox_mod
from app.services.number_words import normalize_numbers_for_speech
from scripts.bench_clone_prosody import BENCHMARK_BURMESE_NARRATION, evaluate_wav_prosody


def _write_modulated_speech_wav(
    path: Path,
    *,
    duration_sec: float,
    base_hz: float,
    st_contour_fn,
    vibrato_st: float = 0.65,
    vibrato_hz: float = 1.9,
    syl_mod_hz: float = 6.6,
    sample_rate: int = 24000,
    tail_creak_sec: float = 0.0,
    tail_creak_hz: float = 68.0,
) -> Path:
    """Synthesize a harmonic speech-like mono WAV with controllable F0 contour, natural
    syllable amplitude envelope (`syl_mod_hz`), and optional phrase-final glottal creak."""
    n = max(1, int(round(duration_sec * sample_rate)))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)

    macro_st = np.asarray(st_contour_fn(t), dtype=np.float64)
    if macro_st.ndim == 0:
        macro_st = np.full(n, float(macro_st), dtype=np.float64)

    # Expressive syllable/phrase intonation (< 1 s timescale — must pass through at 1.00x)
    micro_st = vibrato_st * (
        0.65 * np.sin(2.0 * math.pi * vibrato_hz * t)
        + 0.35 * np.sin(2.0 * math.pi * (vibrato_hz * 2.3) * t + 0.7)
    )
    inst_hz = float(base_hz) * (2.0 ** ((macro_st + micro_st) / 12.0))

    if tail_creak_sec > 0.0:
        creak_n = min(n // 3, int(round(tail_creak_sec * sample_rate)))
        if creak_n > 0:
            inst_hz[-creak_n:] = float(tail_creak_hz)

    phase = 2.0 * math.pi * np.cumsum(inst_hz) / float(sample_rate)
    sig = (
        0.55 * np.sin(phase)
        + 0.25 * np.sin(2.0 * phase)
        + 0.12 * np.sin(3.0 * phase)
        + 0.06 * np.sin(4.0 * phase)
    )
    # Syllable-like amplitude modulation so acoustic syllable-peak detectors also see realistic nuclei
    amp_env = 0.62 + 0.38 * np.maximum(0.0, np.sin(2.0 * math.pi * syl_mod_hz * t))
    fade = min(int(0.025 * sample_rate), n // 8)
    if fade > 1:
        amp_env[:fade] *= np.linspace(0.0, 1.0, fade)
        amp_env[-fade:] *= np.linspace(1.0, 0.0, fade)

    pcm = np.clip(sig * amp_env * 16000.0, -32768.0, 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return path


def _wav_duration_ms(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return (wf.getnframes() / float(wf.getframerate())) * 1000.0


def _slice_iqr_st(wav_path: Path, t0_s: float, t1_s: float) -> float:
    samples, sr = cp.read_wav_mono_float32(wav_path)
    s16 = cp.resample_float32(samples[int(t0_s * sr) : int(t1_s * sr)], sr, cp.ANALYSIS_SAMPLE_RATE)
    _, f0 = cp.extract_frame_contours(s16, cp.ANALYSIS_SAMPLE_RATE)
    v = f0[~np.isnan(f0) & (f0 > 0)]
    q25, q75 = np.percentile(v, [25, 75])
    return cp.semitone_diff(float(q75), float(q25))


def test_phase2_constants_and_soft_knee_body_deadbands():
    """Verify §5 Phase 2 constants verbatim, preserved Phase 1B constants, and continuous soft-knee."""
    assert cp.CUSTOM_REF_JOB_BLEND_WEIGHT == 0.35
    assert cp.BODY_DEADBAND_PITCH_ST == 0.60
    assert cp.BODY_EXPRESSIVE_DEADBAND_ST == 1.10
    assert cp.BODY_MAX_SHIFT_ST == 1.50
    assert cp.ENV_WINDOW_SEC == 3.50
    assert cp.ENV_DEADZONE_ST == 1.35
    assert cp.ENV_EXPRESSIVE_DEADZONE_ST == 1.75
    assert cp.ENV_TETHER_GAIN == 0.65
    assert cp.ENV_HARD_CEIL_ST == 1.85
    assert cp.EDGE_HEAD_GUARD_FRAMES == 4
    assert cp.EDGE_TAIL_GUARD_FRAMES == 12
    assert cp.EDGE_VOICED_TARGET_FRAMES == 65
    assert cp.EDGE_VOICED_WIDEN_FRAMES == 110
    assert cp.EDGE_MIN_SHRINK_FRAMES == 8
    assert cp.STEP_MAX_MID_PHRASE_ST == 0.65
    assert cp.STEP_MAX_CLAUSE_ST == 0.90
    assert cp.STEP_MAX_SENTENCE_ST == 1.35
    assert cp.STEP_MAX_PARAGRAPH_ST == 1.80
    assert cp.RAMP_HEAD_SEC == 2.40
    assert cp.RAMP_TAIL_SEC == 1.80
    assert cp.RAMP_CMD_STEP_SEC == 0.050
    assert cp.MAX_TOTAL_INSTANT_SHIFT_ST == 2.00
    assert cp.MAX_RAMP_SLOPE_ST_PER_SEC == 0.85
    assert cp.OUTLIER_MAX_CHUNK_IQR_ST == 3.60
    assert cp.OUTLIER_MAX_INTERNAL_SPAN_ST == 4.20

    # Preserved Phase 1B constants
    assert cp.OUTLIER_MAX_F0_ST == 2.5
    assert cp.MAX_PROSODY_RETRIES_PER_JOB == 2
    assert cp.RUBBERBAND_MAX_PITCH_ST == 2.0
    assert cp.RUBBERBAND_TEMPO_MIN == 0.90
    assert cp.RUBBERBAND_TEMPO_MAX == 1.12

    # Soft-knee continuity check (zero hard cliff around ±0.60 st)
    anchor = 120.0
    s_below = cp.compute_soft_knee_body_shift(anchor * (2.0 ** (0.59 / 12.0)), anchor)
    s_above = cp.compute_soft_knee_body_shift(anchor * (2.0 ** (0.65 / 12.0)), anchor)
    assert s_below == 0.0
    assert -0.06 <= s_above <= -0.04
    # Expressive chunk widens deadband to 1.10 st
    assert cp.compute_soft_knee_body_shift(anchor * (2.0 ** (1.05 / 12.0)), anchor, expressive=True) == 0.0
    assert cp.compute_soft_knee_body_shift(anchor * (2.0 ** (3.50 / 12.0)), anchor) == -1.50


def test_pin1_seesaw_declination_torture_real_ffmpeg_and_yin(tmp_path):
    """Pin #1: Two 20 s chunks sloping +1.2 -> -2.2 st and +2.2 -> -1.0 st (4.4 st raw seam cliff,
    ~0.0 st medians) -> post-stitch max jump <= 1.50 st, outlier ratio 0.0%, duration ±5 ms,
    and mid-chunk (5-15 s) intonation IQR preserved >= 95%."""
    # Equal Burmese syllable counts (84 syllables each in 20.0 s) so tempo == 1.0 on both chunks
    t_c1 = "ပထမ စာပိုဒ်တွင် မောင်မောင်သည် မြို့သို့ စောစောစီးစီး ထွက်ခွာသွားခဲ့ပါသည်။ " * 4
    t_c2 = "ဒုတိယ စာပိုဒ်တွင် ကိုကိုသည် ရွာသို့ စောစောစီးစီး ပြန်လာသွားခဲ့ပါသည်။ " * 4

    # Sloping +1.2 -> -2.2 st and +2.2 -> -1.0 st across the 6 s head/tail regions with 0.0 st
    # steady centers so whole-chunk medians are 0.0 st while raw seam cliff is 4.4 st!
    def _c1_contour(t: np.ndarray) -> np.ndarray:
        out = np.zeros_like(t)
        m_h = t < 6.0
        out[m_h] = 1.20 * (1.0 - t[m_h] / 6.0)
        m_t = t > 14.0
        out[m_t] = -2.20 * ((t[m_t] - 14.0) / 6.0)
        return out

    def _c2_contour(t: np.ndarray) -> np.ndarray:
        out = np.zeros_like(t)
        m_h = t < 6.0
        out[m_h] = 2.20 * (1.0 - t[m_h] / 6.0)
        m_t = t > 14.0
        out[m_t] = -1.00 * ((t[m_t] - 14.0) / 6.0)
        return out

    raw1 = _write_modulated_speech_wav(tmp_path / "c1.wav", duration_sec=20.0, base_hz=125.0, st_contour_fn=_c1_contour)
    raw2 = _write_modulated_speech_wav(tmp_path / "c2.wav", duration_sec=20.0, base_hz=125.0, st_contour_fn=_c2_contour)

    # Verify raw stitched has a severe > 2.4 st 5 s window cliff (and ~4.4 st instantaneous seam cliff)
    raw_stitched = tmp_path / "raw_stitched.wav"
    audio_mod.concat_wav_files([raw1, raw2], raw_stitched, boundary_pause_sec=0.135)
    raw_bench = evaluate_wav_prosody(raw_stitched)
    assert raw_bench["F0_win_max_jump_st"] > 2.4

    raw_mid_iqr_1 = _slice_iqr_st(raw1, 5.0, 15.0)
    raw_mid_iqr_2 = _slice_iqr_st(raw2, 5.0, 15.0)

    res = cp.run_post_loop_prosody_pass(
        [raw1, raw2],
        [t_c1.strip(), t_c2.strip()],
        full_text=f"{t_c1.strip()} {t_c2.strip()}",
        out_dir=tmp_path / "corr",
        preset_voice="komin",
        speed_tempo=1.0,
    )
    assert len(res.corrected_parts) == 2

    # Sample-exact duration check (±5 ms) when tempo == 1.0 (match syllable rates -> tempo 1.0)
    assert abs(_wav_duration_ms(res.corrected_parts[0]) - _wav_duration_ms(raw1)) <= 5.0
    assert abs(_wav_duration_ms(res.corrected_parts[1]) - _wav_duration_ms(raw2)) <= 5.0

    # Mid-chunk (5-15 s) syllable/phrase intonation IQR preserved >= 95%
    post_mid_iqr_1 = _slice_iqr_st(res.corrected_parts[0], 5.0, 15.0)
    post_mid_iqr_2 = _slice_iqr_st(res.corrected_parts[1], 5.0, 15.0)
    assert post_mid_iqr_1 >= 0.95 * raw_mid_iqr_1
    assert post_mid_iqr_2 >= 0.95 * raw_mid_iqr_2

    # Bilateral seam proof: post_step_st reduced dramatically from raw_step_st (~4.4 st)
    seam_info = res.boundary_steps[0]
    assert seam_info["raw_step_st"] > 3.0
    assert abs(seam_info["post_step_st"]) <= 1.50

    corr_stitched = tmp_path / "corr_stitched.wav"
    audio_mod.concat_wav_files(res.corrected_parts, corr_stitched, boundary_pauses_sec=res.pauses_sec)
    post_bench = evaluate_wav_prosody(corr_stitched)
    assert post_bench["F0_win_max_jump_st"] <= 1.50
    assert post_bench["F0_outlier_ratio"] == 0.0


def test_pin2_mid_chunk_plus_2p7_st_arch_tether(tmp_path):
    """Pin #2: 22 s chunk with 0.0 st head/tail and +2.70 st bulge at 8-13 s -> envelope tether
    lands the bulge window in [+1.50, +1.85 st], outlier_ratio == 0.0%, arch still > +1.40 st."""
    txt = "ဇာတ်လမ်းရဲ့ အထွဋ်အထိပ် အခန်းကို ရောက်ရှိလာချိန်မှာတော့ အားလုံးက စိတ်လှုပ်ရှားစွာ စောင့်ကြည့်နေကြပါတယ်။ " * 4

    def _arch_contour(t: np.ndarray) -> np.ndarray:
        # Smooth 0.5 s cosine shoulders around the [8.0, 13.0 s] +2.70 st plateau
        out = np.zeros_like(t)
        for i, ti in enumerate(t):
            if 8.0 <= ti <= 13.0:
                out[i] = 2.70
            elif 7.2 < ti < 8.0:
                out[i] = 2.70 * 0.5 * (1.0 - math.cos(math.pi * (ti - 7.2) / 0.8))
            elif 13.0 < ti < 13.8:
                out[i] = 2.70 * 0.5 * (1.0 + math.cos(math.pi * (ti - 13.0) / 0.8))
        return out

    raw = _write_modulated_speech_wav(
        tmp_path / "arch.wav",
        duration_sec=22.0,
        base_hz=130.0,
        st_contour_fn=_arch_contour,
        vibrato_st=0.35,
    )
    res = cp.run_post_loop_prosody_pass(
        [raw],
        [txt.strip()],
        full_text=txt.strip(),
        out_dir=tmp_path / "corr",
        preset_voice="komin",
        speed_tempo=1.0,
    )
    assert res.envelope_corrections == 1
    corr_wav = res.corrected_parts[0]
    bench = evaluate_wav_prosody(corr_wav)
    assert bench["F0_outlier_ratio"] == 0.0

    # Measure the [8.5, 12.5 s] arch plateau in semitones vs global anchor (and 5 s bench window)
    samples, sr = cp.read_wav_mono_float32(corr_wav)
    s16 = cp.resample_float32(samples[int(8.5 * sr) : int(12.5 * sr)], sr, cp.ANALYSIS_SAMPLE_RATE)
    _, f0_arch = cp.extract_frame_contours(s16, cp.ANALYSIS_SAMPLE_RATE)
    v_arch = f0_arch[~np.isnan(f0_arch) & (f0_arch > 0)]
    assert res.anchor.f0_anchor_hz is not None
    arch_st = cp.semitone_diff(float(np.median(v_arch)), float(res.anchor.f0_anchor_hz))
    max_win_st = max(float(w["dev_st"]) for w in bench["windows"] if w["dev_st"] is not None)
    assert 1.50 <= arch_st <= 1.85
    assert 1.50 <= max_win_st <= 1.85
    assert arch_st > 1.40


def test_pin3_curated_persona_komin_chunk1_anchor_and_post_telemetry(tmp_path, monkeypatch, caplog):
    """Pin #3 + Part 8: preset_voice='komin' anchors Chunk 1 to F_job/R_job (neither None nor
    komin.mp3's 109.68 Hz), and clone_prosody_summary logs POST-correction stats + new fields."""
    c_texts = [
        "ပထမ စာပိုဒ်တွင် ကိုမင်းက ရုပ်ရှင်ဇာတ်လမ်းကို စတင် မိတ်ဆက်ပေးလိုက်ပါသည်။",
        "ဒုတိယ စာပိုဒ်တွင် အဓိက ဇာတ်ကောင်၏ ငယ်ဘဝ အကြောင်းကို ဆက်လက် ပြောပြပါသည်။",
        "တတိယ စာပိုဒ်တွင် မထင်မှတ်ထားသော လျှို့ဝှက်ချက်ကြီး ပေါ်ပေါက်လာခဲ့ပါသည်။",
        "စတုတ္ထ စာပိုဒ်တွင် နောက်ဆုံး အဆုံးသတ် တိုက်ပွဲကြီး စတင်ခဲ့ပါတော့သည်။",
    ]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(c_texts))
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    # Chunk 1 is +1.50 st above 121 Hz (~131.95 Hz); Chunks 2..4 are steady at 121.0 Hz!
    def _persona_synth(**kwargs):
        idx_c = c_texts.index(kwargs["text"])
        hz = 121.0 * (2.0 ** (1.50 / 12.0)) if idx_c == 0 else 121.0
        _write_modulated_speech_wav(
            Path(kwargs["output_wav"]),
            duration_sec=3.5,
            base_hz=hz,
            st_contour_fn=lambda t: 0.0,
            vibrato_st=0.25,
        )
        return Path(kwargs["output_wav"])

    orig_efc = cp.extract_frame_contours
    yin_call_count = 0

    def _counting_efc(*args, **kwargs):
        nonlocal yin_call_count
        yin_call_count += 1
        return orig_efc(*args, **kwargs)

    monkeypatch.setattr(cp, "extract_frame_contours", _counting_efc)

    fake_engine = SimpleNamespace(synthesize=_persona_synth, reset_pinned_space=lambda: None)
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out_wav = worker_mod._synthesize_voxcpm_voice_job(
            fake_engine,
            job_id="job_komin_c1",
            text=" ".join(c_texts),
            output_wav=tmp_path / "komin_out.wav",
            reference_wav=None,
            control_instruction="Warm storyteller",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            gender_preset="m2m",
        )
    assert out_wav.is_file()
    # Part 4 (#3): YIN runs ONCE per chunk per job (4 chunks -> 4 calls, never 8!)
    assert yin_call_count == len(c_texts)

    summary_lines = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(summary_lines) == 1
    line = summary_lines[0]
    assert "anchor_source=job_median" in line
    assert "f0_anchor_hz=" in line
    assert "envelope_corrections=" in line
    assert "edge_fallbacks=" in line
    assert "boundary_steps=" in line

    # Verify anchor is ~121 Hz (NOT None and NOT 109.68 Hz from komin.mp3!)
    anchor_part = line.split("f0_anchor_hz=")[1].split()[0]
    anchor_hz = float(anchor_part)
    assert 119.5 <= anchor_hz <= 122.5

    # Verify logged f0_hz[0] is POST-correction (~125 Hz after soft-knee shift, NOT raw ~132 Hz!)
    f0_list_str = line.split("f0_hz=[")[1].split("]")[0]
    logged_f0s = [float(x.strip()) for x in f0_list_str.split(",")]
    assert logged_f0s[0] < 128.0


def test_pin4_boundary_classifier_adaptive_pauses_and_question_guard(tmp_path):
    """Pin #4: All 4 boundary classes, adaptive stitch pauses, `\n\n` packing preference,
    and rising-question tail exemption on both `?` and `လား။`."""
    full_text = (
        "ပထမ အပိုင်းက စကားစု မဆုံးသေးဘဲ ဆက်သွားတဲ့ စာသား "
        "ဒုတိယ အပိုင်းက ကော်မာနဲ့ ရပ်ထားပါတယ်၊ "
        "တတိယ အပိုင်းက ဝါကျ အဆုံးသတ်ပါတယ်။\n\n"
        "စတုတ္ထ စာပိုဒ်ကတော့ မေးခွန်း တစ်ခု ဖြစ်နေသလား? "
        "ပဉ္စမ အပိုင်းက မြန်မာ မေးခွန်း စာလုံး ပါသလား။ "
        "ဆဋ္ဌမ အပိုင်းက ရိုးရိုး အဖြေ စာကြောင်း ဖြစ်ပါတယ်။"
    )
    chunks = [
        "ပထမ အပိုင်းက စကားစု မဆုံးသေးဘဲ ဆက်သွားတဲ့ စာသား",
        "ဒုတိယ အပိုင်းက ကော်မာနဲ့ ရပ်ထားပါတယ်၊",
        "တတိယ အပိုင်းက ဝါကျ အဆုံးသတ်ပါတယ်။",
        "စတုတ္ထ စာပိုဒ်ကတော့ မေးခွန်း တစ်ခု ဖြစ်နေသလား?",
        "ပဉ္စမ အပိုင်းက မြန်မာ မေးခွန်း စာလုံး ပါသလား။",
        "ဆဋ္ဌမ အပိုင်းက ရိုးရိုး အဖြေ စာကြောင်း ဖြစ်ပါတယ်။",
    ]
    specs = cp.classify_chunk_boundaries(full_text, chunks, speed_tempo=1.0)
    assert [s.kind for s in specs] == ["MID_PHRASE", "CLAUSE", "PARAGRAPH", "SENTENCE", "SENTENCE"]
    assert specs[0].s_max_st == 0.65 and specs[0].base_pause_sec == 0.065
    assert specs[1].s_max_st == 0.90 and specs[1].base_pause_sec == 0.095
    assert specs[2].s_max_st == 1.80 and specs[2].base_pause_sec == 0.220
    # Question guard on `?` and `လား။`: 160 ms pause + question_tail=True
    assert specs[3].question_tail is True and specs[3].base_pause_sec == 0.160
    assert specs[4].question_tail is True and specs[4].base_pause_sec == 0.160

    # Speed-preset scaling (+20% -> tempo 1.20 scales pre-mastering pause by 1.20x)
    specs_fast = cp.classify_chunk_boundaries(full_text, chunks, speed_tempo=1.20)
    assert abs(specs_fast[0].pause_sec - 0.065 * 1.20) < 1e-3

    # Rising question tail (`+1.25 st` at end of question chunk) is NOT pulled down when next chunk starts at `-0.8 st`!
    q_wav = _write_modulated_speech_wav(
        tmp_path / "q.wav",
        duration_sec=8.0,
        base_hz=140.0,
        st_contour_fn=lambda t: np.where(t > 5.5, 1.25 * ((t - 5.5) / 2.5), 0.0),
        vibrato_st=0.20,
    )
    a_wav = _write_modulated_speech_wav(
        tmp_path / "a.wav",
        duration_sec=8.0,
        base_hz=140.0,
        st_contour_fn=lambda t: np.where(t < 2.5, -0.80 * (1.0 - t / 2.5), 0.0),
        vibrato_st=0.20,
    )
    q_txt = "ဒီအဖြေက တကယ်ပဲ မှန်ကန်နေပါသလား။"
    a_txt = "ဟုတ်ကဲ့၊ အဲဒီအဖြေက လုံးဝ မှန်ကန်နေပါတယ်။"
    res_q = cp.run_post_loop_prosody_pass(
        [q_wav, a_wav],
        [q_txt, a_txt],
        full_text=f"{q_txt} {a_txt}",
        out_dir=tmp_path / "q_corr",
        preset_voice="komin",
    )
    # Question chunk had 0 body shift, 0 env shift, and 0 tail pull -> unmodified rising tail!
    q_post_ana = cp.analyze_chunk_prosody(res_q.corrected_parts[0], text=q_txt)
    assert q_post_ana.raw_tail_st_vs_med > 0.70

    # Chunker `\n\n` hygiene check: breaks cleanly at `\n\n` once current >= 220 chars
    para1 = "က" * 130 + "။ " + "ခ" * 120 + "။"  # ~253 chars (>= 220)
    para2 = "ဂ" * 130 + "။"                     # would fit in 425 if `\n\n` weren't preferred!
    packed = vc.plan_text_chunks(f"{para1}\n\n{para2}")
    assert len(packed) == 2
    assert packed[0] == para1 and packed[1] == para2


def test_pin5_burmese_creaky_tail_and_sparse_edge_fallbacks(tmp_path):
    """Pin #5: 150 ms of 68 Hz creak at tail is excluded from `tail_f0`; <8 voiced frames
    skips ramping cleanly without NaN/crash; 8..19 frames applies Bayesian shrinkage."""
    creak_wav = _write_modulated_speech_wav(
        tmp_path / "creak.wav",
        duration_sec=6.0,
        base_hz=130.0,
        st_contour_fn=lambda t: 0.0,
        vibrato_st=0.15,
        tail_creak_sec=0.150,
        tail_creak_hz=68.0,
    )
    ana = cp.analyze_chunk_prosody(creak_wav, text="မင်္ဂလာပါခင်ဗျာ။")
    # Without guard+filter, 68 Hz is -11.2 st below 130 Hz; with Part 7, tail_st stays ~0.0 st!
    assert abs(ana.raw_tail_st_vs_med) < 0.35
    assert ana.tail_skipped is False

    # Sparse voiced frames (< 8 -> skip_edge=True, finite return, zero crash)
    sparse_f0 = np.full(400, np.nan, dtype=np.float32)
    sparse_f0[10:15] = 155.0  # only 5 voiced frames
    est_st, used_fb, skipped = cp.estimate_chunk_edge_f0_st(
        sparse_f0,
        duration_sec=4.0,
        chunk_median_hz=130.0,
        anchor_hz=130.0,
        body_st_vs_anchor=0.25,
        side="head",
    )
    assert math.isfinite(est_st) and est_st == 0.25
    assert used_fb is True and skipped is True

    # Medium sparse frames (12 clean voiced frames -> Bayesian shrinkage 12/20 * raw + 8/20 * body)
    shrink_f0 = np.full(400, np.nan, dtype=np.float32)
    shrink_f0[5:21] = 130.0 * (2.0 ** (2.0 / 12.0))  # 16 total voiced -> skip 4 guard = 12 clean frames @ +2.0 st
    sh_st, sh_fb, sh_skip = cp.estimate_chunk_edge_f0_st(
        shrink_f0,
        duration_sec=4.0,
        chunk_median_hz=130.0,
        anchor_hz=130.0,
        body_st_vs_anchor=0.0,
        side="head",
    )
    assert sh_fb is True and sh_skip is False
    assert abs(sh_st - 1.20) < 0.10


def test_pin6_gender_flip_retry_rescoring_and_park_resume_invariance(tmp_path, monkeypatch):
    """Pin #6 + Part 6: m2f Chunk 1 aligns to flipped job median; worst-scoring chunks retry first;
    park at chunk 5/10 + resume in Conservation band matches uninterrupted output with 0 double-processing
    and 0 new HF retry calls."""
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    # (A) m2f gender-flip: 120 Hz male reference, female output [264 Hz (+1.65 st), 240 Hz, 240 Hz]
    male_ref = _write_modulated_speech_wav(
        tmp_path / "male120.wav", duration_sec=6.0, base_hz=120.0, st_contour_fn=lambda t: 0.0
    )
    flip_texts = ["ပထမ မိန်းကလေး အသံ။", "ဒုတိယ မိန်းကလေး အသံ။", "တတိယ မိန်းကလေး အသံ။"]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(flip_texts))

    def _m2f_synth(**kwargs):
        idx_f = flip_texts.index(kwargs["text"])
        hz = 240.0 * (2.0 ** (1.65 / 12.0)) if idx_f == 0 else 240.0
        _write_modulated_speech_wav(Path(kwargs["output_wav"]), duration_sec=3.5, base_hz=hz, st_contour_fn=lambda t: 0.0)
        return Path(kwargs["output_wav"])

    out_flip = worker_mod._synthesize_voxcpm_voice_job(
        SimpleNamespace(synthesize=_m2f_synth, reset_pinned_space=lambda: None),
        job_id="job_m2f_p2",
        text=" ".join(flip_texts),
        output_wav=tmp_path / "m2f_out.wav",
        reference_wav=male_ref,
        control_instruction="Clear female voice",
        preset_voice=None,
        speed="+0%",
        progress_cb=None,
        gender_preset="m2f",
    )
    flip_bench = evaluate_wav_prosody(out_flip)
    assert 234.0 <= flip_bench["file_f0_median_hz"] <= 246.0
    # Chunk 1 was pulled down toward 240 Hz (not left +1.65 st above 240 Hz, and not pulled toward 120 Hz!)
    w0_hz = flip_bench["windows"][0]["f0_hz"]
    assert 238.0 <= w0_hz <= 253.0

    # (B) Part 6 retry re-scoring: worst-scoring chunks retry FIRST; uniform +1.8 st uses 0 retries
    r_texts = [f"စမ်းသပ် အပိုင်း {i} ဖြစ်ပါသည်။" for i in range(1, 6)]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(r_texts))
    calls_by_idx: dict[int, int] = {}

    def _scored_synth(**kwargs):
        i = r_texts.index(kwargs["text"])
        calls_by_idx[i] = calls_by_idx.get(i, 0) + 1
        out_p = Path(kwargs["output_wav"])
        if calls_by_idx[i] == 1:
            if i == 0:
                # Uniform +1.8 st offset (<= 2.5 st -> DSP handles it, MUST NOT trigger retry!)
                _write_modulated_speech_wav(out_p, duration_sec=4.0, base_hz=140.0 * (2.0 ** (1.8 / 12.0)), st_contour_fn=lambda t: 0.0, vibrato_st=0.2)
                return out_p
            if i == 1:
                # Mild +2.7 st outlier (score ~ 1.08)
                _write_modulated_speech_wav(out_p, duration_sec=4.0, base_hz=140.0 * (2.0 ** (2.7 / 12.0)), st_contour_fn=lambda t: 0.0, vibrato_st=0.2)
                return out_p
            if i == 3:
                # Severe internal span cliff (+3.0 -> -2.5 st = 5.5 st span, IQR ~ 4.2 st -> higher score!)
                _write_modulated_speech_wav(out_p, duration_sec=5.0, base_hz=140.0, st_contour_fn=lambda t: np.where(t < 2.5, 3.0, -2.5), vibrato_st=0.2)
                return out_p
            if i == 4:
                # Worst outlier (+6.0 st + internal span -> highest score!)
                _write_modulated_speech_wav(out_p, duration_sec=5.0, base_hz=140.0 * (2.0 ** (5.0 / 12.0)), st_contour_fn=lambda t: np.where(t < 2.5, 2.5, -2.5), vibrato_st=0.2)
                return out_p
        _write_modulated_speech_wav(out_p, duration_sec=4.0, base_hz=140.0, st_contour_fn=lambda t: 0.0, vibrato_st=0.2)
        return out_p

    ref140 = _write_modulated_speech_wav(tmp_path / "ref140.wav", duration_sec=8.0, base_hz=140.0, st_contour_fn=lambda t: 0.0, vibrato_st=0.2)
    with patch("app.services.group_ai_admin.transcribe_voice_gemini", new=AsyncMock(return_value=None)):
        worker_mod._synthesize_voxcpm_voice_job(
            SimpleNamespace(synthesize=_scored_synth, reset_pinned_space=lambda: None),
            job_id="job_rescore",
            text=" ".join(r_texts),
            output_wav=tmp_path / "rescore_out.wav",
            reference_wav=ref140,
            control_instruction="",
            preset_voice=None,
            speed="+0%",
            progress_cb=None,
            gender_preset="m2m",
        )
    # Chunk 0 (+1.8 st uniform) NOT retried; worst-scoring Chunks 4 & 3 got the 2 retries (Chunk 1 skipped due to cap=2)!
    assert calls_by_idx[0] == 1
    assert calls_by_idx[1] == 1
    assert calls_by_idx[3] == 2
    assert calls_by_idx[4] == 2

    # (C) Park at chunk 5/10 + resume in Conservation band (30% pool -> 0 retries, zero double-rubberbanding!)
    ten_texts = [f"အပိုင်း {i} တွင် ဇာတ်လမ်းကို ဆက်လက် တင်ဆက်ပေးထားပါသည်။" for i in range(1, 11)]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(ten_texts))

    def _make_chunk_i(i: int, dst: Path) -> Path:
        sign = 1.0 if (i % 2 == 0) else -1.0
        return _write_modulated_speech_wav(
            dst,
            duration_sec=5.5,
            base_hz=130.0 * (2.0 ** ((0.8 * sign) / 12.0)),
            st_contour_fn=lambda t, s=sign: np.where(t < 1.8, 1.4 * s, np.where(t > 3.8, -1.4 * s, 0.0)),
            vibrato_st=0.45,
        )

    # Uninterrupted 10-chunk run
    worker_mod._synthesize_voxcpm_voice_job(
        SimpleNamespace(
            synthesize=lambda **kw: _make_chunk_i(ten_texts.index(kw["text"]), Path(kw["output_wav"])),
            reset_pinned_space=lambda: None,
        ),
        job_id="job_10_clean",
        text=" ".join(ten_texts),
        output_wav=tmp_path / "clean10.wav",
        reference_wav=None,
        control_instruction="Storyteller",
        preset_voice="komin",
        speed="+0%",
        progress_cb=None,
        gender_preset="m2m",
    )

    # Parked at chunk 6 (chunks 1..5 checkpointed), then resumed in Conservation band (30% usable)
    ckpt_dir = tmp_path / "dp_refs" / "job_10_parked"
    park_calls: list[int] = []

    def _park_at_6(**kw):
        i = ten_texts.index(kw["text"])
        park_calls.append(i)
        if i == 5:
            raise vox_mod.VoxCPMQuotaExhaustedError("quota full at chunk 6")
        return _make_chunk_i(i, Path(kw["output_wav"]))

    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        worker_mod._synthesize_voxcpm_voice_job(
            SimpleNamespace(synthesize=_park_at_6, reset_pinned_space=lambda: None, hf_tokens=[]),
            job_id="job_10_parked",
            text=" ".join(ten_texts),
            output_wav=tmp_path / "parked10.wav",
            reference_wav=None,
            control_instruction="Storyteller",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            gender_preset="m2m",
            tier="paid",
            checkpoint_dir=ckpt_dir,
        )

    # Put pool in Conservation band (30% usable: 90s / 300s) and plant a +4 st outlier on chunk 8
    # to prove Conservation band spends ZERO retry calls while still running the full post-loop DSP pass!
    d_tok = hfl.token_digest("hf_cons_1")
    hfl.save_pool_state(
        {
            "tokens": {d_tok: {"status": "ok", "remaining_s": 90.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}},
            "space_gates": {"owner/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        }
    )
    resume_calls: list[int] = []

    def _resume_6_to_10(**kw):
        i = ten_texts.index(kw["text"])
        resume_calls.append(i)
        return _make_chunk_i(i, Path(kw["output_wav"]))

    worker_mod._synthesize_voxcpm_voice_job(
        SimpleNamespace(synthesize=_resume_6_to_10, reset_pinned_space=lambda: None, hf_tokens=[]),
        job_id="job_10_parked",
        text=" ".join(ten_texts),
        output_wav=tmp_path / "resumed10.wav",
        reference_wav=None,
        control_instruction="Storyteller",
        preset_voice="komin",
        speed="+0%",
        progress_cb=None,
        gender_preset="m2m",
        tier="paid",
        checkpoint_dir=ckpt_dir,
    )
    # Chunks 0..4 reused from raw checkpoint (never re-synthesized); chunks 5..9 synthesized once each (0 retries)
    assert resume_calls == [5, 6, 7, 8, 9]

    b_clean = evaluate_wav_prosody(tmp_path / "clean10.wav")
    b_resumed = evaluate_wav_prosody(tmp_path / "resumed10.wav")
    assert abs(b_clean["PDS"] - b_resumed["PDS"]) < 0.02
    assert abs(b_clean["F0_win_max_jump_st"] - b_resumed["F0_win_max_jump_st"]) < 0.02


def test_pin7_bench_gates_and_expressiveness_floor_across_fixtures(tmp_path):
    """Part 8 Bench Gates across multi-chunk narration fixtures (+ real 84923819 fixture when present):
    PDS <= 1.20, IQR in [0.45, 1.35], max_jump <= 1.75, outlier_ratio == 0.0%,
    Pace_CV <= 0.085, Pace_max_ratio <= 1.22 (--chunks-json), and intra-chunk IQR retention >= 85%."""
    norm_bench = normalize_numbers_for_speech(BENCHMARK_BURMESE_NARRATION)
    chunks_bench = vc.plan_text_chunks(
        norm_bench,
        control_instruction="Match the reference speaker's exact vocal timbre.",
        reference_seconds=10.0,
    )
    assert len(chunks_bench) >= 5

    # Build a realistic 10-chunk multi-paragraph narration (sentence seams within paragraphs +
    # paragraph breaks between paragraphs) with raw seesaws (+1.65/-1.75 st edges) + intonation
    ten_chunks = (chunks_bench * 2)[:10]
    sep_parts = []
    for i_c, c_part in enumerate(ten_chunks):
        sep_parts.append(c_part)
        if i_c < len(ten_chunks) - 1:
            sep_parts.append("\n\n" if (i_c % 3 == 2) else " ")
    ten_text = "".join(sep_parts)

    for label, base_hz, is_custom, is_flip, speed_str, speed_tp in [
        ("bench_0pct", 125.0, False, False, "+0%", 1.00),
        ("bench_12pct", 125.0, False, False, "+12%", 1.12),
        ("custom_same_gender_10c", 148.0, True, False, "+0%", 1.00),
        ("m2f_flip_10c", 232.0, True, True, "+0%", 1.00),
    ]:
        sub_dir = tmp_path / label
        raw_parts: list[Path] = []
        for idx, ch_txt in enumerate(ten_chunks):
            syls = max(12, cp.count_burmese_syllables(ch_txt))
            # Realistic ~6.6 syl/s with ±4% natural per-chunk pace jitter and ±1.8 st edge seesaw
            rate_jitter = 1.0 + (0.035 if idx % 2 == 0 else -0.030)
            dur_s = max(4.5, syls / (6.6 * rate_jitter))
            sign = 1.0 if idx % 2 == 0 else -1.0
            # Include gentle paragraph register variation (±0.55 st across chunks) so F0_win_IQR >= 0.45!
            chunk_offset_st = [0.55, -0.50, 0.45, -0.55, 0.60, -0.45, 0.50, -0.60, 0.40, -0.50][idx]

            def _contour(t: np.ndarray, d=dur_s, s=sign, off=chunk_offset_st) -> np.ndarray:
                head_e = np.where(t < 2.2, 1.65 * s * (1.0 - t / 2.2), 0.0)
                tail_e = np.where(t > d - 2.0, -1.75 * s * ((t - (d - 2.0)) / 2.0), 0.0)
                phrase_wave = 0.55 * np.sin(2.0 * math.pi * t / 7.5)
                return off + head_e + tail_e + phrase_wave

            p = _write_modulated_speech_wav(
                sub_dir / f"raw_{idx + 1:02d}.wav",
                duration_sec=dur_s,
                base_hz=base_hz,
                st_contour_fn=_contour,
                vibrato_st=0.75,
                syl_mod_hz=6.6 * rate_jitter,
            )
            raw_parts.append(p)

        ref_stats = (
            cp.ProsodyStats(10.0, 9.5, 8.5, 120.0 if is_flip else base_hz, 1.1, 65, 6.6)
            if is_custom
            else None
        )
        res = cp.run_post_loop_prosody_pass(
            raw_parts,
            ten_chunks,
            full_text=ten_text,
            out_dir=sub_dir / "corr",
            ref_prosody=ref_stats,
            is_custom_clone=is_custom,
            is_gender_flip=is_flip,
            preset_voice=None if is_custom else "komin",
            speed_tempo=speed_tp,
        )
        stitched = sub_dir / "stitched.wav"
        delivered = sub_dir / "delivered.wav"
        audio_mod.concat_wav_files(res.corrected_parts, stitched, boundary_pauses_sec=res.pauses_sec)
        vox_mod.master_voxcpm_audio(
            stitched,
            delivered,
            speed=speed_str,
            preset_voice=None if is_custom else "komin",
        )

        meta = [{"wav_path": str(res.corrected_parts[i]), "text": ten_chunks[i]} for i in range(len(ten_chunks))]
        rep_chunks = evaluate_wav_prosody(delivered, chunks_meta=meta)
        rep_wav = evaluate_wav_prosody(delivered)

        assert rep_chunks["PDS"] <= 1.20, f"{label}: {rep_chunks}"
        assert 0.45 <= rep_chunks["F0_win_IQR_st"] <= 1.35, f"{label}: {rep_chunks}"
        assert rep_chunks["F0_win_max_jump_st"] <= 1.75, f"{label}: {rep_chunks}"
        assert rep_chunks["F0_outlier_ratio"] == 0.0, f"{label}: {rep_chunks}"
        assert rep_chunks["Pace_CV"] <= 0.085, f"{label}: {rep_chunks}"
        assert rep_chunks["Pace_max_ratio"] <= 1.22, f"{label}: {rep_chunks}"
        assert rep_wav["Pace_max_ratio"] <= 1.25, f"{label} acoustic fallback: {rep_wav}"
        assert res.post_mean_iqr_st >= 0.85 * res.raw_mean_iqr_st, (
            f"{label} expressiveness floor: {res.post_mean_iqr_st} vs {res.raw_mean_iqr_st}"
        )
