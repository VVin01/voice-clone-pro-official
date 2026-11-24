"""Clone Prosody Measurement, Reference Conditioning Slice & Post-Loop Contour Pass.

Implements Phase 1A + Phase 1B + Phase 2 (Option e) of the Long-Form Voice Clone Prosody architecture:
  - Pure-numpy octave-guarded YIN median F0 + Burmese syllable rate measurement (cached per chunk)
  - Steadiest ~10 s voiced reference slice selection (ranked by lowest F0 IQR)
  - One-time Gemini ASR reference transcript with speaking-rate plausibility gate (8-22 chars/s)
  - Re-scored outlier detection (anchor F0/pace + intra-chunk IQR / head-tail break)
  - Post-loop global anchor (`F_job` / `ref_blend`) eliminating curated-persona Chunk-1 blindness
  - Continuous 50 ms pitch contour `Δp_k(t) = clamp[-2, +2](Δbody + Δenv(t) + Δhead*w_cos + Δtail*w_cos)`
    with 3.5 s sliding voiced-median tether, Burmese-safe voiced-frame edge F0 estimator,
    text-aware boundary classification + adaptive stitch pauses, question/expressive guards,
    and single-pass `asendcmd + rubberband=formant=preserved` execution.
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
from typing import Any, Callable, Sequence
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

# Phase 1B base constants (kept intact)
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

CLONE_BOUNDARY_PAUSE_SEC: float = 0.135  # legacy default sentence-boundary pause

# Phase 2 (§5) constants — Option (e): Post-Loop Bilateral Seam Ramp + Envelope Tether
CUSTOM_REF_JOB_BLEND_WEIGHT: float = 0.35      # max ±0.35 st job-median accommodation vs custom ref slice
BODY_DEADBAND_PITCH_ST: float = 0.60           # soft-knee deadband (1.10 st on quoted/expressive chunks)
BODY_EXPRESSIVE_DEADBAND_ST: float = 1.10
BODY_MAX_SHIFT_ST: float = 1.50                # leave headroom inside ±2.0 st total cap for edge ramps

ENV_WINDOW_SEC: float = 3.50
ENV_DEADZONE_ST: float = 1.35                  # 0.0 st touch inside ±1.35 st (1.75 st expressive)
ENV_EXPRESSIVE_DEADZONE_ST: float = 1.75
ENV_TETHER_GAIN: float = 0.65                  # soft compression above deadzone
ENV_HARD_CEIL_ST: float = 1.85                 # guarantees 3.5 s envelope stays <= ±1.85 st

EDGE_HEAD_GUARD_FRAMES: int = 4                # 40 ms onset aspiration guard
EDGE_TAIL_GUARD_FRAMES: int = 12               # 120 ms Burmese phrase-final creak guard
EDGE_VOICED_TARGET_FRAMES: int = 65            # 0.65 s voiced speech (~1.0 s wall-clock)
EDGE_VOICED_WIDEN_FRAMES: int = 110            # Tier-1 widen fallback
EDGE_MIN_SHRINK_FRAMES: int = 8                # <8 voiced frames -> skip edge ramp

STEP_MAX_MID_PHRASE_ST: float = 0.65           # pause = 0.065 s
STEP_MAX_CLAUSE_ST: float = 0.90               # pause = 0.095 s
STEP_MAX_SENTENCE_ST: float = 1.35             # pause = 0.135 s
STEP_MAX_PARAGRAPH_ST: float = 1.80            # pause = 0.220 s

PAUSE_MID_PHRASE_SEC: float = 0.065
PAUSE_CLAUSE_SEC: float = 0.095
PAUSE_SENTENCE_SEC: float = 0.135
PAUSE_QUESTION_SEC: float = 0.160
PAUSE_PARAGRAPH_SEC: float = 0.220

RAMP_HEAD_SEC: float = 2.40                    # raised-cosine head decay
RAMP_TAIL_SEC: float = 1.80                    # raised-cosine tail lift
RAMP_CMD_STEP_SEC: float = 0.050               # 50 ms asendcmd grid
MAX_TOTAL_INSTANT_SHIFT_ST: float = 2.00       # hard ceiling on |Δp_k(t)| everywhere
MAX_RAMP_SLOPE_ST_PER_SEC: float = 0.85        # perceptual imperceptibility bound

OUTLIER_MAX_CHUNK_IQR_ST: float = 3.60
OUTLIER_MAX_INTERNAL_SPAN_ST: float = 4.20     # |raw_head_st - raw_tail_st|

_ASCII_SYLLABLE_FALLBACK_RE = re.compile(r"[A-Za-z0-9]+")
_MEDIA_WHITELIST = "file,pipe"
_BURMESE_QUESTION_ENDINGS: tuple[str, ...] = (
    "လား။",
    "လဲ။",
    "နည်း။",
    "တုံး။",
    "နော်။",
    "လား",
    "လဲ",
    "နည်း",
    "တုံး",
    "နော်",
)
_EXPRESSIVE_MARKERS: tuple[str, ...] = ('"', "“", "”", "‘", "’", "!")


@dataclass(frozen=True)
class ProsodyStats:
    duration_sec: float
    active_speech_sec: float
    voiced_sec: float
    f0_median_hz: float | None
    f0_iqr_st: float
    syllable_count: int
    syllable_rate: float | None


@dataclass
class ChunkProsodyAnalysis:
    """Cached single-pass YIN contour + prosody summary for one raw chunk WAV."""
    wav_path: Path
    text: str
    sample_rate: int
    n_samples: int
    stats: ProsodyStats
    rms_arr: np.ndarray
    f0_arr: np.ndarray
    raw_head_st_vs_med: float
    raw_tail_st_vs_med: float
    head_fallback: bool
    tail_fallback: bool
    head_skipped: bool
    tail_skipped: bool

    @property
    def internal_span_st(self) -> float:
        if self.head_skipped or self.tail_skipped:
            return 0.0
        return float(self.raw_head_st_vs_med - self.raw_tail_st_vs_med)


@dataclass(frozen=True)
class BoundarySpec:
    kind: str               # MID_PHRASE | CLAUSE | SENTENCE | PARAGRAPH
    s_max_st: float
    pause_sec: float        # scaled for pre-mastering stitch so delivered pause matches spec
    base_pause_sec: float   # unscaled listener-perceived pause duration
    question_tail: bool


@dataclass(frozen=True)
class GlobalProsodyAnchor:
    f0_anchor_hz: float | None
    rate_anchor: float | None
    f0_job_median_hz: float | None
    rate_job_median: float | None
    anchor_source: str      # "ref_blend" | "job_median"


@dataclass
class PostLoopProsodyResult:
    corrected_parts: list[Path]
    pauses_sec: list[float]
    anchor: GlobalProsodyAnchor
    post_stats: list[ProsodyStats]
    corrections_applied: int
    envelope_corrections: int
    edge_fallbacks: int
    boundary_steps: list[dict[str, Any]]
    raw_mean_iqr_st: float
    post_mean_iqr_st: float


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


def is_expressive_chunk(text: str) -> bool:
    """True when chunk contains quoted dialogue or exclamation (`“...”`, `"..."`, `‘...’`, `!`)."""
    s = str(text or "")
    return any(tok in s for tok in _EXPRESSIVE_MARKERS)


def is_question_ending(text: str) -> bool:
    """True when chunk ends with `?` or a Burmese question particle (`လား။`, `လဲ။`, etc.)."""
    s = str(text or "").strip().rstrip("”’\"') ")
    if not s:
        return False
    if s.endswith("?"):
        return True
    return any(s.endswith(p) for p in _BURMESE_QUESTION_ENDINGS)


def classify_chunk_boundaries(
    full_text: str,
    chunks: Sequence[str],
    *,
    speed_tempo: float = 1.0,
) -> list[BoundarySpec]:
    """Classify each seam `k -> k+1` from `chunks[k]`'s ending and source separator in `full_text`."""
    n_c = len(chunks)
    if n_c <= 1:
        return []
    tempo_scale = max(0.5, min(2.0, float(speed_tempo or 1.0)))
    src = str(full_text or "").replace("\r\n", "\n").replace("\r", "\n")

    spans: list[tuple[int, int]] = []
    pos = 0
    for ch in chunks:
        c_str = str(ch or "").strip()
        if not c_str:
            spans.append((pos, pos))
            continue
        head_probe = c_str[: min(24, len(c_str))].strip()
        tail_probe = c_str[max(0, len(c_str) - 24) :].strip()
        idx_h = src.find(head_probe, pos)
        if idx_h < 0:
            first_tok = c_str.split()[0] if c_str.split() else c_str[:8]
            idx_h = src.find(first_tok, pos)
        if idx_h < 0:
            idx_h = pos
        idx_t = src.find(tail_probe, idx_h)
        end_p = (idx_t + len(tail_probe)) if idx_t >= 0 else min(len(src), idx_h + len(c_str))
        spans.append((idx_h, end_p))
        pos = end_p

    specs: list[BoundarySpec] = []
    for k in range(n_c - 1):
        c_end = str(chunks[k] or "").rstrip()
        sep = src[spans[k][1] : spans[k + 1][0]]
        q_tail = is_question_ending(c_end)
        if "\n\n" in sep or sep.count("\n") >= 2:
            kind = "PARAGRAPH"
            s_max = STEP_MAX_PARAGRAPH_ST
            base_pause = PAUSE_PARAGRAPH_SEC
        elif c_end.endswith(("။", "?", "!")):
            kind = "SENTENCE"
            s_max = STEP_MAX_SENTENCE_ST
            base_pause = PAUSE_QUESTION_SEC if q_tail else PAUSE_SENTENCE_SEC
        elif c_end.endswith(("၊", ",", ";", ":")):
            kind = "CLAUSE"
            s_max = STEP_MAX_CLAUSE_ST
            base_pause = PAUSE_QUESTION_SEC if q_tail else PAUSE_CLAUSE_SEC
        else:
            kind = "MID_PHRASE"
            s_max = STEP_MAX_MID_PHRASE_ST
            base_pause = PAUSE_QUESTION_SEC if q_tail else PAUSE_MID_PHRASE_SEC

        specs.append(
            BoundarySpec(
                kind=kind,
                s_max_st=s_max,
                pause_sec=round(base_pause * tempo_scale, 4),
                base_pause_sec=base_pause,
                question_tail=q_tail,
            )
        )
    return specs


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
    corr = np.fft.irfft(np.conj(fft_x0) * fft_x, n=n_fft)[: tau_max + 1]

    sq = x[: 2 * w] * x[: 2 * w]
    csum = np.concatenate(([0.0], np.cumsum(sq)))
    energy_0 = csum[w]
    taus = np.arange(tau_max + 1)
    energy_tau = csum[taus + w] - csum[taus]

    diff = np.maximum(0.0, energy_0 + energy_tau - 2.0 * corr)
    cmnd = np.ones(tau_max + 1, dtype=np.float64)
    if tau_max >= 1:
        running = np.cumsum(diff[1 : tau_max + 1])
        taus_pos = np.arange(1, tau_max + 1, dtype=np.float64)
        valid_r = running > 1e-12
        safe_r = np.where(valid_r, running, 1.0)
        cmnd[1 : tau_max + 1] = np.where(valid_r, diff[1 : tau_max + 1] * taus_pos / safe_r, 1.0)

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
    win = max(hop * 2, int(round(sr * FRAME_WIN_SEC)) * 2)
    tau_min = max(2, int(sr / F0_MAX_HZ))
    tau_max = max(tau_min + 2, int(sr / F0_MIN_HZ))
    if win <= 2 * tau_max + 2:
        win = 2 * tau_max + 4

    if samples_16k.size < win:
        if samples_16k.size == 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        samples_16k = np.pad(samples_16k, (0, win - samples_16k.size))

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


