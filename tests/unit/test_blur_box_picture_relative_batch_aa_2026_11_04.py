"""Batch AA (2026-11-04): the blur box is drawn on the PICTURE and
rendered exactly as drawn (the owner's recapter standard).

Contract chosen for this batch — blur_y / blur_h are PICTURE-relative
end-to-end:
* Mini App sliders hold percents of the PICTURE height; the preview
  paints centre = oy + blur_y × ph, height = blur_h × ph (canvas
  fractions) via blurBoxCanvasGeom — the Batch P machinery.
* The submit payload and the persisted recap_config carry the same
  picture percents (ints), so the History display and the re-run
  prefill keep reading them unchanged.
* The engine's single conversion point is
  core.user_blur_box_to_canvas_box, which applies the same layout
  math; every downstream consumer keeps reading canvas fractions.

"Explicit user box" rule: the client marks a DELIBERATE set with
blur_box_explicit (slider input / box drag / resize handles set
state.recapBlurBoxTouched; the settings snapshot and the History
prefill carry it; RecapConfigRequest persists it in recap_config).
A marked box is authoritative at render: detection is not run for
the source band and the box ships as drawn. Unmarked jobs (every
pre-AA client / stored config) keep the legacy plate + detection +
fallback-hint path byte-for-byte.

Pins below: picture-relative paint + conversion math on a
letterboxed layout (16:9 source in a 9:16 canvas), drag/resize in
picture fractions, plate-growth cap, new-source reset, the explicit
rule both ways (including values equal to the defaults), detection
not consulted when a user box is present, the legacy path's
byte-identical assembly, Blur OFF ⇒ zero boxes, and the r59 cache
bump.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "app" / "services" / "recap_engine"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ENGINE_DIR))

import core  # noqa: E402
from core import (  # noqa: E402
    frame_layout,
    subtitle_anchor_y,
    subtitle_plate_box,
    user_blur_box_to_canvas_box,
)
from recap_pipeline import assemble_blur_boxes  # noqa: E402

APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
CORE_PY = (ENGINE_DIR / "core.py").read_text(encoding="utf-8")
PIPELINE_PY = (ENGINE_DIR / "recap_pipeline.py").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
DOWNLOADER_PY = (ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")

# Letterboxed test geometry (Batch P's): 1080×1920 canvas, 16:9
# picture centred — oy=656, ph=608 of 1920 (oy=0.3417, ph=0.3167).
LB = frame_layout("9:16", "blur", 1920, 1080)
CW, CH = LB["cw"], LB["ch"]
OY_F, PH_F = LB["oy"] / CH, LB["ph"] / CH


def _js_blur_geom(y_pct: float, h_pct: float) -> tuple[float, float]:
    """Mirror of app.js blurBoxCanvasGeom() for this layout."""
    y, h = y_pct / 100.0, h_pct / 100.0
    if (OY_F + PH_F) < 1 - 0.002:
        return OY_F + y * PH_F, h * PH_F
    return y, h


def _plate(sub_y=0.85) -> dict:
    return subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=sub_y, min_h=0.07
    )


def _anchor(sub_y=0.85) -> float:
    return subtitle_anchor_y(CW, CH, LB, sub_y=sub_y)


def _assemble(**kw):
    kw.setdefault("layout", LB)
    kw.setdefault("canvas_w", CW)
    kw.setdefault("canvas_h", CH)
    return assemble_blur_boxes(**kw)


# ── 1. Picture-relative conversion (engine) ────────────────────────

@pytest.mark.parametrize(
    "blur_y,blur_h",
    [(0.85, 0.07), (0.55, 0.22), (0.30, 0.10), (0.10, 0.03), (0.95, 0.50)],
)
def test_user_box_letterbox_conversion(blur_y, blur_h):
    box = user_blur_box_to_canvas_box(blur_y, blur_h, LB)
    # Centre/height are picture-relative: oy + y×ph, h×ph of canvas.
    assert box["y"] == pytest.approx(OY_F + blur_y * PH_F, abs=0.001)
    assert box["h"] == pytest.approx(blur_h * PH_F, abs=0.001)
    # Width stays 92% centred — the preview's CSS basis.
    assert box["x"] == 0.04
    assert box["w"] == 0.92
    # The centre always lands INSIDE the picture, never in the fill.
    assert OY_F <= box["y"] <= OY_F + PH_F


def test_user_box_defaults_on_letterbox():
    # The tight defaults (85/7) land at the same canvas centre as
    # the default subtitle — 61.1% of canvas, not 85%.
    box = user_blur_box_to_canvas_box(0.85, 0.07, LB)
    assert box == {"x": 0.04, "y": 0.6108, "w": 0.92, "h": 0.0222}


def test_user_box_full_frame_is_identity():
    full = frame_layout("9:16", "blur", 1080, 1920)  # picture fills canvas
    assert user_blur_box_to_canvas_box(0.85, 0.07, full) == {
        "x": 0.04, "y": 0.85, "w": 0.92, "h": 0.07,
    }
    assert user_blur_box_to_canvas_box(0.30, 0.10, full)["y"] == 0.30


def test_user_box_no_layout_falls_back_to_canvas():
    # Probe-failure path: picture assumed to fill the canvas, like
    # the plate's no-layout fallback.
    assert user_blur_box_to_canvas_box(0.85, 0.07, None) == {
        "x": 0.04, "y": 0.85, "w": 0.92, "h": 0.07,
    }


def test_user_box_clamps_out_of_range_inputs():
    box = user_blur_box_to_canvas_box(1.7, -0.5, LB)
    assert box["y"] == pytest.approx(OY_F + PH_F, abs=0.001)
    assert box["h"] == 0.0


# ── 2. Preview paint mirrors the conversion ────────────────────────

def test_preview_paints_picture_relative():
    assert "function blurBoxCanvasGeom(yPercent, hPercent, xPercent, wPercent)" in APP_JS
    geom_body = APP_JS.split("function blurBoxCanvasGeom(", 1)[1].split("\n    }", 1)[0]
    assert "centre: r.oy + y * r.ph" in geom_body
    assert "height: h * r.ph" in geom_body
    sync_body = APP_JS.split("function syncBlurBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "blurBoxCanvasGeom(y, h, x, w)" in sync_body
    assert "previewBlur.style.top = `${(geom.centre * 100).toFixed(3)}%`;" in sync_body
    assert "previewBlur.style.height = `${(geom.height * 100).toFixed(3)}%`;" in sync_body
    # The old canvas-% paint is gone.
    assert "previewBlur.style.top = `${y}%`;" not in sync_body


@pytest.mark.parametrize("y_pct,h_pct", [(85, 7), (55, 22), (30, 10)])
def test_preview_and_engine_agree_exactly(y_pct, h_pct):
    centre, height = _js_blur_geom(y_pct, h_pct)
    box = user_blur_box_to_canvas_box(y_pct / 100.0, h_pct / 100.0, LB)
    assert box["y"] == pytest.approx(centre, abs=0.001)
    assert box["h"] == pytest.approx(height, abs=0.001)


def test_engine_helper_is_the_single_conversion_point():
    assert "def user_blur_box_to_canvas_box(" in CORE_PY
    # Both pipeline paths convert through it (Batch AG: with the
    # horizontal geometry threaded through the same call).
    assert PIPELINE_PY.count("user_blur_box_to_canvas_box(blur_y, blur_h, _layout, blur_x=blur_x, blur_w=blur_w)") == 1
    assert PIPELINE_PY.count("user_blur_box_to_canvas_box(blur_y, blur_h, _p_layout, blur_x=blur_x, blur_w=blur_w)") == 1


# ── 3. Drag / resize operate in picture fractions ──────────────────

def test_blur_drag_scales_delta_like_subtitle_drag():
    drag_body = APP_JS.split("// Attach dragging to Blur Box", 1)[1]
    drag_body = drag_body.split("// Attach dragging to Subtitle Box", 1)[0]
    # The same 1/ph letterbox conversion Batch P gave the subtitle.
    assert "1 / r.ph" in drag_body
    assert "recapPictureRect()" in drag_body
    assert "state.recapBlurBoxTouched = true;" in drag_body


def test_blur_resize_scales_delta_and_clamps():
    resize_body = APP_JS.split("function attachBlurResize(", 1)[1]
    resize_body = resize_body.split("attachBlurResize(blurResizeTop", 1)[0]
    assert "deltaPct * deltaScale" in resize_body
    assert "1 / r.ph" in resize_body
    # The slider's own range clamp is unchanged.
    assert "Math.max(3, Math.min(22" in resize_body
    assert "state.recapBlurBoxTouched = true;" in resize_body


def test_blur_sliders_mark_the_box_explicit():
    y_body = APP_JS.split('sliderBlurY.addEventListener("input"', 1)[1].split("});", 1)[0]
    h_body = APP_JS.split('sliderBlurH.addEventListener("input"', 1)[1].split("});", 1)[0]
    assert "state.recapBlurBoxTouched = true;" in y_body
    assert "state.recapBlurBoxTouched = true;" in h_body


# ── 4. Plate growth is capped (no centre-delta inflation) ──────────

def test_plate_fit_has_no_centre_delta_term():
    # Batch AK rewrite: the JS plate fit (fitBlurPlateToCaption) is
    # RETIRED — the Mini App preview is the engine-composited frame,
    # so the plate the user sees is the engine's own. The centre-
    # delta inflation term is gone with the function.
    assert "function fitBlurPlateToCaption()" not in APP_JS
    assert "centreDelta" not in APP_JS


def test_plate_fit_growth_is_capped_at_a_quarter_canvas():
    # Batch AK rewrite: the cap now lives where the plate is really
    # built — the engine. Even the largest legitimate plate (22pt,
    # two lines, full-frame) stays under a quarter of the canvas,
    # and the engine's absolute plate clamp is half the canvas.
    font_px = core.subtitle_font_px(1080, 1920, 22, 2, None)
    plate_h = core._plate_height_px(font_px, 2, 1920)
    assert plate_h <= 0.25 * 1920
    assert core._plate_height_px(400, 2, 1920) <= 0.50 * 1920
    assert core.SUBTITLE_PLATE_WIDTH_FRACTION == 0.92  # sanity anchor


def test_plate_initial_height_uses_picture_relative_floor():
    # Batch AK rewrite: the subtitle drag handle's height comes from
    # the picture-relative plate fraction (the same formula the
    # engine uses at the 608px reference picture height).
    sub_body = APP_JS.split("function syncSubBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "subtitlePlateHeightFracOfPicture()" in sub_body
    assert "subtitleAnchorCanvasFrac(centre)" in sub_body


def test_reset_function_restores_tight_defaults():
    body = APP_JS.split("function resetBlurBoxForNewSource()", 1)[1]
    body = body.split("\n    }", 1)[0]
    assert "state.recapBlurY = 85;" in body
    assert "state.recapBlurH = 7;" in body
    # Batch AK: the reset restores the 5% bottom-margin default
    # (the centre mirror is re-derived by syncSubBoxVisual).
    assert "state.recapSubMargin = 5;" in body
    assert "state.recapBlurBoxTouched = false;" in body
    assert "sliderBlurY.value = 85;" in body
    assert "sliderBlurH.value = 7;" in body
    assert "sliderSubY.value = 5;" in body
    assert "syncBlurBoxVisual();" in body
    assert "syncSubBoxVisual();" in body
    assert "saveRecapSettings();" in body


def test_reset_fires_on_analyze_success_for_a_new_url_only():
    body = APP_JS.split('const _aaSourceKey = "url:" + (data.url || url);', 1)[1]
    body = body.split("R40 item 9", 1)[0]
    assert "state.recapLoadedSourceKey !== _aaSourceKey" in body
    assert "resetBlurBoxForNewSource();" in body


def test_reset_fires_on_upload_success():
    body = APP_JS.split('const _aaUploadKey = "file:" + (data.local_path || "");', 1)[1]
    body = body.split('if (data.thumbnail)', 1)[0]
    assert "state.recapLoadedSourceKey !== _aaUploadKey" in body
    assert "resetBlurBoxForNewSource();" in body


def test_prefill_does_not_reset_and_marks_its_source():
    body = APP_JS.split("window.prefillRecapFromHistory = function(cfg, item) {", 1)[1]
    body = body.split("// Initial canvas mockup sync", 1)[0]
    assert "resetBlurBoxForNewSource" not in body
    assert 'state.recapLoadedSourceKey = "url:" + srcUrl;' in body
    assert 'state.recapLoadedSourceKey = "file:" + srcUrl;' in body


# ── 6. The explicit marker, end-to-end ─────────────────────────────

def test_marker_state_snapshot_and_payload():
    assert "recapBlurBoxTouched: false," in APP_JS
    assert '"recapBlurBoxTouched"' in APP_JS  # snapshot bool list
    payload = APP_JS.split("const payload = {", 1)[1].split("};", 1)[0]
    assert "blur_box_explicit: !!state.recapBlurBoxTouched," in payload
    # The payload still carries picture percents straight from the
    # sliders (the QA-batch pin, unchanged).
    assert "blur_y: sliderBlurY ? parseInt(sliderBlurY.value) : 85," in payload
    assert "blur_h: sliderBlurH ? parseInt(sliderBlurH.value) : 7," in payload


def test_marker_on_the_request_model_and_persisted():
    assert "blur_box_explicit: bool = False" in API_PY
    # …it rides model_dump into the persisted recap_config and back
    # out through the rebuild used by retry / prefill.
    assert '"config": req.model_dump()' in API_PY
    assert "return RecapConfigRequest(**state[\"config\"])" in API_PY
    # The runner passes it into the downloader funnel.
    assert "blur_box_explicit=bool(req.blur_box_explicit)," in API_PY


def test_marker_round_trips_through_the_request_model():
    import app.webapp.api as api_mod

    plain = api_mod.RecapConfigRequest(url="https://example.com/v")
    assert plain.blur_box_explicit is False  # pre-AA clients/configs
    marked = api_mod.RecapConfigRequest(
        url="https://example.com/v", blur_y=85, blur_h=7, blur_box_explicit=True
    )
    assert marked.blur_box_explicit is True
    # A persisted blob rebuild keeps the marker (and the values).
    rebuilt = api_mod.RecapConfigRequest(**marked.model_dump())
    assert rebuilt.blur_box_explicit is True
    assert (rebuilt.blur_y, rebuilt.blur_h) == (85, 7)


def test_marker_plumbed_through_downloader_and_pipeline():
    assert DOWNLOADER_PY.count("blur_box_explicit: bool = False") == 2
    assert "blur_box_explicit=blur_box_explicit," in DOWNLOADER_PY
    assert PIPELINE_PY.count("blur_box_explicit: bool = False") == 2


def test_prefill_carries_the_marker():
    body = APP_JS.split("window.prefillRecapFromHistory = function(cfg, item) {", 1)[1]
    body = body.split("// Initial canvas mockup sync", 1)[0]
    assert "recapBlurBoxTouched: cfg.blur_box_explicit === true," in body


def test_history_display_still_reads_stored_percents():
    # recap_settings keeps surfacing the stored picture percents;
    # the display formatter is unchanged.
    assert '"blur_y": _cfg.get("blur_y"),' in API_PY
    assert "pctOf(s.blur_y)" in APP_JS


# ── 7. Explicit box is authoritative at render ─────────────────────

def test_user_box_verbatim_and_detection_ignored():
    plate = _plate()
    user = user_blur_box_to_canvas_box(0.30, 0.10, LB)
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        # A detected band AND a fallback hint are both present —
        # with a user box neither may influence the output.
        detected_bands=[{"x": 0.0, "y": 0.50, "w": 1.0, "h": 0.10}],
        fallback_box={"x": 0.05, "y": 0.40, "w": 0.90, "h": 0.07},
        user_box=user, anchor_y=_anchor(),
    )
    assert covered is True
    assert boxes == [plate, user]  # plate untouched, user box verbatim


def test_user_box_at_defaults_is_still_authoritative():
    # Deliberate set with values EQUAL to the defaults (85/7): the
    # marker, not the values, decides — detection is still ignored.
    # (Geometrically the box coincides with the plate here, so the
    # Batch J overlap merge folds them into the one band that covers
    # the drawn box exactly.)
    plate = _plate()
    user = user_blur_box_to_canvas_box(0.85, 0.07, LB)
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[{"x": 0.0, "y": 0.30, "w": 1.0, "h": 0.10}],
        user_box=user, anchor_y=_anchor(),
    )
    assert covered is True
    assert len(boxes) == 1
    merged = boxes[0]
    assert merged["y"] - merged["h"] / 2 <= user["y"] - user["h"] / 2 + 1e-9
    assert merged["y"] + merged["h"] / 2 >= user["y"] + user["h"] / 2 - 1e-9


def test_user_box_alone_in_letterbox_padding_is_never_dropped():
    # A box drawn at the picture's very top (85%→ picture-relative
    # low numbers) — the legacy reconcile drops bands lying in the
    # letterbox padding; the user box bypasses the reconcile.
    plate = _plate()
    user = user_blur_box_to_canvas_box(0.02, 0.05, LB)
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], user_box=user, anchor_y=_anchor(),
    )
    assert covered is True
    assert user in boxes


def test_user_box_burn_off_ships_without_plate():
    plate = _plate()
    user = user_blur_box_to_canvas_box(0.30, 0.10, LB)
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=False, plate=plate,
        detected_bands=[{"x": 0.0, "y": 0.50, "w": 1.0, "h": 0.10}],
        user_box=user, anchor_y=_anchor(),
    )
    assert covered is True
    assert boxes == [user]


def test_blur_off_zero_boxes_even_with_user_box():
    boxes, covered = _assemble(
        blur_subtitles=False, burn_subtitles=True, plate=_plate(),
        user_box=user_blur_box_to_canvas_box(0.30, 0.10, LB),
        anchor_y=_anchor(),
    )
    assert boxes == []
    assert covered is False


def test_detection_not_run_when_user_box_present():
    render_body = PIPELINE_PY.split("def run_recap_pipeline(", 1)[1]
    render_body = render_body.split("\ndef ", 1)[0]
    assert "if blur_subtitles and blur_box_explicit:" in render_body
    assert "_user_box = user_blur_box_to_canvas_box(blur_y, blur_h, _layout, blur_x=blur_x, blur_w=blur_w)" in render_body
    # Batch BI deliberate pin update: both detection gates gained
    # the user's auto-detect switch as a further condition — the
    # user-box suppression this test pins is unchanged.
    assert "if blur_subtitles and _user_box is None and blur_auto_detect:" in render_body
    assert "user_box=_user_box," in render_body
    preview_body = PIPELINE_PY.split("def generate_recap_preview(", 1)[1]
    preview_body = preview_body.split("\ndef ", 1)[0]
    assert "_p_user_box = user_blur_box_to_canvas_box(blur_y, blur_h, _p_layout, blur_x=blur_x, blur_w=blur_w)" in preview_body
    assert "if _p_user_box is None and blur_auto_detect:" in preview_body
    assert "user_box=_p_user_box," in preview_body


# ── 8. No user box ⇒ legacy path byte-for-byte ─────────────────────

def test_legacy_assembly_unchanged_without_user_box():
    plate = _plate()
    band = {"x": 0.0, "y": 0.84, "w": 1.0, "h": 0.09}
    # Same scenario as the Batch J standard-case pin (canvas-space
    # layout there); here the letterbox plate + a band beside it.
    boxes, covered = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[dict(band)], anchor_y=_anchor(),
    )
    assert covered is True
    # The band sits fully below the picture (in the letterbox fill)
    # → the legacy reconcile drops it; only the plate ships.
    assert boxes == [plate]
    # The fallback hint still reconciles exactly as before: a hint
    # overlapping the plate merges into ONE band covering both.
    fallback = {"x": 0.04, "y": 0.63, "w": 0.92, "h": 0.07}
    boxes2, covered2 = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], fallback_box=fallback, anchor_y=_anchor(),
    )
    assert covered2 is True
    assert len(boxes2) == 1
    merged = boxes2[0]
    assert merged["y"] - merged["h"] / 2 <= plate["y"] - plate["h"] / 2 + 0.001
    assert merged["y"] + merged["h"] / 2 >= fallback["y"] + fallback["h"] / 2 - 0.001
    # …and user_box=None is exactly the pre-AA call shape.
    boxes3, covered3 = _assemble(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], fallback_box=None, user_box=None,
        anchor_y=_anchor(),
    )
    assert boxes3 == [plate]
    assert covered3 is False


def test_legacy_fallback_hint_construction_unchanged():
    # The legacy hint keeps its historical canvas-fraction shape and
    # its |blur_y − sub_y| gate (Batch J / Batch P pins).
    # Batch AK: the gate now compares against the EFFECTIVE centre
    # (_sub_centre_eff / _p_centre_eff), which IS sub_y whenever
    # sub_margin is None — the legacy gate is byte-equivalent — and
    # is the margin-derived centre in bottom-margin mode.
    assert '_fallback_box = {"x": 0.05, "y": float(blur_y), "w": 0.90, "h": float(blur_h)}' in PIPELINE_PY
    assert '_p_fallback = {"x": 0.05, "y": float(blur_y), "w": 0.90, "h": float(blur_h)}' in PIPELINE_PY
    assert "abs(float(blur_y) - float(_sub_centre_eff)) > 0.01" in PIPELINE_PY
    assert "abs(float(blur_y) - float(_p_centre_eff)) > 0.01" in PIPELINE_PY


# ── 9. Cache version ───────────────────────────────────────────────

def test_cache_version_r64():
    assert INDEX_HTML.count("?v=20261104_r117") == 4
    assert "20261104_r53" not in INDEX_HTML
