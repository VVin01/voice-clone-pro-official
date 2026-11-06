"""Batch J — recap subtitle-blur SAFE-FIXES (2026-11-03).

Pins the eight audit fixes:

1. the fallback blur_y hint is reconciled against the plate (no
   double-blurred overlap strip);
2. a final merge pass unions ANY overlapping boxes — detected
   extras against each other, and the watermark box against a
   top-positioned plate;
3. blur-box assembly is gated on blur_subtitles (Blur OFF ⇒ zero
   boxes; Burn OFF + Blur ON ⇒ no bare plate strip);
4. source-band detection scans the full frame height (top bands);
5. the engine preview builds boxes through the same assembly helper
   as the render path;
6. the no-coverage warning ("blur_no_source_band") is produced by
   the pipeline and surfaced through the recap status payload;
7. the downloader preview wrapper's NameError is gone;
8. shipped box lists are pairwise non-overlapping across a
   slider/detection matrix (the overlay pass blurs once per box,
   sequentially, so any overlap double-blurs).

All offline; the detector test reuses the synthetic-clip pattern
from test_round4_2026_11_01.py and skips without ffmpeg.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "services" / "recap_engine"))

from core import (  # noqa: E402
    detect_source_subtitle_bands,
    frame_layout,
    subtitle_anchor_y,
    subtitle_plate_box,
)
from recap_pipeline import (  # noqa: E402
    assemble_blur_boxes,
    merge_overlapping_blur_boxes,
)

PIPELINE_PY = (ROOT / "app" / "services" / "recap_engine" / "recap_pipeline.py").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
DOWNLOADER_PY = (ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")

LAYOUT = frame_layout("16:9", "crop", 1920, 1080)
CW, CH = LAYOUT["cw"], LAYOUT["ch"]


def _plate(sub_y: float) -> dict:
    # Batch P note: these scenarios originally used raw_font_size=54
    # (outside the API's 12–30 range), relying on the old formula's
    # 62px clamp. The Batch P height-fraction formula is unbounded,
    # so the scenarios now use 22 — the largest picker size — which
    # reproduces the intended "large plate" geometry.
    return subtitle_plate_box(
        CW, CH, LAYOUT, raw_font_size=22, sub_lines=2, sub_y=sub_y, min_h=0.07
    )


def _anchor(sub_y: float) -> float:
    return subtitle_anchor_y(
        CW, CH, LAYOUT, sub_y=sub_y, raw_font_size=22, sub_lines=2
    )


def _assemble(**kw):
    kw.setdefault("layout", LAYOUT)
    kw.setdefault("canvas_w", CW)
    kw.setdefault("canvas_h", CH)
    return assemble_blur_boxes(**kw)


def _spans_overlap(a: dict, b: dict) -> bool:
    v = abs(a["y"] - b["y"]) < (a["h"] + b["h"]) / 2.0 - 1e-9
    h = abs((a["x"] + a["w"] / 2.0) - (b["x"] + b["w"] / 2.0)) < (a["w"] + b["w"]) / 2.0 - 1e-9
    return v and h


def _assert_disjoint(boxes):
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not _spans_overlap(boxes[i], boxes[j]), (boxes[i], boxes[j])


def _covers(outer: dict, inner: dict, tol: float = 0.001) -> bool:
    return (
        outer["y"] - outer["h"] / 2.0 <= inner["y"] - inner["h"] / 2.0 + tol
        and outer["y"] + outer["h"] / 2.0 >= inner["y"] + inner["h"] / 2.0 - tol
        and outer["x"] <= inner["x"] + tol
        and outer["x"] + outer["w"] >= inner["x"] + inner["w"] - tol
    )


# ── Item 1: fallback hint reconciled against the plate ─────────────

def test_fallback_box_overlapping_plate_merges_into_one():
    plate = _plate(0.85)
    fallback = {"x": 0.04, "y": 0.88, "w": 0.92, "h": 0.07}
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], fallback_box=fallback, anchor_y=_anchor(0.85),
    )
    assert covered is True
    # One band only — the fallback no longer stacks on the plate.
    assert len(boxes) == 1
    _assert_disjoint(boxes)
    # The merged band covers both originals (no coverage lost).
    assert _covers(boxes[0], plate)
    assert _covers(boxes[0], fallback)


def test_fallback_box_far_from_plate_kept_as_single_extra():
    plate = _plate(0.85)
    fallback = {"x": 0.04, "y": 0.30, "w": 0.92, "h": 0.07}
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], fallback_box=fallback, anchor_y=_anchor(0.85),
    )
    assert covered is True
    assert len(boxes) == 2
    _assert_disjoint(boxes)


# ── Item 2: extra↔extra and watermark merges ───────────────────────

def test_two_overlapping_detected_extras_merge():
    plate = _plate(0.85)
    bands = [
        {"x": 0.0, "y": 0.30, "w": 1.0, "h": 0.10},
        {"x": 0.0, "y": 0.36, "w": 1.0, "h": 0.10},
    ]
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=bands, anchor_y=_anchor(0.85),
    )
    assert covered is True
    # Plate + ONE merged extra (the two neighbours unioned).
    assert len(boxes) == 2
    _assert_disjoint(boxes)
    merged = [b for b in boxes if abs(b["y"] - 0.33) < 0.02]
    assert len(merged) == 1
    assert merged[0]["h"] >= 0.15  # union of the two 0.10 bands


def test_watermark_merges_with_top_positioned_plate():
    plate = _plate(0.10)
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], blur_watermark=True, anchor_y=_anchor(0.10),
    )
    # Watermark {0.74, 0.03, 0.24, 0.12} overlaps the top plate; the
    # final pass must ship ONE union box, not two stacked ones.
    assert len(boxes) == 1
    _assert_disjoint(boxes)
    union = boxes[0]
    assert union["y"] - union["h"] / 2.0 <= -0.029  # watermark top edge
    assert union["x"] + union["w"] >= 0.979        # watermark right edge
    assert _covers(union, plate)


def test_watermark_stays_separate_from_bottom_plate():
    plate = _plate(0.85)
    boxes, _covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], blur_watermark=True, anchor_y=_anchor(0.85),
    )
    assert len(boxes) == 2
    _assert_disjoint(boxes)


def test_merge_pass_itself_is_a_fixpoint_union():
    boxes = merge_overlapping_blur_boxes([
        {"x": 0.0, "y": 0.50, "w": 1.0, "h": 0.10},
        {"x": 0.0, "y": 0.56, "w": 1.0, "h": 0.10},
        {"x": 0.0, "y": 0.62, "w": 1.0, "h": 0.10},
    ])
    assert len(boxes) == 1
    assert abs(boxes[0]["y"] - 0.56) < 0.011
    assert boxes[0]["h"] >= 0.21


# ── Item 3: the Blur toggle is the master gate ─────────────────────

def test_blur_off_zero_boxes_even_with_detection_hits():
    boxes, covered = _assemble(
        blur_subtitles=False, burn_subtitles=True, plate=_plate(0.85),
        detected_bands=[{"x": 0.0, "y": 0.70, "w": 1.0, "h": 0.10}],
        fallback_box={"x": 0.04, "y": 0.60, "w": 0.92, "h": 0.07},
        blur_watermark=True, anchor_y=_anchor(0.85),
    )
    assert boxes == []
    assert covered is False


def test_burn_off_blur_on_no_plate_strip_without_a_band():
    plate = _plate(0.85)
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=False, plate=plate,
        detected_bands=[], anchor_y=_anchor(0.85),
    )
    assert boxes == []
    assert covered is False


def test_burn_off_blur_on_far_band_ships_without_plate():
    plate = _plate(0.85)
    band = {"x": 0.0, "y": 0.45, "w": 1.0, "h": 0.10}
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=False, plate=plate,
        detected_bands=[band], anchor_y=_anchor(0.85),
    )
    assert covered is True
    assert boxes == [band]  # the band only — no plate-shaped strip


def test_burn_off_blur_on_band_merging_into_plate_ships_the_plate():
    plate = _plate(0.85)
    band = {"x": 0.0, "y": 0.86, "w": 1.0, "h": 0.10}
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=False, plate=plate,
        detected_bands=[band], anchor_y=_anchor(0.85),
    )
    assert covered is True
    assert len(boxes) == 1
    assert _covers(boxes[0], band)


def test_standard_case_geometry_unchanged():
    # Bottom subtitles, both toggles on, detection hit near the
    # plate: exactly the pre-fix geometry — plate union band, nothing
    # else (the merge pass must not invent or move boxes).
    plate = _plate(0.85)
    band = {"x": 0.0, "y": 0.84, "w": 1.0, "h": 0.09}
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[band], anchor_y=_anchor(0.85),
    )
    assert covered is True
    assert len(boxes) == 1
    assert _covers(boxes[0], plate)
    assert _covers(boxes[0], band)


# ── Item 4: full-frame detection finds a top band ──────────────────

def _synthetic_clip(tmp_path: Path, name: str, band_rows) -> Path:
    """Same pattern as test_round4_2026_11_01._synthetic_clip: a dark
    clip whose given rows carry bright glyph-like stripes, persistent
    in every frame."""
    from PIL import Image

    img = Image.new("L", (320, 240), 40)
    px = img.load()
    for y in band_rows:
        for x in range(320):
            px[x, y] = 235 if (x // 3) % 2 == 0 else 25
    png = tmp_path / f"{name}.png"
    img.save(png)
    out = tmp_path / f"{name}.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    res = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-loop", "1", "-i", str(png),
         "-t", "6", "-pix_fmt", "yuv420p", "-r", "10", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return out


def test_detector_finds_top_band_above_the_old_scan_floor(tmp_path):
    from recap_pipeline import find_binary

    ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
    # Rows 24-43 of 240 → y ≈ 0.10-0.18, far above the old 0.40 scan
    # floor, which could never see this band at all.
    top = _synthetic_clip(tmp_path, "topband", band_rows=tuple(range(24, 44)))
    boxes = detect_source_subtitle_bands(
        ffmpeg, ffprobe, top, tmp_path / "wtop", log=lambda m: None
    )
    assert len(boxes) == 1
    assert boxes[0]["y"] < 0.40
    assert boxes[0]["y"] - boxes[0]["h"] / 2.0 < 0.10


# ── Item 5: preview/render parity ──────────────────────────────────

def test_both_pipeline_paths_use_the_shared_assembly():
    assert "def assemble_blur_boxes(" in PIPELINE_PY
    render_body = PIPELINE_PY.split("def run_recap_pipeline(", 1)[1].split("\ndef ", 1)[0]
    preview_body = PIPELINE_PY.split("def generate_recap_preview(", 1)[1].split("\ndef ", 1)[0]
    assert "assemble_blur_boxes(" in render_body
    assert "assemble_blur_boxes(" in preview_body
    # Detection in the render path is gated on the Blur toggle now.
    # (Batch AA deliberate pin update: detection is additionally
    # skipped when an explicit user box rules the source band.
    # Batch BI deliberate pin update: the user's auto-detect switch
    # is a third gate on the same condition.)
    assert "if blur_subtitles and _user_box is None and blur_auto_detect:\n        try:" in render_body
    assert 'if burn_subtitles:\n        # Auto-cover' not in PIPELINE_PY


def test_canvas_draws_plate_and_hint_as_separate_boxes():
    # Batch AK rewrite: the passive CSS plate box is GONE — the
    # engine-composited frame carries the real plate, so the canvas
    # holds only the draggable hint box plus the outline handles.
    assert 'id="preview-plate-box"' not in INDEX_HTML
    assert 'getElementById("preview-plate-box")' not in APP_JS
    assert "function fitBlurPlateToCaption()" not in APP_JS
    # The hint box still follows the Blur sliders (unchanged wiring;
    # Batch AA deliberate pin update: the paint is now
    # picture-relative via blurBoxCanvasGeom).
    sync_body = APP_JS.split("function syncBlurBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "previewBlur.style.height = `${(geom.height * 100).toFixed(3)}%`;" in sync_body
    assert "scheduleRecapCompositeRefresh();" in sync_body


# ── Item 6: no-coverage warning plumbing ───────────────────────────

def test_no_coverage_scenarios_report_uncovered():
    plate = _plate(0.85)
    # Detection empty, hint coincides with sub_y (caller passes no
    # fallback box) → nothing covers a source band.
    _boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], fallback_box=None, anchor_y=_anchor(0.85),
    )
    assert covered is False
    # Detection never ran / failed (None) → also uncovered.
    _boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=None, fallback_box=None, anchor_y=_anchor(0.85),
    )
    assert covered is False


def test_fallback_dropped_into_letterbox_padding_is_not_coverage():
    layout = frame_layout("16:9", "blur", 1920, 800)
    assert layout["oy"] > 0  # letterboxed: content starts below 0
    plate = subtitle_plate_box(
        layout["cw"], layout["ch"], layout,
        raw_font_size=22, sub_lines=2, sub_y=0.85, min_h=0.07,
    )
    fallback = {"x": 0.04, "y": 0.05, "w": 0.92, "h": 0.06}  # in the top bar
    boxes, covered = assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], fallback_box=fallback,
        layout=layout, canvas_w=layout["cw"], canvas_h=layout["ch"],
        anchor_y=subtitle_anchor_y(
            layout["cw"], layout["ch"], layout,
            sub_y=0.85, raw_font_size=22, sub_lines=2,
        ),
    )
    assert covered is False
    assert len(boxes) == 1  # just the plate; the hint was dropped


def test_warning_plumbed_from_pipeline_to_status_payload():
    assert '_job_warnings.append("blur_no_source_band")' in PIPELINE_PY
    assert '"warnings": _job_warnings' in PIPELINE_PY
    # Runner stores it on the entry, persists it, and both status
    # branches serialize it.
    assert 'entry["warnings"] = list(recap_res.get("warnings") or [])' in API_PY
    assert "await _persist_recap_warnings(entry)" in API_PY
    assert '"warnings": list(entry.get("warnings") or [])' in API_PY
    assert '"warnings": _db_warnings' in API_PY
    # Result card shows it via the shared i18n key.
    assert 't("warn_blur_no_source_band")' in APP_JS
    assert 'id="recap-blur-warning"' in INDEX_HTML


# ── Item 7: downloader wrapper NameError fixed, wrapper kept ───────

def test_downloader_preview_wrapper_has_no_undefined_title_text():
    body = DOWNLOADER_PY.split("def generate_recap_preview(", 1)[1].split("\n    def ", 1)[0]
    # The NameError patterns are gone (the name may still appear in
    # the explanatory comment); the wrapper itself is kept.
    assert "if not title_text" not in body
    assert "title_text =" not in body
    assert "generate_recap_preview" in DOWNLOADER_PY


# ── Item 8: pairwise non-overlap across a scenario matrix ──────────

_BAND_SETS = [
    [],
    [{"x": 0.0, "y": 0.70, "w": 1.0, "h": 0.10}],
    [{"x": 0.0, "y": 0.30, "w": 1.0, "h": 0.10}, {"x": 0.0, "y": 0.36, "w": 1.0, "h": 0.10}],
    [{"x": 0.0, "y": 0.12, "w": 1.0, "h": 0.12}],
    [{"x": 0.0, "y": 0.84, "w": 1.0, "h": 0.09}, {"x": 0.0, "y": 0.30, "w": 1.0, "h": 0.10}],
]


@pytest.mark.parametrize("sub_y", [0.10, 0.67, 0.85])
@pytest.mark.parametrize("bands", _BAND_SETS)
@pytest.mark.parametrize("watermark", [False, True])
@pytest.mark.parametrize("burn", [True, False])
def test_final_boxes_pairwise_non_overlapping_matrix(sub_y, bands, watermark, burn):
    plate = _plate(sub_y)
    diverged = {"x": 0.04, "y": min(0.97, sub_y + 0.13), "w": 0.92, "h": 0.07}
    boxes, _covered = _assemble(
        blur_subtitles=True, burn_subtitles=burn, plate=plate,
        detected_bands=[dict(b) for b in bands],
        fallback_box=None if bands else diverged,
        blur_watermark=watermark, anchor_y=_anchor(sub_y),
    )
    _assert_disjoint(boxes)
    if not burn:
        # Burn OFF: no box may be the untouched plate on its own —
        # every shipped box must trace to a band/fallback/watermark.
        for b in boxes:
            assert b != plate
