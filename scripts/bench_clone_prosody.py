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


def evaluate_wav_prosody(
    wav_path: Path,
    *,
    chunks_meta: list[dict[str, Any]] | None = None,
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
        "targets_pass": {
            "iqr_le_1.5": f0_win_iqr_st <= 1.5,
            "max_jump_le_2.0": f0_win_max_jump_st <= 2.0,
            "outlier_ratio_0": f0_outlier_ratio == 0.0,
            "pace_cv_le_0.10": pace_cv <= 0.10,
            "pace_max_ratio_le_1.25": pace_max_ratio <= 1.25,
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
        report = evaluate_wav_prosody(args.wav, chunks_meta=chunks_meta)
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