def _stats_from_contours(
    duration_sec: float,
    rms_arr: np.ndarray,
    f0_arr: np.ndarray,
    text: str = "",
) -> ProsodyStats:
    syl_count = count_burmese_syllables(text)
    if rms_arr.size == 0:
        return ProsodyStats(round(duration_sec, 3), 0.0, 0.0, None, 0.0, syl_count, None)

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


def measure_prosody(wav_path: Path, text: str = "") -> ProsodyStats:
    """Measure octave-guarded YIN median F0 + F0 IQR + Burmese syllable rate on a WAV."""
    samples, sr = read_wav_mono_float32(Path(wav_path))
    if samples.size == 0 or sr <= 0:
        return ProsodyStats(0.0, 0.0, 0.0, None, 0.0, count_burmese_syllables(text), None)

    duration_sec = samples.size / float(sr)
    s16 = resample_float32(samples, sr, ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = extract_frame_contours(s16, ANALYSIS_SAMPLE_RATE)
    return _stats_from_contours(duration_sec, rms_arr, f0_arr, text=text)


def _trimmed_iqm(vals: np.ndarray) -> float:
    """P20-P80 trimmed interquartile mean (robust against bimodal syllable edges)."""
    if vals.size == 0:
        return 0.0
    if vals.size < 5:
        return float(np.median(vals))
    q20, q80 = np.percentile(vals, [20, 80])
    inner = vals[(vals >= q20) & (vals <= q80)]
    return float(np.mean(inner)) if inner.size > 0 else float(np.median(vals))


def estimate_chunk_edge_f0_st(
    f0_arr: np.ndarray,
    *,
    duration_sec: float,
    chunk_median_hz: float | None,
    anchor_hz: float | None,
    body_st_vs_anchor: float,
    side: str,
) -> tuple[float, bool, bool]:
    """Part 7 Burmese-safe voiced-frame edge F0 estimator.

    Returns ``(edge_st_vs_anchor, used_fallback, skipped_edge)``:
    - Guard trim: skips first 4 voiced frames (head) and last 12 voiced frames (tail creak/aukmyit).
    - Primary window: 65 voiced frames capped at 35% of chunk voiced frames (non-overlapping).
    - Creak/glitch filter: keeps frames in ``[0.72 * chunk_median, 1.50 * chunk_median]``, P20-P80 IQM.
    - Fallbacks:
      (a) widen to 110 voiced frames (max 40% of chunk);
      (b) for ``8 <= n < 20``, Bayesian shrinkage ``(n/20)*e_raw + (1 - n/20)*e_body``;
      (c) for ``n < 8`` or ``duration_sec < 2.5``, ``skipped_edge=True`` (Δedge = 0, never NaN/crash).
    """
    if (
        duration_sec < 2.5
        or chunk_median_hz is None
        or chunk_median_hz <= 0.0
        or anchor_hz is None
        or anchor_hz <= 0.0
        or f0_arr.size == 0
    ):
        return float(body_st_vs_anchor), True, True

    voiced = f0_arr[~np.isnan(f0_arr) & (f0_arr > 0)]
    n_v = int(voiced.size)
    if n_v < EDGE_MIN_SHRINK_FRAMES:
        return float(body_st_vs_anchor), True, True

    lo_hz = 0.72 * float(chunk_median_hz)
    hi_hz = 1.50 * float(chunk_median_hz)
    is_head = (side == "head")
    guard = EDGE_HEAD_GUARD_FRAMES if is_head else EDGE_TAIL_GUARD_FRAMES

    def _slice_clean(target_frames: int, max_frac: float) -> np.ndarray:
        avail = max(0, n_v - guard)
        if avail <= 0:
            return np.zeros(0, dtype=np.float32)
        cap = max(min(avail, max(EDGE_MIN_SHRINK_FRAMES, 16)), int(math.floor(n_v * max_frac)))
        win = min(int(target_frames), cap, avail)
        if is_head:
            sub = voiced[guard : guard + win]
        else:
            end_i = n_v - guard
            st_i = max(0, end_i - win)
            sub = voiced[st_i:end_i]
        return sub[(sub >= lo_hz) & (sub <= hi_hz)]

    clean = _slice_clean(EDGE_VOICED_TARGET_FRAMES, 0.35)
    used_fallback = False
    if clean.size < 20:
        used_fallback = True
        clean = _slice_clean(EDGE_VOICED_WIDEN_FRAMES, 0.40)

    if clean.size >= 20:
        est_hz = _trimmed_iqm(clean)
        if est_hz <= 0.0 or not math.isfinite(est_hz):
            return float(body_st_vs_anchor), True, True
        return semitone_diff(est_hz, float(anchor_hz)), used_fallback, False

    if clean.size >= EDGE_MIN_SHRINK_FRAMES:
        est_hz = _trimmed_iqm(clean)
        if est_hz <= 0.0 or not math.isfinite(est_hz):
            return float(body_st_vs_anchor), True, True
        raw_st = semitone_diff(est_hz, float(anchor_hz))
        alpha = float(clean.size) / 20.0
        shrunk = alpha * raw_st + (1.0 - alpha) * float(body_st_vs_anchor)
        return float(shrunk), True, False

    return float(body_st_vs_anchor), True, True


def analyze_chunk_prosody(wav_path: Path, text: str = "") -> ChunkProsodyAnalysis:
    """Run YIN ONCE on ``wav_path`` and cache both ``ProsodyStats`` and raw contours/edges."""
    p = Path(wav_path)
    samples, sr = read_wav_mono_float32(p)
    if samples.size == 0 or sr <= 0:
        empty_stats = ProsodyStats(0.0, 0.0, 0.0, None, 0.0, count_burmese_syllables(text), None)
        return ChunkProsodyAnalysis(
            wav_path=p,
            text=text,
            sample_rate=ANALYSIS_SAMPLE_RATE,
            n_samples=0,
            stats=empty_stats,
            rms_arr=np.zeros(0, dtype=np.float32),
            f0_arr=np.zeros(0, dtype=np.float32),
            raw_head_st_vs_med=0.0,
            raw_tail_st_vs_med=0.0,
            head_fallback=True,
            tail_fallback=True,
            head_skipped=True,
            tail_skipped=True,
        )

    duration_sec = samples.size / float(sr)
    s16 = resample_float32(samples, sr, ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = extract_frame_contours(s16, ANALYSIS_SAMPLE_RATE)
    stats = _stats_from_contours(duration_sec, rms_arr, f0_arr, text=text)

    med_hz = stats.f0_median_hz
    if med_hz is not None and med_hz > 0.0:
        h_st, h_fb, h_sk = estimate_chunk_edge_f0_st(
            f0_arr,
            duration_sec=duration_sec,
            chunk_median_hz=med_hz,
            anchor_hz=med_hz,
            body_st_vs_anchor=0.0,
            side="head",
        )
        t_st, t_fb, t_sk = estimate_chunk_edge_f0_st(
            f0_arr,
            duration_sec=duration_sec,
            chunk_median_hz=med_hz,
            anchor_hz=med_hz,
            body_st_vs_anchor=0.0,
            side="tail",
        )
    else:
        h_st, h_fb, h_sk = 0.0, True, True
        t_st, t_fb, t_sk = 0.0, True, True

    return ChunkProsodyAnalysis(
        wav_path=p,
        text=text,
        sample_rate=int(sr),
        n_samples=int(samples.size),
        stats=stats,
        rms_arr=rms_arr,
        f0_arr=f0_arr,
        raw_head_st_vs_med=round(h_st, 4),
        raw_tail_st_vs_med=round(t_st, 4),
        head_fallback=h_fb,
        tail_fallback=t_fb,
        head_skipped=h_sk,
        tail_skipped=t_sk,
    )


def select_steady_reference_slice(
    ref_wav: Path,
    work_dir: Path,
    *,
    target_sec: float = REFERENCE_SLICE_SECONDS,
) -> tuple[Path, float, ProsodyStats]:
    """Select the steadiest ~10 s voiced slice ranked by lowest F0 IQR."""
    ref_path = Path(ref_wav)
    samples, sr = read_wav_mono_float32(ref_path)
    if samples.size == 0 or sr <= 0:
        return ref_path, 0.0, ProsodyStats(0.0, 0.0, 0.0, None, 0.0, 0, None)

    total_sec = samples.size / float(sr)
    s16 = resample_float32(samples, sr, ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = extract_frame_contours(s16, ANALYSIS_SAMPLE_RATE)

    voiced_total = int(np.sum(~np.isnan(f0_arr)))
    if voiced_total * FRAME_STEP_SEC < 1.0 or total_sec <= target_sec + 1.0:
        stats = _stats_from_contours(total_sec, rms_arr, f0_arr)
        return ref_path, round(total_sec, 3), stats

    p95 = float(np.percentile(rms_arr, 95))
    speech_mask = rms_arr >= max(0.005, 0.08 * p95)
    active_indices = np.where(speech_mask)[0]
    if active_indices.size >= 2:
        start_f = max(0, int(active_indices[0]) - 5)
        end_f = min(rms_arr.size, int(active_indices[-1]) + 6)
    else:
        start_f, end_f = 0, rms_arr.size

    win_frames = max(10, int(round(target_sec / FRAME_STEP_SEC)))
    step_frames = max(5, int(round(1.0 / FRAME_STEP_SEC)))

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
            stats = _stats_from_contours(total_sec, rms_arr, f0_arr)
            return ref_path, round(total_sec, 3), stats

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
        stats = _stats_from_contours(total_sec, rms_arr, f0_arr)
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
    internal_span_st: float = 0.0,
) -> tuple[bool, float, float]:
    """Part 6 re-scored retry trigger:
    Outlier iff ``|ΔF0_med| > 2.5 st``, pace ratio ``∉ [0.78, 1.28]``,
    ``f0_iqr_st > 3.60 st``, or ``|head_st - tail_st| > 4.20 st``."""
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

    iqr_bad = float(stats.f0_iqr_st) > OUTLIER_MAX_CHUNK_IQR_ST
    span_bad = abs(float(internal_span_st)) > OUTLIER_MAX_INTERNAL_SPAN_ST

    return (f0_bad or rate_bad or iqr_bad or span_bad), delta_st, rate_ratio


def prosody_anchor_distance(
    stats: ProsodyStats,
    *,
    anchor_f0_hz: float | None,
    anchor_rate: float | None,
) -> float:
    """Normalized distance to anchor."""
    dist = 0.0
    if stats.f0_median_hz is not None and anchor_f0_hz is not None and anchor_f0_hz > 0:
        dist += abs(semitone_diff(stats.f0_median_hz, anchor_f0_hz)) / OUTLIER_MAX_F0_ST
    if stats.syllable_rate is not None and anchor_rate is not None and anchor_rate > 0:
        ratio = max(0.2, min(5.0, stats.syllable_rate / anchor_rate))
        dist += abs(math.log(ratio)) / 0.25
    return dist


def score_prosody_candidate(
    stats: ProsodyStats,
    *,
    anchor_f0_hz: float | None,
    anchor_rate: float | None,
    internal_span_st: float = 0.0,
) -> float:
    """Part 6 composite candidate score:
    ``anchor_distance + 0.35 * max(0, iqr_st - 2.0) + 0.25 * max(0, |head_st - tail_st| - 2.0)``."""
    base = prosody_anchor_distance(stats, anchor_f0_hz=anchor_f0_hz, anchor_rate=anchor_rate)
    iqr_pen = 0.35 * max(0.0, float(stats.f0_iqr_st) - 2.0)
    span_pen = 0.25 * max(0.0, abs(float(internal_span_st)) - 2.0)
    return base + iqr_pen + span_pen


def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float | None:
    pairs = [
        (float(v), max(1e-3, float(w)))
        for v, w in zip(values, weights)
        if v is not None and math.isfinite(float(v)) and float(v) > 0.0
    ]
    if not pairs:
        return None
    pairs.sort(key=lambda item: item[0])
    total_w = sum(w for _, w in pairs)
    acc = 0.0
    for val, w in pairs:
        acc += w
        if acc >= 0.5 * total_w:
            return val
    return pairs[-1][0]


def compute_global_prosody_anchor(
    analyses: Sequence[ChunkProsodyAnalysis],
    *,
    ref_prosody: ProsodyStats | None = None,
    is_custom_clone: bool = False,
    is_gender_flip: bool = False,
    preset_voice: str | None = None,
) -> GlobalProsodyAnchor:
    """Part 2 post-loop global anchor across ALL M raw chunks.

    - Custom clone, same gender, valid ``ref_prosody``:
      ``F_anchor = F_ref * 2^(clamp(st(F_job, F_ref), -1.0, +1.0) * 0.35 / 12)``
      ``R_anchor`` analogously log-blended toward ``R_job`` with weight ``0.35`` (clamped ±20%).
    - Curated persona (``preset_voice``) OR gender-flip (``m2f``/``f2m``):
      ``F_anchor = F_job``, ``R_anchor = R_job``.
    """
    f_vals = [a.stats.f0_median_hz for a in analyses if a.stats.f0_median_hz is not None]
    f_wts = [max(0.05, a.stats.voiced_sec) for a in analyses if a.stats.f0_median_hz is not None]
    r_vals = [a.stats.syllable_rate for a in analyses if a.stats.syllable_rate is not None]
    r_wts = [max(0.05, a.stats.active_speech_sec) for a in analyses if a.stats.syllable_rate is not None]

    f_job = _weighted_median(f_vals, f_wts)
    r_job = _weighted_median(r_vals, r_wts)

    use_ref_blend = (
        bool(is_custom_clone)
        and not bool(is_gender_flip)
        and not bool(preset_voice)
        and ref_prosody is not None
        and ref_prosody.f0_median_hz is not None
        and ref_prosody.f0_median_hz > 0.0
    )

    if use_ref_blend and ref_prosody is not None and ref_prosody.f0_median_hz is not None:
        f_ref = float(ref_prosody.f0_median_hz)
        if f_job is not None and f_job > 0.0:
            clamped_st = max(-1.0, min(1.0, semitone_diff(f_job, f_ref)))
            f_anchor = f_ref * (2.0 ** ((clamped_st * CUSTOM_REF_JOB_BLEND_WEIGHT) / 12.0))
        else:
            f_anchor = f_ref

        if ref_prosody.syllable_rate is not None and ref_prosody.syllable_rate > 0.0:
            r_ref = float(ref_prosody.syllable_rate)
            if r_job is not None and r_job > 0.0:
                log_diff = max(-0.20, min(0.20, math.log(r_job / r_ref)))
                r_anchor = r_ref * math.exp(log_diff * CUSTOM_REF_JOB_BLEND_WEIGHT)
            else:
                r_anchor = r_ref
        else:
            r_anchor = r_job
        return GlobalProsodyAnchor(
            f0_anchor_hz=round(f_anchor, 3),
            rate_anchor=round(r_anchor, 4) if r_anchor is not None else None,
            f0_job_median_hz=round(f_job, 3) if f_job is not None else None,
            rate_job_median=round(r_job, 4) if r_job is not None else None,
            anchor_source="ref_blend",
        )

    return GlobalProsodyAnchor(
        f0_anchor_hz=round(f_job, 3) if f_job is not None else None,
        rate_anchor=round(r_job, 4) if r_job is not None else None,
        f0_job_median_hz=round(f_job, 3) if f_job is not None else None,
        rate_job_median=round(r_job, 4) if r_job is not None else None,
        anchor_source="job_median",
    )


def compute_soft_knee_body_shift(
    chunk_f0_hz: float | None,
    anchor_f0_hz: float | None,
    *,
    expressive: bool = False,
) -> float:
    """Part 3.1 continuous soft-knee whole-chunk centering toward ``F_anchor``."""
    if chunk_f0_hz is None or anchor_f0_hz is None or chunk_f0_hz <= 0.0 or anchor_f0_hz <= 0.0:
        return 0.0
    delta_st = semitone_diff(chunk_f0_hz, anchor_f0_hz)
    needed_st = -delta_st
    deadband = BODY_EXPRESSIVE_DEADBAND_ST if expressive else BODY_DEADBAND_PITCH_ST
    mag = abs(needed_st)
    if mag <= deadband:
        return 0.0
    eff = min(BODY_MAX_SHIFT_ST, mag - deadband)
    return round(math.copysign(eff, needed_st), 4)


def compute_envelope_tether(
    f0_arr: np.ndarray,
    *,
    anchor_f0_hz: float | None,
    body_shift_st: float,
    expressive: bool = False,
) -> tuple[np.ndarray, bool]:
    """Part 3.2: 3.5 s sliding voiced-median envelope tether on the 10 ms frame grid.

    - Zero touch inside ``±1.35 st`` (``±1.75 st`` on expressive chunks).
    - Pulls excess beyond deadzone with gain ``0.65``.
    - Enforces hard ceiling ``±1.85 st`` so no 3.5 s envelope point remains beyond ``±1.85 st``,
      and damps rapid opposite-signed 5 s intra-chunk swings while passing ``<3.5 s`` syllable/word
      contours through at ``1.00x``.
    """
    n = int(f0_arr.size)
    if n == 0 or anchor_f0_hz is None or anchor_f0_hz <= 0.0:
        return np.zeros(n, dtype=np.float64), False

    half_w = max(5, int(round((ENV_WINDOW_SEC * 0.5) / FRAME_STEP_SEC)))  # 175 frames = 1.75 s
    st_arr = np.full(n, np.nan, dtype=np.float64)
    valid = ~np.isnan(f0_arr) & (f0_arr > 0.0)
    if int(np.sum(valid)) < 8:
        return np.zeros(n, dtype=np.float64), False
    st_arr[valid] = 12.0 * np.log2(f0_arr[valid] / float(anchor_f0_hz)) + float(body_shift_st)

    grid_idx = np.arange(0, n, 5)  # 50 ms centers
    m_g = int(grid_idx.size)
    grid_med = np.zeros(m_g, dtype=np.float64)
    has_val = np.zeros(m_g, dtype=bool)
    for gi, fi in enumerate(grid_idx):
        sub = st_arr[max(0, fi - half_w) : min(n, fi + half_w + 1)]
        v = sub[~np.isnan(sub)]
        if v.size >= 8:
            grid_med[gi] = float(np.median(v))
            has_val[gi] = True
    if not np.any(has_val):
        return np.zeros(n, dtype=np.float64), False

    env_g = np.interp(
        grid_idx.astype(np.float64),
        grid_idx[has_val].astype(np.float64),
        grid_med[has_val],
    )
    dz = ENV_EXPRESSIVE_DEADZONE_ST if expressive else ENV_DEADZONE_ST

    target_g = env_g.copy()
    modified = False
    for gi in range(m_g):
        e = env_g[gi]
        ae = abs(e)
        if ae > dz:
            pull = ENV_TETHER_GAIN * (ae - dz)
            if ae - pull > ENV_HARD_CEIL_ST:
                pull = ae - ENV_HARD_CEIL_ST
            target_g[gi] = e - math.copysign(pull, e)
            modified = True

    # Damp intra-chunk 5 s bipolar swings (when both <-0.22 and >+0.22 occur within ±6.0 s),
    # while leaving unipolar +2.7 st expressive arches (w_min >= -0.15) untouched in [+1.50, +1.85 st]!
    rad = 120  # 6.0 s at 50 ms step
    max_bipolar_span = 1.18
    damped_g = target_g.copy()
    for _ in range(2):
        cur = damped_g.copy()
        for gi in range(m_g):
            win = cur[max(0, gi - rad) : min(m_g, gi + rad + 1)]
            w_min = float(np.min(win))
            w_max = float(np.max(win))
            span = w_max - w_min
            if w_min < -0.22 and w_max > 0.22 and span > max_bipolar_span:
                mid = max(-0.22, min(0.22, 0.5 * (w_min + w_max)))
                scale = max_bipolar_span / span
                damped_g[gi] = mid + (cur[gi] - mid) * scale
                modified = True

    # Guard steep unipolar negative drops (< -1.40 st next to >= 0 st within 5.5 s)
    rad_uni = 110
    cur_u = damped_g.copy()
    for gi in range(m_g):
        if cur_u[gi] < -1.40:
            win = cur_u[max(0, gi - rad_uni) : min(m_g, gi + rad_uni + 1)]
            w_hi = float(np.max(win))
            if w_hi - cur_u[gi] > 1.50:
                damped_g[gi] = min(0.0, w_hi - 1.50)
                modified = True

    if not modified:
        return np.zeros(n, dtype=np.float64), False

    delta_g = damped_g - env_g
    if m_g >= 15:
        k_win = np.hanning(15)
        k_win /= np.sum(k_win)
        pad_d = np.pad(delta_g, (7, 7), mode="edge")
        delta_g = np.convolve(pad_d, k_win, mode="valid")[:m_g]

    # Enforce strict post-pass ±1.85 st hard ceiling on the 3.5 s envelope
    # (target 1.78 st pre-vocoder so phase-vocoder tracking never overshoots ±1.85 st)
    ceil_eff = ENV_HARD_CEIL_ST - 0.07
    post_g = env_g + delta_g
    over_hi = post_g > ceil_eff
    if np.any(over_hi):
        delta_g[over_hi] = ceil_eff - env_g[over_hi]
    over_lo = post_g < -ceil_eff
    if np.any(over_lo):
        delta_g[over_lo] = -ceil_eff - env_g[over_lo]
    if m_g >= 9:
        k_post = np.hanning(9)
        k_post /= np.sum(k_post)
        delta_g = np.convolve(np.pad(delta_g, (4, 4), mode="edge"), k_post, mode="valid")[:m_g]
        post_g2 = env_g + delta_g
        delta_g = np.where(post_g2 > ceil_eff, ceil_eff - env_g, delta_g)
        delta_g = np.where(post_g2 < -ceil_eff, -ceil_eff - env_g, delta_g)

    delta_frames = np.interp(np.arange(n, dtype=np.float64), grid_idx.astype(np.float64), delta_g)
    return delta_frames, True


def _project_slope_limit(y: np.ndarray, dt: float, max_slope: float = MAX_RAMP_SLOPE_ST_PER_SEC) -> np.ndarray:
    """Symmetric projection onto ``|y[i+1] - y[i]| <= max_slope * dt``."""
    out = y.astype(np.float64, copy=True)
    if out.size <= 1:
        return out
    max_step = float(max_slope) * float(dt)
    for _ in range(50):
        changed = False
        for i in range(out.size - 1):
            d = out[i + 1] - out[i]
            if abs(d) > max_step + 1e-9:
                excess = abs(d) - max_step
                half = 0.5 * excess * (1.0 if d > 0.0 else -1.0)
                out[i] += half
                out[i + 1] -= half
                changed = True
        if not changed:
            break
    return out


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


def _enforce_exact_pcm_frames(wav_path: Path, target_frames: int) -> None:
    """Ensure ``wav_path`` is within exact ``target_frames`` (trims/pads vocoder flush tail)."""
    if target_frames <= 0 or not wav_path.is_file():
        return
    try:
        with wave.open(str(wav_path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            n_cur = wf.getnframes()
            if ch != 1 or sw != 2 or sr <= 0 or n_cur == target_frames:
                return
            if abs(n_cur - target_frames) > int(0.080 * sr):
                return
            raw = wf.readframes(n_cur)
        arr = np.frombuffer(raw, dtype="<i2").copy()
        if arr.size > target_frames:
            arr = arr[:target_frames]
            fade_n = min(max(1, int(round(0.003 * sr))), arr.size // 4)
            if fade_n > 1:
                ramp = np.linspace(1.0, 0.0, fade_n, dtype=np.float32)
                arr[-fade_n:] = (arr[-fade_n:].astype(np.float32) * ramp).astype("<i2")
        elif arr.size < target_frames:
            arr = np.pad(arr, (0, target_frames - arr.size))
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(arr.tobytes())
    except Exception:
        pass


def apply_contour_to_chunk(
    raw_wav: Path,
    out_wav: Path,
    *,
    grid_t: np.ndarray,
    grid_st: np.ndarray,
    tempo_factor: float = 1.0,
    ffmpeg_bin: str | None = None,
) -> bool:
    """Part 4: Single-pass `asendcmd + rubberband=formant=preserved` execution.

    Reads ``raw_wav`` without modifying it and writes ``out_wav`` with sample-exact
    duration matching ``raw_duration / tempo_factor`` within ±5 ms.
    """
    src = Path(raw_wav)
    dst = Path(out_wav)
    dst.parent.mkdir(parents=True, exist_ok=True)

    clamped_tempo = max(RUBBERBAND_TEMPO_MIN, min(RUBBERBAND_TEMPO_MAX, float(tempo_factor)))
    if grid_t.size == 0 or grid_st.size == 0:
        shutil.copy2(src, dst)
        return False

    clamped_grid = np.clip(grid_st, -MAX_TOTAL_INSTANT_SHIFT_ST, MAX_TOTAL_INSTANT_SHIFT_ST)

    orig_frames = 0
    orig_sr = 0
    try:
        with wave.open(str(src), "rb") as wf:
            orig_sr = wf.getframerate()
            orig_frames = wf.getnframes()
    except Exception:
        orig_frames = 0

    expected_frames = (
        max(1, int(round(orig_frames / clamped_tempo)))
        if (orig_frames > 0 and orig_sr > 0)
        else 0
    )

    # Uniform contour (including 0.0 st / 1.0x no-op) -> delegate through apply_rubberband_micro_correction
    if float(np.max(np.abs(clamped_grid - clamped_grid[0]))) <= 1e-3:
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        applied = apply_rubberband_micro_correction(
            dst,
            pitch_shift_st=float(clamped_grid[0]),
            tempo_factor=clamped_tempo,
            ffmpeg_bin=ffmpeg_bin,
        )
        if applied and expected_frames > 0:
            _enforce_exact_pcm_frames(dst, expected_frames)
        return applied

    ff = ffmpeg_bin or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    init_ratio = 2.0 ** (float(clamped_grid[0]) / 12.0)
    cmds = ";".join(
        f"{float(t):.3f} rubberband pitch {2.0 ** (float(st) / 12.0):.6f}"
        for t, st in zip(grid_t, clamped_grid)
    )
    af_expr = (
        f"asendcmd=c='{cmds}',"
        f"rubberband=pitch={init_ratio:.6f}:tempo={clamped_tempo:.6f}:formant=preserved"
    )
    tmp_out = dst.with_name(f"{dst.stem}.asend_tmp.wav")
    ar_args = ["-ar", str(orig_sr)] if orig_sr > 0 else []
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", _MEDIA_WHITELIST,
        "-i", str(src),
        "-af", af_expr,
        *ar_args,
        "-ac", "1", "-c:a", "pcm_s16le",
        str(tmp_out),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
        if res.returncode == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 256:
            shutil.move(str(tmp_out), str(dst))
            if expected_frames > 0:
                _enforce_exact_pcm_frames(dst, expected_frames)
            return True
    except Exception as exc:
        logger.debug("clone_prosody: contoured rubberband skipped (%s)", exc)
    finally:
        tmp_out.unlink(missing_ok=True)

    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return False


def build_chunk_pitch_contour_grid(
    duration_sec: float,
    *,
    body_shift_st: float,
    env_delta_frames: np.ndarray,
    head_ramp_st: float,
    tail_ramp_st: float,
    speed_tempo: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Construct the 50 ms `Δp_k(t)` grid per Part 3 with `|d(Δp)/dt| <= 0.85 st/s`."""
    dur = max(0.05, float(duration_sec))
    n_grid = max(2, int(math.ceil(dur / RAMP_CMD_STEP_SEC)) + 1)
    grid_t = np.linspace(0.0, dur, n_grid)

    if env_delta_frames.size > 0:
        f_idx = np.clip(grid_t / FRAME_STEP_SEC, 0.0, float(env_delta_frames.size - 1))
        d_env_g = np.interp(f_idx, np.arange(env_delta_frames.size, dtype=np.float64), env_delta_frames)
    else:
        d_env_g = np.zeros(n_grid, dtype=np.float64)

    tempo_scale = max(0.5, min(2.0, float(speed_tempo or 1.0)))
    base_head_l = RAMP_HEAD_SEC * tempo_scale
    base_tail_l = RAMP_TAIL_SEC * tempo_scale

    # Raised cosine `w_cos(τ, L) = 0.5*(1 + cos(π*τ/L))` has max slope `|Δ| * π / (2*L)`.
    # Choosing `L >= π * |Δ| / (2 * MAX_RAMP_SLOPE_ST_PER_SEC)` satisfies slope bound by construction;
    # for large >0.75 st cliffs on long chunks, widen smoothly up to <=4.2 s (<=0.24*dur) so 5 s
    # window medians transition gently while leaving the mid-chunk [5 s, T-5 s] core 100% untouched.
    req_head_l = (math.pi * abs(float(head_ramp_st))) / (2.0 * MAX_RAMP_SLOPE_ST_PER_SEC)
    req_tail_l = (math.pi * abs(float(tail_ramp_st))) / (2.0 * MAX_RAMP_SLOPE_ST_PER_SEC)
    wide_cap = min(4.20 * tempo_scale, max(base_head_l, 0.24 * dur))
    wide_cap_t = min(4.20 * tempo_scale, max(base_tail_l, 0.24 * dur))
    smooth_head_l = min(wide_cap, 3.60 * abs(float(head_ramp_st)) * tempo_scale)
    smooth_tail_l = min(wide_cap_t, 3.40 * abs(float(tail_ramp_st)) * tempo_scale)
    l_head = min(max(base_head_l, req_head_l, smooth_head_l), max(0.40, 0.48 * dur))
    l_tail = min(max(base_tail_l, req_tail_l, smooth_tail_l), max(0.40, 0.48 * dur))

    if abs(head_ramp_st) > 1e-4 and l_head > 1e-3:
        w_h = np.where(grid_t < l_head, 0.5 * (1.0 + np.cos(math.pi * grid_t / l_head)), 0.0)
    else:
        w_h = np.zeros(n_grid, dtype=np.float64)

    tau_tail = dur - grid_t
    if abs(tail_ramp_st) > 1e-4 and l_tail > 1e-3:
        w_t = np.where(tau_tail < l_tail, 0.5 * (1.0 + np.cos(math.pi * tau_tail / l_tail)), 0.0)
    else:
        w_t = np.zeros(n_grid, dtype=np.float64)

    # Note: w_h and w_t satisfy |d/dt| <= MAX_RAMP_SLOPE_ST_PER_SEC (0.85 st/s) by construction
    # of `l_head` and `l_tail` above; avoid eroding `d_env_g` so the ±1.85 st envelope ceiling holds!
    ramp_part = float(head_ramp_st) * w_h + float(tail_ramp_st) * w_t
    ramp_smooth = _project_slope_limit(ramp_part, float(grid_t[1] - grid_t[0]), MAX_RAMP_SLOPE_ST_PER_SEC)
    raw_grid = float(body_shift_st) + d_env_g + ramp_smooth
    smooth = np.clip(raw_grid, -MAX_TOTAL_INSTANT_SHIFT_ST, MAX_TOTAL_INSTANT_SHIFT_ST)
    return grid_t, smooth


def run_post_loop_prosody_pass(
    raw_parts: Sequence[Path],
    chunks: Sequence[str],
    *,
    full_text: str,
    out_dir: Path,
    ref_prosody: ProsodyStats | None = None,
    is_custom_clone: bool = False,
    is_gender_flip: bool = False,
    preset_voice: str | None = None,
    speed_tempo: float = 1.0,
    cached_analyses: Sequence[ChunkProsodyAnalysis | None] | None = None,
    ffmpeg_bin: str | None = None,
) -> PostLoopProsodyResult:
    """Parts 1-7: Execute ONE post-loop bilateral contour + tempo pass over raw chunks.

    Never mutates ``raw_parts``; writes ``chunk{idx:02d}.corr.wav`` into ``out_dir`` so
    park/resume or re-entry is strictly idempotent with zero double-rubberbanding.
    """
    m = len(raw_parts)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    analyses: list[ChunkProsodyAnalysis] = []
    for idx in range(m):
        cached = (
            cached_analyses[idx]
            if (cached_analyses is not None and idx < len(cached_analyses))
            else None
        )
        if cached is not None and cached.wav_path.resolve() == Path(raw_parts[idx]).resolve():
            analyses.append(cached)
        else:
            c_txt = str(chunks[idx]) if idx < len(chunks) else ""
            analyses.append(analyze_chunk_prosody(Path(raw_parts[idx]), text=c_txt))

    anchor = compute_global_prosody_anchor(
        analyses,
        ref_prosody=ref_prosody,
        is_custom_clone=is_custom_clone,
        is_gender_flip=is_gender_flip,
        preset_voice=preset_voice,
    )
    boundaries = classify_chunk_boundaries(full_text, chunks, speed_tempo=speed_tempo)
    f_anchor = anchor.f0_anchor_hz
    r_anchor = anchor.rate_anchor

    body_shifts: list[float] = []
    env_deltas: list[np.ndarray] = []
    tempos: list[float] = []
    raw_heads_vs_anchor: list[float] = []
    raw_tails_vs_anchor: list[float] = []
    post_be_heads: list[float] = []
    post_be_tails: list[float] = []
    skip_heads: list[bool] = []
    skip_tails: list[bool] = []
    envelope_corrections = 0
    edge_fallbacks = 0

    for idx, ana in enumerate(analyses):
        c_txt = str(chunks[idx]) if idx < len(chunks) else ""
        expr = is_expressive_chunk(c_txt)
        b_sh = compute_soft_knee_body_shift(ana.stats.f0_median_hz, f_anchor, expressive=expr)
        body_shifts.append(b_sh)

        d_env, env_used = compute_envelope_tether(
            ana.f0_arr,
            anchor_f0_hz=f_anchor,
            body_shift_st=b_sh,
            expressive=expr,
        )
        env_deltas.append(d_env)
        if env_used:
            envelope_corrections += 1

        # Raw edges relative to global anchor (for telemetry)
        raw_body_st = (
            semitone_diff(ana.stats.f0_median_hz, f_anchor)
            if (ana.stats.f0_median_hz and f_anchor)
            else 0.0
        )
        rh_st, rh_fb, _ = estimate_chunk_edge_f0_st(
            ana.f0_arr,
            duration_sec=ana.stats.duration_sec,
            chunk_median_hz=ana.stats.f0_median_hz,
            anchor_hz=f_anchor,
            body_st_vs_anchor=raw_body_st,
            side="head",
        )
        rt_st, rt_fb, _ = estimate_chunk_edge_f0_st(
            ana.f0_arr,
            duration_sec=ana.stats.duration_sec,
            chunk_median_hz=ana.stats.f0_median_hz,
            anchor_hz=f_anchor,
            body_st_vs_anchor=raw_body_st,
            side="tail",
        )
        raw_heads_vs_anchor.append(rh_st)
        raw_tails_vs_anchor.append(rt_st)

        # Post-(body + env) edge estimates (Part 3.3 + Part 7) from cached contour
        if f_anchor and ana.f0_arr.size > 0:
            f0_be = ana.f0_arr * (2.0 ** ((b_sh + d_env) / 12.0))
            v_be = f0_be[~np.isnan(f0_be) & (f0_be > 0)]
            be_med_hz = float(np.median(v_be)) if v_be.size >= 3 else ana.stats.f0_median_hz
            be_body_st = semitone_diff(be_med_hz, f_anchor) if (be_med_hz and f_anchor) else 0.0
            eh, fb_h, sk_h = estimate_chunk_edge_f0_st(
                f0_be,
                duration_sec=ana.stats.duration_sec,
                chunk_median_hz=be_med_hz,
                anchor_hz=f_anchor,
                body_st_vs_anchor=be_body_st,
                side="head",
            )
            et, fb_t, sk_t = estimate_chunk_edge_f0_st(
                f0_be,
                duration_sec=ana.stats.duration_sec,
                chunk_median_hz=be_med_hz,
                anchor_hz=f_anchor,
                body_st_vs_anchor=be_body_st,
                side="tail",
            )
        else:
            eh, fb_h, sk_h = 0.0, True, True
            et, fb_t, sk_t = 0.0, True, True

        if fb_h or rh_fb:
            edge_fallbacks += 1
        if fb_t or rt_fb:
            edge_fallbacks += 1

        post_be_heads.append(eh)
        post_be_tails.append(et)
        skip_heads.append(sk_h)
        skip_tails.append(sk_t)

        # Tempo factor toward global R_anchor
        tp = 1.0
        if ana.stats.syllable_rate and r_anchor and r_anchor > 0.0 and ana.stats.syllable_rate > 0.0:
            raw_tp = float(r_anchor) / float(ana.stats.syllable_rate)
            if abs(raw_tp - 1.0) > RUBBERBAND_DEADBAND_TEMPO:
                tp = max(RUBBERBAND_TEMPO_MIN, min(RUBBERBAND_TEMPO_MAX, raw_tp))
        tempos.append(round(tp, 4))

    # Part 3.3: Bilateral 50/50 seam sharing across every boundary k -> k+1
    head_ramps = [0.0] * m
    tail_ramps = [0.0] * m
    for k in range(m - 1):
        b_spec = boundaries[k]
        et = post_be_tails[k]
        eh = post_be_heads[k + 1]
        diff = eh - et
        s_max = b_spec.s_max_st
        if abs(diff) <= s_max or (skip_tails[k] and skip_heads[k + 1]):
            continue

        # Compensate for raised-cosine weight roll-off across the voiced edge window
        # (tail window center τ ≈ 0.45 s -> w_cos ≈ 0.86; head window center t ≈ 0.37 s -> w_cos ≈ 0.92)
        if b_spec.question_tail and 0.0 <= et <= 2.0 and diff < 0.0:
            # Part 5 Question guard: rising question tail is NOT pulled down; head[k+1] absorbs return
            tail_ramps[k] = 0.0
            if not skip_heads[k + 1]:
                head_ramps[k + 1] = ((et - s_max) - eh) / 0.92
        elif skip_tails[k]:
            head_ramps[k + 1] = (-math.copysign(abs(diff) - s_max, diff)) / 0.92
        elif skip_heads[k + 1]:
            tail_ramps[k] = (math.copysign(abs(diff) - s_max, diff)) / 0.86
        else:
            # 50/50 bilateral split around anchor-bounded midpoint [-0.5*s_max, +0.5*s_max]
            raw_mid = 0.5 * (et + eh)
            mid = max(-0.5 * s_max, min(0.5 * s_max, raw_mid))
            half_span = 0.5 * s_max * (1.0 if diff >= 0.0 else -1.0)
            tgt_tail = mid - half_span
            tgt_head = mid + half_span
            if abs(tgt_tail) > abs(et) and (tgt_tail * et > 0.0):
                tgt_tail = et
                tgt_head = et + (s_max if diff >= 0.0 else -s_max)
            elif abs(tgt_head) > abs(eh) and (tgt_head * eh > 0.0):
                tgt_head = eh
                tgt_tail = eh - (s_max if diff >= 0.0 else -s_max)
            # If one side saturates the ±2.0 st cap, avoid pulling the opposite same-sign edge away from it
            act_head = eh + max(-1.80, min(1.80, tgt_head - eh))
            act_tail = et + max(-1.80, min(1.80, tgt_tail - et))
            rem_diff = act_head - act_tail
            if abs(rem_diff) > s_max + 0.05:
                if abs(tgt_head - eh) > 1.80:
                    ideal_t = act_head - (s_max if diff >= 0.0 else -s_max)
                    act_tail = max(-0.65 * s_max, min(0.65 * s_max, ideal_t))
                elif abs(tgt_tail - et) > 1.80:
                    ideal_h = act_tail + (s_max if diff >= 0.0 else -s_max)
                    act_head = max(-0.65 * s_max, min(0.65 * s_max, ideal_h))
            tail_ramps[k] = (act_tail - et) / 0.86
            head_ramps[k + 1] = (act_head - eh) / 0.92

    corrected_parts: list[Path] = []
    post_analyses: list[ChunkProsodyAnalysis] = []
    corrections_applied = 0

    for idx, ana in enumerate(analyses):
        c_txt = str(chunks[idx]) if idx < len(chunks) else ""
        grid_t, grid_st = build_chunk_pitch_contour_grid(
            ana.stats.duration_sec,
            body_shift_st=body_shifts[idx],
            env_delta_frames=env_deltas[idx],
            head_ramp_st=head_ramps[idx],
            tail_ramp_st=tail_ramps[idx],
            speed_tempo=speed_tempo,
        )
        corr_wav = out_dir / f"{Path(raw_parts[idx]).stem}.corr.wav"
        applied = apply_contour_to_chunk(
            Path(raw_parts[idx]),
            corr_wav,
            grid_t=grid_t,
            grid_st=grid_st,
            tempo_factor=tempos[idx],
            ffmpeg_bin=ffmpeg_bin,
        )
        if applied:
            corrections_applied += 1
        corrected_parts.append(corr_wav)

        if applied and corr_wav.is_file() and ana.f0_arr.size > 0:
            # Part 4 (#3): Reuse cached extract_frame_contours arrays instead of running YIN twice per chunk!
            clamped_tp = max(RUBBERBAND_TEMPO_MIN, min(RUBBERBAND_TEMPO_MAX, float(tempos[idx])))
            clamped_g = np.clip(grid_st, -MAX_TOTAL_INSTANT_SHIFT_ST, MAX_TOTAL_INSTANT_SHIFT_ST)
            t_frames = np.arange(ana.f0_arr.size, dtype=np.float64) * FRAME_STEP_SEC
            f_shifts = np.interp(t_frames, grid_t, clamped_g)
            shifted_f0 = (ana.f0_arr.astype(np.float64) * (2.0 ** (f_shifts / 12.0))).astype(np.float32)
            edge_base_f0 = ana.f0_arr.astype(np.float64, copy=True)
            if ana.stats.f0_median_hz and ana.stats.f0_median_hz > 0.0:
                rmed = float(ana.stats.f0_median_hz)
                glitch_mask = (~np.isnan(edge_base_f0)) & ((edge_base_f0 < 0.72 * rmed) | (edge_base_f0 > 1.50 * rmed))
                edge_base_f0[glitch_mask] = np.nan
            shifted_edge_f0 = (edge_base_f0 * (2.0 ** (f_shifts / 12.0))).astype(np.float32)
            if abs(clamped_tp - 1.0) > 1e-4:
                new_n = max(1, int(round(shifted_f0.size / clamped_tp)))
                src_i = np.clip(np.round(np.arange(new_n) * clamped_tp).astype(int), 0, shifted_f0.size - 1)
                post_f0 = shifted_f0[src_i]
                post_edge_f0 = shifted_edge_f0[src_i]
                post_rms = ana.rms_arr[np.clip(src_i, 0, max(0, ana.rms_arr.size - 1))] if ana.rms_arr.size > 0 else ana.rms_arr
            else:
                post_f0 = shifted_f0
                post_edge_f0 = shifted_edge_f0
                post_rms = ana.rms_arr
            post_dur = ana.stats.duration_sec / clamped_tp
            p_stats = _stats_from_contours(post_dur, post_rms, post_f0, text=c_txt)
            p_med = p_stats.f0_median_hz
            if p_med is not None and p_med > 0.0:
                ph_m, ph_fb, ph_sk = estimate_chunk_edge_f0_st(
                    post_edge_f0, duration_sec=post_dur, chunk_median_hz=p_med, anchor_hz=p_med, body_st_vs_anchor=0.0, side="head"
                )
                pt_m, pt_fb, pt_sk = estimate_chunk_edge_f0_st(
                    post_edge_f0, duration_sec=post_dur, chunk_median_hz=p_med, anchor_hz=p_med, body_st_vs_anchor=0.0, side="tail"
                )
            else:
                ph_m, ph_fb, ph_sk = 0.0, True, True
                pt_m, pt_fb, pt_sk = 0.0, True, True
            post_analyses.append(
                ChunkProsodyAnalysis(
                    wav_path=corr_wav,
                    text=c_txt,
                    sample_rate=ana.sample_rate,
                    n_samples=max(1, int(round(ana.n_samples / clamped_tp))),
                    stats=p_stats,
                    rms_arr=post_rms,
                    f0_arr=post_edge_f0,
                    raw_head_st_vs_med=round(ph_m, 4),
                    raw_tail_st_vs_med=round(pt_m, 4),
                    head_fallback=ph_fb,
                    tail_fallback=pt_fb,
                    head_skipped=ph_sk,
                    tail_skipped=pt_sk,
                )
            )
        else:
            post_analyses.append(ana)

    # Compute post-correction boundary steps for Part 8 telemetry
    boundary_steps: list[dict[str, Any]] = []
    for k in range(m - 1):
        b_spec = boundaries[k]
        raw_step = raw_heads_vs_anchor[k + 1] - raw_tails_vs_anchor[k]
        pa_k = post_analyses[k]
        pa_next = post_analyses[k + 1]
        if f_anchor and pa_k.stats.f0_median_hz and pa_next.stats.f0_median_hz:
            pk_body = semitone_diff(pa_k.stats.f0_median_hz, f_anchor)
            pn_body = semitone_diff(pa_next.stats.f0_median_hz, f_anchor)
            pt_st, _, _ = estimate_chunk_edge_f0_st(
                pa_k.f0_arr,
                duration_sec=pa_k.stats.duration_sec,
                chunk_median_hz=pa_k.stats.f0_median_hz,
                anchor_hz=f_anchor,
                body_st_vs_anchor=pk_body,
                side="tail",
            )
            ph_st, _, _ = estimate_chunk_edge_f0_st(
                pa_next.f0_arr,
                duration_sec=pa_next.stats.duration_sec,
                chunk_median_hz=pa_next.stats.f0_median_hz,
                anchor_hz=f_anchor,
                body_st_vs_anchor=pn_body,
                side="head",
            )
            post_step = ph_st - pt_st
        else:
            post_step = raw_step

        boundary_steps.append(
            {
                "seam": f"{k + 1}->{k + 2}",
                "class": b_spec.kind,
                "question": b_spec.question_tail,
                "pause_s": b_spec.base_pause_sec,
                "raw_step_st": round(raw_step, 3),
                "post_step_st": round(post_step, 3),
            }
        )

    raw_iqrs = [a.stats.f0_iqr_st for a in analyses if a.stats.f0_iqr_st > 0.0]
    post_iqrs = [a.stats.f0_iqr_st for a in post_analyses if a.stats.f0_iqr_st > 0.0]
    raw_mean_iqr = float(np.mean(raw_iqrs)) if raw_iqrs else 0.0
    post_mean_iqr = float(np.mean(post_iqrs)) if post_iqrs else 0.0

    return PostLoopProsodyResult(
        corrected_parts=corrected_parts,
        pauses_sec=[b.pause_sec for b in boundaries],
        anchor=anchor,
        post_stats=[a.stats for a in post_analyses],
        corrections_applied=corrections_applied,
        envelope_corrections=envelope_corrections,
        edge_fallbacks=edge_fallbacks,
        boundary_steps=boundary_steps,
        raw_mean_iqr_st=round(raw_mean_iqr, 3),
        post_mean_iqr_st=round(post_mean_iqr, 3),
    )
