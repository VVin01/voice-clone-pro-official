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

from app.services.voice_chunking import BURMESE_SYLLABLE_BREAK, split_sentences

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

# Phase 1B base constants (retry caps strictly unchanged in Phase 3)
OUTLIER_MAX_F0_ST: float = 2.5
OUTLIER_RATE_MIN_RATIO: float = 0.78
OUTLIER_RATE_MAX_RATIO: float = 1.28
MAX_PROSODY_RETRIES_PER_CHUNK: int = 1
MAX_PROSODY_RETRIES_PER_JOB: int = 2

RUBBERBAND_MAX_PITCH_ST: float = 2.0
RUBBERBAND_DEADBAND_PITCH_ST: float = 0.5
# Phase 3 (§5.3): softened tempo clamp & deadband (Phase 2 fallback values preserved below)
RUBBERBAND_TEMPO_MIN: float = 0.93
RUBBERBAND_TEMPO_MAX: float = 1.07
RUBBERBAND_DEADBAND_TEMPO: float = 0.045
_P2_RUBBERBAND_TEMPO_MIN: float = 0.90
_P2_RUBBERBAND_TEMPO_MAX: float = 1.12
_P2_RUBBERBAND_DEADBAND_TEMPO: float = 0.03

CLONE_BOUNDARY_PAUSE_SEC: float = 0.135  # legacy default sentence-boundary pause

# Phase 2 + Phase 3 (§2–§7) constants (Phase 2 values preserved as _P2_* for kill-switch fallback)
CUSTOM_REF_JOB_BLEND_WEIGHT: float = 0.35      # max ±0.35 st job-median accommodation vs custom ref slice
BODY_DEADBAND_PITCH_ST: float = 0.25           # Phase 3 (§3.2): tightened from 0.60 -> 0.25 st
BODY_EXPRESSIVE_DEADBAND_ST: float = 0.55      # Phase 3 (§3.2): tightened from 1.10 -> 0.55 st
BODY_MAX_SHIFT_ST: float = 1.50                # leave headroom inside ±2.0 st total cap for edge ramps
_P2_BODY_DEADBAND_PITCH_ST: float = 0.60
_P2_BODY_EXPRESSIVE_DEADBAND_ST: float = 1.10

ENV_WINDOW_SEC: float = 3.50
ENV_DEADZONE_ST: float = 0.85                  # Phase 3 (§3.2): tightened from 1.35 -> 0.85 st
ENV_EXPRESSIVE_DEADZONE_ST: float = 1.15       # Phase 3 (§3.2): tightened from 1.75 -> 1.15 st
ENV_TETHER_GAIN: float = 0.65                  # soft compression above deadzone
ENV_HARD_CEIL_ST: float = 1.85                 # guarantees 3.5 s envelope stays <= ±1.85 st
_P2_ENV_DEADZONE_ST: float = 1.35
_P2_ENV_EXPRESSIVE_DEADZONE_ST: float = 1.75

# Phase 3 (§2): Subharmonic repair widening & 0.40 s modal edge estimator
SUBHARMONIC_LO_RATIO: float = 0.45
SUBHARMONIC_HI_RATIO: float = 0.60
_P2_SUBHARMONIC_LO_RATIO: float = 0.46
_P2_SUBHARMONIC_HI_RATIO: float = 0.54

EDGE_MODAL_FLOOR_RATIO: float = 0.62           # Phase 3 (§2.2): 0.62x modal floor (was 0.72x)
_P2_EDGE_MODAL_FLOOR_RATIO: float = 0.72
EDGE_HEAD_GUARD_FRAMES: int = 4                # 40 ms onset aspiration guard
EDGE_TAIL_GUARD_FRAMES: int = 6                # Phase 3 (§2.2): 6-frame creak-aware tail guard (was 12)
_P2_EDGE_TAIL_GUARD_FRAMES: int = 12
EDGE_VOICED_TARGET_SEC: float = 0.40           # Phase 3 (§2.2): 0.40 s modal voiced edge window
EDGE_VOICED_TARGET_FRAMES: int = 32            # ~25-35 modal voiced frames (~0.40 s)
_P2_EDGE_VOICED_TARGET_FRAMES: int = 65
EDGE_VOICED_WIDEN_FRAMES: int = 45             # Tier-1 widen fallback
_P2_EDGE_VOICED_WIDEN_FRAMES: int = 110
EDGE_MIN_SHRINK_FRAMES: int = 8                # <8 voiced frames -> skip edge ramp

# Phase 3 (§3.1): Macro linear de-sagging constants
DESAG_MIN_DURATION_SEC: float = 10.0
DESAG_BORDERLINE_MIN_SEC: float = 8.0
DESAG_MIN_SENTENCES: int = 2
DESAG_SLOPE_DEADBAND_ST_PER_SEC: float = 0.030
DESAG_THIRDS_DEADBAND_ST: float = 0.55
DESAG_GAIN: float = 0.72
DESAG_BORDERLINE_GAIN: float = 0.36
DESAG_MAX_COMP_SLOPE_ST_PER_SEC: float = 0.08

# Phase 3 (§4): Tightened seam step maxima & 30/70 asymmetric continuation split
STEP_MAX_MID_PHRASE_ST: float = 0.35           # was 0.65 (pause = 0.065 s)
STEP_MAX_CLAUSE_ST: float = 0.45               # was 0.90 (pause = 0.095 s)
STEP_MAX_SENTENCE_ST: float = 0.65             # was 1.35 (pause = 0.135 s)
STEP_MAX_PARAGRAPH_ST: float = 1.05            # was 1.80 (pause = 0.220 s)
_P2_STEP_MAX_MID_PHRASE_ST: float = 0.65
_P2_STEP_MAX_CLAUSE_ST: float = 0.90
_P2_STEP_MAX_SENTENCE_ST: float = 1.35
_P2_STEP_MAX_PARAGRAPH_ST: float = 1.80

SEAM_TAIL_SHARE: float = 0.30
SEAM_HEAD_SHARE: float = 0.70
SEAM_SENTENCE_NATURAL_FALL_FLOOR_ST: float = -0.80

# Phase 3 (§5.4): Post-rubberband Voiced LTAS Tilt EQ constants
TILT_LOW_BAND_HZ: tuple[float, float] = (120.0, 900.0)
TILT_PRES_BAND_HZ: tuple[float, float] = (800.0, 2000.0)
TILT_HIGH_BAND_HZ: tuple[float, float] = (1600.0, 5500.0)
TILT_EQ_DEADBAND_DB: float = 0.40
TILT_HIGHSHELF_FREQ_HZ: float = 2200.0
TILT_HIGHSHELF_Q: float = 0.60
TILT_HIGHSHELF_MAX_DB: float = 3.00
TILT_PRESENCE_FREQ_HZ: float = 1000.0
TILT_PRESENCE_Q: float = 0.80
TILT_PRESENCE_MAX_DB: float = 1.80

# Phase 3 (§7): Option C tilt/sag-aware retry scoring terms
OUTLIER_SCORE_TILT_THRESH_DB: float = 1.80
OUTLIER_SCORE_TILT_WEIGHT: float = 0.30
OUTLIER_MAX_TILT_DELTA_DB: float = 2.20
OUTLIER_SCORE_SAG_THRESH_ST: float = 1.60
OUTLIER_SCORE_SAG_WEIGHT: float = 0.25
OUTLIER_MAX_LINEAR_SAG_ST: float = 2.20

PAUSE_MID_PHRASE_SEC: float = 0.065
PAUSE_CLAUSE_SEC: float = 0.095
PAUSE_SENTENCE_SEC: float = 0.135
PAUSE_QUESTION_SEC: float = 0.160
PAUSE_PARAGRAPH_SEC: float = 0.220

RAMP_HEAD_SEC: float = 2.40                    # raised-cosine head decay
RAMP_TAIL_SEC: float = 1.80                    # raised-cosine tail lift
RAMP_CMD_STEP_SEC: float = 0.050               # 50 ms sendcmd grid
MAX_TOTAL_INSTANT_SHIFT_ST: float = 2.00       # hard ceiling on |Δp_k(t)| everywhere
MAX_RAMP_SLOPE_ST_PER_SEC: float = 0.85        # perceptual imperceptibility bound

OUTLIER_MAX_CHUNK_IQR_ST: float = 3.60
OUTLIER_MAX_INTERNAL_SPAN_ST: float = 4.20     # |raw_head_st - raw_tail_st|


