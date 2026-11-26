#!/usr/bin/env python3
"""Standalone Benchmark & Calibration Tool for Long-Form Voice Clone Prosody.

Supports:
  1. Measure mode on ANY arbitrary WAV path (with or without chunk metadata):
       python scripts/bench_clone_prosody.py --wav path/to/audio.wav [--chunks-json chunks.json] [--baseline-wav d1bf3376.wav]
     Computes per-5s-window median F0 (Hz + semitone deviation vs file median),
     F0_win_IQR_st, F0_win_max_jump_st, F0_outlier_ratio (>2.5 st windows),
     Pace_CV, Pace_max_ratio, and composite Prosody Drift Score:
       PDS = 0.40 * IQR + 0.30 * max_jump + 0.30 * (20 * Pace_CV)
  2. Calibration sweep modes (measurement/planning tables, non-gating):
       python scripts/bench_clone_prosody.py --sweep-chunk-chars 300,425,600
       python scripts/bench_clone_prosody.py --sweep-slice-secs 8,10,15 --ref-wav ref.wav
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import tempfile
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services import clone_prosody as cp  # noqa: E402
from app.services import voice_chunking as vc  # noqa: E402
from app.services.number_words import normalize_numbers_for_speech  # noqa: E402

BENCH_SPEEDS = ("+12%", "+0%")

# Fixed ~2,000-char Burmese storytelling narration (numbers, multi-clause sentences, paragraph breaks)
BENCHMARK_BURMESE_NARRATION = (
    "1998 ခုနှစ်ရဲ့ ဆောင်းဦးညနေခင်းတစ်ခုမှာ တောင်ပေါ်မြို့လေးဆီကို အမြန်ရထားကြီးတစ်စင်း တဖြည်းဖြည်း ဝင်ရောက်လာခဲ့ပါတယ်။ "
    "ရထားပေါ်ကနေ ဆင်းလာတဲ့ အသက် 28 နှစ်အရွယ် စုံထောက်လူငယ် ကိုသက်နောင်ဟာ လက်ဆွဲသေတ္တာအဟောင်းလေးကို ကိုင်ရင်း၊ "
    "မြူခိုးတွေ ဖုံးလွှမ်းနေတဲ့ ဘူတာရုံပတ်ဝန်းကျင်ကို သတိကြီးစွာ အကဲခတ်ကြည့်လိုက်ပါတယ်။ သူ့အိတ်ကပ်ထဲမှာတော့ လွန်ခဲ့တဲ့ 15 ရက်က "
    "ပျောက်ဆုံးသွားခဲ့တဲ့ ပန်းချီဆရာကြီး ဦးဘဉာဏ် ပေးပို့ခဲ့တဲ့ လျှို့ဝှက်စာတစ်စောင် ပါလာခဲ့ပါတယ်။\n\n"
    "အဲဒီစာထဲမှာ ရေးသားထားတာကတော့ အရမ်းကို ထူးဆန်းနေပါတယ်၊ မြို့ရဲ့ မြောက်ဘက် မိုင် 12 အကွာမှာ ရှိတဲ့ နာရီစင်အိုကြီးထဲမှာ "
    "နှစ်ပေါင်း 60 ကျော် ဖွက်ထားခဲ့တဲ့ မိသားစု ရတနာသေတ္တာတစ်လုံး ရှိနေတယ်လို့ ဆိုထားပါတယ်။ ဒါပေမဲ့ အဲဒီနေရာကို သွားတဲ့လမ်းဟာ "
    "မတ်စောက်တဲ့ တောင်ကြားလမ်းတွေ၊ သစ်တောထူထပ်တဲ့ ချိုင့်ဝှမ်းတွေကို ဖြတ်ကျော်ရမှာ ဖြစ်သလို၊ ဒေသခံတွေကလည်း ညနေ 6 နာရီကျော်ရင် "
    "ဘယ်သူမှ မသွားရဲကြဘူးလို့ သတိပေးထားကြပါတယ်။\n\n"
    "ကိုသက်နောင်ဟာ မြို့ထဲက လက်ဖက်ရည်ဆိုင်သေးသေးလေးထဲမှာ ခဏဝင်ထိုင်ပြီး၊ ဆိုင်ရှင်အဘိုးအိုဆီကနေ သတင်းအချက်အလက်တွေကို "
    "မသိမသာ စုံစမ်းကြည့်ခဲ့ပါတယ်။ အဘိုးအိုက ရေနွေးကြမ်းခွက်ကို ချပေးရင်း၊ လွန်ခဲ့တဲ့ 3 ရက်လောက်ကလည်း အနက်ရောင်ကုတ်အင်္ကျီနဲ့ "
    "လူစိမ်း 4 ယောက် အဲဒီနာရီစင်အကြောင်း လာမေးသွားခဲ့တယ်လို့ တိုးတိုးလေး ပြောပြလိုက်ပါတယ်။ အဲဒီစကားကို ကြားလိုက်ရချိန်မှာတော့ "
    "အချိန်ဆွဲနေလို့ လုံးဝမဖြစ်တော့ဘူးဆိုတာ သူ ချက်ချင်း သဘောပေါက်သွားခဲ့ပါတယ်။\n\n"
    "မိုးစက်လေးတွေ တဖွဲဖွဲ ကျလာတဲ့ ည 8 နာရီအချိန်မှာ သူဟာ လက်နှိပ်ဓာတ်မီးတစ်လက်နဲ့အတူ တောင်တက်လမ်းအတိုင်း တစ်ယောက်တည်း "
    "စတင်ထွက်ခွာလာခဲ့ပါတယ်။ လေတိုးသံတွေကြားထဲမှာ ခြေသံလိုလို အသံတွေကို မကြာခဏ ကြားနေရပေမယ့်၊ နောက်ပြန်လှည့်ဖို့ စိတ်ကူးလုံးဝမရှိဘဲ "
    "ရှေ့ကိုသာ ဆက်လျှောက်ခဲ့ပါတယ်။ နာရီစင်ကြီးရဲ့ သံတံခါးဝရှေ့ကို ရောက်သွားတဲ့အခါမှာတော့ သော့ခလောက်ဟာ အရင်ကတည်းက "
    "ရိုက်ချိုးခံထားရတာကို အံ့အားသင့်ဖွယ် တွေ့လိုက်ရပါတော့တယ်။\n\n"
    "အထဲကို ဓာတ်မီးနဲ့ ထိုးကြည့်လိုက်တဲ့အခါ ကြမ်းပြင်ပေါ်မှာ ကျနေတဲ့ ပန်းချီဆရာကြီးရဲ့ မျက်မှန်တစ်လက်နဲ့အတူ၊ နံရံပေါ်မှာ မြေဖြူနဲ့ "
    "အလျင်အမြန် ရေးခြစ်ထားတဲ့ ဂဏန်း 4 လုံး 2049 ကို တွေ့မြင်လိုက်ရပါတယ်။ အဲဒီဂဏန်းဟာ ပျောက်ဆုံးနေတဲ့ အမှုကြီးတစ်ခုလုံးရဲ့ "
    "အဓိက သော့ချက်ဖြစ်နေမယ်ဆိုတာကိုတော့ အဲဒီအချိန်မှာ သူ တပ်အပ်သိလိုက်ပါတော့တယ်။"
)


def _estimate_window_syllable_peaks(rms_slice: np.ndarray, hop_sec: float = cp.FRAME_STEP_SEC) -> float | None:
    """Estimate acoustic syllable-nucleus rate (peaks/active_sec) when chunk texts are absent."""
    if rms_slice.size < 20:
        return None
    p95 = float(np.percentile(rms_slice, 95))
    if p95 < 0.004:
        return None
    gate = max(0.005, 0.10 * p95)
    active_frames = int(np.sum(rms_slice >= gate))
    active_sec = active_frames * hop_sec
    if active_sec < 0.5:
        return None

    # Smooth envelope with 50 ms moving average to find syllable nuclei (~120 ms min spacing)
    kernel = np.ones(5, dtype=np.float32) / 5.0
    smooth = np.convolve(rms_slice, kernel, mode="same")
    min_dist = max(1, int(round(0.11 / hop_sec)))
    peaks = 0
    last_idx = -min_dist
    thresh = max(gate * 1.15, float(np.percentile(smooth, 45)))
    for i in range(1, smooth.size - 1):
        if i - last_idx < min_dist:
            continue
        if smooth[i] >= thresh and smooth[i] >= smooth[i - 1] and smooth[i] > smooth[i + 1]:
            peaks += 1
            last_idx = i
    if peaks < 2:
        return None
    return peaks / active_sec


def compute_sub_1p2s_tonal_iqr_st(f0_arr: np.ndarray, *, window_sec: float = 1.20) -> float:
    """Compute IQR (semitones) of the < 1.2 s high-pass voiced F0 residual (Metric M5)."""
    if f0_arr.size == 0:
        return 0.0
    cleaned = cp.repair_subharmonic_f0_contour(f0_arr)
    valid = ~np.isnan(cleaned) & (cleaned > 0.0)
    v_idx = np.where(valid)[0]
    if v_idx.size < 12:
        return 0.0
    med = float(np.median(cleaned[v_idx]))
    if med <= 0.0:
        return 0.0
    st_arr = np.full(cleaned.size, np.nan, dtype=np.float64)
    st_arr[v_idx] = 12.0 * np.log2(cleaned[v_idx].astype(np.float64) / med)

    half_w = max(5, int(round((0.5 * float(window_sec)) / cp.FRAME_STEP_SEC)))
    hp_vals: list[float] = []
    n = st_arr.size
    for idx in v_idx:
        i = int(idx)
        sub = st_arr[max(0, i - half_w) : min(n, i + half_w + 1)]
        sv = sub[~np.isnan(sub)]
        if sv.size >= 5:
            hp_vals.append(float(st_arr[i] - np.median(sv)))
    if len(hp_vals) < 8:
        return 0.0
    q25, q75 = np.percentile(np.asarray(hp_vals, dtype=np.float64), [25, 75])
    return float(q75 - q25)


def build_exact_chunks_metadata(
    part_wavs: list[Path],
    chunk_texts: list[str],
    *,
    full_text: str = "",
    pauses_sec: list[float] | None = None,
    raw_part_wavs: list[Path] | None = None,
    post_stitch_tempo: float = 1.0,
) -> list[dict[str, Any]]:
    """Build exact post-stitch (and post-mastering ``atempo``) boundary metadata for ``--chunks-json``."""
    m = len(part_wavs)
    tempo = max(0.5, min(2.0, float(post_stitch_tempo or 1.0)))
    b_specs = (
        cp.classify_chunk_boundaries(full_text or " ".join(chunk_texts), chunk_texts, speed_tempo=tempo)
        if m >= 2
        else []
    )
    eff_pauses = list(pauses_sec) if pauses_sec is not None else [b.pause_sec for b in b_specs]

    meta: list[dict[str, Any]] = []
    cursor_s = 0.0
    for idx in range(m):
        p = Path(part_wavs[idx])
        samples, sr = cp.read_wav_mono_float32(p)
        raw_dur = (samples.size / float(sr)) if (samples.size > 0 and sr > 0) else 0.0
        deliv_dur = raw_dur / tempo
        start_s = cursor_s
        end_s = start_s + deliv_dur
        txt = str(chunk_texts[idx]) if idx < len(chunk_texts) else ""
        pause_after_raw = float(eff_pauses[idx]) if idx < len(eff_pauses) else 0.0
        pause_deliv = (pause_after_raw / tempo) if idx < m - 1 else 0.0
        b_kind = b_specs[idx].kind if idx < len(b_specs) else "SENTENCE"
        q_tail = b_specs[idx].question_tail if idx < len(b_specs) else cp.is_question_ending(txt)

        row: dict[str, Any] = {
            "index": idx + 1,
            "start_sec": round(start_s, 4),
            "end_sec": round(end_s, 4),
            "pause_after_sec": round(pause_deliv, 4),
            "boundary_kind": b_kind,
            "question_tail": bool(q_tail),
            "text": txt,
            "syllables": cp.count_burmese_syllables(txt),
            "wav_path": str(p),
        }
        if raw_part_wavs is not None and idx < len(raw_part_wavs):
            row["raw_wav_path"] = str(raw_part_wavs[idx])
        meta.append(row)
        cursor_s = end_s + pause_deliv
    return meta


def _resolve_exact_chunk_spans(
    chunks_meta: list[dict[str, Any]] | None,
    total_sec: float,
) -> list[dict[str, Any]]:
    """Resolve exact ``(start_sec, end_sec)`` spans from ``chunks_meta`` (never syllable-proportional)."""
    if not chunks_meta:
        return []
    if all(("start_sec" in item and "end_sec" in item) for item in chunks_meta):
        out = []
        for idx, item in enumerate(chunks_meta):
            row = dict(item)
            row["start_sec"] = max(0.0, float(item["start_sec"]))
            row["end_sec"] = min(total_sec, float(item["end_sec"]))
            if "boundary_kind" not in row:
                txt = str(row.get("text") or "").rstrip()
                if txt.endswith(("၊", ",", ";", ":")):
                    row["boundary_kind"] = "CLAUSE"
                elif txt.endswith(("။", "?", "!")):
                    row["boundary_kind"] = "SENTENCE"
                else:
                    row["boundary_kind"] = "SENTENCE"
            if "question_tail" not in row:
                row["question_tail"] = cp.is_question_ending(str(row.get("text") or ""))
            if "syllables" not in row:
                row["syllables"] = cp.count_burmese_syllables(str(row.get("text") or ""))
            out.append(row)
        return out

    # Derive exact sequential spans if per-chunk wav_paths exist
    if all(("wav_path" in item and Path(item["wav_path"]).is_file()) for item in chunks_meta):
        durs: list[float] = []
        pauses: list[float] = []
        texts = [str(it.get("text") or "") for it in chunks_meta]
        b_specs = cp.classify_chunk_boundaries(" ".join(texts), texts) if len(texts) >= 2 else []
        for idx, it in enumerate(chunks_meta):
            s_p, sr_p = cp.read_wav_mono_float32(Path(it["wav_path"]))
            durs.append((s_p.size / float(sr_p)) if sr_p > 0 else 0.0)
            if idx < len(chunks_meta) - 1:
                p_s = float(it.get("pause_after_sec", b_specs[idx].base_pause_sec if idx < len(b_specs) else cp.CLONE_BOUNDARY_PAUSE_SEC))
                pauses.append(p_s)
        raw_sum = sum(durs) + sum(pauses)
        scale = (total_sec / raw_sum) if (raw_sum > 0.1 and abs(total_sec - raw_sum) / raw_sum < 0.30) else 1.0
        cursor = 0.0
        out = []
        for idx, it in enumerate(chunks_meta):
            row = dict(it)
            st_s = cursor
            en_s = min(total_sec, st_s + durs[idx] * scale)
            row["start_sec"] = round(st_s, 4)
            row["end_sec"] = round(en_s, 4)
            row.setdefault("boundary_kind", b_specs[idx].kind if idx < len(b_specs) else "SENTENCE")
            row.setdefault("question_tail", b_specs[idx].question_tail if idx < len(b_specs) else cp.is_question_ending(texts[idx]))
            row.setdefault("syllables", cp.count_burmese_syllables(texts[idx]))
            out.append(row)
            if idx < len(pauses):
                cursor = en_s + pauses[idx] * scale
        return out

    return []


def _estimate_boundary_500ms_f0_st(
    f0_slice: np.ndarray,
    *,
    anchor_hz: float,
    side: str,
) -> float | None:
    """Measure modal voiced F0 in the first/last 0.50 s of a chunk contour (subharmonic-cleaned) for M2."""
    if f0_slice.size == 0 or anchor_hz <= 0.0:
        return None
    cleaned = cp.repair_subharmonic_f0_contour(f0_slice)
    v_idx = np.where(~np.isnan(cleaned) & (cleaned > 0.0))[0]
    if v_idx.size < 6:
        return None
    chunk_med = float(np.median(cleaned[v_idx]))
    lo_hz = cp.EDGE_MODAL_FLOOR_RATIO * chunk_med
    hi_hz = 1.55 * chunk_med
    modal_idx = v_idx[(cleaned[v_idx] >= lo_hz) & (cleaned[v_idx] <= hi_hz)]
    if modal_idx.size < 5:
        return None

    win_frames = int(round(0.50 / cp.FRAME_STEP_SEC))  # 50 frames = 0.50 s
    if side == "head":
        guard = min(cp.EDGE_HEAD_GUARD_FRAMES, max(0, modal_idx.size - 5))
        trimmed = modal_idx[guard:]
        t0 = int(trimmed[0])
        sel = trimmed[trimmed <= t0 + win_frames]
        if sel.size < 25:
            sel = trimmed[: min(35, trimmed.size)]
    else:
        guard = min(cp.EDGE_TAIL_GUARD_FRAMES, max(0, modal_idx.size - 5))
        trimmed = modal_idx[: modal_idx.size - guard] if guard > 0 else modal_idx
        t1 = int(trimmed[-1])
        sel = trimmed[trimmed >= t1 - win_frames]
        if sel.size < 25:
            sel = trimmed[max(0, trimmed.size - 35) :]

    vals = cleaned[sel]
    est_hz = cp._trimmed_iqm(vals)
    return cp.semitone_diff(est_hz, anchor_hz) if est_hz > 0.0 else None


def evaluate_phase3_metrics(
    samples: np.ndarray,
    sr: int,
    f0_arr: np.ndarray,
    *,
    file_f0_med: float,
    chunks_meta: list[dict[str, Any]] | None = None,
    raw_baseline_wav: Path | None = None,
) -> dict[str, Any] | None:
    """Compute Phase 3 percept-correlated metrics M1-M5 at exact post-stitch chunk boundaries."""
    total_sec = samples.size / float(sr) if sr > 0 else 0.0
    spans = _resolve_exact_chunk_spans(chunks_meta, total_sec)
    if not spans:
        return None

    m = len(spans)
    sag_abs_list: list[float] = []
    per_chunk_sags: list[dict[str, Any]] = []
    tilts_db: list[float] = []
    tilt_weights: list[float] = []
    cents_hz: list[float] = []
    syls_list: list[int] = []
    chunk_f0_slices: list[np.ndarray] = []

    for idx, sp in enumerate(spans):
        t0 = float(sp["start_sec"])
        t1 = float(sp["end_sec"])
        dur_c = max(0.0, t1 - t0)
        f0_i0 = max(0, int(round(t0 / cp.FRAME_STEP_SEC)))
        f0_i1 = min(f0_arr.size, int(round(t1 / cp.FRAME_STEP_SEC)))
        sub_f0 = cp.repair_subharmonic_f0_contour(f0_arr[f0_i0:f0_i1])
        chunk_f0_slices.append(sub_f0)

        s_i0 = max(0, int(round(t0 * sr)))
        s_i1 = min(samples.size, int(round(t1 * sr)))
        sub_samp = samples[s_i0:s_i1]

        tilt_hi, _tilt_pr, v_cent = cp.measure_voiced_spectral_stats(sub_samp, sr, sub_f0)
        v_sec = float(np.sum(~np.isnan(sub_f0) & (sub_f0 > 0.0))) * cp.FRAME_STEP_SEC
        if v_sec >= 0.10 and v_cent > 0.0:
            tilts_db.append(tilt_hi)
            tilt_weights.append(v_sec)
            cents_hz.append(v_cent)

        sag_st, slope_st_s = cp.measure_chunk_sag_and_slope(sub_f0, anchor_hz=file_f0_med if file_f0_med > 0 else None)
        if dur_c >= 10.0:
            sag_abs_list.append(abs(sag_st))
        per_chunk_sags.append(
            {
                "chunk": idx + 1,
                "duration_sec": round(dur_c, 2),
                "sag_st": round(sag_st, 3),
                "slope_st_per_s": round(slope_st_s, 4),
                "voiced_tilt_db": round(tilt_hi, 3),
                "voiced_centroid_hz": round(v_cent, 1),
                "syllables": int(sp.get("syllables") or 0),
            }
        )
        syls_list.append(int(sp.get("syllables") or 0))

    # M1: Per-chunk |sag_st| on chunks >= 10 s (Gate: max <= 0.70 st, median <= 0.40 st)
    m1_max = float(max(sag_abs_list)) if sag_abs_list else 0.0
    m1_med = float(np.median(sag_abs_list)) if sag_abs_list else 0.0
    m1_pass = (m1_max <= 0.70) and (m1_med <= 0.40)

    # M2: Exact-boundary continuation step (SENTENCE [-0.40, +0.85], PARAGRAPH [-0.50, +1.20], CLAUSE |step| <= 0.55)
    seam_rows: list[dict[str, Any]] = []
    m2_all_pass = True
    for k in range(m - 1):
        tail_st = _estimate_boundary_500ms_f0_st(chunk_f0_slices[k], anchor_hz=file_f0_med, side="tail")
        head_st = _estimate_boundary_500ms_f0_st(chunk_f0_slices[k + 1], anchor_hz=file_f0_med, side="head")
        b_kind = str(spans[k].get("boundary_kind") or "SENTENCE").upper()
        q_tail = bool(spans[k].get("question_tail", False))
        if tail_st is None or head_st is None:
            step_st = 0.0
            s_ok = True
        else:
            step_st = head_st - tail_st
            if q_tail:
                s_ok = (-2.20 <= step_st <= 0.85)
            elif b_kind == "PARAGRAPH":
                s_ok = (-0.50 <= step_st <= 1.20)
            elif b_kind == "CLAUSE":
                s_ok = abs(step_st) <= 0.55
            elif b_kind == "MID_PHRASE":
                s_ok = abs(step_st) <= 0.45
            else:
                s_ok = (-0.40 <= step_st <= 0.85)
        if not s_ok:
            m2_all_pass = False
        seam_rows.append(
            {
                "seam": f"{k + 1}->{k + 2}",
                "boundary_sec": round(float(spans[k]["end_sec"]), 3),
                "kind": b_kind,
                "question_tail": q_tail,
                "step_st": round(step_st, 3),
                "pass": s_ok,
            }
        )

    # M3: Voiced LTAS tilt range (every chunk within ±0.90 dB of job median; range <= 1.60 dB)
    if tilts_db:
        job_tilt_med = cp._weighted_median_signed(tilts_db, tilt_weights) or float(np.median(tilts_db))
        m3_max_dev = float(max(abs(t - job_tilt_med) for t in tilts_db))
        m3_range = float(max(tilts_db) - min(tilts_db))
    else:
        job_tilt_med = 0.0
        m3_max_dev = 0.0
        m3_range = 0.0
    m3_pass = (m3_max_dev <= 0.90) and (m3_range <= 1.60)

    # M4: Adjacent voiced centroid step <= 8% & zero chunks < 65 syllables in jobs >= 140 syllables
    if len(cents_hz) >= 2:
        c_med = max(1.0, float(np.median(cents_hz)))
        c_steps = [abs(cents_hz[i + 1] - cents_hz[i]) / c_med for i in range(len(cents_hz) - 1)]
        m4_max_step = float(max(c_steps))
    else:
        m4_max_step = 0.0
    tot_syls = sum(syls_list)
    runt_chunks = sum(1 for s in syls_list if 0 < s < vc.MIN_TAIL_SYLLABLES) if tot_syls >= 140 else 0
    m4_pass = (m4_max_step <= 0.08) and (runt_chunks == 0)

    # M5: < 1.2 s high-pass voiced F0 modulation retention >= 95% (post-pass vs raw)
    has_corr_and_raw = all(
        ("wav_path" in sp and Path(sp["wav_path"]).is_file() and "raw_wav_path" in sp and Path(sp["raw_wav_path"]).is_file())
        for sp in spans
    )
    if has_corr_and_raw:
        post_iqrs: list[float] = []
        raw_iqrs: list[float] = []
        weights_v: list[float] = []
        for sp in spans:
            cs, csr = cp.read_wav_mono_float32(Path(sp["wav_path"]))
            cs16 = cp.resample_float32(cs, csr, cp.ANALYSIS_SAMPLE_RATE)
            _, cf0 = cp.extract_frame_contours(cs16, cp.ANALYSIS_SAMPLE_RATE)

            rs, rsr = cp.read_wav_mono_float32(Path(sp["raw_wav_path"]))
            rs16 = cp.resample_float32(rs, rsr, cp.ANALYSIS_SAMPLE_RATE)
            _, rf0 = cp.extract_frame_contours(rs16, cp.ANALYSIS_SAMPLE_RATE)

            c_dur = (cs.size / float(csr)) if csr > 0 else 1.0
            r_dur = (rs.size / float(rsr)) if rsr > 0 else 1.0
            dur_ratio = max(0.75, min(1.35, c_dur / max(0.1, r_dur)))
            ri = compute_sub_1p2s_tonal_iqr_st(rf0, window_sec=1.20)
            ci = compute_sub_1p2s_tonal_iqr_st(cf0, window_sec=1.20 * dur_ratio)
            v_n = float(np.sum(~np.isnan(rf0) & (rf0 > 0.0)))
            if ri > 1e-4 and v_n >= 12:
                raw_iqrs.append(ri)
                post_iqrs.append(ci)
                weights_v.append(v_n)
        if raw_iqrs:
            w_arr = np.asarray(weights_v, dtype=np.float64)
            w_arr /= np.sum(w_arr)
            raw_hp_iqr = float(np.sum(np.asarray(raw_iqrs) * w_arr))
            post_hp_iqr = float(np.sum(np.asarray(post_iqrs) * w_arr))
        else:
            post_hp_iqr = 0.0
            raw_hp_iqr = None
    else:
        post_hp_iqr = compute_sub_1p2s_tonal_iqr_st(f0_arr)
        raw_hp_iqr = None
        if raw_baseline_wav is not None and Path(raw_baseline_wav).is_file():
            rs, rsr = cp.read_wav_mono_float32(Path(raw_baseline_wav))
            rs16 = cp.resample_float32(rs, rsr, cp.ANALYSIS_SAMPLE_RATE)
            _, rf0 = cp.extract_frame_contours(rs16, cp.ANALYSIS_SAMPLE_RATE)
            raw_hp_iqr = compute_sub_1p2s_tonal_iqr_st(rf0)
        elif all(("raw_wav_path" in sp and Path(sp["raw_wav_path"]).is_file()) for sp in spans):
            raw_f0s_only: list[np.ndarray] = []
            raw_dur_sum = 0.0
            for sp in spans:
                rs, rsr = cp.read_wav_mono_float32(Path(sp["raw_wav_path"]))
                raw_dur_sum += (rs.size / float(rsr)) if rsr > 0 else 0.0
                rs16 = cp.resample_float32(rs, rsr, cp.ANALYSIS_SAMPLE_RATE)
                _, rf0 = cp.extract_frame_contours(rs16, cp.ANALYSIS_SAMPLE_RATE)
                raw_f0s_only.append(rf0)
            if raw_f0s_only:
                raw_hp_iqr = compute_sub_1p2s_tonal_iqr_st(np.concatenate(raw_f0s_only))
                deliv_speech_dur = sum(max(0.0, float(sp["end_sec"]) - float(sp["start_sec"])) for sp in spans)
                if raw_dur_sum > 1.0 and deliv_speech_dur > 1.0:
                    eff_tempo = raw_dur_sum / deliv_speech_dur
                    post_hp_iqr = compute_sub_1p2s_tonal_iqr_st(f0_arr, window_sec=1.20 / max(0.7, min(1.5, eff_tempo)))

    if raw_hp_iqr is not None and raw_hp_iqr > 1e-4:
        m5_retention = post_hp_iqr / raw_hp_iqr
    else:
        m5_retention = 1.0
    m5_pass = m5_retention >= 0.95

    return {
        "M1_max_abs_sag_st": round(m1_max, 3),
        "M1_median_abs_sag_st": round(m1_med, 3),
        "M1_pass": m1_pass,
        "M2_seams": seam_rows,
        "M2_max_abs_step_st": round(max((abs(r["step_st"]) for r in seam_rows), default=0.0), 3),
        "M2_pass": m2_all_pass,
        "M3_job_tilt_median_db": round(job_tilt_med, 3),
        "M3_max_abs_dev_db": round(m3_max_dev, 3),
        "M3_range_db": round(m3_range, 3),
        "M3_pass": m3_pass,
        "M4_max_centroid_step_pct": round(m4_max_step * 100.0, 2),
        "M4_runt_chunks_lt_65_syl": int(runt_chunks),
        "M4_min_chunk_syllables": int(min(syls_list)) if syls_list else 0,
        "M4_pass": m4_pass,
        "M5_post_hp_iqr_st": round(post_hp_iqr, 4),
        "M5_raw_hp_iqr_st": round(raw_hp_iqr, 4) if raw_hp_iqr is not None else None,
        "M5_tonal_retention_pct": round(m5_retention * 100.0, 2),
        "M5_pass": m5_pass,
        "all_phase3_gates_pass": bool(m1_pass and m2_all_pass and m3_pass and m4_pass and m5_pass),
        "per_chunk": per_chunk_sags,
    }


def evaluate_wav_prosody(
    wav_path: Path,
    *,
    chunks_meta: list[dict[str, Any]] | None = None,
    raw_baseline_wav: Path | None = None,
    window_sec: float = 5.0,
) -> dict[str, Any]:
    """Compute 5s-window F0 metrics, Pace CV/max_ratio, and composite PDS for a WAV file."""
    samples, sr = cp.read_wav_mono_float32(Path(wav_path))
    if samples.size == 0 or sr <= 0:
        raise RuntimeError(f"Unreadable or empty WAV file: {wav_path}")

    total_sec = samples.size / float(sr)
    s16 = cp.resample_float32(samples, sr, cp.ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = cp.extract_frame_contours(s16, cp.ANALYSIS_SAMPLE_RATE)

    voiced_all = f0_arr[~np.isnan(f0_arr)]
    file_f0_med = float(np.median(voiced_all)) if voiced_all.size >= 3 else 0.0

    win_frames = max(10, int(round(window_sec / cp.FRAME_STEP_SEC)))
    windows: list[dict[str, Any]] = []
    win_f0_st: list[float] = []
    acoustic_rates: list[float] = []

    n_wins = max(1, int(math.ceil(rms_arr.size / float(win_frames))))
    for w_idx in range(n_wins):
        st_f = w_idx * win_frames
        en_f = min(rms_arr.size, st_f + win_frames)
        if en_f - st_f < max(5, win_frames // 4):
            continue
        sub_f0 = f0_arr[st_f:en_f]
        sub_rms = rms_arr[st_f:en_f]
        v = sub_f0[~np.isnan(sub_f0)]
        t_start = round(st_f * cp.FRAME_STEP_SEC, 2)
        t_end = round(min(total_sec, en_f * cp.FRAME_STEP_SEC), 2)
        if v.size >= 5 and file_f0_med > 0:
            w_med = float(np.median(v))
            dev_st = cp.semitone_diff(w_med, file_f0_med)
            win_f0_st.append(dev_st)
        else:
            w_med = None
            dev_st = None

        ac_rate = _estimate_window_syllable_peaks(sub_rms)
        if ac_rate is not None:
            acoustic_rates.append(ac_rate)

        windows.append(
            {
                "window": f"{t_start:.1f}-{t_end:.1f}s",
                "f0_hz": round(w_med, 2) if w_med is not None else None,
                "dev_st": round(dev_st, 3) if dev_st is not None else None,
                "acoustic_syl_rate": round(ac_rate, 3) if ac_rate is not None else None,
            }
        )

    if len(win_f0_st) >= 2:
        q25, q75 = np.percentile(win_f0_st, [25, 75])
        f0_win_iqr_st = float(q75 - q25)
        jumps = [abs(win_f0_st[i + 1] - win_f0_st[i]) for i in range(len(win_f0_st) - 1)]
        f0_win_max_jump_st = float(max(jumps))
        outliers = sum(1 for st in win_f0_st if abs(st) > cp.OUTLIER_MAX_F0_ST)
        f0_outlier_ratio = outliers / float(len(win_f0_st))
    else:
        f0_win_iqr_st = 0.0
        f0_win_max_jump_st = 0.0
        f0_outlier_ratio = 0.0

    # Pace metrics: prefer explicit chunk WAV/span measurements when supplied, else window acoustic rates
    pace_rates: list[float] = []
    pace_source = "window_acoustic_nuclei"
    if chunks_meta:
        pace_source = "chunk_syllable_rates"
        for item in chunks_meta:
            if "wav_path" in item and Path(item["wav_path"]).is_file():
                st = cp.measure_prosody(Path(item["wav_path"]), text=str(item.get("text") or ""))
                if st.syllable_rate is not None:
                    pace_rates.append(st.syllable_rate)
            elif "start_sec" in item and "end_sec" in item and item.get("text"):
                s_idx = max(0, int(round(float(item["start_sec"]) * sr)))
                e_idx = min(samples.size, int(round(float(item["end_sec"]) * sr)))
                seg_dur = max(0.1, (e_idx - s_idx) / float(sr))
                syls = cp.count_burmese_syllables(str(item["text"]))
                if syls > 0:
                    pace_rates.append(syls / seg_dur)
    if len(pace_rates) < 2:
        pace_rates = acoustic_rates
        pace_source = "window_acoustic_nuclei"

    if len(pace_rates) >= 2:
        mean_r = float(np.mean(pace_rates))
        std_r = float(np.std(pace_rates))
        pace_cv = (std_r / mean_r) if mean_r > 1e-6 else 0.0
        min_r = max(1e-6, float(min(pace_rates)))
        pace_max_ratio = float(max(pace_rates)) / min_r
    else:
        pace_cv = 0.0
        pace_max_ratio = 1.0

    pds = 0.40 * f0_win_iqr_st + 0.30 * f0_win_max_jump_st + 0.30 * (20.0 * pace_cv)
    phase3_metrics = evaluate_phase3_metrics(
        samples,
        sr,
        f0_arr,
        file_f0_med=file_f0_med,
        chunks_meta=chunks_meta,
        raw_baseline_wav=raw_baseline_wav,
    )

    return {
        "wav": str(wav_path),
        "duration_sec": round(total_sec, 2),
        "file_f0_median_hz": round(file_f0_med, 2),
        "F0_win_IQR_st": round(f0_win_iqr_st, 3),
        "F0_win_max_jump_st": round(f0_win_max_jump_st, 3),
        "F0_outlier_ratio": round(f0_outlier_ratio, 4),
        "Pace_CV": round(pace_cv, 4),
        "Pace_max_ratio": round(pace_max_ratio, 3),
        "pace_source": pace_source,
        "PDS": round(pds, 3),
        "phase3": phase3_metrics,
        "targets_pass": {
            "iqr_le_1.5": f0_win_iqr_st <= 1.5,
            "iqr_le_1.35": f0_win_iqr_st <= 1.35,
            "iqr_ge_0.45_expressive_floor": f0_win_iqr_st >= 0.45,
            "max_jump_le_2.0": f0_win_max_jump_st <= 2.0,
            "max_jump_le_1.75": f0_win_max_jump_st <= 1.75,
            "outlier_ratio_0": f0_outlier_ratio == 0.0,
            "pace_cv_le_0.10": pace_cv <= 0.10,
            "pace_cv_le_0.085": pace_cv <= 0.085,
            "pace_max_ratio_le_1.25": pace_max_ratio <= 1.25,
            "pace_max_ratio_le_1.22": pace_max_ratio <= 1.22,
            "pds_le_1.20": pds <= 1.20,
        },
        "windows": windows,
    }


def _print_measure_report(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> None:
    print(f"=== Clone Prosody Report: {report['wav']} ===")
    print(f"Duration          : {report['duration_sec']:.2f} s | File Median F0: {report['file_f0_median_hz']:.2f} Hz")
    print(f"F0_win_IQR_st     : {report['F0_win_IQR_st']:.3f} st   (target <= 1.500)")
    print(f"F0_win_max_jump_st: {report['F0_win_max_jump_st']:.3f} st   (target <= 2.000)")
    print(f"F0_outlier_ratio  : {report['F0_outlier_ratio'] * 100:.1f}%     (target == 0.0%)")
    print(f"Pace_CV           : {report['Pace_CV']:.4f}    (target <= 0.1000, source={report['pace_source']})")
    print(f"Pace_max_ratio    : {report['Pace_max_ratio']:.3f}     (target <= 1.250)")
    print(f"Composite PDS     : {report['PDS']:.3f}")
    if baseline is not None and baseline["PDS"] > 0:
        reduction = (1.0 - report["PDS"] / baseline["PDS"]) * 100.0
        print(f"vs Baseline PDS   : {baseline['PDS']:.3f} -> {reduction:+.1f}% reduction (target >= +60.0%)")

    print("\nWindow Breakdown (5s):")
    print(f"  {'Window':14s} | {'F0 (Hz)':>9s} | {'Dev (st)':>9s} | {'Ac.Rate':>8s}")
    print("  " + "-" * 48)
    for w in report["windows"]:
        f0_s = f"{w['f0_hz']:.1f}" if w["f0_hz"] is not None else "unvoiced"
        st_s = f"{w['dev_st']:+.2f}" if w["dev_st"] is not None else "-"
        rt_s = f"{w['acoustic_syl_rate']:.2f}" if w["acoustic_syl_rate"] is not None else "-"
        print(f"  {w['window']:14s} | {f0_s:>9s} | {st_s:>9s} | {rt_s:>8s}")

    p3 = report.get("phase3")
    if isinstance(p3, dict):
        print("\nPhase 3 Percept-Correlated Gates (exact boundaries):")
        print(f"  M1 Sag (>=10s)     : max={p3['M1_max_abs_sag_st']:.3f} st (<=0.70), med={p3['M1_median_abs_sag_st']:.3f} st (<=0.40) -> {'PASS' if p3['M1_pass'] else 'FAIL'}")
        print(f"  M2 Seam Steps      : max|step|={p3['M2_max_abs_step_st']:.3f} st -> {'PASS' if p3['M2_pass'] else 'FAIL'} | {p3['M2_seams']}")
        print(f"  M3 Voiced Tilt     : max_dev=±{p3['M3_max_abs_dev_db']:.3f} dB (<=0.90), range={p3['M3_range_db']:.3f} dB (<=1.60) -> {'PASS' if p3['M3_pass'] else 'FAIL'}")
        print(f"  M4 Centroid/Runts  : max_step={p3['M4_max_centroid_step_pct']:.2f}% (<=8.0%), runts<65syl={p3['M4_runt_chunks_lt_65_syl']} -> {'PASS' if p3['M4_pass'] else 'FAIL'}")
        print(f"  M5 Tonal Retention : {p3['M5_tonal_retention_pct']:.2f}% (>=95.0%) -> {'PASS' if p3['M5_pass'] else 'FAIL'}")
        print(f"  OVERALL PHASE 3    : {'ALL GATES PASS' if p3['all_phase3_gates_pass'] else 'FAILED'}")


def _run_chunk_sweep(caps_csv: str) -> int:
    norm_text = normalize_numbers_for_speech(BENCHMARK_BURMESE_NARRATION)
    control = "Match the reference speaker's exact vocal timbre, pitch register, and breathing cadence."
    print(f"=== Chunk-Char Calibration Sweep (fixture raw={len(BENCHMARK_BURMESE_NARRATION)}c, spoken={len(norm_text)}c) ===")
    print(f"Speeds configured : {', '.join(BENCH_SPEEDS)}")
    print(f"  {'MaxCap':>6s} | {'Chunks':>6s} | {'MinLen':>6s} | {'MedLen':>6s} | {'MaxLen':>6s} | {'MaxEstTok':>9s}")
    print("  " + "-" * 54)
    for item in caps_csv.split(","):
        cap = int(item.strip())
        chunks = vc.plan_text_chunks(
            norm_text,
            control_instruction=control,
            reference_seconds=cp.REFERENCE_SLICE_SECONDS,
            max_chunk_chars=cap,
        )
        lengths = [len(c) for c in chunks]
        max_tok = max(
            vc.estimated_input_tokens(
                c,
                control_instruction=control,
                reference_seconds=cp.REFERENCE_SLICE_SECONDS,
            )
            for c in chunks
        )
        med = int(round(float(np.median(lengths))))
        print(f"  {cap:6d} | {len(chunks):6d} | {min(lengths):6d} | {med:6d} | {max(lengths):6d} | {max_tok:9d}")
    return 0


def _run_slice_sweep(secs_csv: str, ref_wav: Path) -> int:
    if not ref_wav.is_file():
        print(f"ERROR: --ref-wav not found: {ref_wav}", file=sys.stderr)
        return 1
    print(f"=== Reference Slice Calibration Sweep: {ref_wav} ===")
    print(f"  {'Target(s)':>9s} | {'Actual(s)':>9s} | {'Voiced(s)':>9s} | {'MedF0(Hz)':>9s} | {'F0_IQR(st)':>10s}")
    print("  " + "-" * 58)
    with tempfile.TemporaryDirectory() as tmp:
        for item in secs_csv.split(","):
            t_sec = float(item.strip())
            _, act_sec, st = cp.select_steady_reference_slice(ref_wav, Path(tmp), target_sec=t_sec)
            f0_s = f"{st.f0_median_hz:.2f}" if st.f0_median_hz is not None else "unvoiced"
            print(f"  {t_sec:9.1f} | {act_sec:9.2f} | {st.voiced_sec:9.2f} | {f0_s:>9s} | {st.f0_iqr_st:10.3f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Long-form Voice Clone Prosody Benchmark & Sweep Tool")
    parser.add_argument("--wav", type=Path, help="Arbitrary WAV path to measure (no job context needed)")
    parser.add_argument("--chunks-json", type=Path, help="Optional JSON list of chunk texts/spans/wav_paths")
    parser.add_argument("--baseline-wav", type=Path, help="Optional baseline WAV (e.g. d1bf3376) for PDS delta")
    parser.add_argument("--json-out", type=Path, help="Optional path to write JSON metrics")
    parser.add_argument("--sweep-chunk-chars", type=str, help="Comma-separated chunk caps, e.g. 300,425,600")
    parser.add_argument("--sweep-slice-secs", type=str, help="Comma-separated slice durations, e.g. 8,10,15")
    parser.add_argument("--ref-wav", type=Path, help="Reference WAV for --sweep-slice-secs")
    parser.add_argument("--print-fixture", action="store_true", help="Print embedded ~2,000-char Burmese fixture info")
    args = parser.parse_args(argv)

    ran_any = False
    if args.print_fixture:
        ran_any = True
        norm = normalize_numbers_for_speech(BENCHMARK_BURMESE_NARRATION)
        print(f"Fixture raw chars : {len(BENCHMARK_BURMESE_NARRATION)} | normalized chars: {len(norm)}")
        print(f"Speeds            : {', '.join(BENCH_SPEEDS)}")

    if args.sweep_chunk_chars:
        ran_any = True
        rc = _run_chunk_sweep(args.sweep_chunk_chars)
        if rc != 0:
            return rc

    if args.sweep_slice_secs:
        ran_any = True
        if not args.ref_wav:
            print("ERROR: --sweep-slice-secs requires --ref-wav <path>", file=sys.stderr)
            return 2
        rc = _run_slice_sweep(args.sweep_slice_secs, args.ref_wav)
        if rc != 0:
            return rc

    if args.wav:
        ran_any = True
        chunks_meta = None
        if args.chunks_json and args.chunks_json.is_file():
            chunks_meta = json.loads(args.chunks_json.read_text(encoding="utf-8"))
        report = evaluate_wav_prosody(args.wav, chunks_meta=chunks_meta, raw_baseline_wav=args.baseline_wav)
        base_rep = evaluate_wav_prosody(args.baseline_wav) if args.baseline_wav else None
        _print_measure_report(report, baseline=base_rep)
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not ran_any:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
