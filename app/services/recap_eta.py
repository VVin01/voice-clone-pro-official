"""Batch CB — honest recap ETA: calibration constants + pure estimators.

WHY THIS MODULE EXISTS
----------------------
Before CB, the in-job ETA was ONE global median of recent recap
durations scaled by (1 - progress%): a 19-second clip and a
38-segment drama averaged together, and a job sitting at high
progress in the serial narration stage displayed "~1 minute" for
tens of minutes (owner screenshot 2026-11-07 11:30: a job RUNNING
for 3,034 s labelled ~1 min). CB replaces that with:

* shape-conditioned medians (engine class x duration band) computed
  in app/webapp/api.py from real Job rows — the measured truth wins
  whenever enough samples exist (see MEDIAN_THRESHOLDS);
* a STAGE DECOMPOSITION for the remaining time of a running job:
  the narration stage scales with segments remaining x the job's OWN
  observed seconds-per-segment (which captures the free-quota
  deceleration as it happens), floored at the calibrated typical
  pace so a fast start cannot promise an impossible finish;
* the calibration constants below as (a) the stage-share model the
  decomposition normalises a median total through, (b) the pace
  floors, and (c) the cold-start estimate for a shape with no
  samples yet (the Step-4 / submit-time surface only — the in-job
  status surfaces keep Batch L's rule: no samples, no number).

CALIBRATION SOURCE (Item 5)
---------------------------
Every constant is derived from the measured 2026-11-05 latency
investigation (~/workspace/recap-latency-2026-11-05.md), job
rc_1793848913_13571c: 101 s source, 38 segments, VoxCPM narration,
~35-36 min end-to-end (MEASURED timeline):

* download ~0.5-1 min; Groq transcription <0.5 min;
* Gemini planning (context + translate + audit) ~2-4 min;
* serial VoxCPM narration 20-24 min for 38 segments: ~15-21 s/seg
  early, decelerating to ~86 s/seg as the free GPU quota drains
  (average ~35 s/seg); Gemini TTS ~5-15 s/seg and Edge TTS ~1-3 s/seg
  per the same report's engine notes;
* per-segment video-part encodes: 38 parts in ~4-7 min (~6-11 s each,
  modelled 5-9 s with the pipeline optimisations since);
* concat + two overlay encodes + mux ~3-5 min for a 139 s output;
* a 19 s Gemini-narrated job measured ~2 min end-to-end.

This module is the single tuning point: when a constant proves
wrong against production medians, change it here.
"""
from __future__ import annotations

from typing import Optional

# ── Shape vocabulary ────────────────────────────────────────────────
ENGINE_CLASSES = ("voxcpm", "gemini", "edge")

# Source-duration bands for the median buckets (Item 1). Sources
# above the 600 s per-job cap exist (admins, segmented parents), so
# an overflow band keeps them out of the <=600 bucket's statistics.
DURATION_BANDS = ("le60", "le180", "le600", "gt600")

# Sample thresholds before a median level is trusted (Item 1:
# >= 5 for buckets; the global level keeps Batch L's >= 3).
MEDIAN_THRESHOLDS = {"bucket": 5, "engine": 5, "global": 3}

# Canonical pipeline stages, in order (mirrors the stage keys the
# progress mapper in app/webapp/api.py writes to the job registry).
STAGE_ORDER = (
    "download",
    "transcribe",
    "context",
    "translate",
    "plan",
    "tts",
    "sync",
    "finalize",
)

# Progress bands per stage — mirrors api._RECAP_STAGE_BANDS (the
# registry percent a stage spans). Only used to infer a stage from a
# bare progress percent on the cold path, where no stage key exists.
PROGRESS_BANDS = {
    "download": (5, 15),
    "transcribe": (15, 30),
    "context": (30, 42),
    "translate": (42, 54),
    "plan": (54, 64),
    "tts": (64, 82),
    "sync": (82, 95),
    "finalize": (95, 99),
}

# ── Calibration constants (2026-11-05 measured report) ─────────────
# Plan segments per second of source — used ONLY pre-submit, when
# the authored count does not exist yet. Segments track SPEECH
# (planner target 4.2 s of speech per segment), so density varies
# with content: the measured dense-speech job ran 38 segments from
# 101 s of source (~0.38/s); the owner's sparse-speech drama ran 24
# segments from ~552 s (~0.04/s). 0.12/s is the mid planning value
# between those measured points; once a job exists, its REAL
# segment count drives every estimate, and bucket medians correct
# the totals per shape in production.
SEGMENTS_PER_SOURCE_SECOND = 0.12

# Narration pace per segment, (low, high) seconds. VoxCPM: fresh
# quota runs ~15-21 s/seg, a draining pool reaches ~86 s/seg; the
# band brackets the realistic spread around the ~35 s/seg average.
NARRATION_PACE = {
    "voxcpm": (20.0, 60.0),
    "gemini": (4.0, 10.0),
    "edge": (1.5, 4.0),
}
# The pace floor for the RUNNING decomposition: the job's observed
# pace may raise the estimate, never drop it below the calibrated
# typical (measured averages from the same report).
NARRATION_TYPICAL_PACE = {"voxcpm": 35.0, "gemini": 7.0, "edge": 2.5}