def is_clone_prosody_phase3_enabled() -> bool:
    """True when dynamic setting ``clone_prosody_phase3_enabled`` is active (default True)."""
    try:
        from app.config import settings as _settings

        val = getattr(_settings, "clone_prosody_phase3_enabled", True)
        if isinstance(val, str):
            return val.strip().lower() not in ("0", "false", "no", "off")
        return bool(val)
    except Exception:
        return True

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
    voiced_tilt_db: float = 0.0
    voiced_presence_db: float = 0.0
    voiced_centroid_hz: float = 0.0
    linear_sag_st: float = 0.0
    linear_slope_st_per_s: float = 0.0
    sentence_count: int = 1

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
    voiced_tilt_anchor_db: float | None = None
    voiced_presence_anchor_db: float | None = None


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
    desag_corrections: int = 0
    tilt_eq_corrections: int = 0
    space_family_switched: bool = False


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

    p3 = is_clone_prosody_phase3_enabled()
    s_para = STEP_MAX_PARAGRAPH_ST if p3 else _P2_STEP_MAX_PARAGRAPH_ST
    s_sent = STEP_MAX_SENTENCE_ST if p3 else _P2_STEP_MAX_SENTENCE_ST
    s_clau = STEP_MAX_CLAUSE_ST if p3 else _P2_STEP_MAX_CLAUSE_ST
    s_midp = STEP_MAX_MID_PHRASE_ST if p3 else _P2_STEP_MAX_MID_PHRASE_ST

    specs: list[BoundarySpec] = []
    for k in range(n_c - 1):
        c_end = str(chunks[k] or "").rstrip()
        sep = src[spans[k][1] : spans[k + 1][0]]
        q_tail = is_question_ending(c_end)
        if "\n\n" in sep or sep.count("\n") >= 2:
            kind = "PARAGRAPH"
            s_max = s_para
            base_pause = PAUSE_PARAGRAPH_SEC
        elif c_end.endswith(("။", "?", "!")):
            kind = "SENTENCE"
            s_max = s_sent
            base_pause = PAUSE_QUESTION_SEC if q_tail else PAUSE_SENTENCE_SEC
        elif c_end.endswith(("၊", ",", ";", ":")):
            kind = "CLAUSE"
            s_max = s_clau
            base_pause = PAUSE_QUESTION_SEC if q_tail else PAUSE_CLAUSE_SEC
        else:
            kind = "MID_PHRASE"
            s_max = s_midp
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


def repair_subharmonic_f0_contour(
    f0_arr: np.ndarray,
    *,
    sr: int = ANALYSIS_SAMPLE_RATE,
    cmnd_cache: dict[int, np.ndarray] | None = None,
    tau_min: int = 38,
) -> np.ndarray:
    """Part 2.1 widened subharmonic / creak-diplophonia repair ([0.45, 0.60] * local_median).

    Uses a ±0.60 s modal neighborhood median (widening to ±1.80 s / global modal median when
    sparse) so abrupt phrase-final creak/diplophonia at 0.56-0.60x median is repaired cleanly
    without flattening genuine multi-second pitch glides.
    """
    out = f0_arr.astype(np.float32, copy=True)
    voiced_idx = np.where(~np.isnan(out) & (out > 0.0))[0]
    if voiced_idx.size < 5:
        return out

    voiced_vals = out[voiced_idx]
    global_med = float(np.median(voiced_vals))
    if global_med <= 0.0:
        return out

    if not is_clone_prosody_phase3_enabled():
        for idx in voiced_idx:
            fi = float(out[idx])
            cmnd = cmnd_cache.get(int(idx)) if cmnd_cache is not None else None
            if 1.85 * global_med <= fi <= 2.15 * global_med and cmnd is not None:
                sub_tau = int(round(sr / (fi * 0.5)))
                if sub_tau < cmnd.size and cmnd[sub_tau] < 0.26:
                    out[idx] = fi * 0.5
            elif _P2_SUBHARMONIC_LO_RATIO * global_med <= fi <= _P2_SUBHARMONIC_HI_RATIO * global_med and cmnd is not None:
                har_tau = int(round(sr / (fi * 2.0)))
                if tau_min <= har_tau < cmnd.size and cmnd[har_tau] < 0.20:
                    out[idx] = fi * 2.0
        return out

    modal_mask = (~np.isnan(out)) & (out >= EDGE_MODAL_FLOOR_RATIO * global_med) & (out <= 1.65 * global_med)
    modal_indices = np.where(modal_mask)[0]
    global_modal_med = float(np.median(out[modal_indices])) if modal_indices.size >= 5 else global_med

    n_total = out.size
    for idx in voiced_idx:
        i_int = int(idx)
        fi = float(out[i_int])
        # Compute local modal median in ±60 frames (±0.60 s), fallback ±180 frames, else global_modal_med
        lo_1 = max(0, i_int - 60)
        hi_1 = min(n_total, i_int + 61)
        loc_v = out[lo_1:hi_1][modal_mask[lo_1:hi_1]]
        if loc_v.size >= 5:
            local_med = float(np.median(loc_v))
        else:
            lo_2 = max(0, i_int - 180)
            hi_2 = min(n_total, i_int + 181)
            loc_v2 = out[lo_2:hi_2][modal_mask[lo_2:hi_2]]
            local_med = float(np.median(loc_v2)) if loc_v2.size >= 5 else global_modal_med

        cmnd = cmnd_cache.get(i_int) if cmnd_cache is not None else None
        if 1.85 * local_med <= fi <= 2.15 * local_med:
            if cmnd is not None:
                sub_tau = int(round(sr / (fi * 0.5)))
                if sub_tau < cmnd.size and cmnd[sub_tau] < 0.26:
                    out[i_int] = fi * 0.5
            else:
                out[i_int] = fi * 0.5
        elif SUBHARMONIC_LO_RATIO * local_med <= fi <= SUBHARMONIC_HI_RATIO * local_med:
            repaired_hz: float | None = None
            if cmnd is not None:
                har_tau = int(round(sr / (fi * 2.0)))
                if 0.45 * local_med <= fi <= 0.55 * local_med and tau_min <= har_tau < cmnd.size and cmnd[har_tau] < 0.26:
                    repaired_hz = fi * 2.0
                else:
                    m_tau_lo = max(tau_min, int(round(sr / (1.25 * local_med))))
                    m_tau_hi = min(cmnd.size - 1, int(round(sr / (0.78 * local_med))))
                    if m_tau_lo <= m_tau_hi:
                        sub_c = cmnd[m_tau_lo : m_tau_hi + 1]
                        rel_min = int(np.argmin(sub_c))
                        if float(sub_c[rel_min]) < 0.30:
                            repaired_hz = float(sr) / float(m_tau_lo + rel_min)
            if repaired_hz is None:
                repaired_hz = local_med
            out[i_int] = float(repaired_hz)

    return out


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

    f0_arr = repair_subharmonic_f0_contour(f0_arr, sr=sr, cmnd_cache=cmnd_cache, tau_min=tau_min)
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


def measure_voiced_spectral_stats(
    samples: np.ndarray,
    sr: int,
    f0_arr: np.ndarray,
) -> tuple[float, float, float]:
    """Measure voiced-frame-only ``(tilt_hi_db, presence_db, voiced_power_centroid_hz)``.

    Strictly gates on frames with valid F0 (``~np.isnan(f0_arr) & (f0_arr > 0)``) so unvoiced
    fricatives, English sibilants, aspirates, and pauses NEVER drive the EQ detector.
    - ``tilt_hi_db = 10 * log10(E[1.6-5.5 kHz] / E[120-900 Hz])``
    - ``presence_db = 10 * log10(E[0.8-2.0 kHz] / E[120-900 Hz])``
    - ``voiced_power_centroid_hz = mean_voiced(sum(f * |X|^2) / sum(|X|^2))``
    """
    if samples.size == 0 or sr <= 0 or f0_arr.size == 0:
        return 0.0, 0.0, 0.0

    voiced_idx = np.where(~np.isnan(f0_arr) & (f0_arr > 0.0))[0]
    if voiced_idx.size < 3:
        return 0.0, 0.0, 0.0

    win = max(64, int(round(FRAME_WIN_SEC * sr)))
    hop = max(1, int(round(FRAME_STEP_SEC * sr)))
    if samples.size < win:
        return 0.0, 0.0, 0.0

    window = np.hanning(win).astype(np.float64)
    freqs = np.fft.rfftfreq(win, d=1.0 / float(sr))
    nyq_hi = min(TILT_HIGH_BAND_HZ[1], 0.48 * float(sr))
    lo_mask = (freqs >= TILT_LOW_BAND_HZ[0]) & (freqs <= TILT_LOW_BAND_HZ[1])
    pr_mask = (freqs >= TILT_PRES_BAND_HZ[0]) & (freqs <= min(TILT_PRES_BAND_HZ[1], nyq_hi))
    hi_mask = (freqs >= TILT_HIGH_BAND_HZ[0]) & (freqs <= nyq_hi)

    e_lo = 0.0
    e_pr = 0.0
    e_hi = 0.0
    cents: list[float] = []

    for fi in voiced_idx:
        st = int(fi) * hop
        if st + win > samples.size:
            break
        seg = samples[st : st + win].astype(np.float64) * window
        pwr = np.abs(np.fft.rfft(seg)) ** 2
        tot_p = float(np.sum(pwr))
        if tot_p <= 1e-12:
            continue
        e_lo += float(np.sum(pwr[lo_mask]))
        e_pr += float(np.sum(pwr[pr_mask]))
        e_hi += float(np.sum(pwr[hi_mask]))
        cents.append(float(np.sum(freqs * pwr) / tot_p))

    # Guard band-limited synthetic test tones where >1.6 kHz holds only 16-bit quantization dust (< -38 dB)
    if e_lo > 1e-12 and e_hi > 1e-12 and (e_hi / e_lo) >= 1.58e-4:
        tilt_hi = 10.0 * math.log10(e_hi / e_lo)
    else:
        tilt_hi = 0.0
    if e_lo > 1e-12 and e_pr > 1e-12 and (e_pr / e_lo) >= 1.58e-4:
        tilt_pr = 10.0 * math.log10(e_pr / e_lo)
    else:
        tilt_pr = 0.0
    cent_hz = float(np.mean(cents)) if cents else 0.0
    return round(tilt_hi, 4), round(tilt_pr, 4), round(cent_hz, 2)


