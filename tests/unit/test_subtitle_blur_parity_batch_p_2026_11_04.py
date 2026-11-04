"""Batch P (2026-11-04): subtitle/blur preview=render parity,
reworked to the recapter standard (VVin01/recapter).

Pins:
* sub_y is PICTURE-relative on letterboxed layouts — the subtitle
  centre sits at oy + sub_y × ph — identically in the ASS writer,
  the SRT/force_style fallback, subtitle_anchor_y and the plate.
* The font size is ONE height-relative formula (picker pt →
  fraction of canvas height), four distinct sizes, no letterbox
  content-box scaling, ×1.25 for two lines.
* Full-frame position geometry is unchanged from baseline.
* The Mini App preview mirrors the same anchor + size formulas in
  JS (no mockup table), and the blur box stays canvas-relative on
  BOTH sides (audit: the bases already agreed — Batch J assembly
  and every render consumer read canvas fractions, and the Live
  Canvas positions the box in % of the whole canvas element).

Batch AA (2026-11-04) supersedes the blur-box basis above: blur_y /
blur_h are now PICTURE-relative end-to-end, like sub_y. Section 6
below is updated accordingly; the subtitle pins are untouched.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "app" / "services" / "recap_engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import core  # noqa: E402
from core import (  # noqa: E402
    frame_layout,
    subtitle_anchor_y,
    subtitle_centre_px,
    subtitle_font_px,
    subtitle_plate_box,
    write_synced_ass_file,
)

APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CORE_PY = (ENGINE_DIR / "core.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")

# Test geometry from the live parity check: 1080×1920 canvas, 16:9
# picture centred (oy=656, ph=608, rim=1264).
LB = frame_layout("9:16", "blur", 1920, 1080)
CW, CH = LB["cw"], LB["ch"]
OY, PH = LB["oy"], LB["ph"]


def _ass_fields(path: Path) -> dict:
    line = next(
        l for l in path.read_text(encoding="utf-8").splitlines()
        if l.startswith("Style:")
    )
    parts = [p.strip() for p in line.split(",")]
    return {"font_size": int(parts[2]), "margin_v": int(parts[-2])}


def _ass_centre_px(fields: dict, font_px: int, sub_lines: int = 1) -> float:
    """Invert the ASS writer's MarginV back to the subtitle centre."""
    text_half = font_px * (1.15 if sub_lines == 1 else 2.3) / 2.0
    return CH - fields["margin_v"] - text_half


# ── 1. Picture-relative sub_y on letterboxed layouts ───────────────

@pytest.mark.parametrize("sub_y", [0.10, 0.55, 0.85, 0.95])
def test_centre_px_letterbox_is_picture_relative(sub_y):
    centre = subtitle_centre_px(CW, CH, LB, sub_y)
    assert centre == pytest.approx(OY + sub_y * PH)
    # The centre always lands INSIDE the picture, never in the fill.
    assert OY <= centre <= OY + PH


def test_centre_px_live_parity_case():
    # The live test that failed: sub_y=55% rendered at ~65% of the
    # canvas. Now the centre is 55% of the PICTURE height.
    centre = subtitle_centre_px(CW, CH, LB, 0.55)
    assert centre / CH == pytest.approx(0.5158, abs=0.001)
    assert centre == pytest.approx(OY + 0.55 * PH)


def test_ass_writer_letterbox_anchor(tmp_path):
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "lb.ass", CW, CH,
        raw_font_size=16, sub_lines=1, sub_y=0.55, layout=LB,
    )
    fields = _ass_fields(out)
    centre = _ass_centre_px(fields, fields["font_size"])
    assert centre == pytest.approx(OY + 0.55 * PH, abs=2.0)


def test_anchor_and_plate_agree_with_ass(tmp_path):
    anchor = subtitle_anchor_y(CW, CH, LB, sub_y=0.55)
    assert anchor == pytest.approx((OY + 0.55 * PH) / CH)
    plate = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.55, min_h=0.07
    )
    # The plate is glued to the subtitle: same centre.
    assert plate["y"] == pytest.approx(anchor, abs=0.001)


