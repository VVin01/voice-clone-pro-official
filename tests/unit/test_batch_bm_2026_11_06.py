"""Batch BM — prominence gate removal + caption-zone drop
(2026-11-06; Batch BL spec, owner-approved, Option A ordering).

Two measured disasters shaped this batch:

* The zoo job (Batch BI): a caption-FREE 240p source shipped with
  blur strips across the picture, two of them at extents
  ~0.15-0.28 and ~0.35-0.50 — mid-picture scene texture (a talking
  face) the detector mistook for caption bands.
* The GlunZpix30I drift (BL investigation, fix-bl-work/
  report-build.md): Batch BI's prominence gate (peak agg >= 4x
  the frame-median agg) kills GENUINE weak captions on busy real
  frames — the real strip seeds and groups cleanly (peak 0.0306,
  pers 0.58) but its peak/median ratio is only ~1.4x against the
  gate's 4x floor, so on the production-geometry crop the detector
  returned texture bands and no strip.

Batch BM therefore removes the prominence gate and protects by
POSITION instead: a band ships only when its whole extent
(y +/- h/2, source fractions) lies in a caption zone — top
[0.00, 0.24] / bottom [0.58, 1.00] (owner-moved bottom edge; the
BL spec's 0.60 left a pinned genuine band only 0.0066 of
clearance). The zone drop runs BEFORE the 2-band cap (owner
Option A): a weak strip's peak is an order of magnitude below
busy-frame texture, so cap-first ordering spends both slots
out-of-zone and evicts it — measured on the real crop, where
cap-first returned [] and zone-first ships the strip (the
ordering is pinned synthetically below; the real-file proof is
test_real_glunzpix30i_caption_strip_covered in the Batch AI
suite, re-geometryed to the production crop by this batch).

The zone rule governs static bands too (owner Decision 1: no
static exemption — pinned in the Batch AI suite).

Batch BQ amendment (owner decision 2026-11-06, same day): the
TOP zone is removed — auto-detected bands ship ONLY from the
bottom zone [0.58, 1.00]. Both top-zone bands observed live
(the zoo re-run's strip above the subject's head and this
suite's GlunZpix30I-crop top texture band) were scene texture,
never a real caption; top-of-frame captions are covered by the
user's manual blur box. The pins below were re-pinned in place:
the top-band-kept pin now expects the zone drop, and the
ordering pin's top band is now a zone-drop witness instead of
a survivor. Bottom-zone expectations are byte-identical.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "services" / "recap_engine"))

from core import _subtitle_band_boxes  # noqa: E402

H = 200  # synthetic score-matrix height


def _matrix(n: int = 12) -> np.ndarray:
    return np.zeros((n, H))


def _covers(box: dict, lo: float, hi: float) -> bool:
    return box["y"] - box["h"] / 2.0 <= lo and box["y"] + box["h"] / 2.0 >= hi


def _band(S: np.ndarray, y0: int, rows: int, peak: float) -> None:
    """A dynamic band: alternating strength between samples, so the
    static-overlay demotion never applies (dyn ratio ~0.35)."""
    for k in range(S.shape[0]):
        S[k, y0:y0 + rows] = peak if k % 2 == 0 else peak * 0.7


# ── the zone drop ───────────────────────────────────────────────────

def test_face_band_mid_picture_is_zone_dropped():
    # The zoo disaster's worst strip: a strong dynamic band through
    # the subject's face (rows 80-95, padded extent 0.288-0.528).
    S = _matrix()
    _band(S, 80, 16, 0.20)
    logs: list[str] = []
    assert _subtitle_band_boxes(S, log=logs.append) == []
    assert any("caption-zone" in m for m in logs), logs


def test_band_crossing_old_top_zone_edge_is_dropped():
    # Renamed/re-reasoned by Batch BQ: rows 44-51, padded extent
    # 0.164-0.284. Under BM this pinned the whole-extent rule (the
    # band straddled the top zone's 0.24 edge); BQ removed the top
    # zone entirely, so the band is dropped regardless — same
    # fixture, same outcome, broader reason.
    S = _matrix()
    _band(S, 44, 8, 0.20)
    logs: list[str] = []
    assert _subtitle_band_boxes(S, log=logs.append) == []
    assert any("caption-zone" in m for m in logs), logs


def test_straddling_band_crossing_bottom_zone_edge_is_dropped():
    # Rows 124-131: padded extent 0.564-0.684 — crosses the
    # owner-moved bottom edge (0.58) from above.
    S = _matrix()
    _band(S, 124, 8, 0.20)
    assert _subtitle_band_boxes(S) == []


def test_top_caption_band_is_zone_dropped_batch_bq():
    # Re-pinned by Batch BQ (owner decision 2026-11-06): rows
    # 16-23, padded extent 0.024-0.144 — squarely inside BM's top
    # zone, where this pin (as test_top_caption_band_is_kept)
    # expected it to ship. The top zone is removed: auto-detect
    # ships bottom-zone bands only, and a top caption is covered
    # by the user's manual blur box instead.
    S = _matrix()
    _band(S, 16, 8, 0.20)
    logs: list[str] = []
    assert _subtitle_band_boxes(S, log=logs.append) == []
    assert any("caption-zone" in m for m in logs), logs


def test_bottom_caption_band_is_kept():
    # Rows 144-151: padded extent 0.664-0.784, inside the bottom
    # zone — the classic raised caption position (y~0.70 sources).
    S = _matrix()
    _band(S, 144, 8, 0.20)
    boxes = _subtitle_band_boxes(S)
    assert len(boxes) == 1, boxes
    assert _covers(boxes[0], 0.72, 0.76), boxes


# ── the Option A ordering: zone drop before the cap ────────────────

def _caption_profile() -> np.ndarray:
    """The Batch AI GlunZpix30I profile: a weak, intermittent
    bottom strip (peak ~0.03) over a low noise floor."""
    S = _matrix()
    S[:, :] = 0.004
    for y in range(176, 186):
        S[1, y] = 0.013
        S[7, y] = 0.012
    for k, v in enumerate((0.030, 0.026, 0.033, 0.022, 0.028)):
        for y in range(186, 192):
            S[2 * k, y] = v
    S[9, 189] = 0.024
    return S


def test_zone_drop_runs_before_cap_weak_strip_survives():
    # The synthetic twin of the measured GlunZpix30I crop: two
    # STRONG out-of-zone bands (a burn-in-like band at rows 96-103,
    # peak 0.40, extent ~0.424-0.544; scene texture at rows 52-59,
    # peak 0.18, straddling extent ~0.204-0.324), one strong
    # top band (rows 4-11, peak 0.16), and the weak genuine
    # strip (peak ~0.03). Under cap-first ordering the cap spends
    # both slots on the strong out-of-zone bands and the strip is
    # evicted (the pre-BM measured behaviour on the real crop:
    # the strip ranked 4th of 4 and the result was []).
    # Batch BQ re-pin: BM's top zone is removed, so the top band
    # is now zone-dropped like the other two strong bands — with
    # the zone drop first, exactly ONE band ships: the weak
    # genuine strip, and the cap never fires.
    S = _caption_profile()
    _band(S, 96, 8, 0.40)
    _band(S, 52, 8, 0.18)
    _band(S, 4, 8, 0.16)
    logs: list[str] = []
    boxes = _subtitle_band_boxes(S, log=logs.append)
    assert len(boxes) == 1, boxes
    assert _covers(boxes[0], 0.90, 0.955), boxes  # the weak strip
    assert any("caption-zone" in m for m in logs), logs
    assert not any("2-band cap" in m for m in logs), logs