# Per-segment video-part encode (sync stage), seconds.
PART_ENCODE_PER_SEGMENT = (5.0, 9.0)

# Final overlay/mux encode cost grows with output length and with
# the render-quality encoder preset (core RENDER_QUALITIES).
QUALITY_FACTOR = {"standard": 1.0, "high": 1.25, "max": 1.6}

# A median's uncertainty spread when it is surfaced as a range:
# real recap durations for one shape vary roughly within this band
# (quota state alone swings narration by ~2x on bad days; the
# median-of-shape already absorbs most of it).
MEDIAN_SPREAD = (0.70, 1.45)

# A RUNNING job never displays less than this while unfinished —
# a floor of honesty, NOT the old client-side 1-minute paint.
MIN_RUNNING_REMAINING_SECONDS = 20.0


def engine_class(tts_engine, voice_ref_used: bool = False) -> str:
    """Bucket engine class for a recap config's tts_engine value.

    Own-voice narration rides tts_engine='voxcpm' (Batch BV) and is
    VoxCPM-priced time, so it lands in the voxcpm class either way;
    unknown values fall back to the product default class.
    """
    e = str(tts_engine or "").strip().lower()
    if e in ENGINE_CLASSES:
        return e
    return "voxcpm"


def duration_band(duration_seconds) -> str:
    """Median-bucket band for a source duration in seconds."""
    try:
        d = float(duration_seconds or 0)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 60:
        return "le60"
    if d <= 180:
        return "le180"
    if d <= 600:
        return "le600"
    return "gt600"


def estimate_segments(duration_seconds) -> int:
    """Planning estimate of the narration segment count for a source
    of this many seconds (used only when the real count is unknown —
    pre-submit, or before the plan exists)."""
    try:
        d = float(duration_seconds or 0)
    except (TypeError, ValueError):
        d = 0.0
    return max(1, int(round(d * SEGMENTS_PER_SOURCE_SECOND)))


def stage_costs(engine: str, duration_seconds, segments, quality: str) -> dict:
    """Calibrated (low, high) seconds per stage for one job shape.

    The planning stages (context/translate/plan) share one modelled
    cost — the three sequential Gemini calls scale with the unit
    count (prompt size), split in thirds.
    """
    e = engine_class(engine)
    try:
        d = max(0.0, float(duration_seconds or 0))
    except (TypeError, ValueError):
        d = 0.0
    n = max(1, int(segments or 1))
    planning = (15.0 + 1.2 * n, 25.0 + 2.6 * n)
    pace = NARRATION_PACE[e]
    qf = QUALITY_FACTOR.get(str(quality or "standard").strip().lower(), 1.0)
    final = ((20.0 + 0.22 * d) * qf, (40.0 + 0.42 * d) * qf)
    return {
        "download": (8.0 + 0.06 * d, 20.0 + 0.20 * d),
        "transcribe": (5.0 + 0.03 * d, 12.0 + 0.08 * d),
        "context": (planning[0] / 3.0, planning[1] / 3.0),
        "translate": (planning[0] / 3.0, planning[1] / 3.0),
        "plan": (planning[0] / 3.0, planning[1] / 3.0),
        "tts": (pace[0] * n, pace[1] * n),
        "sync": (PART_ENCODE_PER_SEGMENT[0] * n, PART_ENCODE_PER_SEGMENT[1] * n),
        "finalize": final,
    }


def total_range(engine: str, duration_seconds, segments, quality: str) -> tuple:
    """Calibrated (low, high) TOTAL seconds for a job shape."""
    costs = stage_costs(engine, duration_seconds, segments, quality)
    return (
        sum(c[0] for c in costs.values()),
        sum(c[1] for c in costs.values()),
    )


def spread_range(median_seconds: float) -> tuple:
    """The (low, high) range a trusted median is surfaced with."""
    m = float(median_seconds)
    return (m * MEDIAN_SPREAD[0], m * MEDIAN_SPREAD[1])