def test_srt_fallback_uses_the_shared_anchor():
    # Batch Q superseded the force_style burn itself (the SRT file
    # is now parsed back to cues and burned as PNG overlays), but
    # the intent stands: the fallback path resolves through the
    # shared anchor (subtitle_centre_px, via _subtitle_block_top_px)
    # and the shared size formula (subtitle_font_px, via
    # _resolve_png_subtitle_job) — no rim rule of its own.
    body = CORE_PY.split("def apply_production_overlays(", 1)[1]
    body = body.split("def extract_preview_with_overlays(", 1)[0]
    assert "_subtitle_block_top_px(" in body
    assert "_resolve_png_subtitle_job(" in body
    top_body = CORE_PY.split("def _subtitle_block_top_px(", 1)[1]
    top_body = top_body.split("def subtitle_plate_box(", 1)[0]
    assert "subtitle_centre_px(" in top_body
    resolve_body = CORE_PY.split("def _resolve_png_subtitle_job(", 1)[1]
    resolve_body = resolve_body.split("def apply_production_overlays(", 1)[0]
    assert "subtitle_font_px(" in resolve_body
    assert "parse_srt_cues(" in resolve_body


# ── 2. One height-relative font formula ────────────────────────────

@pytest.mark.parametrize(
    # Batch Q raised every anchor one notch (see Batch Q test file).
    "pt,frac", [(13, 0.032), (16, 0.040), (19, 0.048), (22, 0.056)]
)
def test_font_fraction_table(pt, frac):
    assert core._subtitle_font_fraction(pt) == pytest.approx(frac)


def test_font_sizes_distinct_and_exact():
    sizes = {pt: subtitle_font_px(CW, CH, pt, 1, LB) for pt in (13, 16, 19, 22)}
    assert sizes == {13: 61, 16: 77, 19: 92, 22: 108}
    assert len(set(sizes.values())) == 4  # no compression


def test_font_no_letterbox_scaling():
    for pt in (13, 16, 19, 22):
        assert subtitle_font_px(CW, CH, pt, 1, LB) == subtitle_font_px(CW, CH, pt, 1, None)


def test_font_two_line_factor():
    for pt in (13, 16, 19, 22):
        one = subtitle_font_px(CW, CH, pt, 1, None)
        two = subtitle_font_px(CW, CH, pt, 2, None)
        assert two == pytest.approx(one * 1.25, abs=1.5)
    assert subtitle_font_px(CW, CH, 16, 2, None) == 96


def test_font_minimum_and_interpolation():
    # Tiny canvas → the 18px floor.
    assert subtitle_font_px(320, 240, 13, 1, None) == 18
    # Interpolated sizes stay between their table neighbours.
    mid = subtitle_font_px(CW, CH, 18, 1, None)
    assert subtitle_font_px(CW, CH, 16, 1, None) < mid < subtitle_font_px(CW, CH, 19, 1, None)


def test_font_scales_with_canvas_height_only():
    # Same fraction at another canvas height (16:9 output).
    assert subtitle_font_px(1920, 1080, 22, 1, None) == round(1080 * 0.056)
    assert subtitle_font_px(1920, 1080, 16, 1, None) == round(1080 * 0.040)


# ── 3. Full-frame position geometry unchanged ──────────────────────

def test_full_frame_anchor_unchanged():
    assert subtitle_centre_px(CW, CH, None, 0.85) == pytest.approx(0.85 * CH)
    assert subtitle_anchor_y(CW, CH, None, sub_y=0.67) == pytest.approx(0.67)
    full = frame_layout("9:16", "blur", 1080, 1920)  # picture fills canvas
    assert subtitle_centre_px(CW, CH, full, 0.85) == pytest.approx(0.85 * CH)


def test_full_frame_plate_unchanged():
    # Position rule unchanged; the fitted height follows the font
    # (Batch Q: 77px at 16pt → h 0.0782 under the settle-fit
    # contract, above the caller's 0.07 minimum).
    plate = subtitle_plate_box(CW, CH, None, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    assert plate == {"x": 0.04, "y": 0.85, "w": 0.92, "h": 0.0782}
    # Safe-bottom clamp still applies full-frame.
    plate2 = subtitle_plate_box(CW, CH, None, raw_font_size=16, sub_lines=1, sub_y=0.98, min_h=0.07)
    assert abs((plate2["y"] + plate2["h"] / 2) - 0.92) < 1e-9


def test_full_frame_ass_margin_unchanged_rule(tmp_path):
    # Full-frame keeps the historical rule: centre at sub_y of the
    # canvas (only the font formula changed — 61px at 16pt).
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "ff.ass", CW, CH,
        raw_font_size=16, sub_lines=1, sub_y=0.85,
    )
    fields = _ass_fields(out)
    assert _ass_centre_px(fields, fields["font_size"]) == pytest.approx(0.85 * CH, abs=1.5)


