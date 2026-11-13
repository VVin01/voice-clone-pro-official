"""Batch AG (2026-11-05): free 2-axis adjustment for BOTH the blur
box and the subtitle — blur_x / blur_w and sub_x / sub_w.

Contract, extending Batch AA (blur) and Batch P (subtitle):

* blur_x / blur_w are percents of the PICTURE width, converted at
  the ONE conversion point (core.user_blur_box_to_canvas_box) with
  the same layout math as source_band_to_canvas_box. Both None —
  every pre-AG caller and stored config — keeps the legacy centred
  92%-of-canvas box BYTE-IDENTICAL.
* sub_x is the subtitle CENTRE as a percent of the picture width
  (sub_y's centre semantics, rotated); sub_w is the wrap/plate
  width in the same basis. ONE horizontal resolver
  (core.subtitle_centre_x_px) feeds every consumer: the PNG burn's
  overlay expression, the ASS writer's \\pos tag, the plate, and
  subtitle_anchor_x — the Batch P invariant, extended sideways.
* The plate FOLLOWS the text: width derives from sub_w, centre X
  is the subtitle's, clamped into the frame. Plate height stays
  text-fit; the blur_h → plate min_h coupling is untouched.
* Defaults reproduce today's rendering byte-identically WHERE
  THE CUE FITS: sub_x None or 50 keeps the centred "(W-w)/2"
  expression and emits no ASS \\pos tag; sub_w None or 0.92 keeps
  the legacy cue budgets and the 0.92 plate width.
* ONE deliberate exception (Ulfa, after the 2026-11-05 full-res
  frame evidence): cues that OVERFLOW today get a measured fit —
  sub_w is a HARD constraint on the PNG burn (core.fit_png_cue_
  lines): wrap into the allowed line count, shrink-to-fit only
  when wrapping cannot do it; cues that already fit render
  byte-identically. Pinned in section 15.
* API: RecapConfigRequest gains the four optional fields (absent =
  legacy). blur_w ∈ [10, 100]; sub_x ∈ [10, 90]; blur_x is CLAMPED
  against the effective width so the box stays inside the picture.
* Prefs: recapSubX / recapSubW JOIN the account allowlist (like
  recapSubY); recapBlurX / recapBlurW stay OUT (per-source, like
  recapBlurY / recapBlurH).
* Frontend: X/W sliders for both boxes, explicit left/width paint,
  horizontal drag + left/right ew-resize handles on the blur box,
  snapshot / History prefill round-trips, new-source reset of the
  per-source geometry (sub_w NOT reset — it follows font size).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "app" / "services" / "recap_engine"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ENGINE_DIR))

import core  # noqa: E402
import png_subtitles as pq  # noqa: E402
from core import (  # noqa: E402
    _subtitle_max_chars,
    _subtitle_overlay_x_expr,
    ensure_plate_contains_anchor,
    frame_layout,
    subtitle_anchor_x,
    subtitle_centre_x_px,
    subtitle_plate_box,
    user_blur_box_to_canvas_box,
    write_synced_ass_file,
)
from recap_pipeline import assemble_blur_boxes  # noqa: E402

APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")
CORE_PY = (ENGINE_DIR / "core.py").read_text(encoding="utf-8")
PIPELINE_PY = (ENGINE_DIR / "recap_pipeline.py").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
DOWNLOADER_PY = (ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")

# Letterboxed: 1080×1920 canvas, 16:9 picture centred (ox=0, pw=cw).
LB = frame_layout("9:16", "blur", 1920, 1080)
CW, CH = LB["cw"], LB["ch"]
# Pillarboxed: 1920×1080 canvas, 9:16 picture centred (ox>0, pw<cw).
PB = frame_layout("16:9", "blur", 1080, 1920)
PB_CW, PB_CH = PB["cw"], PB["ch"]
PB_OX, PB_PW = PB["ox"], PB["pw"]


# ── 1. Blur: defaults byte-identical ───────────────────────────────

@pytest.mark.parametrize("layout", [LB, PB, None], ids=["letterbox", "pillarbox", "no-layout"])
@pytest.mark.parametrize("blur_y,blur_h", [(0.85, 0.07), (0.55, 0.22), (0.30, 0.10)])
def test_blur_none_args_reproduce_legacy_exactly(layout, blur_y, blur_h):
    legacy = user_blur_box_to_canvas_box(blur_y, blur_h, layout)
    explicit_none = user_blur_box_to_canvas_box(
        blur_y, blur_h, layout, blur_x=None, blur_w=None
    )
    assert legacy == explicit_none


def test_blur_legacy_width_is_canvas_based_even_pillarboxed():
    # The legacy contract never knew the picture width: 92% of the
    # CANVAS, centred — on every layout, including pillarbox.
    for layout in (LB, PB, None):
        box = user_blur_box_to_canvas_box(0.85, 0.07, layout)
        assert box["x"] == 0.04
        assert box["w"] == 0.92


def test_blur_explicit_defaults_match_legacy_on_letterbox():
    # The new client's factory values (x=4, w=92) land exactly where
    # the legacy conversion put them on the standard layout.
    legacy = user_blur_box_to_canvas_box(0.85, 0.07, LB)
    explicit = user_blur_box_to_canvas_box(0.85, 0.07, LB, blur_x=0.04, blur_w=0.92)
    assert explicit == legacy


# ── 2. Blur: explicit horizontal conversion ────────────────────────

def test_blur_explicit_letterbox_is_picture_relative():
    box = user_blur_box_to_canvas_box(0.85, 0.07, LB, blur_x=0.20, blur_w=0.50)
    assert box["x"] == 0.20
    assert box["w"] == 0.50
    # The vertical axis is untouched by the horizontal values.
    legacy = user_blur_box_to_canvas_box(0.85, 0.07, LB)
    assert box["y"] == legacy["y"] and box["h"] == legacy["h"]


def test_blur_explicit_pillarbox_converts_through_picture():
    box = user_blur_box_to_canvas_box(0.85, 0.07, PB, blur_x=0.20, blur_w=0.50)
    assert box["x"] == pytest.approx((PB_OX + 0.20 * PB_PW) / PB_CW, abs=0.001)
    assert box["w"] == pytest.approx(0.50 * PB_PW / PB_CW, abs=0.001)
    # Inside the picture, never in the side padding.
    assert PB_OX / PB_CW <= box["x"]
    assert box["x"] + box["w"] <= (PB_OX + PB_PW) / PB_CW + 0.001


def test_blur_explicit_no_layout_is_identity():
    box = user_blur_box_to_canvas_box(0.85, 0.07, None, blur_x=0.20, blur_w=0.50)
    assert box == {"x": 0.20, "y": 0.85, "w": 0.50, "h": 0.07}


def test_blur_lone_width_stays_centred():
    box = user_blur_box_to_canvas_box(0.85, 0.07, LB, blur_w=0.50)
    assert box["w"] == 0.50
    assert box["x"] == 0.25


# ── 3. Subtitle centre-X resolver ──────────────────────────────────

def test_centre_x_none_keeps_centred_placement():
    assert subtitle_centre_x_px(CW, CH, LB, None) is None
    assert subtitle_anchor_x(CW, CH, LB, sub_x=None) == 0.5


@pytest.mark.parametrize("sub_x", [0.10, 0.30, 0.50, 0.85])
def test_centre_x_letterbox_is_picture_relative(sub_x):
    # LB's picture spans the full canvas width: centre = sub_x × cw,
    # always inside the picture.
    assert subtitle_centre_x_px(CW, CH, LB, sub_x) == pytest.approx(sub_x * CW)
    assert subtitle_anchor_x(CW, CH, LB, sub_x=sub_x) == pytest.approx(sub_x)


def test_centre_x_pillarbox_offsets_by_picture_origin():
    centre = subtitle_centre_x_px(PB_CW, PB_CH, PB, 0.30)
    assert centre == pytest.approx(PB_OX + 0.30 * PB_PW)
    anchor = subtitle_anchor_x(PB_CW, PB_CH, PB, sub_x=0.30)
    assert anchor == pytest.approx((PB_OX + 0.30 * PB_PW) / PB_CW)
    # The centre lands inside the PICTURE, never in the side bars.
    assert PB_OX <= centre <= PB_OX + PB_PW


def test_centre_x_full_frame_is_canvas_fraction():
    full = frame_layout("9:16", "blur", 1080, 1920)
    assert subtitle_centre_x_px(CW, CH, full, 0.30) == pytest.approx(0.30 * CW)
    assert subtitle_centre_x_px(CW, CH, None, 0.30) == pytest.approx(0.30 * CW)


# ── 4. Plate follows the text ──────────────────────────────────────

def test_plate_defaults_reproduce_pre_ag_dicts():
    # Batch P's pinned full-frame plate, now via the AG signature.
    plate = subtitle_plate_box(CW, CH, None, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    assert plate == {"x": 0.04, "y": 0.85, "w": 0.92, "h": 0.0782}
    lb = subtitle_plate_box(CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    assert lb["x"] == 0.04 and lb["w"] == 0.92


def test_plate_explicit_defaults_match_legacy():
    plate = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.5, sub_w=0.92,
    )
    assert plate["x"] == 0.04 and plate["w"] == 0.92


def test_plate_centre_follows_sub_x():
    plate = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.30, sub_w=0.40,
    )
    assert plate["w"] == pytest.approx(0.40)
    assert plate["x"] + plate["w"] / 2 == pytest.approx(0.30, abs=0.001)
    # …and the plate centre IS the anchor the burn resolves to.
    assert plate["x"] + plate["w"] / 2 == pytest.approx(
        subtitle_anchor_x(CW, CH, LB, sub_x=0.30), abs=0.001
    )


def test_plate_width_scales_with_picture_on_pillarbox():
    plate = subtitle_plate_box(
        PB_CW, PB_CH, PB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_w=0.40,
    )
    assert plate["w"] == pytest.approx(0.40 * PB_PW / PB_CW, abs=0.001)


def test_plate_clamped_into_frame_at_edges():
    left = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.10, sub_w=0.40,
    )
    assert left["x"] == 0.0
    right = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.90, sub_w=0.40,
    )
    assert right["x"] + right["w"] == pytest.approx(1.0, abs=0.001)


def test_plate_height_unchanged_by_horizontal_values():
    base = subtitle_plate_box(CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    moved = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.30, sub_w=0.40,
    )
    assert moved["h"] == base["h"] and moved["y"] == base["y"]


# ── 5. PNG overlay x expression ────────────────────────────────────

def test_png_x_expr_defaults_are_the_legacy_string():
    assert _subtitle_overlay_x_expr(CW, CH, LB, None) == "(W-w)/2"
    assert _subtitle_overlay_x_expr(CW, CH, LB, 0.5) == "(W-w)/2"
    assert _subtitle_overlay_x_expr(PB_CW, PB_CH, PB, None) == "(W-w)/2"


def test_png_x_expr_uses_the_shared_resolver():
    expr = _subtitle_overlay_x_expr(CW, CH, LB, 0.30)
    centre = int(round(subtitle_centre_x_px(CW, CH, LB, 0.30)))
    assert expr == f"max(0,min(W-w,{centre}-w/2))"
    pb_expr = _subtitle_overlay_x_expr(PB_CW, PB_CH, PB, 0.30)
    pb_centre = int(round(subtitle_centre_x_px(PB_CW, PB_CH, PB, 0.30)))
    assert pb_expr == f"max(0,min(W-w,{pb_centre}-w/2))"


def test_png_x_expr_threaded_into_both_burn_paths():
    prod = CORE_PY.split("def apply_production_overlays(", 1)[1]
    prod = prod.split("def extract_preview_with_overlays(", 1)[0]
    assert '_subtitle_overlay_x_expr(cw, ch, lay_dict, overlays.get("sub_x"))' in prod
    assert "overlay=x='{x_expr}':y={top}:" in prod
    preview = CORE_PY.split("def extract_preview_with_overlays(", 1)[1]
    assert "_subtitle_overlay_x_expr(cw, ch, layout, overlays.get('sub_x'))" in preview


# ── 6. ASS writer \pos ─────────────────────────────────────────────

def _write_ass(tmp_path, name, **kw):
    return write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / name, CW, CH,
        raw_font_size=16, sub_lines=1, sub_y=0.55, layout=LB, **kw,
    )


def test_ass_default_and_centred_are_byte_identical(tmp_path):
    plain = _write_ass(tmp_path, "plain.ass").read_bytes()
    centred = _write_ass(tmp_path, "centred.ass", sub_x=0.5).read_bytes()
    assert plain == centred
    assert b"\\pos(" not in plain


def test_ass_offcentre_emits_pos_at_the_resolver_centre(tmp_path):
    out = _write_ass(tmp_path, "moved.ass", sub_x=0.30)
    text = out.read_text(encoding="utf-8")
    style = next(l for l in text.splitlines() if l.startswith("Style:"))
    margin_v = int([p.strip() for p in style.split(",")][-2])
    dialogue = next(l for l in text.splitlines() if l.startswith("Dialogue:"))
    m = re.search(r"\{\\pos\((\d+),(\d+)\)\}", dialogue)
    assert m is not None
    assert int(m.group(1)) == int(round(subtitle_centre_x_px(CW, CH, LB, 0.30)))
    # The tag's Y is exactly where MarginV already placed the
    # baseline — the vertical geometry is untouched.
    assert int(m.group(2)) == CH - margin_v


# ── 7. Cue-wrap budgets ────────────────────────────────────────────

def test_wrap_budgets_legacy_table_and_default():
    # Batch DJ: VERTICAL values are the measured-capacity budgets
    # (lines × per-line capacity at the 16pt default); HORIZONTAL
    # keeps the legacy table.
    assert _subtitle_max_chars(True, 1) == 25
    assert _subtitle_max_chars(False, 1) == 52
    assert _subtitle_max_chars(True, 2) == 40
    assert _subtitle_max_chars(False, 2) == 96
    for is_vert, lines in [(True, 1), (False, 1), (True, 2), (False, 2)]:
        assert _subtitle_max_chars(is_vert, lines, 0.92) == _subtitle_max_chars(is_vert, lines)


def test_wrap_budgets_scale_with_sub_w():
    assert _subtitle_max_chars(True, 1, 0.46) == 12    # half width → half budget
    assert _subtitle_max_chars(True, 1, 0.23) == 8     # quarter width → the 8-char floor
    assert _subtitle_max_chars(True, 1, 1.0) == 27     # full width → more per cue
    assert _subtitle_max_chars(False, 2, 0.46) == 48


def test_wrap_budget_and_ass_tag_read_the_overlays_dict():
    # Batch DD: the production call site is size-aware — the
    # chosen font size feeds the budget (Batch DJ: measured
    # capacity ladders on the vertical path).
    assert 'font_size=int(self.overlays.get("subtitles_font_size", 16))' in CORE_PY
    assert "_subtitle_max_chars(" in CORE_PY
    assert 'sub_x=self.overlays.get("sub_x"),' in CORE_PY


# ── 8. Plate containment, horizontal ───────────────────────────────

def test_ensure_plate_contains_anchor_x():
    plate = {"x": 0.40, "y": 0.85, "w": 0.30, "h": 0.0782}
    # Anchor inside → untouched (and anchor_x=None is the old call).
    assert ensure_plate_contains_anchor(dict(plate), 0.85, 0.50) == plate
    assert ensure_plate_contains_anchor(dict(plate), 0.85) == plate
    # Anchor left of the plate → plate shifts left, size unchanged.
    moved = ensure_plate_contains_anchor(dict(plate), 0.85, 0.20)
    assert moved["x"] == 0.20 and moved["w"] == 0.30 and moved["y"] == 0.85
    # Anchor right of the plate → plate's right edge reaches it.
    moved_r = ensure_plate_contains_anchor(dict(plate), 0.85, 0.90)
    assert moved_r["x"] == pytest.approx(0.60) and moved_r["w"] == 0.30


def test_assembly_shifts_plate_to_offcentre_anchor():
    # A plate built away from the anchor (the divergent-client case
    # ensure_* exists for) is pulled onto the subtitle's centre X.
    plate = {"x": 0.50, "y": 0.6108, "w": 0.30, "h": 0.0782}
    anchor_x = subtitle_anchor_x(CW, CH, LB, sub_x=0.30)
    boxes, _covered = assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], layout=LB, canvas_w=CW, canvas_h=CH,
        anchor_y=0.6108, anchor_x=anchor_x,
    )
    shipped = boxes[0]
    assert shipped["x"] <= anchor_x <= shipped["x"] + shipped["w"]


def test_assembly_offcentre_user_box_and_plate_ship_verbatim():
    plate = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.30, sub_w=0.40,
    )
    user = user_blur_box_to_canvas_box(0.30, 0.10, LB, blur_x=0.55, blur_w=0.40)
    boxes, covered = assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[{"x": 0.0, "y": 0.50, "w": 1.0, "h": 0.10}],
        user_box=user, layout=LB, canvas_w=CW, canvas_h=CH,
        anchor_y=0.6108, anchor_x=subtitle_anchor_x(CW, CH, LB, sub_x=0.30),
    )
    assert covered is True
    assert user in boxes and plate in boxes


def test_assembly_overlap_user_box_wins_batch_as():
    # SUPERSEDED by Batch AS (AS-4, owner report 2026-11-05): this
    # test used to pin the Batch J union — an off-centre user box
    # overlapping the plate folded into ONE union covering both.
    # The owner found that union is exactly what made his blur
    # WIDER than the box he set whenever it overlapped the caption
    # band. The AS rule: the plate YIELDS to an overlapping user
    # box and the user box ships VERBATIM — the box set is the box
    # rendered, on the new axis too.
    plate = subtitle_plate_box(
        CW, CH, LB, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07,
        sub_x=0.30, sub_w=0.40,
    )
    anchor = subtitle_anchor_x(CW, CH, LB, sub_x=0.85)  # plate's own y centre row
    user = user_blur_box_to_canvas_box(0.85, 0.10, LB, blur_x=0.35, blur_w=0.40)
    boxes, _covered = assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], user_box=user, layout=LB, canvas_w=CW, canvas_h=CH,
        anchor_y=0.6108, anchor_x=None,
    )
    assert anchor is not None  # (anchor_x unused in this scenario)
    assert boxes == [user]


# ── 9. API model ───────────────────────────────────────────────────

def _req(**kw):
    import app.webapp.api as api_mod

    return api_mod.RecapConfigRequest(url="https://example.com/v", **kw)


def _rebuild(req):
    dump = req.model_dump()
    dump.pop("url", None)  # _req supplies the url itself
    return _req(**dump)


def test_api_fields_default_to_legacy_none():
    req = _req()
    assert req.blur_x is None and req.blur_w is None
    assert req.sub_x is None and req.sub_w is None
    rebuilt = _rebuild(req)
    assert rebuilt.blur_x is None and rebuilt.sub_w is None


def test_api_fields_round_trip_through_persisted_config():
    req = _req(blur_x=20, blur_w=50, sub_x=65, sub_w=70)
    assert (req.blur_x, req.blur_w, req.sub_x, req.sub_w) == (20, 50, 65, 70)
    rebuilt = _rebuild(req)
    assert (rebuilt.blur_x, rebuilt.blur_w, rebuilt.sub_x, rebuilt.sub_w) == (20, 50, 65, 70)


def test_api_bounds_reject_out_of_range():
    from pydantic import ValidationError

    for kw in ({"blur_w": 9}, {"blur_w": 101}, {"sub_x": 9}, {"sub_x": 91},
               {"sub_w": 9}, {"sub_w": 101}, {"blur_x": -1}, {"blur_x": 101}):
        with pytest.raises(ValidationError):
            _req(**kw)
    # Boundary values are accepted.
    _req(blur_w=10, sub_x=10, sub_w=10)
    _req(blur_w=100, sub_x=90, sub_w=100)


def test_api_blur_x_clamped_against_effective_width():
    assert _req(blur_x=95, blur_w=20).blur_x == 80
    assert _req(blur_x=50).blur_x == 8          # legacy width 92 → max x 8
    assert _req(blur_x=4, blur_w=92).blur_x == 4
    assert _req(blur_x=0, blur_w=100).blur_x == 0


def test_api_runner_threads_fractions():
    assert "blur_x=(float(req.blur_x) / 100.0) if req.blur_x is not None else None," in API_PY
    assert "blur_w=(float(req.blur_w) / 100.0) if req.blur_w is not None else None," in API_PY
    assert "sub_x=(float(req.sub_x) / 100.0) if req.sub_x is not None else None," in API_PY
    assert "sub_w=(float(req.sub_w) / 100.0) if req.sub_w is not None else None," in API_PY


# ── 10. Threading: downloader + pipeline ───────────────────────────

def test_downloader_threads_both_entry_points():
    assert DOWNLOADER_PY.count("blur_x: Optional[float] = None") == 2
    assert DOWNLOADER_PY.count("sub_w: Optional[float] = None") == 2
    assert DOWNLOADER_PY.count("blur_x=blur_x,") == 2
    assert DOWNLOADER_PY.count("sub_x=sub_x,") == 2


def test_pipeline_threads_both_paths():
    assert PIPELINE_PY.count("blur_x: Optional[float] = None") == 2
    assert PIPELINE_PY.count("sub_x=sub_x, sub_w=sub_w,") == 2
    assert PIPELINE_PY.count('overlays["sub_x"] = float(sub_x)') == 2
    assert PIPELINE_PY.count('overlays["sub_w"] = float(sub_w)') == 2
    assert "anchor_x=_anchor_x," in PIPELINE_PY
    assert "anchor_x=_p_anchor_x," in PIPELINE_PY
    # The no-layout fallback plates stay the fixed legacy dicts.
    # Batch AK: the run path's fallback reads the EFFECTIVE centre
    # (_sub_centre_eff — margin-converted when a bottom margin is
    # set, identical to _anchor_sub_y when it is not).
    assert PIPELINE_PY.count('_plate = {"x": 0.05, "y": _sub_centre_eff, "w": 0.90, "h": float(blur_h)}') == 1


# ── 11. Preferences allowlist ──────────────────────────────────────

def test_prefs_sub_x_w_are_account_preferences():
    import app.webapp.api as api_mod

    assert api_mod.DEFAULT_USER_PREFS["recapSubX"] == 50
    assert api_mod.DEFAULT_USER_PREFS["recapSubW"] == 92
    assert api_mod.USER_PREF_INTS["recapSubX"] == (10, 90)
    assert api_mod.USER_PREF_INTS["recapSubW"] == (10, 100)
    clean = api_mod._sanitize_user_prefs({"recapSubX": 90, "recapSubW": 100})
    assert clean == {"recapSubX": 90, "recapSubW": 100}
    # Out-of-range values drop, exactly like every other int pref.
    assert api_mod._sanitize_user_prefs({"recapSubX": 5}) == {}
    assert api_mod._sanitize_user_prefs({"recapSubW": 101}) == {}
    eff = api_mod._effective_user_prefs({})
    assert eff["recapSubX"] == 50 and eff["recapSubW"] == 92


# ── 12. Frontend pins ──────────────────────────────────────────────

def test_frontend_markup_sliders_and_handles():
    for el_id in ("slider-blur-x", "slider-blur-w", "slider-sub-x", "slider-sub-w",
                  "val-blur-x", "val-blur-w", "val-sub-x", "val-sub-w"):
        assert f'id="{el_id}"' in INDEX_HTML
    assert '<div class="box-resize-handle left"></div>' in INDEX_HTML
    assert '<div class="box-resize-handle right"></div>' in INDEX_HTML


def test_frontend_geometry_and_paint_pins():
    assert "function subtitleCentreXCanvasFrac(" in APP_JS
    assert "function subtitleWidthCanvasFrac(" in APP_JS
    sync_blur = APP_JS.split("function syncBlurBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "previewBlur.style.left = `${(geom.left * 100).toFixed(3)}%`;" in sync_blur
    assert "previewBlur.style.width = `${(geom.width * 100).toFixed(3)}%`;" in sync_blur
    assert 'previewBlur.style.right = "auto";' in sync_blur
    sync_sub = APP_JS.split("function syncSubBoxVisual()", 1)[1].split("\n    }", 1)[0]
    assert "subtitleCentreXCanvasFrac(x)" in sync_sub
    assert "subtitleWidthCanvasFrac(w)" in sync_sub
    assert 'previewSub.style.transform = "translate(-50%, -50%)";' in sync_sub


def test_frontend_drag_and_resize_pins():
    assert "function enableHorizontalDrag(" in APP_JS
    assert "enableHorizontalDrag(previewBlur," in APP_JS
    assert "enableHorizontalDrag(previewSub," in APP_JS
    assert "1 / r.pw" in APP_JS
    assert "function attachBlurResizeX(" in APP_JS
    assert "attachBlurResizeX(blurResizeLeft, true);" in APP_JS
    assert "attachBlurResizeX(blurResizeRight, false);" in APP_JS
    # Every blur x/w interaction marks the box explicit.
    bx = APP_JS.split('sliderBlurX.addEventListener("input"', 1)[1].split("});", 1)[0]
    bw = APP_JS.split('sliderBlurW.addEventListener("input"', 1)[1].split("});", 1)[0]
    assert "state.recapBlurBoxTouched = true;" in bx
    assert "state.recapBlurBoxTouched = true;" in bw


def test_frontend_state_snapshot_restore_prefill_payload():
    assert "recapBlurX: 4," in APP_JS and "recapSubX: 50," in APP_JS
    assert "recapBlurX: [0, 100], recapBlurW: [10, 100]," in APP_JS
    assert "recapSubX: [10, 90], recapSubW: [10, 100]," in APP_JS
    snap = APP_JS.split("function snapshotRecapSettings()", 1)[1].split("return snap;", 1)[0]
    assert "snap.recapBlurX = bx ? parseInt(bx.value) : 4;" in snap
    assert "snap.recapSubW = sw ? parseInt(sw.value) : 92;" in snap
    prefill = APP_JS.split("window.prefillRecapFromHistory = function(cfg, item) {", 1)[1]
    prefill = prefill.split("// Initial canvas mockup sync", 1)[0]
    assert "recapBlurX: cfg.blur_x, recapBlurW: cfg.blur_w," in prefill
    assert "recapSubX: cfg.sub_x, recapSubW: cfg.sub_w," in prefill
    payload = APP_JS.split("const payload = {", 1)[1].split("};", 1)[0]
    assert "blur_x: sliderBlurX ? parseInt(sliderBlurX.value) : 4," in payload
    assert "blur_w: sliderBlurW ? parseInt(sliderBlurW.value) : 92," in payload
    assert "sub_x: sliderSubX ? parseInt(sliderSubX.value) : 50," in payload
    assert "sub_w: sliderSubW ? parseInt(sliderSubW.value) : 92," in payload


def test_frontend_reset_rules():
    body = APP_JS.split("function resetBlurBoxForNewSource()", 1)[1]
    body = body.split("\n    }", 1)[0]
    # Per-source geometry resets (blur x/w with y/h; sub_x with sub_y)…
    assert "state.recapBlurX = 4;" in body
    assert "state.recapBlurW = 92;" in body
    assert "state.recapSubX = 50;" in body
    assert "sliderBlurX.value = 4;" in body
    assert "sliderSubX.value = 50;" in body
    # …sub_w does NOT reset (it follows the font-size rule).
    assert "sliderSubW" not in body
    assert "recapSubW" not in body


def test_frontend_css_and_i18n():
    assert ".box-resize-handle.left" in STYLE_CSS
    assert "cursor: ew-resize" in STYLE_CSS
    assert ".preview-blur-box.draggable-box, .preview-sub-box.draggable-box { max-width: 100%; }" in STYLE_CSS
    for key in ("slider_blur_x", "slider_blur_w", "slider_sub_x", "slider_sub_w"):
        assert I18N_JS.count(key + ":") >= 2, key


def test_cache_version_r58():
    assert INDEX_HTML.count("?v=20261113_r145") == 4


# ── 13. Client preview mirror: node behaviour ──────────────────────

def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = ROOT / "tests/unit/_ag_harness_tmp.js"
    try:
        tmp.write_text(script, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _geom_slice() -> str:
    start = APP_JS.index("function recapPictureRect()")
    end = APP_JS.index("function resetBlurBoxForNewSource()")
    return APP_JS[start:end]


def test_client_geometry_matches_engine_node():
    script = (
        "const canvasVideo = null;\n"
        "let state = { recapRatio:'9:16', recapRatioFill:'blur', recapSourceWidth:1920, recapSourceHeight:1080,"
        " recapCropTop:0, recapCropBottom:0, recapCropLeft:0, recapCropRight:0 };\n"
        + _geom_slice()
        + "\nconst lb = { g: blurBoxCanvasGeom(85, 7, 20, 50), legacy: blurBoxCanvasGeom(85, 7),"
        + " cx: subtitleCentreXCanvasFrac(30), w: subtitleWidthCanvasFrac(50) };\n"
        + "state = { recapRatio:'16:9', recapRatioFill:'blur', recapSourceWidth:1080, recapSourceHeight:1920,"
        + " recapCropTop:0, recapCropBottom:0, recapCropLeft:0, recapCropRight:0 };\n"
        + "const pb = { g: blurBoxCanvasGeom(85, 7, 20, 50), cx: subtitleCentreXCanvasFrac(30) };\n"
        + "console.log(JSON.stringify({ lb, pb }));\n"
    )
    out = _run_node(script)
    # Letterbox (picture spans the full width): identity basis.
    assert out["lb"]["g"]["left"] == pytest.approx(0.20, abs=0.001)
    assert out["lb"]["g"]["width"] == pytest.approx(0.50, abs=0.001)
    assert out["lb"]["legacy"]["left"] == 0.04 and out["lb"]["legacy"]["width"] == 0.92
    assert out["lb"]["cx"] == pytest.approx(0.30, abs=0.001)
    assert out["lb"]["w"] == pytest.approx(0.50, abs=0.001)
    # Pillarbox: the JS mirror agrees with the engine's conversion.
    eng = user_blur_box_to_canvas_box(0.85, 0.07, PB, blur_x=0.20, blur_w=0.50)
    assert out["pb"]["g"]["left"] == pytest.approx(eng["x"], abs=0.002)
    assert out["pb"]["g"]["width"] == pytest.approx(eng["w"], abs=0.002)
    assert out["pb"]["cx"] == pytest.approx(
        subtitle_anchor_x(PB_CW, PB_CH, PB, sub_x=0.30), abs=0.002
    )


# ── 14. Fit guarantee: sub_w is a HARD constraint on the burn ─────
#
# Ulfa's mid-build correction (2026-11-05, verified at full res on
# the delivered eyes-on video, frame at 65.7s): a max-length cue
# rendered WIDER than the canvas, clipped at both edges. The cue
# splitter's character budget is not a pixel width. These pins hold
# the measured guarantee: rendered PNG width ≤ sub_w × picture
# width, at default settings AND at the largest font size, on the
# engine path and the preview path; cues that already fit render
# byte-identically (the fast path).

FIT_LONG = "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်မှာ ကျောင်းသားတွေ လွှမ်းမိုးနေတဲ့ ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"
FIT_MID = "မြို့ကြီးဆီကို ထွက်ခွာပြီး ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"
FIT_SHORT = "မင်္ဂလာပါ"
FIT_BOUND = int(0.92 * 1080)  # default sub_w on a 1080-wide picture
FFMPEG_BIN = shutil.which("ffmpeg")


def _fit_renderer(tmp_path):
    return pq.PngSubtitleRenderer(tmp_path / "fit_cache")


def _fit(renderer, text, sub_lines, font_px, bound=FIT_BOUND, preset="yellow"):
    font = core.subtitle_font_file("padauk")
    visuals = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS[preset], CH, font_px)
    lines, px, vis = core.fit_png_cue_lines(
        renderer, text, sub_lines, font_px, font, visuals, bound
    )
    assert lines, "fit returned no lines for a visible cue"
    _path, w, _h = renderer.render_lines(lines, px, font, vis)
    return lines, px, w


def test_fit_defect_baseline_unfitted_cue_is_wider_than_canvas(tmp_path):
    # Documents the defect the fit removes: measured with the real
    # font at the largest size, the long cue is ~4x the canvas width.
    r = _fit_renderer(tmp_path)
    font = core.subtitle_font_file("padauk")
    font_px = core.subtitle_font_px(CW, CH, 22, 1, LB)
    visuals = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["yellow"], CH, font_px)
    out = r.render_text(FIT_LONG, 1, font_px, font, visuals)
    assert out is not None and out[1] > CW


@pytest.mark.parametrize("sub_lines,size_pt", [(1, 16), (1, 22), (2, 16), (2, 22)])
def test_fit_max_length_cue_fits_default_bound(tmp_path, sub_lines, size_pt):
    # Batch CX locked the size; Batch DD (2026-11-09) lets the
    # fit wrap into up to the chosen line count: the width bound
    # and the locked size still hold for every piece, and a
    # one-line job still renders one line.
    r = _fit_renderer(tmp_path)
    font_px = core.subtitle_font_px(CW, CH, size_pt, sub_lines, LB)
    lines, px, w = _fit(r, FIT_LONG, sub_lines, font_px)
    assert w <= FIT_BOUND
    assert 1 <= len(lines) <= sub_lines
    if sub_lines == 1:
        assert len(lines) == 1
    assert px == font_px


def test_fit_never_wraps_to_two_lines(tmp_path):
    # Batch DD supersedes the CX pin: at Lines=2 the mid-length cue
    # may wrap into up to two lines at the locked size (never more
    # than the chosen count, never wider than the bound).
    r = _fit_renderer(tmp_path)
    ff = core.frame_layout("9:16", "blur", 1080, 1920)
    font_px = core.subtitle_font_px(CW, CH, 13, 2, ff)
    lines, px, w = _fit(r, FIT_MID, 2, font_px)
    assert px == font_px and 1 <= len(lines) <= 2 and w <= FIT_BOUND


def test_fit_fitting_cue_is_byte_identical(tmp_path):
    r = _fit_renderer(tmp_path)
    font = core.subtitle_font_file("padauk")
    font_px = core.subtitle_font_px(CW, CH, 16, 1, LB)
    visuals = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["yellow"], CH, font_px)
    lines, px, vis = core.fit_png_cue_lines(
        r, FIT_SHORT, 1, font_px, font, visuals, FIT_BOUND
    )
    assert px == font_px and vis is visuals
    legacy = r.render_text(FIT_SHORT, 1, font_px, font, visuals)
    fitted = r.render_lines(lines, px, font, vis)
    assert fitted[0] == legacy[0] and fitted[1] == legacy[1]


def test_fit_respects_a_narrower_sub_w(tmp_path):
    r = _fit_renderer(tmp_path)
    font_px = core.subtitle_font_px(CW, CH, 16, 1, LB)
    _lines, _px, w = _fit(r, FIT_MID, 1, font_px, bound=int(0.50 * 1080))
    assert w <= int(0.50 * 1080)


def test_fit_band_preset_also_fits(tmp_path):
    r = _fit_renderer(tmp_path)
    font_px = core.subtitle_font_px(CW, CH, 22, 1, LB)
    _lines, _px, w = _fit(r, FIT_LONG, 1, font_px, preset="band")
    assert w <= FIT_BOUND


def test_fit_wired_into_both_burn_paths():
    # Batch CX: the production burn splits cues via
    # fit_png_cue_pieces (every piece shown in turn); the preview
    # still fits the single sample frame via fit_png_cue_lines.
    prod = CORE_PY.split("def apply_production_overlays(", 1)[1].split("\ndef ", 1)[0]
    prev = CORE_PY.split("def extract_preview_with_overlays(", 1)[1].split("\ndef ", 1)[0]
    assert "fit_png_cue_pieces(" in prod and "_sub_w_fraction(overlays)" in prod
    assert "fit_png_cue_lines(" in prev and "_sub_w_fraction(overlays)" in prev


def _make_fit_src(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=320x240:rate=30:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    return src


def _cached_png_widths(work_dir):
    from PIL import Image

    widths = []
    for p in (work_dir / "subtitle_png_cache").glob("sub_*.png"):
        with Image.open(p) as im:
            widths.append(im.width)
    return widths


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_fit_end_to_end_production_cue_png_within_bound(tmp_path):
    src = _make_fit_src(tmp_path)
    out = tmp_path / "out.mp4"
    core.apply_production_overlays(
        FFMPEG_BIN, src, out,
        {"subtitle_cues": [[0.5, 3.5, FIT_LONG]], "subtitles_font_size": 22,
         "sub_style": "yellow", "sub_lines": 1, "sub_y": 0.85},
        "libx264", tmp_path / "w", frame_size=(320, 240),
        layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
    )
    assert out.is_file()
    widths = _cached_png_widths(tmp_path / "w")
    assert widths and max(widths) <= int(0.92 * 320)


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_fit_end_to_end_preview_sample_within_bound(tmp_path):
    src = _make_fit_src(tmp_path)
    out_img = tmp_path / "preview.png"
    core.extract_preview_with_overlays(
        FFMPEG_BIN, src, out_img, timestamp=1.0,
        overlays={"burn_subtitles": True, "sub_y": 0.85, "sub_lines": 1,
                  "subtitles_font_size": 22, "sub_style": "yellow"},
        output_ratio="Original", frame_fill="blur",
        work_dir=tmp_path / "pw", sample_text=FIT_LONG,
    )
    assert out_img.is_file()
    widths = _cached_png_widths(tmp_path / "pw")
    assert widths and max(widths) <= int(0.92 * 320)

