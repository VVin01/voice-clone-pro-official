"""Clone Prosody Measurement, Reference Conditioning Slice & Per-Chunk Normalization.

Implements Phase 1A + Phase 1B of the Long-Form Voice Clone Prosody architecture:
  - Pure-numpy octave-guarded YIN median F0 + Burmese syllable rate measurement
  - Steadiest ~10 s voiced reference slice selection (ranked by lowest F0 IQR)
  - One-time Gemini ASR reference transcript with speaking-rate plausibility gate (8-22 chars/s)
  - Per-chunk outlier distance scoring + bounded FFmpeg rubberband micro-correction
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import logging
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable
import wave

import numpy as np

from app.services.voice_chunking import BURMESE_SYLLABLE_BREAK

logger = logging.getLogger(__name__)

ANALYSIS_SAMPLE_RATE: int = 16000
FRAME_STEP_SEC: float = 0.010  # 10 ms
FRAME_WIN_SEC: float = 0.040   # 40 ms
F0_MIN_HZ: float = 65.0
F0_MAX_HZ: float = 420.0
YIN_THRESHOLD: float = 0.15
YIN_FALLBACK_MAX: float = 0.28

REFERENCE_SLICE_SECONDS: float = 10.0
ASR_MIN_CHARS_PER_SEC: float = 8.0
ASR_MAX_CHARS_PER_SEC: float = 22.0

OUTLIER_MAX_F0_ST: float = 2.5
OUTLIER_RATE_MIN_RATIO: float = 0.78
OUTLIER_RATE_MAX_RATIO: float = 1.28
MAX_PROSODY_RETRIES_PER_CHUNK: int = 1
MAX_PROSODY_RETRIES_PER_JOB: int = 2

RUBBERBAND_MAX_PITCH_ST: float = 2.0
RUBBERBAND_DEADBAND_PITCH_ST: float = 0.5
RUBBERBAND_TEMPO_MIN: float = 0.90
RUBBERBAND_TEMPO_MAX: float = 1.12
RUBBERBAND_DEADBAND_TEMPO: float = 0.03

CLONE_BOUNDARY_PAUSE_SEC: float = 0.135  # 135 ms sentence-boundary pause

_ASCII_SYLLABLE_FALLBACK_RE = re.compile(r"[A-Za-z0-9]+")
_MEDIA_WHITELIST = "file,pipe"


@dataclass(frozen=True)
class ProsodyStats:
    duration_sec: float
    active_speech_sec: float
    voiced_sec: float
    f0_median_hz: float | None
    f0_iqr_st: float
    syllable_count: int
    syllable_rate: float | None


def semitone_diff(f0_hz: float, anchor_hz: float) -> float:
    if f0_hz <= 0.0 or anchor_hz <= 0.0:
        return 0.0
    return 12.0 * math.log2(float(f0_hz) / float(anchor_hz))


def count_burmese_syllables(text: str) -> int:
    """Count Burmese syllables via BURMESE_SYLLABLE_BREAK plus Latin/digit token fallback."""
    s = str(text or "").strip()
    if not s:
        return 0
    mm_syls = len(BURMESE_SYLLABLE_BREAK.findall(s))
    ascii_words = len(_ASCII_SYLLABLE_FALLBACK_RE.findall(s))
    return mm_syls + ascii_words


def read_wav_mono_float32(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a WAV (or FFmpeg-decodable audio) into mono float32 [-1, 1] + sample_rate."""
    p = Path(wav_path)
    if not p.is_file() or p.stat().st_size == 0:
        return np.zeros(0, dtype=np.float32), ANALYSIS_SAMPLE_RATE

    try:
        with wave.open(str(p), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            n = wf.getnframes()
            raw = wf.readframes(n)
        if sr > 0 and ch > 0 and len(raw) >= ch * sw:
            if sw == 2:
                arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            elif sw == 1:
                arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            elif sw == 4:
                arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
            else:
                arr = None
            if arr is not None:
                if ch > 1:
                    arr = arr[: (arr.size // ch) * ch].reshape(-1, ch).mean(axis=1)
                return arr.astype(np.float32, copy=False), int(sr)
    except Exception:
        pass

    # Fallback: decode via ffmpeg pipe
    ff = shutil.which("ffmpeg")
    if not ff:
        return np.zeros(0, dtype=np.float32), ANALYSIS_SAMPLE_RATE
    try:
        proc = subprocess.run(
            [
                ff, "-protocol_whitelist", _MEDIA_WHITELIST,
                "-hide_banner", "-loglevel", "error",
                "-i", str(p), "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE),
                "-f", "s16le", "pipe:1",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            arr = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
            return arr, ANALYSIS_SAMPLE_RATE
    except Exception:
        pass
    return np.zeros(0, dtype=np.float32), ANALYSIS_SAMPLE_RATE


def resample_float32(samples: np.ndarray, src_sr: int, dst_sr: int = ANALYSIS_SAMPLE_RATE) -> np.ndarray:
    if samples.size == 0 or src_sr <= 0 or src_sr == dst_sr:
        return samples.astype(np.float32, copy=False)
    target_len = max(1, int(round(samples.size * (float(dst_sr) / float(src_sr)))))
    idx = np.linspace(0.0, float(samples.size - 1), num=target_len, dtype=np.float64)
    out = np.interp(idx, np.arange(samples.size, dtype=np.float64), samples)
    return out.astype(np.float32)


def _yin_frame_f0(frame: np.ndarray, sr: int, tau_min: int, tau_max: int) -> tuple[float | None, np.ndarray]:
    """Compute YIN F0 on one frame via FFT difference function + CMND."""
    w = frame.size // 2
    if w <= tau_max:
        return None, np.zeros(0, dtype=np.float64)

    x = frame.astype(np.float64)
    n_fft = 1 << (2 * w).bit_length()
    x0 = x[:w]
    fft_x0 = np.fft.rfft(x0, n=n_fft)
    fft_x = np.fft.rfft(x[: 2 * w], n=n_fft)
    # Cross-correlation sum_{j=0}^{w-1} x[j] * x[j + tau]
    corr = np.fft.irfft(np.conj(fft_x0) * fft_x, n=n_fft)[: tau_max + 1]

    sq = x[: 2 * w] * x[: 2 * w]
    csum = np.concatenate(([0.0], np.cumsum(sq)))
    energy_0 = csum[w]
    taus = np.arange(tau_max + 1)
    energy_tau = csum[taus + w] - csum[taus]

    diff = np.maximum(0.0, energy_0 + energy_tau - 2.0 * corr)
    cmnd = np.ones(tau_max + 1, dtype=np.float64)
    running = 0.0
    for tau in range(1, tau_max + 1):
        running += diff[tau]
        cmnd[tau] = (diff[tau] * tau / running) if running > 1e-12 else 1.0

    best_tau: int | None = None
    tau = tau_min
    while tau <= tau_max:
        if cmnd[tau] < YIN_THRESHOLD:
            while tau + 1 <= tau_max and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
            best_tau = tau
            break
        tau += 1

    if best_tau is None:
        sub = cmnd[tau_min : tau_max + 1]
        if sub.size == 0:
            return None, cmnd
        rel = int(np.argmin(sub))
        if float(sub[rel]) < YIN_FALLBACK_MAX:
            best_tau = tau_min + rel
        else:
            return None, cmnd

    # Parabolic interpolation around best_tau
    interp_tau = float(best_tau)
    if 1 <= best_tau < tau_max:
        s0, s1, s2 = cmnd[best_tau - 1], cmnd[best_tau], cmnd[best_tau + 1]
        denom = 2.0 * (2.0 * s1 - s2 - s0)
        if abs(denom) > 1e-12:
            interp_tau = best_tau + (s2 - s0) / denom
    if interp_tau <= 0.0:
        return None, cmnd
    return float(sr) / interp_tau, cmnd


def extract_frame_contours(
    samples_16k: np.ndarray,
    sr: int = ANALYSIS_SAMPLE_RATE,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(rms_per_frame, f0_hz_per_frame_with_nan_for_unvoiced)`` at 10 ms hop."""
    hop = max(1, int(round(sr * FRAME_STEP_SEC)))
    win = max(hop * 2, int(round(sr * FRAME_WIN_SEC)) * 2)  # 2*W needed by YIN
    tau_min = max(2, int(sr / F0_MAX_HZ))
    tau_max = max(tau_min + 2, int(sr / F0_MIN_HZ))
    if win <= 2 * tau_max + 2:
        win = 2 * tau_max + 4

    if samples_16k.size < win:
        if samples_16k.size == 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        padded = np.pad(samples_16k, (0, win - samples_16k.size))
        samples_16k = padded

    n_frames = 1 + (samples_16k.size - win) // hop
    rms_arr = np.zeros(n_frames, dtype=np.float32)
    f0_arr = np.full(n_frames, np.nan, dtype=np.float32)
    cmnd_cache: dict[int, np.ndarray] = {}

    for idx in range(n_frames):
        st = idx * hop
        frame = samples_16k[st : st + win]
        rms_arr[idx] = float(np.sqrt(np.mean(frame[: win // 2] ** 2)))

    if n_frames == 0:
        return rms_arr, f0_arr

    p90 = float(np.percentile(rms_arr, 90))
    if p90 < 0.003:
        # Essentially silent audio
        return rms_arr, f0_arr

    voice_gate = max(0.006, 0.12 * p90)
    for idx in range(n_frames):
        if rms_arr[idx] < voice_gate:
            continue
        st = idx * hop
        f0, cmnd = _yin_frame_f0(samples_16k[st : st + win], sr, tau_min, tau_max)
        if f0 is not None and F0_MIN_HZ <= f0 <= F0_MAX_HZ:
            f0_arr[idx] = f0
            cmnd_cache[idx] = cmnd

    # Intra-clip octave-glitch guard: only folds isolated within-clip harmonic/subharmonic
    # frame locks relative to the clip's own dominant median (never hides whole-chunk shifts).
    voiced_vals = f0_arr[~np.isnan(f0_arr)]
    if voiced_vals.size >= 5:
        med = float(np.median(voiced_vals))
        for idx in np.where(~np.isnan(f0_arr))[0]:
            fi = float(f0_arr[idx])
            cmnd = cmnd_cache.get(int(idx))
            if 1.85 * med <= fi <= 2.15 * med and cmnd is not None:
                sub_tau = int(round(sr / (fi * 0.5)))
                if sub_tau < cmnd.size and cmnd[sub_tau] < 0.26:
                    f0_arr[idx] = fi * 0.5
            elif 0.46 * med <= fi <= 0.54 * med and cmnd is not None:
                har_tau = int(round(sr / (fi * 2.0)))
                if tau_min <= har_tau < cmnd.size and cmnd[har_tau] < 0.20:
                    f0_arr[idx] = fi * 2.0

    return rms_arr, f0_arr


def measure_prosody(wav_path: Path, text: str = "") -> ProsodyStats:
    """Measure octave-guarded YIN median F0 + F0 IQR + Burmese syllable rate on a WAV."""
    samples, sr = read_wav_mono_float32(Path(wav_path))
    if samples.size == 0 or sr <= 0:
        return ProsodyStats(0.0, 0.0, 0.0, None, 0.0, count_burmese_syllables(text), None)

    duration_sec = samples.size / float(sr)
    s16 = resample_float32(samples, sr, ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = extract_frame_contours(s16, ANALYSIS_SAMPLE_RATE)

    if rms_arr.size == 0:
        return ProsodyStats(duration_sec, 0.0, 0.0, None, 0.0, count_burmese_syllables(text), None)

    p95 = float(np.percentile(rms_arr, 95))
    active_gate = max(0.005, 0.08 * p95) if p95 >= 0.003 else 1.0
    active_frames = int(np.sum(rms_arr >= active_gate))
    active_sec = active_frames * FRAME_STEP_SEC

    voiced = f0_arr[~np.isnan(f0_arr)]
    voiced_sec = voiced.size * FRAME_STEP_SEC
    if voiced.size >= 3:
        f0_med = float(np.median(voiced))
        q25, q75 = np.percentile(voiced, [25, 75])
        f0_iqr_st = semitone_diff(float(q75), float(q25)) if q25 > 0 else 0.0
    else:
        f0_med = None
        f0_iqr_st = 0.0

    syl_count = count_burmese_syllables(text)
    syl_rate = (syl_count / active_sec) if (syl_count > 0 and active_sec >= 0.08) else None

    return ProsodyStats(
        duration_sec=round(duration_sec, 3),
        active_speech_sec=round(active_sec, 3),
        voiced_sec=round(voiced_sec, 3),
        f0_median_hz=round(f0_med, 2) if f0_med is not None else None,
        f0_iqr_st=round(f0_iqr_st, 3),
        syllable_count=syl_count,
        syllable_rate=round(syl_rate, 3) if syl_rate is not None else None,
    )


def select_steady_reference_slice(
    ref_wav: Path,
    work_dir: Path,
    *,
    target_sec: float = REFERENCE_SLICE_SECONDS,
) -> tuple[Path, float, ProsodyStats]:
    """Select the steadiest ~10 s voiced slice ranked by lowest F0 IQR.

    Returns ``(selected_wav_path, selected_duration_sec, selected_prosody_stats)``.
    Leaves ``ref_wav`` untouched when it is already <= target_sec + 1.0 s or unvoiced.
    """
    ref_path = Path(ref_wav)
    samples, sr = read_wav_mono_float32(ref_path)
    if samples.size == 0 or sr <= 0:
        return ref_path, 0.0, ProsodyStats(0.0, 0.0, 0.0, None, 0.0, 0, None)

    total_sec = samples.size / float(sr)
    s16 = resample_float32(samples, sr, ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = extract_frame_contours(s16, ANALYSIS_SAMPLE_RATE)

    voiced_total = int(np.sum(~np.isnan(f0_arr)))
    if voiced_total * FRAME_STEP_SEC < 1.0 or total_sec <= target_sec + 1.0:
        stats = measure_prosody(ref_path)
        return ref_path, round(total_sec, 3), stats

    # Trim outer silence at frame resolution
    p95 = float(np.percentile(rms_arr, 95))
    speech_mask = rms_arr >= max(0.005, 0.08 * p95)
    active_indices = np.where(speech_mask)[0]
    if active_indices.size >= 2:
        start_f = max(0, int(active_indices[0]) - 5)
        end_f = min(rms_arr.size, int(active_indices[-1]) + 6)
    else:
        start_f, end_f = 0, rms_arr.size

    win_frames = max(10, int(round(target_sec / FRAME_STEP_SEC)))
    step_frames = max(5, int(round(1.0 / FRAME_STEP_SEC)))  # 1.0 s stride

    if end_f - start_f <= win_frames:
        best_start_f = start_f
        best_end_f = end_f
    else:
        candidates: list[tuple[float, float, int, int]] = []
        pos = start_f
        while pos + win_frames <= end_f:
            sub_f0 = f0_arr[pos : pos + win_frames]
            v = sub_f0[~np.isnan(sub_f0)]
            voiced_ratio = v.size / float(win_frames)
            if v.size >= 10:
                q25, q75 = np.percentile(v, [25, 75])
                iqr_st = semitone_diff(float(q75), float(q25)) if q25 > 0 else 99.0
                candidates.append((voiced_ratio, iqr_st, pos, pos + win_frames))
            pos += step_frames

        if not candidates:
            stats = measure_prosody(ref_path)
            return ref_path, round(total_sec, 3), stats

        # Prefer windows with healthy voiced density (>= 45%), ranked by lowest F0 IQR.
        dense = [c for c in candidates if c[0] >= 0.45]
        pool = dense if dense else candidates
        pool.sort(key=lambda item: (item[1], -item[0]))
        _, _, best_start_f, best_end_f = pool[0]

    start_sec = best_start_f * FRAME_STEP_SEC
    end_sec = min(total_sec, best_end_f * FRAME_STEP_SEC)
    start_samp = max(0, int(round(start_sec * sr)))
    end_samp = min(samples.size, int(round(end_sec * sr)))
    sliced = samples[start_samp:end_samp]
    if sliced.size < int(sr * 1.0):
        stats = measure_prosody(ref_path)
        return ref_path, round(total_sec, 3), stats

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    stat = ref_path.stat()
    digest = hashlib.sha256(
        f"{ref_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}|{target_sec:.1f}".encode("utf-8")
    ).hexdigest()[:14]
    out_slice = work_dir / f"ref_steady_{digest}.wav"

    pcm16 = np.clip(sliced * 32767.0, -32768.0, 32767.0).astype("<i2")
    with wave.open(str(out_slice), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(pcm16.tobytes())

    slice_dur = sliced.size / float(sr)
    slice_stats = measure_prosody(out_slice)
    return out_slice, round(slice_dur, 3), slice_stats


def _run_coro_sync(coro_fn: Callable[[], Any]) -> Any:
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        return asyncio.run(coro_fn())

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(coro_fn()))
        return fut.result(timeout=60)


def transcribe_reference_slice_checked(
    slice_wav: Path,
    slice_seconds: float,
    *,
    asr_fn: Callable[..., Any] | None = None,
) -> str:
    """One-time Gemini ASR of the reference slice with 8-22 chars/s plausibility gate."""
    p = Path(slice_wav)
    if not p.is_file() or p.stat().st_size == 0 or slice_seconds <= 0.2:
        return ""
    try:
        from app.services import group_ai_admin as gai

        target_fn = asr_fn or gai.transcribe_voice_gemini
        wav_bytes = p.read_bytes()

        async def _call() -> str | None:
            res = target_fn(wav_bytes, mime_type="audio/wav")
            if asyncio.iscoroutine(res):
                return await res
            return res

        raw_text = _run_coro_sync(_call)
        cleaned = " ".join(str(raw_text or "").strip().split())
        if not cleaned:
            return ""
        cps = len(cleaned) / float(slice_seconds)
        if not (ASR_MIN_CHARS_PER_SEC <= cps <= ASR_MAX_CHARS_PER_SEC):
            logger.info(
                "clone_prosody: reference ASR rejected by plausibility gate (chars=%d, secs=%.2f, cps=%.2f)",
                len(cleaned),
                slice_seconds,
                cps,
            )
            return ""
        return cleaned
    except Exception as exc:
        logger.info("clone_prosody: reference ASR failed (%s); falling back to prompt_text=''", exc)
        return ""


def is_prosody_outlier(
    stats: ProsodyStats,
    *,
    anchor_f0_hz: float | None,
    anchor_rate: float | None,
) -> tuple[bool, float, float]:
    """Return ``(is_outlier, delta_f0_st, rate_ratio)`` against anchor."""
    delta_st = 0.0
    rate_ratio = 1.0
    f0_bad = False
    rate_bad = False

    if stats.f0_median_hz is not None and anchor_f0_hz is not None and anchor_f0_hz > 0:
        delta_st = semitone_diff(stats.f0_median_hz, anchor_f0_hz)
        if abs(delta_st) > OUTLIER_MAX_F0_ST:
            f0_bad = True

    if stats.syllable_rate is not None and anchor_rate is not None and anchor_rate > 0:
        rate_ratio = stats.syllable_rate / anchor_rate
        if rate_ratio < OUTLIER_RATE_MIN_RATIO or rate_ratio > OUTLIER_RATE_MAX_RATIO:
            rate_bad = True

    return (f0_bad or rate_bad), delta_st, rate_ratio


def prosody_anchor_distance(
    stats: ProsodyStats,
    *,
    anchor_f0_hz: float | None,
    anchor_rate: float | None,
) -> float:
    """Normalized distance to anchor so outlier retries keep the closer candidate."""
    dist = 0.0
    if stats.f0_median_hz is not None and anchor_f0_hz is not None and anchor_f0_hz > 0:
        dist += abs(semitone_diff(stats.f0_median_hz, anchor_f0_hz)) / OUTLIER_MAX_F0_ST
    if stats.syllable_rate is not None and anchor_rate is not None and anchor_rate > 0:
        ratio = max(0.2, min(5.0, stats.syllable_rate / anchor_rate))
        dist += abs(math.log(ratio)) / 0.25
    return dist


def compute_rubberband_params(
    stats: ProsodyStats,
    *,
    anchor_f0_hz: float | None,
    anchor_rate: float | None,
) -> tuple[float, float]:
    """Return ``(pitch_shift_st, tempo_factor)`` clamped to ±2.0 st and [0.90, 1.12]."""
    shift_st = 0.0
    if stats.f0_median_hz is not None and anchor_f0_hz is not None and anchor_f0_hz > 0:
        delta_st = semitone_diff(stats.f0_median_hz, anchor_f0_hz)
        needed_st = -delta_st
        if abs(needed_st) > RUBBERBAND_DEADBAND_PITCH_ST:
            shift_st = max(-RUBBERBAND_MAX_PITCH_ST, min(RUBBERBAND_MAX_PITCH_ST, needed_st))

    tempo_factor = 1.0
    if stats.syllable_rate is not None and anchor_rate is not None and anchor_rate > 0 and stats.syllable_rate > 0:
        # If chunk is slower than anchor (chunk_rate < anchor_rate), tempo_factor > 1 speeds it up.
        raw_tempo = anchor_rate / stats.syllable_rate
        if abs(raw_tempo - 1.0) > RUBBERBAND_DEADBAND_TEMPO:
            tempo_factor = max(RUBBERBAND_TEMPO_MIN, min(RUBBERBAND_TEMPO_MAX, raw_tempo))

    return round(shift_st, 4), round(tempo_factor, 4)


def apply_rubberband_micro_correction(
    wav_path: Path,
    *,
    pitch_shift_st: float,
    tempo_factor: float,
    ffmpeg_bin: str | None = None,
) -> bool:
    """Apply formant-preserved pitch/rate micro-correction in-place via FFmpeg rubberband."""
    if abs(pitch_shift_st) <= 1e-4 and abs(tempo_factor - 1.0) <= 1e-4:
        return False

    clamped_st = max(-RUBBERBAND_MAX_PITCH_ST, min(RUBBERBAND_MAX_PITCH_ST, float(pitch_shift_st)))
    clamped_tempo = max(RUBBERBAND_TEMPO_MIN, min(RUBBERBAND_TEMPO_MAX, float(tempo_factor)))

    ff = ffmpeg_bin or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    p = Path(wav_path)
    if not p.is_file():
        return False

    pitch_ratio = 2.0 ** (clamped_st / 12.0)
    tmp_out = p.with_name(f"{p.stem}.rb_tmp.wav")
    af_expr = f"rubberband=pitch={pitch_ratio:.6f}:tempo={clamped_tempo:.6f}:formant=preserved"
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", _MEDIA_WHITELIST,
        "-i", str(p),
        "-af", af_expr,
        "-ac", "1", "-c:a", "pcm_s16le",
        str(tmp_out),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if res.returncode == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 256:
            shutil.move(str(tmp_out), str(p))
            return True
    except Exception as exc:
        logger.debug("clone_prosody: rubberband correction skipped (%s)", exc)
    finally:
        tmp_out.unlink(missing_ok=True)
    return False
