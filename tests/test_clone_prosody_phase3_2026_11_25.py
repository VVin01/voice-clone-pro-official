"""Unit & DSP Integration Tests — Clone Prosody Phase 3 (2026-11-25).

Covers all §0–§10 Phase 3 requirements (Option A + Option C re-scoring hook):
  1. Part 1 DP balanced sentence chunker: balance objective, 65-syllable tail floor on jobs >=140 syls,
     effective_cap invariance, `\n\n` lookahead guard, single-chunk preservation, and Job A / Job B reference profiles.
  2. Part 2 Estimator fixes: `[0.45, 0.60] * local_median` subharmonic repair + 0.40 s modal edge estimator (`0.62x` floor)
     preventing 0.58x male phrase-final creak from registering as a -9..-10 st cliff.
  3. Part 3 Macro linear de-sag (`γ=0.72` >=10 s, `0.36` 8-10 s, `±0.08 st/s` cap, single-sentence/<8 s skip)
     + tightened deadbands + M5 (<1.2 s) Burmese tonal contour retention >= 95%.
  4. Part 4 Asymmetric 30/70 continuation seams + Burmese sentence-final natural fall preservation
     (-0.60 st untouched; -1.20 st lifted strictly within its 0.40 st excess below -0.80 st).
  5. Part 5 Uniform FFmpeg DSP chain (`pitchq=consistency:smoothing=on` capability probe + `asendcmd=f=<tmp>`)
     + Voiced LTAS Tilt EQ (`±3.0 dB` highshelf @ 2.2 kHz, `±1.8 dB` presence @ 1.0 kHz, `±0.4 dB` deadband)
     + strict voiced-frame-only gating (unvoiced sibilant bursts never trigger treble cuts).
  6. Part 6 Seam assembly: 35 ms raised-cosine fades over deterministic -58 dBFS (±1.5 dB) speech-shaped comfort noise.
  7. Part 7 Option C retry re-scoring terms (`+0.30*max(0, |Δtilt|-1.8) + 0.25*max(0, |sag|-1.6)`) inside existing
     solvency-gated retry cap (`MAX_PROSODY_RETRIES_PER_JOB == 2`, 0 retries in Conservation band).
  8. Part 8 Space-family switch instrumentation (`space_family_switched=True` on cross-endpoint re-pin).
  9. Part 9/0.5 Kill switches (`clone_prosody_phase3_enabled`, `chunk_balancing_v2_enabled`) registered in
     `SETTING_TYPES` / `CONFIG_SETTING_KEYS` and restoring byte-for-byte Phase 2 behavior when False,
     plus end-to-end M1–M5 gate verification on expressive question/quote and custom `\n\n` control cases.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from app import worker as worker_mod
from app.config import settings
from app.services import audio as audio_mod
from app.services import clone_prosody as cp
from app.services import dynamic_settings as ds_mod
from app.services import hf_pool_ledger as hfl
from app.services import voice_chunking as vc
from app.services import voxcpm_engine as vox_mod
from app.services.number_words import normalize_numbers_for_speech
from scripts.bench_clone_prosody import (
    BENCHMARK_BURMESE_NARRATION,
    build_exact_chunks_metadata,
    compute_sub_1p2s_tonal_iqr_st,
    evaluate_wav_prosody,
)


def _synth_rich_speech_wav(
    path: Path,
    *,
    duration_sec: float,
    base_hz: float,
    macro_st_fn=None,
    tonal_mod_st: float = 1.10,
    tilt_db_shift: float = 0.0,
    sample_rate: int = 24000,
    tail_creak_sec: float = 0.0,
    tail_creak_ratio: float = 0.58,
    unvoiced_hiss_windows: list[tuple[float, float]] | None = None,
) -> Path:
    """Synthesize a 24-harmonic speech-like WAV with Burmese syllable-scale (< 1.2 s) tonal
    modulation, controllable spectral tilt, optional 0.58x tail creak, and optional unvoiced hiss bursts."""
    n = max(1, int(round(duration_sec * sample_rate)))
    t = np.arange(n, dtype=np.float64) / float(sample_rate)

    if macro_st_fn is None:
        macro_st = np.zeros(n, dtype=np.float64)
    else:
        macro_st = np.asarray(macro_st_fn(t), dtype=np.float64)
        if macro_st.ndim == 0:
            macro_st = np.full(n, float(macro_st), dtype=np.float64)

    # Syllable-scale (< 1.2 s) Burmese tonal contours (~4.5 Hz syllable + ~2.1 Hz disyllabic word)
    tonal_st = float(tonal_mod_st) * (
        0.68 * np.sin(2.0 * np.pi * 4.5 * t)
        + 0.32 * np.sin(2.0 * np.pi * 2.1 * t + 0.5)
    )
    inst_hz = float(base_hz) * (2.0 ** ((macro_st + tonal_st) / 12.0))

    if tail_creak_sec > 0.0:
        creak_n = min(n // 3, int(round(tail_creak_sec * sample_rate)))
        if creak_n > 0:
            inst_hz[-creak_n:] = float(base_hz) * float(tail_creak_ratio)

    phase = 2.0 * np.pi * np.cumsum(inst_hz) / float(sample_rate)
    sig = np.zeros(n, dtype=np.float64)
    for h in range(1, 26):
        if h * base_hz > 0.45 * sample_rate:
            break
        hf_frac = max(0.0, min(1.5, (h * base_hz - 850.0) / 2200.0))
        amp = (1.0 / (h ** 1.35)) * (10.0 ** ((float(tilt_db_shift) * hf_frac) / 20.0))
        sig += amp * np.sin(h * phase + 0.13 * h)

    max_s = float(np.max(np.abs(sig)))
    if max_s > 1e-9:
        sig /= max_s

    amp_env = 0.65 + 0.35 * np.maximum(0.0, np.sin(2.0 * np.pi * 5.8 * t))
    fade = min(int(0.025 * sample_rate), n // 8)
    if fade > 1:
        amp_env[:fade] *= np.linspace(0.0, 1.0, fade)
        amp_env[-fade:] *= np.linspace(1.0, 0.0, fade)
    out = sig * amp_env * 0.52

    # Inject pure unvoiced high-frequency fricative bursts (aperiodic 2.5-6.0 kHz noise, 0voiced F0)
    if unvoiced_hiss_windows:
        rng = np.random.default_rng(99123)
        noise = rng.standard_normal(n)
        spec = np.fft.rfft(noise)
        freqs = np.fft.rfftfreq(n, d=1.0 / float(sample_rate))
        bp = (freqs >= 2600.0) & (freqs <= 6200.0)
        hiss = np.fft.irfft(spec * bp, n=n)
        hiss /= max(1e-6, float(np.max(np.abs(hiss))))
        for t0, t1 in unvoiced_hiss_windows:
            i0 = max(0, int(round(t0 * sample_rate)))
            i1 = min(n, int(round(t1 * sample_rate)))
            if i1 > i0:
                out[i0:i1] = hiss[i0:i1] * 0.60

    pcm = np.clip(np.round(out * 32767.0), -32768.0, 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return path


def test_part0_kill_switches_registered_in_dynamic_settings_and_default_true():
    """§0.5: Both Phase 3 kill switches are registered in SETTING_TYPES / CONFIG_SETTING_KEYS and default True."""
    for key in ("clone_prosody_phase3_enabled", "chunk_balancing_v2_enabled"):
        assert key in ds_mod.SETTING_TYPES
        assert ds_mod.SETTING_TYPES[key] is bool
        assert key in ds_mod.DYNAMIC_SETTING_KEYS
        assert key in ds_mod.CONFIG_SETTING_KEYS
        assert getattr(settings, key) is True
    assert cp.is_clone_prosody_phase3_enabled() is True
    assert vc.is_chunk_balancing_v2_enabled() is True


def test_part1_dp_chunker_balance_runt_guard_reference_outcomes_and_kill_switch(monkeypatch):
    """§1 & §0.4: Single-chunk invariance, Job A (~695 syls -> 6 chunks >=100 syls), Job B (2,441c / 647 syls -> no 31-syl runt),
    `\n\n` lookahead guard (<65 syl tail), cap invariance, and kill-switch byte-for-byte Phase 2 fallback."""
    # 1. Single-chunk invariance (§0.4)
    short_single = "မင်္ဂလာပါခင်ဗျာ။ ဒါက တိုတောင်းတဲ့ စမ်းသပ် စာကြောင်းလေး တစ်ခုပဲ ဖြစ်ပါတယ်။"
    short_para = "ပထမ စာကြောင်း တိုလေးပါ။\n\nဒုတိယ စာကြောင်း တိုလေးပါ။"  # < 220c para floor & <= 425c -> 1 chunk today!
    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", False)
    assert len(vc.plan_text_chunks(short_single)) == 1
    greedy_short_para = vc.plan_text_chunks(short_para)
    assert len(greedy_short_para) == 1
    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", True)
    assert vc.plan_text_chunks(short_single) == [short_single]
    assert vc.plan_text_chunks(short_para) == greedy_short_para

    # 2. Job A reference profile (~2,200 chars, ~695 syllables -> greedy [~128x5, 55] vs DP 6 chunks all >= 100 syls)
    s_a26 = "မနက်ခင်းမှာ မောင်မောင်နဲ့ သူငယ်ချင်းတွေ မြို့ထဲက ဈေးကြီးဆီ အတူတူ သွားကြပါတယ်။"  # 77c, 24 syls -> add 2 syls:
    s_a_hi = "မနက်ခင်းမှာ မောင်မောင်နဲ့ သူငယ်ချင်းတွေက မြို့ထဲက ဈေးကြီးဆီသို့ အတူတူ သွားကြပါတယ်။"  # 82c, 26 syls (5*82+4 = 414 <= 425; 6*82 > 425)
    s_a_lo = "မနက်ခင်းမှာ မောင်မောင်နဲ့ သူငယ်ချင်းတွေ မြို့ထဲက ဈေးကြီးဆီသို့ အတူတူ သွားကြပါတယ်။"  # 81c, 25 syls
    # 5 sentences per greedy chunk = 3*26 + 2*25 = 128 syls (~412 chars <= 425)! 5 chunks = 640 syls + 2 tail sentences (55 syls) = 695 syls!
    block_5 = [s_a_hi, s_a_lo, s_a_hi, s_a_lo, s_a_hi]
    tail_2 = [
        s_a_hi,
        "ညနေပိုင်းတွင် သူတို့အားလုံး အိမ်သို့ ပျော်ရွှင်စွာဖြင့် အတူတကွ ပြန်လည် ရောက်ရှိလာခဲ့ကြပါသည်။",
    ]
    job_a_text = " ".join(block_5 * 5 + tail_2)
    job_a_syls_total = cp.count_burmese_syllables(job_a_text)
    assert 685 <= job_a_syls_total <= 705, f"got {job_a_syls_total}"
    assert 2100 <= len(job_a_text) <= 2300, f"got {len(job_a_text)}"

    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", False)
    greedy_a = vc.plan_text_chunks(job_a_text, reference_seconds=7.6, reference_transcript="မင်္ဂလာပါရှင်။")
    greedy_a_syls = [cp.count_burmese_syllables(c) for c in greedy_a]
    assert len(greedy_a) == 6
    assert greedy_a_syls[-1] < 65, f"Expected greedy runt tail < 65 syls, got {greedy_a_syls}"

    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", True)
    bal_a = vc.plan_text_chunks(job_a_text, reference_seconds=7.6, reference_transcript="မင်္ဂလာပါရှင်။")
    bal_a_syls = [cp.count_burmese_syllables(c) for c in bal_a]
    assert len(bal_a) == 6
    assert min(bal_a_syls) >= 100, f"Expected all 6 chunks >= 100 syls, got {bal_a_syls}"
    assert all(len(c) <= vc.MAX_CHUNK_CHARS for c in bal_a)
    assert " ".join(bal_a).split() == job_a_text.split()

    # 3. Job B reference profile (exact 2,441 chars, ~647 syllables -> greedy ~26-31 syl runt tail vs DP 6-7 balanced chunks >= 65 syls)
    s_b_26 = "ကျောင်းသားလူငယ်လေးတွေဟာ တောင်တန်းလမ်းမကြီးအတိုင်း ပျော်ရွှင်စွာ အတူတူ လမ်းလျှောက်လာကြပါတယ်။"  # 92-96c, ~26 syls
    s_b_pad = s_b_26 + ("ာ" * max(0, 96 - len(s_b_26) - 1)) if len(s_b_26) < 96 else s_b_26
    s_b_96 = "ကျောင်းသားလူငယ်လေးများသည် တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်ချမ်းမြေ့စွာ လမ်းလျှောက်လာကြပါသည်။"  # exact 96c, 25 syls
    s_b_96_hi = "ကျောင်းသားလူငယ်လေးတွေဟာ တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်ချမ်းမြေ့စွာပဲ လျှောက်လာခဲ့ကြပါတယ်။"  # 95c, 26 syls
    b_sents = [s_b_96_hi if i < 16 else s_b_96 for i in range(24)]
    b_core = " ".join(b_sents)
    rem_chars = 2441 - len(b_core) - 1
    assert rem_chars > 60, f"unexpected rem_chars={rem_chars}"
    b_tail_seed = "မြို့တွင်းရှိ ရှေးဟောင်းအဆောက်အအုံကြီးများရှေ့တွင် အမှတ်တရ ဓာတ်ပုံများ အတူတကွ ရိုက်ကူးခဲ့ကြပါသည်"
    while len(b_tail_seed) < rem_chars - 1:
        b_tail_seed += "ာ"
    job_b_text = f"{b_core} {b_tail_seed[:rem_chars - 1]}။"
    assert len(job_b_text) == 2441
    tot_b_syls = cp.count_burmese_syllables(job_b_text)
    assert 620 <= tot_b_syls <= 665, f"got {tot_b_syls}"

    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", False)
    greedy_b = vc.plan_text_chunks(job_b_text, reference_seconds=6.2, reference_transcript="မင်္ဂလာပါခင်ဗျာ။")
    greedy_b_syls = [cp.count_burmese_syllables(c) for c in greedy_b]
    assert greedy_b_syls[-1] < 45, f"Expected greedy runt tail, got {greedy_b_syls}"

    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", True)
    bal_b = vc.plan_text_chunks(job_b_text, reference_seconds=6.2, reference_transcript="မင်္ဂလာပါခင်ဗျာ။")
    bal_b_syls = [cp.count_burmese_syllables(c) for c in bal_b]
    assert len(bal_b) in (6, 7)
    assert min(bal_b_syls) >= vc.MIN_TAIL_SYLLABLES, f"Expected min >= 65 syls, got {bal_b_syls}"
    assert all(len(c) <= vc.MAX_CHUNK_CHARS for c in bal_b)

    # 4. `\n\n` lookahead guard: prevents stranding a 30-syllable orphan paragraph at the end
    p_main = ("မောင်မောင်သည် မြို့သို့ စောစောစီးစီး ထွက်ခွာသွားခဲ့ပါသည်။ " * 11).strip()  # ~198 syls, ~620 chars
    p_runt = "နောက်ဆုံး စာကြောင်း တိုလေး တစ်ခုသာ ကျန်ပါတော့သည်။"  # ~14 syls (< 65!)
    with_runt_para = f"{p_main}\n\n{p_runt}"
    packed_runt = vc.plan_text_chunks(with_runt_para)
    packed_runt_syls = [cp.count_burmese_syllables(c) for c in packed_runt]
    assert min(packed_runt_syls) >= vc.MIN_TAIL_SYLLABLES, f"Lookahead failed: {packed_runt_syls}"

    # 5. Kill-switch exact byte-for-byte Phase 2 restoration
    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", False)
    assert vc.plan_text_chunks(job_b_text, reference_seconds=6.2, reference_transcript="မင်္ဂလာပါခင်ဗျာ။") == greedy_b


def test_part2_subharmonic_widening_and_modal_edge_estimator_on_0p58x_creak_tail(tmp_path, monkeypatch):
    """§2 & §10: 0.58x median male phrase-final creak (above 65 Hz floor) is repaired instead of reading as a cliff."""
    creak_058_wav = _synth_rich_speech_wav(
        tmp_path / "male_creak_058.wav",
        duration_sec=8.0,
        base_hz=120.0,
        macro_st_fn=lambda t: np.where(t > 7.2, -0.55 * ((t - 7.2) / 0.8), 0.0),
        tonal_mod_st=0.20,
        tail_creak_sec=0.22,
        tail_creak_ratio=0.58,  # 69.6 Hz > F0_MIN_HZ (65 Hz), outside old [0.46, 0.54] band!
    )

    # With Phase 3 OFF: raw contour keeps ~69.6 Hz (-9.4 st cliff) at the tail frames
    monkeypatch.setattr(settings, "clone_prosody_phase3_enabled", False)
    samples, sr = cp.read_wav_mono_float32(creak_058_wav)
    s16 = cp.resample_float32(samples, sr, cp.ANALYSIS_SAMPLE_RATE)
    _, f0_p2 = cp.extract_frame_contours(s16, cp.ANALYSIS_SAMPLE_RATE)
    v_p2 = f0_p2[~np.isnan(f0_p2)]
    assert float(np.min(v_p2)) < 78.0  # Unrepaired ~69.6 Hz creak frames survived in Phase 2 contour!

    # With Phase 3 ON: [0.45, 0.60] * local_median repair + 0.40 s modal edge estimator (0.62x floor)
    monkeypatch.setattr(settings, "clone_prosody_phase3_enabled", True)
    _, f0_p3 = cp.extract_frame_contours(s16, cp.ANALYSIS_SAMPLE_RATE)
    v_p3 = f0_p3[~np.isnan(f0_p3)]
    assert float(np.min(v_p3)) > 95.0, f"0.58x creak was not repaired in contour: min={np.min(v_p3)}"

    ana_p3 = cp.analyze_chunk_prosody(creak_058_wav, text="ကိုမင်းက ဇာတ်လမ်းကို အဆုံးသတ်လိုက်ပါသည်။")
    # Real modal tail settled smoothly to ~ -0.4..-0.6 st (NOT a -9.4 st creak cliff, and NOT averaged to 0.0 over 2 s!)
    assert -0.90 <= ana_p3.raw_tail_st_vs_med <= 0.10


def test_part3_linear_desag_math_rules_and_m5_tonal_retention(tmp_path):
    """§3 & §9 M5: γ=0.72 (>=10s, >=2 sents), halved 8-10s, skipped on single-sentence or <8s,
    capped at ±0.08 st/s, and M5 (<1.2s high-pass voiced F0 IQR) retains >=95% after FFmpeg pass."""
    sr_f = 100  # 10 ms hop = 100 frames/s

    # (A) Single-sentence 14 s chunk -> MUST skip de-sag
    n14 = 14 * sr_f
    t14 = np.arange(n14, dtype=np.float64) * cp.FRAME_STEP_SEC
    f0_slope14 = (130.0 * (2.0 ** ((-0.12 * (t14 - 7.0)) / 12.0))).astype(np.float32)
    d_single, sl_single, used_single = cp.compute_linear_desag_contour(
        f0_slope14, duration_sec=14.0, sentence_count=1, anchor_f0_hz=130.0
    )
    assert used_single is False and sl_single == 0.0 and np.all(d_single == 0.0)

    # (B) Short 6.5 s chunk with 2 sentences -> MUST skip de-sag (< 8.0 s)
    n65 = int(6.5 * sr_f)
    t65 = np.arange(n65, dtype=np.float64) * cp.FRAME_STEP_SEC
    f0_short = (130.0 * (2.0 ** ((-0.15 * (t65 - 3.25)) / 12.0))).astype(np.float32)
    _, sl_short, used_short = cp.compute_linear_desag_contour(
        f0_short, duration_sec=6.5, sentence_count=2, anchor_f0_hz=130.0
    )
    assert used_short is False and sl_short == 0.0

    # (C) Multi-sentence >= 10 s vs borderline 9.0 s: exact γ=0.72 vs halved γ=0.36
    # Raw slope = -0.100 st/s -> excess beyond 0.030 st/s = 0.070 st/s -> comp = +0.72 * 0.070 = +0.0504 st/s (12 s) vs +0.0252 st/s (9 s)
    n12 = 12 * sr_f
    t12 = np.arange(n12, dtype=np.float64) * cp.FRAME_STEP_SEC
    f0_12 = (130.0 * (2.0 ** ((-0.100 * (t12 - 6.0)) / 12.0))).astype(np.float32)
    _, sl_12, used_12 = cp.compute_linear_desag_contour(
        f0_12, duration_sec=12.0, sentence_count=2, anchor_f0_hz=130.0
    )
    assert used_12 is True
    assert abs(sl_12 - (0.72 * 0.070)) < 0.004

    n9 = 9 * sr_f
    t9 = np.arange(n9, dtype=np.float64) * cp.FRAME_STEP_SEC
    f0_9 = (130.0 * (2.0 ** ((-0.100 * (t9 - 4.5)) / 12.0))).astype(np.float32)
    _, sl_9, used_9 = cp.compute_linear_desag_contour(
        f0_9, duration_sec=9.0, sentence_count=2, anchor_f0_hz=130.0
    )
    assert used_9 is True
    assert abs(sl_9 - 0.5 * sl_12) < 0.004

    # (D) Steep slope (-0.25 st/s) -> strictly capped at +0.08 st/s
    f0_steep = (130.0 * (2.0 ** ((-0.250 * (t12 - 6.0)) / 12.0))).astype(np.float32)
    _, sl_steep, used_steep = cp.compute_linear_desag_contour(
        f0_steep, duration_sec=12.0, sentence_count=3, anchor_f0_hz=130.0
    )
    assert used_steep is True and abs(sl_steep - 0.080) < 1e-4

    # (E) Real FFmpeg pass on 3 sloped multi-sentence tonal chunks: M1 (max<=0.70, med<=0.40) AND M5 tonal retention >= 95%
    txt_1 = (
        "ပထမ စာကြောင်းတွင် မောင်မောင်သည် မနက်စောစောထ၍ မြို့ကြီးဆီသို့ သူငယ်ချင်းများနှင့်အတူ သွားခဲ့ပါသည်။ "
        "ဒုတိယ စာကြောင်းတွင် ဈေးကြီးအတွင်းရှိ ဆိုင်ခန်းများကို စိတ်ဝင်စားဖွယ်ရာ လှည့်လည် ကြည့်ရှုခဲ့ကြပါသည်။ "
        "တတိယ စာကြောင်းတွင် ညနေစောင်းအချိန်၌ အိမ်သို့ အေးအေးဆေးဆေး ပြန်လည် ရောက်ရှိလာခဲ့ပါသည်။"
    )
    txt_2 = (
        "စတုတ္ထ စာကြောင်းတွင် မိုးသားတိမ်လိပ်များ တက်လာပြီး မိုးဖွဲလေးများ စတင် ရွာသွန်းလာခဲ့ပါသည်။ "
        "ပဉ္စမ စာကြောင်းတွင် လေပြည်အေးလေးများ တိုက်ခတ်လာသဖြင့် ပတ်ဝန်းကျင်တစ်ခုလုံး အေးချမ်းသွားပါသည်။ "
        "ဆဋ္ဌမ စာကြောင်းတွင် မိုးတိတ်သွားသောအခါ ကောင်းကင်ပြင်ကြီး ပြန်လည် ကြည်လင်တောက်ပလာခဲ့ပါသည်။"
    )
    txt_3 = (
        "သတ္တမ စာကြောင်းတွင် ရွာသူရွာသားများသည် လယ်ကွင်းပြင်ဆီသို့ တက်ကြွစွာ ထွက်ခွာလာခဲ့ကြပါသည်။ "
        "အဋ္ဌမ စာကြောင်းတွင် စပါးခင်းစိမ်းစိမ်းများသည် လေအဝှေ့တွင် လှပစွာ ယိမ်းနွဲ့နေကြပါသည်။ "
        "နဝမ စာကြောင်းတွင် အားလုံးသည် မိမိတို့၏ လုပ်ငန်းခွင်၌ ပျော်ရွှင်စွာ လုပ်ကိုင်နေကြပါသည်။"
    )
    raw1 = _synth_rich_speech_wav(
        tmp_path / "tonal_sag_1.wav",
        duration_sec=15.0,
        base_hz=135.0,
        macro_st_fn=lambda t: 0.80 - 1.65 * (t / 15.0),
        tonal_mod_st=1.25,
    )
    raw2 = _synth_rich_speech_wav(
        tmp_path / "tonal_sag_2.wav",
        duration_sec=15.0,
        base_hz=135.0,
        macro_st_fn=lambda t: 0.41 - 0.82 * (t / 15.0),
        tonal_mod_st=1.25,
    )
    raw3 = _synth_rich_speech_wav(
        tmp_path / "tonal_sag_3.wav",
        duration_sec=15.0,
        base_hz=135.0,
        macro_st_fn=lambda t: 0.36 - 0.72 * (t / 15.0),
        tonal_mod_st=1.25,
    )
    full_tonal_txt = f"{txt_1} {txt_2} {txt_3}"
    res = cp.run_post_loop_prosody_pass(
        [raw1, raw2, raw3],
        [txt_1, txt_2, txt_3],
        full_text=full_tonal_txt,
        out_dir=tmp_path / "corr_tonal",
        preset_voice="komin",
    )
    assert res.desag_corrections == 3
    stitched = tmp_path / "stitched_tonal.wav"
    audio_mod.concat_wav_files(res.corrected_parts, stitched, boundary_pauses_sec=res.pauses_sec)
    meta = build_exact_chunks_metadata(
        res.corrected_parts, [txt_1, txt_2, txt_3], full_text=full_tonal_txt, pauses_sec=res.pauses_sec, raw_part_wavs=[raw1, raw2, raw3]
    )
    bench = evaluate_wav_prosody(stitched, chunks_meta=meta)
    p3m = bench["phase3"]
    assert p3m["M1_pass"] is True, p3m
    assert p3m["M5_tonal_retention_pct"] >= 95.0, p3m
    assert p3m["M5_pass"] is True


def test_part4_asymmetric_30_70_seams_and_natural_fall_preservation():
    """§4 & §10: 30/70 split on CLAUSE/PARAGRAPH; SENTENCE -0.60 st tail untouched; -1.20 st tail lifted only by excess (<=-0.80 st)."""
    clause_spec = cp.BoundarySpec(kind="CLAUSE", s_max_st=0.45, pause_sec=0.095, base_pause_sec=0.095, question_tail=False)
    sent_spec = cp.BoundarySpec(kind="SENTENCE", s_max_st=0.65, pause_sec=0.135, base_pause_sec=0.135, question_tail=False)

    # 1. CLAUSE boundary: et = -0.60 st, eh = +0.85 st -> diff = 1.45 st, excess = 1.00 st -> 30% (+0.30 st) tail, 70% (-0.70 st) head
    t_r, h_r = cp.compute_seam_ramps_for_pair(-0.60, 0.85, clause_spec)
    d_tail = t_r * 0.95
    d_head = h_r * 0.97
    assert abs(d_tail - 0.30) < 0.02
    assert abs(d_head - (-0.70)) < 0.02

    # 2. SENTENCE boundary with natural -0.60 st fall (in [-0.80, -0.45] st) and +0.80 st head:
    # Tail MUST stay untouched (0.0 st lift); head absorbs 100% of excess (1.40 - 0.65 = 0.75 st -> eh' = +0.05 st)
    t_s1, h_s1 = cp.compute_seam_ramps_for_pair(-0.60, 0.80, sent_spec)
    assert t_s1 == 0.0, f"Natural -0.60 st sentence fall must NOT be lifted: got {t_s1}"
    assert abs(h_s1 * 0.97 - (-0.75)) < 0.02

    # 3. SENTENCE boundary with deep -1.20 st fall (0.40 st excess beyond -0.80 st) and +0.85 st head:
    # diff = 2.05 st, excess = 1.40 st -> raw 30% = +0.42 st, capped strictly at +0.40 st (excess below -0.80 st)!
    t_s2, h_s2 = cp.compute_seam_ramps_for_pair(-1.20, 0.85, sent_spec)
    d_tail_2 = t_s2 * 0.95
    d_head_2 = h_s2 * 0.97
    assert 0.0 < d_tail_2 <= 0.4001, f"-1.20 st tail lifted beyond its 0.40 st excess: {d_tail_2}"
    assert abs(d_tail_2 - 0.40) < 0.02
    assert abs((-1.20 + d_tail_2) - (-0.80)) < 0.02
    assert abs((0.85 + d_head_2) - (-1.20 + d_tail_2) - 0.65) < 0.03


def test_part5_uniform_chain_voiced_only_tilt_eq_clamps_and_unvoiced_hiss_immunity(tmp_path):
    """§5 & §10: Capability probe, tilt EQ clamps/deadband, unvoiced-hiss immunity, and real FFmpeg Chunk-1 brightness leveling."""
    caps = cp.probe_rubberband_capabilities()
    assert caps["available"] is True
    assert isinstance(caps["pitchq_consistency"], bool)
    assert isinstance(caps["smoothing"], bool)

    # 1. Deadband (±0.40 dB -> 0.0 dB) and clamps (±3.0 dB highshelf, ±1.8 dB presence)
    assert cp.compute_chunk_tilt_eq_gains(-11.80, -6.10, target_tilt_db=-12.05, target_pres_db=-6.30) == (0.0, 0.0)
    hs_hi, pr_hi = cp.compute_chunk_tilt_eq_gains(-5.0, -1.0, target_tilt_db=-13.0, target_pres_db=-7.0)
    assert hs_hi == -cp.TILT_HIGHSHELF_MAX_DB and pr_hi == -cp.TILT_PRESENCE_MAX_DB
    hs_lo, pr_lo = cp.compute_chunk_tilt_eq_gains(-20.0, -14.0, target_tilt_db=-12.0, target_pres_db=-6.0)
    assert hs_lo == cp.TILT_HIGHSHELF_MAX_DB and pr_lo == cp.TILT_PRESENCE_MAX_DB

    # 2. Unvoiced-heavy chunk immunity: loud 2.6-6.2 kHz fricative hiss bursts MUST NOT drive voiced LTAS EQ!
    clean_v = _synth_rich_speech_wav(tmp_path / "v_clean.wav", duration_sec=6.0, base_hz=135.0, tilt_db_shift=0.0)
    hissy_v = _synth_rich_speech_wav(
        tmp_path / "v_hissy.wav",
        duration_sec=6.0,
        base_hz=135.0,
        tilt_db_shift=0.0,
        unvoiced_hiss_windows=[(0.8, 1.5), (2.4, 3.2), (4.1, 4.9)],
    )
    # Verify whole-file high-band energy IS much higher on hissy_v (> +3 dB), while voiced-only LTAS is unchanged!
    s_cl, sr_cl = cp.read_wav_mono_float32(clean_v)
    s_hs, sr_hs = cp.read_wav_mono_float32(hissy_v)
    f_cl = np.abs(np.fft.rfft(s_cl)) ** 2
    f_hs = np.abs(np.fft.rfft(s_hs)) ** 2
    freqs = np.fft.rfftfreq(s_cl.size, d=1.0 / sr_cl)
    whole_hi_diff_db = 10.0 * math.log10(
        float(np.sum(f_hs[(freqs >= 2600) & (freqs <= 5500)]))
        / float(np.sum(f_cl[(freqs >= 2600) & (freqs <= 5500)]))
    )
    assert whole_hi_diff_db > 3.0

    ana_cl = cp.analyze_chunk_prosody(clean_v, text="ပထမ စမ်းသပ် စာကြောင်း ဖြစ်ပါသည်။")
    ana_hs = cp.analyze_chunk_prosody(hissy_v, text="ဒုတိယ စမ်းသပ် စာကြောင်း ဖြစ်ပါသည်။")
    assert abs(ana_hs.voiced_tilt_db - ana_cl.voiced_tilt_db) <= 0.35
    eq_hs, eq_pr = cp.compute_chunk_tilt_eq_gains(
        ana_hs.voiced_tilt_db,
        ana_hs.voiced_presence_db,
        target_tilt_db=ana_cl.voiced_tilt_db,
        target_pres_db=ana_cl.voiced_presence_db,
    )
    assert eq_hs == 0.0 and eq_pr == 0.0, f"Unvoiced hiss wrongly triggered EQ: {(eq_hs, eq_pr)}"

    # 3. Real FFmpeg uniform chain + Chunk-1 voiced brightness outlier (+3.0 dB) leveled within M3 & M4 gates
    c_txts = [
        "ပထမ အပိုင်းတွင် အသံအရောင် ပိုမို တောက်ပနေသော စမ်းသပ်ချက် ဖြစ်ပါသည်။ ဒုတိယ ဝါကျလည်း ပါဝင်ပါသည်။",
        "ဒုတိယ အပိုင်းတွင် ပုံမှန် အသံအရောင်ဖြင့် ဆက်လက် ပြောကြားထားပါသည်။ နောက်ထပ် ဝါကျတစ်ခု ဆက်ပါသည်။",
        "တတိယ အပိုင်းတွင်လည်း ပုံမှန် အသံအရောင် အတိုင်းသာ တည်ငြိမ်စွာ ရှိနေပါသည်။ အဆုံးသတ် ဝါကျ ဖြစ်ပါသည်။",
    ]
    b_parts = [
        _synth_rich_speech_wav(tmp_path / "b1_bright.wav", duration_sec=10.5, base_hz=122.0, tilt_db_shift=3.2),
        _synth_rich_speech_wav(tmp_path / "b2_norm.wav", duration_sec=10.5, base_hz=122.0, tilt_db_shift=0.0),
        _synth_rich_speech_wav(tmp_path / "b3_norm.wav", duration_sec=10.5, base_hz=122.0, tilt_db_shift=0.0),
    ]
    res_b = cp.run_post_loop_prosody_pass(
        b_parts, c_txts, full_text=" ".join(c_txts), out_dir=tmp_path / "b_corr", preset_voice="komin"
    )
    # Uniform chain: ALL 3 chunks ran through FFmpeg even though Chunks 2 & 3 had 0 pitch/tempo/EQ shift!
    assert res_b.corrections_applied == 3
    assert res_b.tilt_eq_corrections >= 1
    stitched_b = tmp_path / "b_stitched.wav"
    audio_mod.concat_wav_files(res_b.corrected_parts, stitched_b, boundary_pauses_sec=res_b.pauses_sec)
    meta_b = build_exact_chunks_metadata(res_b.corrected_parts, c_txts, full_text=" ".join(c_txts), pauses_sec=res_b.pauses_sec, raw_part_wavs=b_parts)
    rep_b = evaluate_wav_prosody(stitched_b, chunks_meta=meta_b)
    assert rep_b["phase3"]["M3_pass"] is True, rep_b["phase3"]
    assert rep_b["phase3"]["M4_max_centroid_step_pct"] <= 8.0, rep_b["phase3"]


def test_part6_room_tone_bed_minus_58_dbfs_and_35ms_cosine_fades(tmp_path):
    """§6 & §10: Pause durations unchanged, pause bed at -58 dBFS ± 1.5 dB (deterministic), and 35 ms cosine fades."""
    sr = 24000
    w1 = _synth_rich_speech_wav(tmp_path / "rt1.wav", duration_sec=1.2, base_hz=140.0, sample_rate=sr)
    w2 = _synth_rich_speech_wav(tmp_path / "rt2.wav", duration_sec=1.2, base_hz=140.0, sample_rate=sr)

    out1 = tmp_path / "joined_rt1.wav"
    out2 = tmp_path / "joined_rt2.wav"
    audio_mod.concat_wav_files([w1, w2], out1, boundary_pauses_sec=[0.220])
    audio_mod.concat_wav_files([w1, w2], out2, boundary_pauses_sec=[0.220])

    # Deterministic bit-exact across runs
    assert out1.read_bytes() == out2.read_bytes()

    s_out, sr_out = cp.read_wav_mono_float32(out1)
    assert sr_out == sr
    expected_n = int(round(1.2 * sr)) * 2 + int(round(0.220 * sr))
    assert abs(s_out.size - expected_n) <= 2

    # Pause region [1.200 s .. 1.420 s] sits at -58.0 dBFS ± 1.5 dB (not -inf digital zeros!)
    p_start = int(round(1.205 * sr))
    p_end = int(round(1.415 * sr))
    pause_seg = s_out[p_start:p_end]
    rms = float(np.sqrt(np.mean(pause_seg.astype(np.float64) ** 2)))
    dbfs = 20.0 * math.log10(max(1e-12, rms))
    assert -59.5 <= dbfs <= -56.5, f"Expected -58.0 ± 1.5 dBFS room-tone bed, got {dbfs:.2f} dBFS"

    # Verify 35 ms raised-cosine fade-in on constant-amplitude step
    const_pcm = np.full(int(sr * 0.5), 20000, dtype="<i2")
    c1 = tmp_path / "c1.wav"
    c2 = tmp_path / "c2.wav"
    for cp_p in (c1, c2):
        with wave.open(str(cp_p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(const_pcm.tobytes())
    c_join = tmp_path / "c_join.wav"
    audio_mod.concat_wav_files([c1, c2], c_join, boundary_pauses_sec=[0.135])
    with wave.open(str(c_join), "rb") as wf:
        joined_i16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    fade_n = int(round(0.035 * sr))
    c2_start = const_pcm.size + int(round(0.135 * sr))
    # Midpoint of 35 ms raised-cosine fade (17.5 ms) is ~0.50 * 20000 = 10000 (whereas 10 ms linear fade was already 20000 at 17.5 ms!)
    mid_sample = int(joined_i16[c2_start + fade_n // 2])
    assert 8500 <= mid_sample <= 11500, f"35 ms raised-cosine midpoint mismatch: {mid_sample}"


def test_part7_and_part8_option_c_rescoring_and_space_family_switch_telemetry(tmp_path, monkeypatch, caplog):
    """§7 & §8: Exact tilt/sag candidate scoring formula, solvency-gated retry on Chunk-1 brightness outlier,
    and space_family_switched=True instrumentation across /generate_clone -> /generate re-pin."""
    base_stats = cp.ProsodyStats(12.0, 11.0, 10.0, 130.0, 1.5, 66, 6.0)
    s0 = cp.score_prosody_candidate(base_stats, anchor_f0_hz=130.0, anchor_rate=6.0)
    s_tilt = cp.score_prosody_candidate(
        base_stats, anchor_f0_hz=130.0, anchor_rate=6.0, voiced_tilt_delta_db=2.80
    )
    s_sag = cp.score_prosody_candidate(
        base_stats, anchor_f0_hz=130.0, anchor_rate=6.0, linear_sag_st=-2.60
    )
    assert abs((s_tilt - s0) - 0.30 * (2.80 - 1.80)) < 1e-6
    assert abs((s_sag - s0) - 0.25 * (2.60 - 1.60)) < 1e-6

    # Part 8: Space-family switch instrumentation on VoxCPMEngine one-time re-pin
    monkeypatch.setenv("VOXCPM_SPACES", "yunaisbusy/voxcpm2-fast,owner/voxcpm2-main")
    monkeypatch.setenv("HF_TOKENS", "hf_tok_a,hf_tok_b")
    monkeypatch.setattr(vox_mod.VoxCPMEngine, "_ensure_cooldowns_restored", classmethod(lambda cls, tokens=None: {}))
    monkeypatch.setattr(vox_mod, "space_supports_clone", lambda _sp: True)
    import gradio_client
    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)

    ref_w = _synth_rich_speech_wav(tmp_path / "ref_sp.wav", duration_sec=3.0, base_hz=130.0)
    gen_w = _synth_rich_speech_wav(tmp_path / "gen_sp.wav", duration_sec=3.0, base_hz=130.0)
    eng = vox_mod.VoxCPMEngine(work_dir=tmp_path / "eng_work")
    call_step = {"n": 0}

    class _FakeGradioJob:
        def done(self):
            return True

        def status(self):
            return SimpleNamespace(eta=None, position=None)

        def result(self):
            return str(gen_w)

    class _FakeClient:
        def __init__(self, sp: str):
            self.sp = sp

        def submit(self, *args, **kwargs):
            if call_step["n"] == 2 and self.sp == "yunaisbusy/voxcpm2-fast":
                raise RuntimeError("Space A overloaded on chunk 2")
            return _FakeGradioJob()

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient(sp)),
    )

    call_step["n"] = 1
    eng.synthesize("ပထမ အပိုင်း။", tmp_path / "s1.wav", reference_wav=ref_w, prompt_text="နမူနာ။", skip_mastering=True, allow_fallback=False)
    assert eng.pinned_space == "yunaisbusy/voxcpm2-fast"
    assert eng.space_family_switched is False

    call_step["n"] = 2
    eng.synthesize("ဒုတိယ အပိုင်း။", tmp_path / "s2.wav", reference_wav=ref_w, prompt_text="နမူနာ။", skip_mastering=True, allow_fallback=False)
    assert eng.pinned_space == "owner/voxcpm2-main"
    assert eng.space_family_switched is True

    # Verify worker logs space_family_switched=True and spends 1 retry on severe Chunk-1 voiced tilt outlier in Normal band
    hfl.reset_ledger_for_tests(clear_store=True)
    c_texts = [
        "ပထမ အပိုင်းတွင် အသံအရောင် လွန်ကဲစွာ တောက်ပနေပါသည်။ ဒုတိယ ဝါကျ ဆက်ပါသည်။",
        "ဒုတိယ အပိုင်းတွင် ပုံမှန် အသံအရောင် ရှိနေပါသည်။ ဒုတိယ ဝါကျ ဆက်ပါသည်။",
        "တတိယ အပိုင်းတွင် ပုံမှန် အသံအရောင် ရှိနေပါသည်။ ဒုတိယ ဝါကျ ဆက်ပါသည်။",
    ]
    monkeypatch.setattr(worker_mod, "plan_text_chunks", lambda *a, **kw: list(c_texts))
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    per_chunk_calls: dict[int, int] = {}

    def _tilt_outlier_synth(**kwargs):
        idx = c_texts.index(kwargs["text"])
        per_chunk_calls[idx] = per_chunk_calls.get(idx, 0) + 1
        # Chunk 0 first attempt has +4.0 dB voiced tilt outlier (same 130 Hz F0 & pace!); retry lands clean at 0.0 dB
        shift = 4.0 if (idx == 0 and per_chunk_calls[idx] == 1) else 0.0
        return _synth_rich_speech_wav(Path(kwargs["output_wav"]), duration_sec=10.5, base_hz=130.0, tilt_db_shift=shift)

    fake_eng = SimpleNamespace(
        synthesize=_tilt_outlier_synth,
        reset_pinned_space=lambda: None,
        space_family_switched=True,
    )
    with caplog.at_level(logging.INFO, logger="app.worker"):
        worker_mod._synthesize_voxcpm_voice_job(
            fake_eng,
            job_id="job_p3_opt_c",
            text=" ".join(c_texts),
            output_wav=tmp_path / "opt_c_out.wav",
            reference_wav=None,
            control_instruction="Storyteller",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
        )
    assert per_chunk_calls == {0: 2, 1: 1, 2: 1}
    assert any("space_family_switched=True" in r.getMessage() for r in caplog.records)


def test_part9_kill_switch_phase2_restoration_and_control_cases_m1_to_m5(tmp_path, monkeypatch):
    """§0.5 & §9: Kill switch restores exact Phase 2 zero-pause/bypass behavior; Phase 3 passes M1-M5 on
    both Expressive (questions/quotes) and Custom-Voice (`\n\n`, +12% speed) control cases."""
    # 1. Kill switch OFF -> Phase 2 digital-zero pause & raw bypass when inside ±0.60 st deadband
    monkeypatch.setattr(settings, "clone_prosody_phase3_enabled", False)
    p1 = _synth_rich_speech_wav(tmp_path / "ks1.wav", duration_sec=5.0, base_hz=130.0, tonal_mod_st=0.2)
    p2 = _synth_rich_speech_wav(tmp_path / "ks2.wav", duration_sec=5.0, base_hz=130.0 * (2.0 ** (0.45 / 12.0)), tonal_mod_st=0.2)
    res_off = cp.run_post_loop_prosody_pass(
        [p1, p2],
        ["ပထမ ဝါကျ။", "ဒုတိယ ဝါကျ။"],
        full_text="ပထမ ဝါကျ။ ဒုတိယ ဝါကျ။",
        out_dir=tmp_path / "ks_off",
        preset_voice="komin",
    )
    assert res_off.corrections_applied == 0  # Raw bypass inside ±0.60 st deadband in Phase 2!
    joined_off = tmp_path / "joined_off.wav"
    audio_mod.concat_wav_files(res_off.corrected_parts, joined_off, boundary_pauses_sec=[0.150])
    s_off, sr_off = cp.read_wav_mono_float32(joined_off)
    mid_p = s_off[int(5.03 * sr_off) : int(5.12 * sr_off)]
    assert np.all(mid_p == 0.0)  # Exact digital zeros in Phase 2!

    # 2. Phase 3 ON -> Control Case A (Expressive persona with `လား။`, `?`, `"..."`, `!`, >= 65 syls/chunk)
    monkeypatch.setattr(settings, "clone_prosody_phase3_enabled", True)
    monkeypatch.setattr(settings, "chunk_balancing_v2_enabled", True)
    expr_full = (
        '“ဒီတံခါးနောက်ကွယ်မှာ ဘာတွေရှိနေမလဲ?” လို့ မနဒီက တိုးတိုးလေး မေးလိုက်ပါတယ်။ '
        'အမှောင်ထုထဲကနေ ခြေသံတစ်ခု တဖြည်းဖြည်း နီးကပ်လာနေတာကို သူမ အသေအချာ ကြားနေရပါတယ်! '
        'ဒါဟာ တကယ်ပဲ သူမ နှစ်ပေါင်းများစွာ ရှာဖွေနေခဲ့တဲ့ လျှို့ဝှက်ချက်ကြီး ဖြစ်နေပါသလား။\n\n'
        '“မကြောက်ပါနဲ့၊ ရှေ့ကိုသာ ရဲရဲဝံ့ဝံ့ ဆက်လျှောက်ပါ!” ဆိုတဲ့ အသံကြီးက ခန်းမကြီးတစ်ခုလုံးကို ပဲ့တင်ထပ်သွားစေခဲ့ပါတယ်။ '
        'သူမရဲ့ နှလုံးခုန်သံတွေ မြန်ဆန်လာပေမဲ့ လက်ထဲက မီးအိမ်လေးကို တင်းတင်းကျပ်ကျပ် ဆုပ်ကိုင်ထားလိုက်ပါတယ်။ '
        'နောက်ဆုံးမှာတော့ ရွှေရောင်သေတ္တာကြီးကို သူမ မျက်စိရှေ့မှာပဲ အံ့ဩဖွယ်ရာ တွေ့မြင်လိုက်ရပါတော့တယ်။\n\n'
        'နာရီသံ တချောက်ချောက် မြည်နေတဲ့ကြားကနေ သူမဟာ ပဟေဠိရဲ့ အဖြေမှန်ကို ချက်ချင်းပဲ ရှာဖွေတွေ့ရှိသွားခဲ့ပါတယ်! '
        'ရှေးဟောင်း သံတံခါးချပ်ကြီး ပွင့်သွားတဲ့အခါ အလင်းရောင်တွေ ဖြာကျလာခဲ့ပါတော့တယ်။ '
        'ဒီအောင်မြင်မှုဟာ သူမတို့အဖွဲ့အတွက် အကြီးမားဆုံး ဆုလာဘ်ကြီးတစ်ခုပဲ ဖြစ်ပါတော့တယ်။'
    )
    expr_chunks = vc.plan_text_chunks(expr_full, reference_seconds=8.0)
    assert len(expr_chunks) >= 3
    expr_raw = []
    for idx, ch in enumerate(expr_chunks):
        q = cp.is_question_ending(ch)
        dur_ch = max(12.5, cp.count_burmese_syllables(ch) / 5.5)
        sag_span = 1.45 if idx == 0 else (0.85 if idx == 1 else 0.65)
        expr_raw.append(
            _synth_rich_speech_wav(
                tmp_path / f"ctrl_expr_{idx}.wav",
                duration_sec=dur_ch,
                base_hz=210.0,
                macro_st_fn=lambda t, d=dur_ch, is_q=q, sg=sag_span: 0.5 * sg - sg * (t / d) + (1.30 * np.maximum(0.0, (t - (d - 1.6)) / 1.6) if is_q else 0.0),
                tilt_db_shift=(2.4 if idx == 0 else 0.0),
            )
        )
    res_expr = cp.run_post_loop_prosody_pass(
        expr_raw, expr_chunks, full_text=expr_full, out_dir=tmp_path / "expr_corr", preset_voice="manadi"
    )
    stitched_expr = tmp_path / "expr_stitched.wav"
    deliv_expr = tmp_path / "expr_deliv.wav"
    audio_mod.concat_wav_files(res_expr.corrected_parts, stitched_expr, boundary_pauses_sec=res_expr.pauses_sec)
    vox_mod.master_voxcpm_audio(stitched_expr, deliv_expr, speed="+0%", preset_voice="manadi")
    meta_expr = build_exact_chunks_metadata(
        res_expr.corrected_parts, expr_chunks, full_text=expr_full, pauses_sec=res_expr.pauses_sec, raw_part_wavs=expr_raw
    )
    rep_expr = evaluate_wav_prosody(deliv_expr, chunks_meta=meta_expr)
    assert rep_expr["phase3"]["all_phase3_gates_pass"] is True, rep_expr["phase3"]

    # 3. Control Case B (Custom-voice narration with `\n\n` breaks at +12% default speed)
    cust_text = normalize_numbers_for_speech(BENCHMARK_BURMESE_NARRATION)
    cust_chunks = vc.plan_text_chunks(cust_text, control_instruction=vox_mod.DEFAULT_CLONE_FIDELITY_CONTROL, reference_seconds=10.0)
    cust_sags = [1.55, 1.15, 0.52, 0.35, 0.42, 0.30]
    cust_raw = [
        _synth_rich_speech_wav(
            tmp_path / f"ctrl_cust_{i}.wav",
            duration_sec=max(11.5, cp.count_burmese_syllables(ch) / 5.6),
            base_hz=148.0 * (2.0 ** ((0.45 if i == 0 else -0.15) / 12.0)),
            macro_st_fn=lambda t, d=max(11.5, cp.count_burmese_syllables(ch) / 5.6), sg=cust_sags[i % len(cust_sags)]: 0.5 * sg - sg * (t / d),
            tilt_db_shift=(2.5 if i == 0 else (-0.6 if i == len(cust_chunks) - 1 else 0.0)),
            tail_creak_sec=0.16,
            tail_creak_ratio=0.58,
        )
        for i, ch in enumerate(cust_chunks)
    ]
    ref_st = cp.ProsodyStats(10.0, 9.2, 8.4, 148.0, 1.1, 55, 5.6)
    res_cust = cp.run_post_loop_prosody_pass(
        cust_raw,
        cust_chunks,
        full_text=cust_text,
        out_dir=tmp_path / "cust_corr",
        ref_prosody=ref_st,
        is_custom_clone=True,
        speed_tempo=1.12,
    )
    stitched_cust = tmp_path / "cust_stitched.wav"
    deliv_cust = tmp_path / "cust_deliv.wav"
    audio_mod.concat_wav_files(res_cust.corrected_parts, stitched_cust, boundary_pauses_sec=res_cust.pauses_sec)
    vox_mod.master_voxcpm_audio(stitched_cust, deliv_cust, speed="+12%", preset_voice=None)
    meta_cust = build_exact_chunks_metadata(
        res_cust.corrected_parts,
        cust_chunks,
        full_text=cust_text,
        pauses_sec=res_cust.pauses_sec,
        raw_part_wavs=cust_raw,
        post_stitch_tempo=1.12,
    )
    rep_cust = evaluate_wav_prosody(deliv_cust, chunks_meta=meta_cust)
    assert rep_cust["phase3"]["all_phase3_gates_pass"] is True, rep_cust["phase3"]