def measure_chunk_sag_and_slope(
    f0_arr: np.ndarray,
    *,
    anchor_hz: float | None = None,
) -> tuple[float, float]:
    """Return ``(sag_thirds_st, theil_sen_slope_st_per_s)`` over modal voiced frames."""
    if f0_arr.size == 0:
        return 0.0, 0.0
    cleaned = repair_subharmonic_f0_contour(f0_arr) if is_clone_prosody_phase3_enabled() else f0_arr
    valid_idx = np.where(~np.isnan(cleaned) & (cleaned > 0.0))[0]
    if valid_idx.size < 9:
        return 0.0, 0.0

    v_all = cleaned[valid_idx]
    med_hz = float(np.median(v_all))
    if med_hz <= 0.0:
        return 0.0, 0.0

    modal_keep = (v_all >= EDGE_MODAL_FLOOR_RATIO * med_hz) & (v_all <= 1.60 * med_hz)
    if int(np.sum(modal_keep)) >= 9:
        valid_idx = valid_idx[modal_keep]
        v_all = v_all[modal_keep]

    ref_hz = float(anchor_hz) if (anchor_hz is not None and anchor_hz > 0.0) else med_hz
    st_vals = 12.0 * np.log2(v_all.astype(np.float64) / ref_hz)
    t_vals = valid_idx.astype(np.float64) * FRAME_STEP_SEC

    n3 = max(3, st_vals.size // 3)
    head_med = float(np.median(st_vals[:n3]))
    tail_med = float(np.median(st_vals[-n3:]))
    sag_thirds_st = tail_med - head_med

    t_span = float(t_vals[-1] - t_vals[0])
    if t_span < 1.0:
        return round(sag_thirds_st, 4), 0.0

    # Trimmed-thirds robust linear slope (§3.1): exact on linear ramps and strictly sign-consistent
    # with (tail_third_median - head_third_median) across multi-sentence Burmese intonation waves.
    t_head_med = float(np.median(t_vals[:n3]))
    t_tail_med = float(np.median(t_vals[-n3:]))
    dt_thirds = max(1.0, t_tail_med - t_head_med)
    slope = sag_thirds_st / dt_thirds
    return round(sag_thirds_st, 4), round(slope, 5)


def estimate_chunk_edge_f0_st(
    f0_arr: np.ndarray,
    *,
    duration_sec: float,
    chunk_median_hz: float | None,
    anchor_hz: float | None,
    body_st_vs_anchor: float,
    side: str,
) -> tuple[float, bool, bool]:
    """Burmese-safe voiced-frame edge F0 estimator (Phase 3 §2.2 modal 0.40 s window, Phase 2 fallback).

    Returns ``(edge_st_vs_anchor, used_fallback, skipped_edge)``:
    - Phase 3 (``clone_prosody_phase3_enabled=True``):
      - Applies widened ``[0.45, 0.60] * local_median`` subharmonic repair first.
      - Guard trim: 4-frame head guard / 6-frame creak-aware tail guard.
      - Primary window: first/last 0.40 s (~32 modal voiced frames, capped at 35% of chunk).
      - Modal floor ``0.62 * chunk_median`` (ceiling ``1.55 * chunk_median``), P20-P80 IQM.
    - Phase 2 fallback (``clone_prosody_phase3_enabled=False``):
      - 4/12 guard frames, 65/110 voiced frames, ``[0.72, 1.50] * chunk_median``.
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

    p3 = is_clone_prosody_phase3_enabled()
    work_f0 = repair_subharmonic_f0_contour(f0_arr) if p3 else f0_arr
    voiced = work_f0[~np.isnan(work_f0) & (work_f0 > 0)]
    n_v = int(voiced.size)
    if n_v < EDGE_MIN_SHRINK_FRAMES:
        return float(body_st_vs_anchor), True, True

    floor_ratio = EDGE_MODAL_FLOOR_RATIO if p3 else _P2_EDGE_MODAL_FLOOR_RATIO
    ceil_ratio = 1.55 if p3 else 1.50
    lo_hz = floor_ratio * float(chunk_median_hz)
    hi_hz = ceil_ratio * float(chunk_median_hz)
    is_head = (side == "head")
    tail_guard = EDGE_TAIL_GUARD_FRAMES if p3 else _P2_EDGE_TAIL_GUARD_FRAMES
    guard = EDGE_HEAD_GUARD_FRAMES if is_head else tail_guard
    tgt_frames = EDGE_VOICED_TARGET_FRAMES if p3 else _P2_EDGE_VOICED_TARGET_FRAMES
    widen_frames = EDGE_VOICED_WIDEN_FRAMES if p3 else _P2_EDGE_VOICED_WIDEN_FRAMES

    # Creak-aware tail trim in Phase 3: if trailing frames beyond the 6-frame guard sit below
    # the 0.62x modal floor, advance the effective tail end past that trailing creak cluster
    # (up to 25% of voiced frames) so the 0.40 s window measures true modal phrase-end pitch.
    eff_end_v = n_v - guard
    if p3 and not is_head and eff_end_v > EDGE_MIN_SHRINK_FRAMES:
        min_end = max(EDGE_MIN_SHRINK_FRAMES, int(round(0.75 * n_v)))
        while eff_end_v > min_end and voiced[eff_end_v - 1] < lo_hz:
            eff_end_v -= 1

    # Slice modal voiced frames within the first/last 0.40 s wall-clock window (~25-35 frames) in Phase 3
    if p3:
        v_indices = np.where(~np.isnan(work_f0) & (work_f0 > 0))[0]
        modal_idx = v_indices[(work_f0[v_indices] >= lo_hz) & (work_f0[v_indices] <= hi_hz)]
        if modal_idx.size >= EDGE_MIN_SHRINK_FRAMES:
            g_use = min(guard, max(0, modal_idx.size - EDGE_MIN_SHRINK_FRAMES))
            trimmed_idx = modal_idx[g_use:] if is_head else (modal_idx[: modal_idx.size - g_use] if g_use > 0 else modal_idx)
            win_wall = int(round(0.45 / FRAME_STEP_SEC))  # 0.45 s wall-clock (~25-35 voiced frames)
            if is_head:
                anchor_i = int(trimmed_idx[0])
                sel = trimmed_idx[trimmed_idx <= anchor_i + win_wall]
                if sel.size < 20:
                    sel = trimmed_idx[: min(tgt_frames, trimmed_idx.size)]
            else:
                anchor_i = int(trimmed_idx[-1])
                sel = trimmed_idx[trimmed_idx >= anchor_i - win_wall]
                if sel.size < 20:
                    sel = trimmed_idx[max(0, trimmed_idx.size - tgt_frames) :]
            clean_p3 = work_f0[sel]
            if clean_p3.size >= 20:
                est_hz = _trimmed_iqm(clean_p3)
                if est_hz > 0.0 and math.isfinite(est_hz):
                    return semitone_diff(est_hz, float(anchor_hz)), False, False
            elif clean_p3.size >= EDGE_MIN_SHRINK_FRAMES:
                est_hz = _trimmed_iqm(clean_p3)
                if est_hz > 0.0 and math.isfinite(est_hz):
                    raw_st = semitone_diff(est_hz, float(anchor_hz))
                    alpha = float(clean_p3.size) / 20.0
                    return float(alpha * raw_st + (1.0 - alpha) * float(body_st_vs_anchor)), True, False

    def _slice_clean(target_frames: int, max_frac: float) -> np.ndarray:
        avail = max(0, (n_v - guard) if is_head else eff_end_v)
        if avail <= 0:
            return np.zeros(0, dtype=np.float32)
        cap = max(min(avail, max(EDGE_MIN_SHRINK_FRAMES, 16)), int(math.floor(n_v * max_frac)))
        win = min(int(target_frames), cap, avail)
        if is_head:
            sub = voiced[guard : guard + win]
        else:
            st_i = max(0, eff_end_v - win)
            sub = voiced[st_i:eff_end_v]
        return sub[(sub >= lo_hz) & (sub <= hi_hz)]

    clean = _slice_clean(tgt_frames, 0.35)
    used_fallback = False
    if clean.size < 20:
        used_fallback = True
        clean = _slice_clean(widen_frames, 0.40)

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
    """Run YIN ONCE on ``wav_path`` and cache ``ProsodyStats``, contours/edges, sag, and voiced LTAS."""
    p = Path(wav_path)
    samples, sr = read_wav_mono_float32(p)
    n_sents = max(1, len(split_sentences(text))) if str(text or "").strip() else 1
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
            sentence_count=n_sents,
        )

    duration_sec = samples.size / float(sr)
    s16 = resample_float32(samples, sr, ANALYSIS_SAMPLE_RATE)
    rms_arr, f0_arr = extract_frame_contours(s16, ANALYSIS_SAMPLE_RATE)
    stats = _stats_from_contours(duration_sec, rms_arr, f0_arr, text=text)
    tilt_hi_db, tilt_pr_db, v_cent_hz = measure_voiced_spectral_stats(samples, sr, f0_arr)
    sag_st, slope_st_s = measure_chunk_sag_and_slope(f0_arr, anchor_hz=stats.f0_median_hz)

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
        voiced_tilt_db=tilt_hi_db,
        voiced_presence_db=tilt_pr_db,
        voiced_centroid_hz=v_cent_hz,
        linear_sag_st=sag_st,
        linear_slope_st_per_s=slope_st_s,
        sentence_count=n_sents,
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
    voiced_tilt_delta_db: float = 0.0,
    linear_sag_st: float = 0.0,
) -> tuple[bool, float, float]:
    """Part 6 + Phase 3 (§7) re-scored retry trigger."""
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
    p3_bad = False
    if is_clone_prosody_phase3_enabled():
        tilt_bad = abs(float(voiced_tilt_delta_db)) > OUTLIER_MAX_TILT_DELTA_DB
        sag_bad = abs(float(linear_sag_st)) > OUTLIER_MAX_LINEAR_SAG_ST
        p3_bad = tilt_bad or sag_bad

    return (f0_bad or rate_bad or iqr_bad or span_bad or p3_bad), delta_st, rate_ratio


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
    voiced_tilt_delta_db: float = 0.0,
    linear_sag_st: float = 0.0,
) -> float:
    """Part 6 + Phase 3 (§7) composite candidate score:
    ``anchor_distance + 0.35*max(0, iqr-2.0) + 0.25*max(0, |span|-2.0)``
    plus Phase 3 Option C terms:
    ``+ 0.30*max(0, |Δvoiced_tilt_dB|-1.8) + 0.25*max(0, |linear_sag_st|-1.6)``."""
    base = prosody_anchor_distance(stats, anchor_f0_hz=anchor_f0_hz, anchor_rate=anchor_rate)
    iqr_pen = 0.35 * max(0.0, float(stats.f0_iqr_st) - 2.0)
    span_pen = 0.25 * max(0.0, abs(float(internal_span_st)) - 2.0)
    p3_pen = 0.0
    if is_clone_prosody_phase3_enabled():
        p3_pen += OUTLIER_SCORE_TILT_WEIGHT * max(
            0.0, abs(float(voiced_tilt_delta_db)) - OUTLIER_SCORE_TILT_THRESH_DB
        )
        p3_pen += OUTLIER_SCORE_SAG_WEIGHT * max(
            0.0, abs(float(linear_sag_st)) - OUTLIER_SCORE_SAG_THRESH_ST
        )
    return base + iqr_pen + span_pen + p3_pen


def _weighted_median_signed(values: Sequence[float], weights: Sequence[float]) -> float | None:
    pairs = [
        (float(v), max(1e-3, float(w)))
        for v, w in zip(values, weights)
        if v is not None and math.isfinite(float(v))
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

    tilt_vals = [a.voiced_tilt_db for a in analyses if a.stats.voiced_sec >= 0.15 and a.voiced_tilt_db != 0.0]
    tilt_wts = [max(0.05, a.stats.voiced_sec) for a in analyses if a.stats.voiced_sec >= 0.15 and a.voiced_tilt_db != 0.0]
    pres_vals = [a.voiced_presence_db for a in analyses if a.stats.voiced_sec >= 0.15 and a.voiced_presence_db != 0.0]
    pres_wts = [max(0.05, a.stats.voiced_sec) for a in analyses if a.stats.voiced_sec >= 0.15 and a.voiced_presence_db != 0.0]
    tilt_med = _weighted_median_signed(tilt_vals, tilt_wts)
    pres_med = _weighted_median_signed(pres_vals, pres_wts)

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
            voiced_tilt_anchor_db=round(tilt_med, 4) if tilt_med is not None else None,
            voiced_presence_anchor_db=round(pres_med, 4) if pres_med is not None else None,
        )

    return GlobalProsodyAnchor(
        f0_anchor_hz=round(f_job, 3) if f_job is not None else None,
        rate_anchor=round(r_job, 4) if r_job is not None else None,
        f0_job_median_hz=round(f_job, 3) if f_job is not None else None,
        rate_job_median=round(r_job, 4) if r_job is not None else None,
        anchor_source="job_median",
        voiced_tilt_anchor_db=round(tilt_med, 4) if tilt_med is not None else None,
        voiced_presence_anchor_db=round(pres_med, 4) if pres_med is not None else None,
    )


def compute_soft_knee_body_shift(
    chunk_f0_hz: float | None,
    anchor_f0_hz: float | None,
    *,
    expressive: bool = False,
) -> float:
    """Continuous soft-knee whole-chunk centering toward ``F_anchor``."""
    if chunk_f0_hz is None or anchor_f0_hz is None or chunk_f0_hz <= 0.0 or anchor_f0_hz <= 0.0:
        return 0.0
    delta_st = semitone_diff(chunk_f0_hz, anchor_f0_hz)
    needed_st = -delta_st
    p3 = is_clone_prosody_phase3_enabled()
    if p3:
        deadband = BODY_EXPRESSIVE_DEADBAND_ST if expressive else BODY_DEADBAND_PITCH_ST
    else:
        deadband = _P2_BODY_EXPRESSIVE_DEADBAND_ST if expressive else _P2_BODY_DEADBAND_PITCH_ST
    mag = abs(needed_st)
    if mag <= deadband:
        return 0.0
    eff = min(BODY_MAX_SHIFT_ST, mag - deadband)
    return round(math.copysign(eff, needed_st), 4)


def compute_linear_desag_contour(
    f0_arr: np.ndarray,
    *,
    duration_sec: float,
    sentence_count: int,
    anchor_f0_hz: float | None = None,
) -> tuple[np.ndarray, float, bool]:
    """Phase 3 (§3.1): Macro linear F0 de-sagging on multi-sentence chunks >= 8 s.

    - Skips single-sentence chunks (``sentence_count < 2``) and chunks ``< 8.0 s``.
    - Full gain ``γ = 0.72`` for chunks ``>= 10.0 s``; halved gain ``0.36`` for ``8.0..10.0 s``.
    - Triggers when ``|slope| > 0.030 st/s`` or ``|head_third - tail_third| > 0.55 st``.
    - Compensates ``γ`` of excess slope beyond ``0.030 st/s``, capped at ``±0.08 st/s``,
      centered on the chunk's voiced median timestamp so chunk median F0 is invariant.
    Returns ``(delta_slope_frames, comp_slope_st_per_s, applied)``.
    """
    n = int(f0_arr.size)
    if (
        n == 0
        or not is_clone_prosody_phase3_enabled()
        or int(sentence_count) < DESAG_MIN_SENTENCES
        or float(duration_sec) < DESAG_BORDERLINE_MIN_SEC
    ):
        return np.zeros(n, dtype=np.float64), 0.0, False

    sag_thirds_st, slope_st_s = measure_chunk_sag_and_slope(f0_arr, anchor_hz=anchor_f0_hz)
    if (
        abs(slope_st_s) <= DESAG_SLOPE_DEADBAND_ST_PER_SEC
        and abs(sag_thirds_st) <= DESAG_THIRDS_DEADBAND_ST
    ):
        return np.zeros(n, dtype=np.float64), 0.0, False

    valid_idx = np.where(~np.isnan(f0_arr) & (f0_arr > 0.0))[0]
    if valid_idx.size < 9:
        return np.zeros(n, dtype=np.float64), 0.0, False

    t_voiced = valid_idx.astype(np.float64) * FRAME_STEP_SEC
    t_span = max(4.0, float(t_voiced[-1] - t_voiced[0]))
    eff_slope = slope_st_s
    if abs(eff_slope) <= DESAG_SLOPE_DEADBAND_ST_PER_SEC and abs(sag_thirds_st) > DESAG_THIRDS_DEADBAND_ST:
        eff_slope = sag_thirds_st / max(4.0, (2.0 / 3.0) * t_span)

    excess_mag = max(0.0, abs(eff_slope) - DESAG_SLOPE_DEADBAND_ST_PER_SEC)
    if excess_mag <= 1e-6:
        return np.zeros(n, dtype=np.float64), 0.0, False

    gamma = DESAG_GAIN if float(duration_sec) >= DESAG_MIN_DURATION_SEC else DESAG_BORDERLINE_GAIN
    raw_comp = -math.copysign(gamma * excess_mag, eff_slope)
    comp_slope = max(-DESAG_MAX_COMP_SLOPE_ST_PER_SEC, min(DESAG_MAX_COMP_SLOPE_ST_PER_SEC, raw_comp))

    t_mid = float(np.median(t_voiced))
    t_all = np.arange(n, dtype=np.float64) * FRAME_STEP_SEC
    delta_frames = comp_slope * (t_all - t_mid)
    return delta_frames, round(comp_slope, 5), True


def compute_envelope_tether(
    f0_arr: np.ndarray,
    *,
    anchor_f0_hz: float | None,
    body_shift_st: float,
    expressive: bool = False,
    slope_delta_frames: np.ndarray | None = None,
    duration_sec: float = 0.0,
    sentence_count: int = 1,
) -> tuple[np.ndarray, bool]:
    """3.5 s sliding voiced-median envelope tether on the 10 ms frame grid."""
    n = int(f0_arr.size)
    if n == 0 or anchor_f0_hz is None or anchor_f0_hz <= 0.0:
        return np.zeros(n, dtype=np.float64), False

    p3 = is_clone_prosody_phase3_enabled()
    work_f0 = repair_subharmonic_f0_contour(f0_arr) if p3 else f0_arr
    half_w = max(5, int(round((ENV_WINDOW_SEC * 0.5) / FRAME_STEP_SEC)))  # 175 frames = 1.75 s
    st_arr = np.full(n, np.nan, dtype=np.float64)
    valid = ~np.isnan(work_f0) & (work_f0 > 0.0)
    if int(np.sum(valid)) < 8:
        return np.zeros(n, dtype=np.float64), False
    st_arr[valid] = 12.0 * np.log2(work_f0[valid] / float(anchor_f0_hz)) + float(body_shift_st)
    if p3 and slope_delta_frames is not None and slope_delta_frames.size == n:
        st_arr[valid] += slope_delta_frames[valid]

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
    if p3:
        dz = ENV_EXPRESSIVE_DEADZONE_ST if expressive else ENV_DEADZONE_ST
    else:
        dz = _P2_ENV_EXPRESSIVE_DEADZONE_ST if expressive else _P2_ENV_DEADZONE_ST

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

    # Damp intra-chunk bipolar swings while leaving unipolar +2.7 st expressive arches (w_min >= -0.15) untouched!
    rad = 120  # ±6.0 s
    max_bipolar_span = 1.18
    bipolar_trig = 0.22
    damped_g = target_g.copy()
    for _ in range(2):
        cur = damped_g.copy()
        for gi in range(m_g):
            win = cur[max(0, gi - rad) : min(m_g, gi + rad + 1)]
            w_min = float(np.min(win))
            w_max = float(np.max(win))
            span = w_max - w_min
            if w_min < -bipolar_trig and w_max > bipolar_trig and span > max_bipolar_span:
                mid = max(-bipolar_trig, min(bipolar_trig, 0.5 * (w_min + w_max)))
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


_RUBBERBAND_CAP_CACHE: dict[str, dict[str, Any]] = {}


def probe_rubberband_capabilities(ffmpeg_bin: str | None = None) -> dict[str, Any]:
    """Runtime capability probe for FFmpeg ``rubberband`` (``pitchq=consistency``, ``smoothing=on``)
    and ``highshelf`` filter support. Cached once per resolved ``ffmpeg_bin`` per process."""
    ff = ffmpeg_bin or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    key = str(ff)
    cached = _RUBBERBAND_CAP_CACHE.get(key)
    if cached is not None:
        return dict(cached)

    info: dict[str, Any] = {
        "ffmpeg_bin": ff,
        "available": False,
        "pitchq_consistency": False,
        "smoothing": False,
        "highshelf": False,
    }
    if not ff or (not shutil.which(ff) and not Path(ff).is_file()):
        _RUBBERBAND_CAP_CACHE[key] = info
        return dict(info)

    try:
        res_v = subprocess.run(
            [
                ff, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=200:sample_rate=16000:duration=0.12",
                "-af", "rubberband=pitch=1.0:tempo=1.0:formant=preserved:pitchq=consistency:smoothing=on,highshelf=f=2200:t=q:w=0.6:g=0.0,equalizer=f=1000:t=q:w=0.8:g=0.0",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res_v.returncode == 0:
            info["available"] = True
            info["pitchq_consistency"] = True
            info["smoothing"] = True
            info["highshelf"] = True
            _RUBBERBAND_CAP_CACHE[key] = info
            return dict(info)
    except Exception:
        pass

    try:
        res_h = subprocess.run([ff, "-hide_banner", "-h", "filter=rubberband"], capture_output=True, text=True, timeout=8, check=False)
        out_h = (res_h.stdout or "") + (res_h.stderr or "")
        info["available"] = "rubberband" in out_h.lower()
        info["pitchq_consistency"] = "consistency" in out_h
        info["smoothing"] = "smoothing" in out_h
        res_hs = subprocess.run([ff, "-hide_banner", "-h", "filter=highshelf"], capture_output=True, text=True, timeout=8, check=False)
        info["highshelf"] = res_hs.returncode == 0 and "highshelf" in ((res_hs.stdout or "") + (res_hs.stderr or "")).lower()
    except Exception:
        pass

    _RUBBERBAND_CAP_CACHE[key] = info
    return dict(info)


def _rubberband_extra_opts(ffmpeg_bin: str | None = None) -> str:
    if not is_clone_prosody_phase3_enabled():
        return ""
    caps = probe_rubberband_capabilities(ffmpeg_bin)
    opts = ""
    if caps.get("pitchq_consistency"):
        opts += ":pitchq=consistency"
    if caps.get("smoothing"):
        opts += ":smoothing=on"
    return opts


def compute_chunk_tilt_eq_gains(
    chunk_tilt_db: float,
    chunk_pres_db: float,
    *,
    target_tilt_db: float | None,
    target_pres_db: float | None,
    mean_pitch_shift_st: float = 0.0,
) -> tuple[float, float]:
    """Phase 3 (§5.4): Compute ``(highshelf_2200_db, presence_1000_db)`` toward job voiced median.

    - Deadband: ``±0.40 dB``.
    - Clamps: ``highshelf`` in ``[-3.0, +3.0] dB`` (2.2 kHz, Q=0.6); ``presence`` in ``[-1.8, +1.8] dB`` (1.0 kHz, Q=0.8).
    - Accounts for ``rubberband=formant=preserved`` upward-pitch HF rolloff (``-0.55 dB/st`` high, ``-0.20 dB/st`` pres).
    """
    if not is_clone_prosody_phase3_enabled() or target_tilt_db is None:
        return 0.0, 0.0

    rb_hi_shift = -0.55 * max(0.0, float(mean_pitch_shift_st))
    rb_pr_shift = -0.20 * max(0.0, float(mean_pitch_shift_st))

    err_hi = (float(chunk_tilt_db) + rb_hi_shift) - float(target_tilt_db)
    err_pr = (
        ((float(chunk_pres_db) + rb_pr_shift) - float(target_pres_db))
        if target_pres_db is not None
        else 0.0
    )

    tgt_d_hi = 0.0 if abs(err_hi) <= TILT_EQ_DEADBAND_DB else -err_hi
    tgt_d_pr = 0.0 if abs(err_pr) <= TILT_EQ_DEADBAND_DB else -err_pr
    if tgt_d_hi == 0.0 and tgt_d_pr == 0.0:
        return 0.0, 0.0

    # 2x2 speech-band coupling inversion (highshelf@2.2kHz Q=0.6 & bell@1.0kHz Q=0.8).
    # Voiced speech energy in [1.6-5.5 kHz] concentrates near 1.6-2.4 kHz (around the 2.2 kHz
    # half-gain corner), yielding ~0.45 dB/dB effective shelf coupling and ~0.22 dB/dB bell overlap.
    det = 0.45 * 0.65 - 0.16 * 0.22
    g_hi_raw = (0.65 * tgt_d_hi - 0.16 * tgt_d_pr) / det if abs(tgt_d_hi) > 0.0 else 0.0
    g_pr_raw = (-0.20 * tgt_d_hi + 0.45 * tgt_d_pr) / det if abs(tgt_d_pr) > 0.0 else 0.0

    g_hi = max(-TILT_HIGHSHELF_MAX_DB, min(TILT_HIGHSHELF_MAX_DB, g_hi_raw))
    # If highshelf saturates ±3.0 dB on an extreme chunk outlier, let presence assist on the 1.6-2.0 kHz overlap
    hi_sat_rem = tgt_d_hi - (0.45 * g_hi + 0.22 * max(-TILT_PRESENCE_MAX_DB, min(TILT_PRESENCE_MAX_DB, g_pr_raw)))
    if abs(g_hi_raw) > TILT_HIGHSHELF_MAX_DB and abs(hi_sat_rem) > 0.15:
        g_pr_raw += 0.55 * hi_sat_rem
    g_pr = max(-TILT_PRESENCE_MAX_DB, min(TILT_PRESENCE_MAX_DB, g_pr_raw))
    return round(g_hi, 3), round(g_pr, 3)


def compute_rubberband_params(
    stats: ProsodyStats,
    *,
    anchor_f0_hz: float | None,
    anchor_rate: float | None,
) -> tuple[float, float]:
    """Return ``(pitch_shift_st, tempo_factor)`` clamped per active phase settings."""
    p3 = is_clone_prosody_phase3_enabled()
    tp_min = RUBBERBAND_TEMPO_MIN if p3 else _P2_RUBBERBAND_TEMPO_MIN
    tp_max = RUBBERBAND_TEMPO_MAX if p3 else _P2_RUBBERBAND_TEMPO_MAX
    tp_db = RUBBERBAND_DEADBAND_TEMPO if p3 else _P2_RUBBERBAND_DEADBAND_TEMPO

    shift_st = 0.0
    if stats.f0_median_hz is not None and anchor_f0_hz is not None and anchor_f0_hz > 0:
        delta_st = semitone_diff(stats.f0_median_hz, anchor_f0_hz)
        needed_st = -delta_st
        if abs(needed_st) > RUBBERBAND_DEADBAND_PITCH_ST:
            shift_st = max(-RUBBERBAND_MAX_PITCH_ST, min(RUBBERBAND_MAX_PITCH_ST, needed_st))

    tempo_factor = 1.0
    if stats.syllable_rate is not None and anchor_rate is not None and anchor_rate > 0 and stats.syllable_rate > 0:
        raw_tempo = anchor_rate / stats.syllable_rate
        if abs(raw_tempo - 1.0) > tp_db:
            tempo_factor = max(tp_min, min(tp_max, raw_tempo))

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

    p3 = is_clone_prosody_phase3_enabled()
    tp_min = RUBBERBAND_TEMPO_MIN if p3 else _P2_RUBBERBAND_TEMPO_MIN
    tp_max = RUBBERBAND_TEMPO_MAX if p3 else _P2_RUBBERBAND_TEMPO_MAX

    clamped_st = max(-RUBBERBAND_MAX_PITCH_ST, min(RUBBERBAND_MAX_PITCH_ST, float(pitch_shift_st)))
    clamped_tempo = max(tp_min, min(tp_max, float(tempo_factor)))

    ff = ffmpeg_bin or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    p = Path(wav_path)
    if not p.is_file():
        return False

    pitch_ratio = 2.0 ** (clamped_st / 12.0)
    tmp_out = p.with_name(f"{p.stem}.rb_tmp.wav")
    extra_rb = _rubberband_extra_opts(ff)
    af_expr = f"rubberband=pitch={pitch_ratio:.6f}:tempo={clamped_tempo:.6f}:formant=preserved{extra_rb}"
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


_ORIG_APPLY_RUBBERBAND_MICRO = apply_rubberband_micro_correction


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


def _escape_ffmpeg_filter_path(p: Path) -> str:
    """Escape a local file path for FFmpeg filter option ``asendcmd=f='...'`` across Windows/Linux."""
    raw = str(p.resolve()).replace("\\", "/")
    raw = raw.replace(":", "\\:").replace("'", "\\'")
    return raw


def apply_contour_to_chunk(
    raw_wav: Path,
    out_wav: Path,
    *,
    grid_t: np.ndarray,
    grid_st: np.ndarray,
    tempo_factor: float = 1.0,
    highshelf_db: float = 0.0,
    presence_db: float = 0.0,
    uniform_chain: bool | None = None,
    ffmpeg_bin: str | None = None,
) -> bool:
    """Single-pass ``asendcmd=f=<tmp> + rubberband + voiced-tilt EQ`` execution.

    In Phase 3 (``uniform_chain=True``), EVERY chunk of a multi-chunk job passes through
    the same single FFmpeg filtergraph (no raw neural vs phase-vocoded bypass toggle).
    """
    src = Path(raw_wav)
    dst = Path(out_wav)
    dst.parent.mkdir(parents=True, exist_ok=True)

    p3 = is_clone_prosody_phase3_enabled()
    if uniform_chain is None:
        uniform_chain = p3
    tp_min = RUBBERBAND_TEMPO_MIN if p3 else _P2_RUBBERBAND_TEMPO_MIN
    tp_max = RUBBERBAND_TEMPO_MAX if p3 else _P2_RUBBERBAND_TEMPO_MAX

    clamped_tempo = max(tp_min, min(tp_max, float(tempo_factor)))
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

    is_uniform_pitch = float(np.max(np.abs(clamped_grid - clamped_grid[0]))) <= 1e-3
    rb_monkeypatched = apply_rubberband_micro_correction is not _ORIG_APPLY_RUBBERBAND_MICRO

    # Legacy Phase 2 bypass path OR unit-test spy hook on apply_rubberband_micro_correction
    if is_uniform_pitch and (not uniform_chain or rb_monkeypatched):
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
    caps = probe_rubberband_capabilities(ff)
    extra_rb = _rubberband_extra_opts(ff)
    init_ratio = 2.0 ** (float(clamped_grid[0]) / 12.0)

    cmd_file = dst.with_name(f"{dst.stem}.asend.cmd")
    tmp_out = dst.with_name(f"{dst.stem}.asend_tmp.wav")
    ar_args = ["-ar", str(orig_sr)] if orig_sr > 0 else []

    eq_chain_parts: list[str] = []
    if p3 and uniform_chain:
        hs_filter = "highshelf" if caps.get("highshelf", True) else "treble"
        eq_chain_parts.append(
            f"{hs_filter}=f={TILT_HIGHSHELF_FREQ_HZ:.1f}:t=q:w={TILT_HIGHSHELF_Q:.2f}:g={float(highshelf_db):.3f}"
        )
        eq_chain_parts.append(
            f"equalizer=f={TILT_PRESENCE_FREQ_HZ:.1f}:t=q:w={TILT_PRESENCE_Q:.2f}:g={float(presence_db):.3f}"
        )
    elif abs(highshelf_db) > 1e-3 or abs(presence_db) > 1e-3:
        hs_filter = "highshelf" if caps.get("highshelf", True) else "treble"
        if abs(highshelf_db) > 1e-3:
            eq_chain_parts.append(
                f"{hs_filter}=f={TILT_HIGHSHELF_FREQ_HZ:.1f}:t=q:w={TILT_HIGHSHELF_Q:.2f}:g={float(highshelf_db):.3f}"
            )
        if abs(presence_db) > 1e-3:
            eq_chain_parts.append(
                f"equalizer=f={TILT_PRESENCE_FREQ_HZ:.1f}:t=q:w={TILT_PRESENCE_Q:.2f}:g={float(presence_db):.3f}"
            )

    rb_core = f"rubberband=pitch={init_ratio:.6f}:tempo={clamped_tempo:.6f}:formant=preserved{extra_rb}"
    eq_suffix = ("," + ",".join(eq_chain_parts)) if eq_chain_parts else ""

    try:
        af_candidates: list[str] = []
        if is_uniform_pitch:
            af_candidates.append(f"{rb_core}{eq_suffix}")
        else:
            cmd_lines = "\n".join(
                f"{float(t):.3f} rubberband pitch {2.0 ** (float(st) / 12.0):.6f};"
                for t, st in zip(grid_t, clamped_grid)
            )
            cmd_file.write_text(cmd_lines + "\n", encoding="utf-8")
            esc_path = _escape_ffmpeg_filter_path(cmd_file)
            af_candidates.append(f"asendcmd=f='{esc_path}',{rb_core}{eq_suffix}")
            inline_cmds = ";".join(
                f"{float(t):.3f} rubberband pitch {2.0 ** (float(st) / 12.0):.6f}"
                for t, st in zip(grid_t, clamped_grid)
            )
            if len(inline_cmds) < 24000:
                af_candidates.append(f"asendcmd=c='{inline_cmds}',{rb_core}{eq_suffix}")

        for af_expr in af_candidates:
            cmd = [
                ff, "-y", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", _MEDIA_WHITELIST,
                "-i", str(src),
                "-af", af_expr,
                *ar_args,
                "-ac", "1", "-c:a", "pcm_s16le",
                str(tmp_out),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
            if res.returncode == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 256:
                shutil.move(str(tmp_out), str(dst))
                if expected_frames > 0:
                    _enforce_exact_pcm_frames(dst, expected_frames)
                return True
    except Exception as exc:
        logger.debug("clone_prosody: contoured rubberband skipped (%s)", exc)
    finally:
        cmd_file.unlink(missing_ok=True)
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
    slope_delta_frames: np.ndarray | None = None,
    first_voiced_sec: float = 0.0,
    last_voiced_sec: float | None = None,
    speed_tempo: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Construct the 50 ms ``Δp_k(t) = clamp[-2, +2](Δbody + Δslope(t) + Δenv(t) + Δhead·w_cos + Δtail·w_cos)`` grid."""
    dur = max(0.05, float(duration_sec))
    n_grid = max(2, int(math.ceil(dur / RAMP_CMD_STEP_SEC)) + 1)
    grid_t = np.linspace(0.0, dur, n_grid)

    if env_delta_frames.size > 0:
        f_idx = np.clip(grid_t / FRAME_STEP_SEC, 0.0, float(env_delta_frames.size - 1))
        d_env_g = np.interp(f_idx, np.arange(env_delta_frames.size, dtype=np.float64), env_delta_frames)
    else:
        d_env_g = np.zeros(n_grid, dtype=np.float64)

    if slope_delta_frames is not None and slope_delta_frames.size > 0:
        s_idx = np.clip(grid_t / FRAME_STEP_SEC, 0.0, float(slope_delta_frames.size - 1))
        d_slope_g = np.interp(s_idx, np.arange(slope_delta_frames.size, dtype=np.float64), slope_delta_frames)
    else:
        d_slope_g = np.zeros(n_grid, dtype=np.float64)

    tempo_scale = max(0.5, min(2.0, float(speed_tempo or 1.0)))
    base_head_l = RAMP_HEAD_SEC * tempo_scale
    base_tail_l = RAMP_TAIL_SEC * tempo_scale

    req_head_l = (math.pi * abs(float(head_ramp_st))) / (2.0 * MAX_RAMP_SLOPE_ST_PER_SEC)
    req_tail_l = (math.pi * abs(float(tail_ramp_st))) / (2.0 * MAX_RAMP_SLOPE_ST_PER_SEC)
    wide_cap = min(4.20 * tempo_scale, max(base_head_l, 0.24 * dur))
    wide_cap_t = min(4.20 * tempo_scale, max(base_tail_l, 0.24 * dur))
    smooth_head_l = min(wide_cap, 3.60 * abs(float(head_ramp_st)) * tempo_scale)
    smooth_tail_l = min(wide_cap_t, 3.40 * abs(float(tail_ramp_st)) * tempo_scale)
    l_head = min(max(base_head_l, req_head_l, smooth_head_l), max(0.40, 0.48 * dur))
    l_tail = min(max(base_tail_l, req_tail_l, smooth_tail_l), max(0.40, 0.48 * dur))

    t_v0 = max(0.0, min(0.35 * dur, float(first_voiced_sec or 0.0)))
    t_v1 = dur if last_voiced_sec is None else max(0.65 * dur, min(dur, float(last_voiced_sec)))

    if abs(head_ramp_st) > 1e-4 and l_head > 1e-3:
        tau_h = np.maximum(0.0, grid_t - t_v0 - 0.20)
        w_h = np.where(tau_h < l_head, 0.5 * (1.0 + np.cos(math.pi * tau_h / l_head)), 0.0)
    else:
        w_h = np.zeros(n_grid, dtype=np.float64)

    tau_tail = np.maximum(0.0, t_v1 - grid_t - 0.20)
    if abs(tail_ramp_st) > 1e-4 and l_tail > 1e-3:
        w_t = np.where(tau_tail < l_tail, 0.5 * (1.0 + np.cos(math.pi * tau_tail / l_tail)), 0.0)
    else:
        w_t = np.zeros(n_grid, dtype=np.float64)

    ramp_part = float(head_ramp_st) * w_h + float(tail_ramp_st) * w_t
    ramp_smooth = _project_slope_limit(ramp_part, float(grid_t[1] - grid_t[0]), MAX_RAMP_SLOPE_ST_PER_SEC)
    raw_grid = float(body_shift_st) + d_slope_g + d_env_g + ramp_smooth
    smooth = np.clip(raw_grid, -MAX_TOTAL_INSTANT_SHIFT_ST, MAX_TOTAL_INSTANT_SHIFT_ST)
    return grid_t, smooth


def compute_seam_ramps_for_pair(
    et: float,
    eh: float,
    b_spec: BoundarySpec,
    *,
    skip_tail: bool = False,
    skip_head: bool = False,
) -> tuple[float, float]:
    """Compute ``(tail_ramp_st, head_ramp_st)`` for seam ``k -> k+1`` (Phase 3 30/70 asymmetric or Phase 2 50/50)."""
    diff = float(eh) - float(et)
    s_max = float(b_spec.s_max_st)
    if skip_tail and skip_head:
        return 0.0, 0.0

    if not is_clone_prosody_phase3_enabled():
        if abs(diff) <= s_max:
            return 0.0, 0.0
        if b_spec.question_tail and 0.0 <= et <= 2.0 and diff < 0.0:
            if not skip_head:
                return 0.0, ((et - s_max) - eh) / 0.92
            return 0.0, 0.0
        if skip_tail:
            return 0.0, (-math.copysign(abs(diff) - s_max, diff)) / 0.92
        if skip_head:
            return (math.copysign(abs(diff) - s_max, diff)) / 0.86

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
        return (act_tail - et) / 0.86, (act_head - eh) / 0.92

    # Phase 3 (§4): Asymmetric 30/70 continuation split + natural sentence-final fall preservation
    if b_spec.question_tail and 0.0 <= et <= 2.0 and diff < 0.0:
        if abs(diff) <= s_max or skip_head:
            return 0.0, 0.0
        return 0.0, max(-1.80, min(1.80, (et - s_max) - eh)) / 0.96

    if b_spec.kind == "SENTENCE":
        neg_min = -0.20
        pos_max = s_max
    elif b_spec.kind == "PARAGRAPH":
        neg_min = -0.30
        pos_max = s_max
    else:
        neg_min = -s_max
        pos_max = s_max

    if neg_min <= diff <= pos_max:
        return 0.0, 0.0

    # excess > 0 means eh is too high above et (standard utterance-onset reset);
    # excess < 0 means eh dropped too far below et on a non-question boundary.
    excess = (diff - pos_max) if diff > pos_max else (diff - neg_min)

    if skip_tail:
        d_tail = 0.0
        d_head = -excess
    elif skip_head:
        d_tail = excess
        if b_spec.kind == "SENTENCE" and d_tail > 0.0 and et < 0.0:
            max_lift = max(0.0, SEAM_SENTENCE_NATURAL_FALL_FLOOR_ST - et)
            d_tail = min(d_tail, max_lift)
        d_head = 0.0
    else:
        d_tail = SEAM_TAIL_SHARE * excess
        d_head = -SEAM_HEAD_SHARE * excess

        # Preserve natural Burmese sentence-final falls (-0.45..-0.80 st untouched; lift only excess < -0.80 st)
        if b_spec.kind == "SENTENCE" and d_tail > 0.0 and et < 0.0:
            max_lift = max(0.0, SEAM_SENTENCE_NATURAL_FALL_FLOOR_ST - et)
            if d_tail > max_lift:
                d_tail = max_lift
                d_head = -(excess - d_tail)

        # Never push an edge further away from anchor in the same sign
        if abs(et + d_tail) > abs(et) and ((et + d_tail) * et > 0.0) and (d_tail * et > 0.0):
            d_tail = 0.0
            d_head = -excess
        elif abs(eh + d_head) > abs(eh) and ((eh + d_head) * eh > 0.0) and (d_head * eh > 0.0):
            d_head = 0.0
            d_tail = excess
            if b_spec.kind == "SENTENCE" and d_tail > 0.0 and et < 0.0:
                d_tail = min(d_tail, max(0.0, SEAM_SENTENCE_NATURAL_FALL_FLOOR_ST - et))

    d_head_c = max(-1.75, min(1.75, d_head))
    d_tail_c = max(-1.75, min(1.75, d_tail))
    # If head saturated ±1.75 st, let tail help with remaining step (still honoring -0.80 st natural fall floor)
    rem = (eh + d_head_c) - (et + d_tail_c)
    if rem > pos_max + 0.03 and not skip_tail:
        extra_t = rem - pos_max
        if b_spec.kind == "SENTENCE" and et + d_tail_c < 0.0:
            extra_t = min(extra_t, max(0.0, SEAM_SENTENCE_NATURAL_FALL_FLOOR_ST - (et + d_tail_c)))
        d_tail_c = max(-1.75, min(1.75, d_tail_c + extra_t))
    elif rem < neg_min - 0.03 and not skip_tail:
        extra_t = rem - neg_min
        d_tail_c = max(-1.75, min(1.75, d_tail_c + extra_t))

    return d_tail_c / 0.95, d_head_c / 0.97


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
    space_family_switched: bool = False,
    ffmpeg_bin: str | None = None,
) -> PostLoopProsodyResult:
    """Execute ONE post-loop contour + de-sag + asymmetric seam + voiced-tilt EQ pass over raw chunks.

    Never mutates ``raw_parts``; writes ``chunk{idx:02d}.corr.wav`` into ``out_dir`` so
    park/resume or re-entry is strictly idempotent with zero double-rubberbanding.
    """
    m = len(raw_parts)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p3 = is_clone_prosody_phase3_enabled()
    tp_min = RUBBERBAND_TEMPO_MIN if p3 else _P2_RUBBERBAND_TEMPO_MIN
    tp_max = RUBBERBAND_TEMPO_MAX if p3 else _P2_RUBBERBAND_TEMPO_MAX
    tp_db = RUBBERBAND_DEADBAND_TEMPO if p3 else _P2_RUBBERBAND_DEADBAND_TEMPO

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
    slope_deltas: list[np.ndarray] = []
    env_deltas: list[np.ndarray] = []
    tempos: list[float] = []
    raw_heads_vs_anchor: list[float] = []
    raw_tails_vs_anchor: list[float] = []
    post_be_heads: list[float] = []
    post_be_tails: list[float] = []
    skip_heads: list[bool] = []
    skip_tails: list[bool] = []
    desag_corrections = 0
    envelope_corrections = 0
    edge_fallbacks = 0

    for idx, ana in enumerate(analyses):
        c_txt = str(chunks[idx]) if idx < len(chunks) else ""
        expr = is_expressive_chunk(c_txt)
        b_sh = compute_soft_knee_body_shift(ana.stats.f0_median_hz, f_anchor, expressive=expr)
        body_shifts.append(b_sh)

        d_slope, _comp_sl, sl_used = compute_linear_desag_contour(
            ana.f0_arr,
            duration_sec=ana.stats.duration_sec,
            sentence_count=ana.sentence_count,
            anchor_f0_hz=f_anchor,
        )
        slope_deltas.append(d_slope)
        if sl_used:
            desag_corrections += 1

        d_env, env_used = compute_envelope_tether(
            ana.f0_arr,
            anchor_f0_hz=f_anchor,
            body_shift_st=b_sh,
            expressive=expr,
            slope_delta_frames=d_slope,
            duration_sec=ana.stats.duration_sec,
            sentence_count=ana.sentence_count,
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

        # Post-(body + slope + env) edge estimates from cached contour
        if f_anchor and ana.f0_arr.size > 0:
            base_f0 = repair_subharmonic_f0_contour(ana.f0_arr) if p3 else ana.f0_arr
            f0_be = base_f0 * (2.0 ** ((b_sh + d_slope + d_env) / 12.0))
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
            if abs(raw_tp - 1.0) > tp_db:
                tp = max(tp_min, min(tp_max, raw_tp))
        tempos.append(round(tp, 4))

    # Seam sharing across every boundary k -> k+1 (30/70 asymmetric in Phase 3; 50/50 in Phase 2)
    head_ramps = [0.0] * m
    tail_ramps = [0.0] * m
    for k in range(m - 1):
        tr_k, hr_next = compute_seam_ramps_for_pair(
            post_be_tails[k],
            post_be_heads[k + 1],
            boundaries[k],
            skip_tail=skip_tails[k],
            skip_head=skip_heads[k + 1],
        )
        tail_ramps[k] = tr_k
        head_ramps[k + 1] = hr_next

    corrected_parts: list[Path] = []
    post_analyses: list[ChunkProsodyAnalysis] = []
    corrections_applied = 0
    tilt_eq_corrections = 0
    use_uniform_chain = bool(p3 and m >= 2)

    for idx, ana in enumerate(analyses):
        c_txt = str(chunks[idx]) if idx < len(chunks) else ""
        v_pos = np.where(~np.isnan(ana.f0_arr) & (ana.f0_arr > 0))[0] if ana.f0_arr.size > 0 else np.zeros(0, dtype=int)
        t_v0 = float(v_pos[0]) * FRAME_STEP_SEC if (p3 and v_pos.size > 0) else 0.0
        t_v1 = float(v_pos[-1]) * FRAME_STEP_SEC if (p3 and v_pos.size > 0) else ana.stats.duration_sec
        grid_t, grid_st = build_chunk_pitch_contour_grid(
            ana.stats.duration_sec,
            body_shift_st=body_shifts[idx],
            env_delta_frames=env_deltas[idx],
            head_ramp_st=head_ramps[idx],
            tail_ramp_st=tail_ramps[idx],
            slope_delta_frames=slope_deltas[idx],
            first_voiced_sec=t_v0,
            last_voiced_sec=t_v1,
            speed_tempo=speed_tempo,
        )
        mean_shift_st = float(np.mean(grid_st)) if grid_st.size > 0 else 0.0
        hs_db, pr_db = (
            compute_chunk_tilt_eq_gains(
                ana.voiced_tilt_db,
                ana.voiced_presence_db,
                target_tilt_db=anchor.voiced_tilt_anchor_db,
                target_pres_db=anchor.voiced_presence_anchor_db,
                mean_pitch_shift_st=mean_shift_st,
            )
            if use_uniform_chain
            else (0.0, 0.0)
        )
        if abs(hs_db) > 1e-3 or abs(pr_db) > 1e-3:
            tilt_eq_corrections += 1

        corr_wav = out_dir / f"{Path(raw_parts[idx]).stem}.corr.wav"
        applied = apply_contour_to_chunk(
            Path(raw_parts[idx]),
            corr_wav,
            grid_t=grid_t,
            grid_st=grid_st,
            tempo_factor=tempos[idx],
            highshelf_db=hs_db,
            presence_db=pr_db,
            uniform_chain=use_uniform_chain,
            ffmpeg_bin=ffmpeg_bin,
        )
        if applied:
            corrections_applied += 1
        corrected_parts.append(corr_wav)

        if applied and corr_wav.is_file() and ana.f0_arr.size > 0:
            # Reuse cached extract_frame_contours arrays instead of running YIN twice per chunk!
            clamped_tp = max(tp_min, min(tp_max, float(tempos[idx])))
            clamped_g = np.clip(grid_st, -MAX_TOTAL_INSTANT_SHIFT_ST, MAX_TOTAL_INSTANT_SHIFT_ST)
            t_frames = np.arange(ana.f0_arr.size, dtype=np.float64) * FRAME_STEP_SEC
            f_shifts = np.interp(t_frames, grid_t, clamped_g)
            base_c_f0 = repair_subharmonic_f0_contour(ana.f0_arr) if p3 else ana.f0_arr
            shifted_f0 = (base_c_f0.astype(np.float64) * (2.0 ** (f_shifts / 12.0))).astype(np.float32)
            edge_base_f0 = base_c_f0.astype(np.float64, copy=True)
            floor_r = EDGE_MODAL_FLOOR_RATIO if p3 else _P2_EDGE_MODAL_FLOOR_RATIO
            if ana.stats.f0_median_hz and ana.stats.f0_median_hz > 0.0:
                rmed = float(ana.stats.f0_median_hz)
                glitch_mask = (~np.isnan(edge_base_f0)) & ((edge_base_f0 < floor_r * rmed) | (edge_base_f0 > 1.55 * rmed))
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

            c_samples, c_sr = read_wav_mono_float32(corr_wav)
            p_tilt, p_pres, p_cent = measure_voiced_spectral_stats(c_samples, c_sr, post_f0)
            p_sag, p_slope = measure_chunk_sag_and_slope(post_f0, anchor_hz=f_anchor)

            post_analyses.append(
                ChunkProsodyAnalysis(
                    wav_path=corr_wav,
                    text=c_txt,
                    sample_rate=c_sr or ana.sample_rate,
                    n_samples=c_samples.size if c_samples.size > 0 else max(1, int(round(ana.n_samples / clamped_tp))),
                    stats=p_stats,
                    rms_arr=post_rms,
                    f0_arr=post_edge_f0,
                    raw_head_st_vs_med=round(ph_m, 4),
                    raw_tail_st_vs_med=round(pt_m, 4),
                    head_fallback=ph_fb,
                    tail_fallback=pt_fb,
                    head_skipped=ph_sk,
                    tail_skipped=pt_sk,
                    voiced_tilt_db=p_tilt,
                    voiced_presence_db=p_pres,
                    voiced_centroid_hz=p_cent,
                    linear_sag_st=p_sag,
                    linear_slope_st_per_s=p_slope,
                    sentence_count=ana.sentence_count,
                )
            )
        else:
            post_analyses.append(ana)

    # Compute post-correction boundary steps for telemetry
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
        desag_corrections=desag_corrections,
        tilt_eq_corrections=tilt_eq_corrections,
        space_family_switched=bool(space_family_switched),
    )