def test_sub_y_none_keeps_legacy_fallbacks(tmp_path):
    # sub_y=None callers (margin/box/default chain) are untouched.
    assert subtitle_centre_px(CW, CH, LB, None) is None
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "n.ass", CW, CH,
        raw_font_size=16, sub_lines=1, sub_y=None, layout=LB,
    )
    fields = _ass_fields(out)
    assert fields["margin_v"] == max(20, int(round(CH * 0.12)))


# ── 4. Default placement on the live-test geometry ─────────────────

def test_default_subtitle_landing_letterbox():
    # Default sub_y=85, 16pt, 1 line — where a default job's
    # subtitle lands now (centre 61.1% of canvas, inside the
    # picture near its bottom; font 77px since Batch Q's bump).
    assert subtitle_anchor_y(CW, CH, LB, sub_y=0.85) == pytest.approx(0.6108, abs=0.001)
    assert subtitle_font_px(CW, CH, 16, 1, LB) == 77


# ── 5. Preview mirrors the same geometry (no mockup) ───────────────

def test_preview_has_no_font_mockup_table():
    assert "fontScaleMap" not in APP_JS


def test_preview_anchor_and_size_helpers():
    assert "function recapPictureRect()" in APP_JS
    assert "function subtitleAnchorCanvasFrac(" in APP_JS
    assert "function subtitleFontFraction(" in APP_JS
    # The JS table is the same table as core.py's.
    assert "[[13, 3.2], [16, 4.0], [19, 4.8], [22, 5.6]]" in APP_JS
    for pt, frac in core.SUBTITLE_FONT_HEIGHT_FRACTIONS:
        assert frac in (0.032, 0.040, 0.048, 0.056)


def test_preview_sub_and_plate_use_anchor():
    sub_body = APP_JS.split("function syncSubBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "subtitleAnchorCanvasFrac(y)" in sub_body
    fit_body = APP_JS.split("function fitBlurPlateToCaption()", 1)[1].split("\n    }", 1)[0]
    assert "subtitleAnchorCanvasFrac(plateY)" in fit_body
    preview_body = APP_JS.split("function updateSubtitleVisualPreview()", 1)[1]
    preview_body = preview_body.split("// 11. Font Size Picker", 1)[0]
    assert "subtitlePreviewFontPx()" in preview_body


# ── 6. Blur box basis audit ────────────────────────────────────────

def test_blur_box_basis_canvas_on_both_sides():
    # Batch AA deliberate rewrite of this pin: the INTERNAL box
    # format is still canvas fractions on both sides (render
    # consumers read fractions of the FULL output canvas)…
    assert "fractions of the FULL output canvas" in CORE_PY
    pipeline_py = (ENGINE_DIR / "recap_pipeline.py").read_text(encoding="utf-8")
    # …the LEGACY fallback hint (no explicit user box) still passes
    # the user's blur_y straight through as a canvas fraction…
    assert '_fallback_box = {"x": 0.05, "y": float(blur_y), "w": 0.90, "h": float(blur_h)}' in pipeline_py
    # …detected SOURCE bands are converted to canvas fractions…
    assert "source_band_to_canvas_box(band, _band_layout)" in pipeline_py
    # …but the USER-facing basis changed in Batch AA: blur_y/blur_h
    # are picture-relative, the preview paints through
    # blurBoxCanvasGeom, and an explicit box is converted by
    # user_blur_box_to_canvas_box (full pins in the Batch AA file).
    sync_body = APP_JS.split("function syncBlurBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "blurBoxCanvasGeom(y, h)" in sync_body
    assert "user_blur_box_to_canvas_box(blur_y, blur_h, _layout)" in pipeline_py


# ── 7. Cache version ───────────────────────────────────────────────

def test_cache_version_r50():
    assert INDEX_HTML.count("?v=20261104_r57") == 4
    assert "20261104_r48" not in INDEX_HTML