def queue_waves(position) -> int:
    """Execution waves ahead of + including a queued job: the recap
    semaphore drains two renders at a time (Batch L's formula)."""
    return ((max(0, int(position or 0)) + 2) // 2)


def _scaled_costs(costs: dict, total_low: float, total_high: float) -> dict:
    """Normalise calibrated stage costs so their sums equal the given
    totals — the decomposition of a MEASURED total (a median) through
    the calibrated stage shares."""
    sum_low = sum(c[0] for c in costs.values()) or 1.0
    sum_high = sum(c[1] for c in costs.values()) or 1.0
    return {
        stage: (lo * total_low / sum_low, hi * total_high / sum_high)
        for stage, (lo, hi) in costs.items()
    }


def remaining_range(
    stage: Optional[str],
    *,
    engine: str = "voxcpm",
    duration_seconds=0,
    quality: str = "standard",
    segments_total=None,
    segments_done=None,
    parts_done=None,
    observed_pace=None,
    stage_frac=None,
    total_override: Optional[tuple] = None,
) -> tuple:
    """(low, high, point) seconds remaining for a RUNNING job.

    Decomposition: the current stage's remainder + the typical costs
    of every stage still ahead. The narration (tts) stage is computed
    from segment counters, never from a percent: segments remaining x
    the job's own observed pace, with the pace floored at the
    calibrated typical and the range's low edge never below the
    calibrated fast pace (or 85% of the observed pace when the job is
    running slower than the calibrated band — its own deceleration is
    the truth). ``total_override`` (a trusted median-shaped total
    range) rescales the NON-narration stage costs to that total; the
    narration remainder always follows the segment math.
    """
    e = engine_class(engine)
    n = int(segments_total) if segments_total else estimate_segments(duration_seconds)
    n = max(1, n)
    costs = stage_costs(e, duration_seconds, n, quality)
    if total_override is not None:
        costs = _scaled_costs(costs, float(total_override[0]), float(total_override[1]))

    stage_key = stage if stage in STAGE_ORDER else None
    if stage_key is None:
        # Unknown stage (cold path): the whole job is the remainder.
        low = sum(c[0] for c in costs.values())
        high = sum(c[1] for c in costs.values())
        point = max(MIN_RUNNING_REMAINING_SECONDS, (low + high) / 2.0)
        return (low, high, point)

    idx = STAGE_ORDER.index(stage_key)

    # Stages still ahead: full typical cost.
    tail_low = sum(costs[later][0] for later in STAGE_ORDER[idx + 1:])
    tail_high = sum(costs[later][1] for later in STAGE_ORDER[idx + 1:])

    # Current stage remainder.
    if stage_key == "tts":
        done = int(segments_done or 0)
        left = max(0, n - done)
        pace_low, pace_high = NARRATION_PACE[e]
        typical = NARRATION_TYPICAL_PACE[e]
        if observed_pace and float(observed_pace) > 0:
            obs = float(observed_pace)
            # The range follows the job's own deceleration: the low
            # edge never promises faster than the observed pace
            # (discounted slightly), the high edge never below it.
            eff_low = max(pace_low, obs * 0.85)
            eff_high = max(pace_high, obs)
            # The point pace is the observed pace, floored at the
            # calibrated typical (the spec's floor rule).
            point_pace = max(obs, typical)
        else:
            eff_low, eff_high = pace_low, pace_high
            point_pace = typical
        low = tail_low + left * eff_low
        high = tail_high + left * eff_high
        point = (tail_low + tail_high) / 2.0 + left * point_pace
    elif stage_key == "sync":
        done_parts = int(parts_done or 0)
        left_frac = max(0.0, (n - done_parts) / n) if n else 0.0
        # costs["sync"] is the full parts cost for this shape (already
        # rescaled when a total_override is in play).
        sync_lo, sync_hi = costs["sync"]
        low = tail_low + sync_lo * left_frac
        high = tail_high + sync_hi * left_frac
        point = (low + high) / 2.0
    else:
        frac = stage_frac if isinstance(stage_frac, (int, float)) else 0.0
        frac = max(0.0, min(1.0, float(frac)))
        lo, hi = costs[stage_key]
        low = tail_low + lo * (1.0 - frac)
        high = tail_high + hi * (1.0 - frac)
        point = (low + high) / 2.0

    # Stage minimum-remainder floor: a stage with work left can never
    # honestly promise less than its fastest possible remainder.
    if stage_key == "tts" and segments_total and int(segments_done or 0) < int(segments_total):
        low = max(low, NARRATION_PACE[e][0])
    if stage_key == "finalize":
        low = max(low, 15.0)
    point = max(point, MIN_RUNNING_REMAINING_SECONDS, low)
    high = max(high, point)
    return (low, high, point)


def infer_stage_from_progress(progress_percent) -> tuple:
    """(stage, stage_frac) for a bare progress percent — the cold
    path's only signal. Percent is clamped into the first band."""
    try:
        pct = float(progress_percent or 0)
    except (TypeError, ValueError):
        pct = 0.0
    for stage in STAGE_ORDER:
        lo, hi = PROGRESS_BANDS[stage]
        if pct <= hi:
            return stage, max(0.0, min(1.0, (pct - lo) / max(1.0, hi - lo)))
    return "finalize", 1.0


def remaining_from_progress(progress_percent, total_seconds: float) -> tuple:
    """Legacy-shape fallback: decompose a bare median total by the
    stage a progress percent implies (reference shape shares). Used
    where nothing but (progress, median) survives — the pre-CB
    helper's call shape."""
    stage, frac = infer_stage_from_progress(progress_percent)
    return remaining_range(
        stage,
        engine="voxcpm",
        duration_seconds=120,
        quality="standard",
        segments_total=estimate_segments(120),
        stage_frac=frac,
        total_override=(float(total_seconds), float(total_seconds)),
    )
