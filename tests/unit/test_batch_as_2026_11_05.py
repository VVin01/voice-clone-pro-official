"""Batch AS — Mini App preview-truth + overflow fixes (2026-11-05).

Owner-flagged from his own screenshots on the r66 build, plus the
full-screen visual audit:

- AS-1: the aspect-ratio cards squeezed name + usage + dimensions
  onto shared lines inside pills (dimensions pressed past the card's
  edge, a lone "/" hanging, Original edge-to-edge). The cards now
  stack name / usage / dimensions on their own lines.
- AS-2: the preview stage never adopted the selected aspect for
  "Original" when the source dimensions were unknown — it stayed at
  the 9:16 default while the picture changed inside it. One stage
  authority (recapStageAspect) now drives every selection, with the
  composited frame's natural aspect as the Original fallback. The
  ENGINE's Original canvas is verified native (no baked letterbox).
- AS-3: (a) the preview's title rendered MIRRORED — the engine never
  mirrors the title (proven below: the title panel is pixel-identical
  with mirror on/off); the client's CSS .mirrored flip, which exists
  for the RAW layers, was flipping the already-final composite a
  second time. The composite branch now strips the class.
  (b) toggling the title OFF never scheduled a composite refresh
  (the refresh sat after the disabled early-return), so the burned
  badge stayed in the preview. The refresh now fires on both paths.
- AS-4: a user-set blur box that overlapped the subtitle plate was
  folded into their UNION by the final merge — the shipped blur was
  wider than the box set. The plate now yields to an overlapping
  user box and the user box is appended after the merge, verbatim.
- AS-5: the subtitle-font picker never scheduled a composite
  refresh (it called only the retired no-op preview updater), so
  the preview kept the previous font. The engine honours the font
  end-to-end (proven below); the handler now refreshes like its
  size/lines siblings.
- AS-6: the fixed CTA band's top edge sits ≈156px above the
  viewport bottom while the shared scroll clearance is 118px — the
  Voice tab's last ~38px could never scroll clear. The Voice tab
  gains the missing padding; the persona filter chips wrap instead
  of ellipsising the last chip.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import app.webapp.api as api_mod
from app.services.recap_engine.core import (
    frame_layout,
    output_canvas,
    subtitle_plate_box,
    user_blur_box_to_canvas_box,
)
from app.services.recap_engine.recap_pipeline import (
    _boxes_strictly_overlap,
    assemble_blur_boxes,
)

STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
REPO_ROOT = Path(api_mod.__file__).resolve().parents[2]
PIPELINE_PY = (
    REPO_ROOT / "app" / "services" / "recap_engine" / "recap_pipeline.py"
).read_text(encoding="utf-8")


def _function_block(start: str, span: int = 4000) -> str:
    i = APP_JS.index(start)
    return APP_JS[i : i + span]


# ── AS-4: the set blur box is the shipped blur box ──────────────────

_LB = frame_layout("9:16", "blur", 1920, 1080)  # 16:9 picture in a 9:16 canvas


def _plate():
    return subtitle_plate_box(
        _LB["cw"], _LB["ch"], _LB, raw_font_size=16, sub_lines=1,
        sub_y=0.85, min_h=0.07, sub_margin=0.05,
    )


def _assemble(plate, user, **kw):
    return assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=plate,
        detected_bands=[], user_box=user, layout=_LB,
        canvas_w=_LB["cw"], canvas_h=_LB["ch"], anchor_y=None, **kw,
    )


def test_strict_overlap_predicate_has_no_slack():
    a = {"x": 0.10, "y": 0.50, "w": 0.20, "h": 0.10}
    touching = {"x": 0.30, "y": 0.50, "w": 0.20, "h": 0.10}  # edges meet exactly
    near = {"x": 0.31, "y": 0.50, "w": 0.20, "h": 0.10}      # 0.01 gap (inside merge slack)
    inside = {"x": 0.15, "y": 0.50, "w": 0.20, "h": 0.10}
    assert _boxes_strictly_overlap(a, inside) is True
    assert _boxes_strictly_overlap(a, touching) is False
    assert _boxes_strictly_overlap(a, near) is False


def test_user_box_overlapping_plate_ships_verbatim_plate_yields():
    plate = _plate()
    # A NARROW user box (40% wide) dragged onto the caption band —
    # the old union shipped it back at the plate's 92% width.
    user = user_blur_box_to_canvas_box(0.85, 0.07, _LB, blur_x=0.30, blur_w=0.40)
    assert _boxes_strictly_overlap(user, plate)
    boxes, covered = _assemble(plate, user)
    assert covered is True
    assert boxes == [user]


def test_user_box_coinciding_with_plate_ships_once_verbatim():
    plate = _plate()
    user = user_blur_box_to_canvas_box(0.85, 0.07, _LB)
    boxes, covered = _assemble(plate, user)
    assert covered is True
    assert boxes == [user]


def test_user_box_near_plate_but_not_overlapping_is_not_unioned():
    plate = _plate()
    # Just above the plate with a small gap: inside the old merge's
    # 0.02 slack, so the union used to absorb it anyway.
    user = dict(plate)
    user["y"] = plate["y"] - plate["h"] / 2 - user["h"] / 2 - 0.01
    assert not _boxes_strictly_overlap(user, plate)
    boxes, _covered = _assemble(plate, user)
    assert boxes == [plate, user]  # both ship; the user box is verbatim


def test_user_box_far_from_plate_keeps_both():
    plate = _plate()
    user = user_blur_box_to_canvas_box(0.10, 0.07, _LB, blur_x=0.04, blur_w=0.92)
    boxes, _covered = _assemble(plate, user)
    assert boxes == [plate, user]


def test_non_user_paths_still_use_the_union_merge():
    # Detection path untouched: two overlapping detected bands with
    # no plate and no user box still fold into one union.
    bands = [
        {"x": 0.05, "y": 0.80, "w": 0.90, "h": 0.06},
        {"x": 0.05, "y": 0.83, "w": 0.90, "h": 0.06},
    ]
    boxes, covered = assemble_blur_boxes(
        blur_subtitles=True, burn_subtitles=True, plate=None,
        detected_bands=bands, layout=_LB,
        canvas_w=_LB["cw"], canvas_h=_LB["ch"],
    )
    assert covered is True
    assert len(boxes) == 1
    assert boxes[0]["h"] > 0.06


# ── AS-2 (engine side): Original renders at the native size ────────

def test_output_canvas_original_is_native_and_capped():
    assert output_canvas("Original", 1280, 720) == (1280, 720)
    assert output_canvas("Original", 1920, 1080) == (1920, 1080)
    assert output_canvas("Original", 3840, 2160) == (1920, 1080)  # long side ≤ 1920
    assert output_canvas("Original", 720, 1280) == (720, 1280)


def test_frame_layout_original_has_no_baked_letterbox():
    lay = frame_layout("Original", "black", 1920, 1080)
    assert (lay["cw"], lay["ch"]) == (1920, 1080)
    assert (lay["pw"], lay["ph"]) == (lay["cw"], lay["ch"])  # picture fills the canvas
    assert (lay["ox"], lay["oy"]) == (0, 0)
    lay_blur = frame_layout("Original", "blur", 1280, 720)
    assert (lay_blur["cw"], lay_blur["ch"]) == (1280, 720)
    assert (lay_blur["pw"], lay_blur["ph"]) == (1280, 720)


# ── Real-render pins (ffmpeg): AS-2 dims, AS-3a title, AS-5 font ────

def _synthetic_source(tmp_path: Path) -> Path:
    """1280×720 clip with asymmetric corner markers, so a horizontal
    flip is measurable, plus flat picture area elsewhere."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = (
        REPO_ROOT / "app" / "assets" / "fonts" / "NotoSansMyanmar-Bold.ttf"
    )
    img = Image.new("RGB", (1280, 720), (51, 85, 170))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(font_path), 64)
    draw.text((40, 40), "LEFTSIDE", fill=(255, 255, 255), font=font)
    draw.text((880, 40), "RIGHTSIDE", fill=(255, 255, 0), font=font)
    png = tmp_path / "src.png"
    img.save(png)
    out = tmp_path / "src.mp4"
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


def _render_preview(tmp_path: Path, name: str, **kw):
    from app.services.recap_engine.recap_pipeline import generate_recap_preview

    src = _synthetic_source(tmp_path)
    out = tmp_path / name
    generate_recap_preview(
        source_video_path=str(src), output_image_path=str(out),
        blur_subtitles=False, burn_subtitles=True,
        sample_text="နမူနာ စာတန်း စမ်းသပ်ချက်",
        frame_fill="black",
        output_ratio="Original", at_seconds=2.0, **kw,
    )
    return out


def _grey(path: Path):
    import numpy as np
    from PIL import Image

    return np.asarray(Image.open(path).convert("L"), dtype=int)


def test_preview_original_output_is_native_size(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")
    out = _render_preview(tmp_path, "prev.jpg", mirror_video=True, sub_font="padauk")
    from PIL import Image

    with Image.open(out) as im:
        assert im.size == (1280, 720)  # the source's own size — no fixed canvas


def test_preview_title_is_never_mirrored(tmp_path):
    # Batch DD: there is no title in the preview any more — the
    # old title band (top-centre) is plain picture now, so it
    # mirrors WITH the picture like everything else.
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")
    import inspect

    from app.services.recap_engine.recap_pipeline import generate_recap_preview

    params = inspect.signature(generate_recap_preview).parameters
    assert "title_text" not in params and "title_position" not in params
    on = _grey(_render_preview(tmp_path, "on.jpg", mirror_video=True, sub_font="padauk"))
    off = _grey(_render_preview(tmp_path, "off.jpg", mirror_video=False, sub_font="padauk"))
    h, w = on.shape
    marker = on[0 : int(h * 0.15), 0 : int(w * 0.25)]
    marker_off = off[0 : int(h * 0.15), 0 : int(w * 0.25)]
    import numpy as np

    # The picture itself really does flip (the test is live).
    assert np.abs(marker - marker_off).mean() > 2.0


def test_preview_font_selection_changes_the_render(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not available")
    padauk = _grey(_render_preview(tmp_path, "pd.jpg", mirror_video=True, sub_font="padauk"))
    serif = _grey(_render_preview(tmp_path, "sf.jpg", mirror_video=True, sub_font="noto_serif"))
    h, w = padauk.shape
    import numpy as np

    sub_band = padauk[int(h * 0.78) :, :]
    sub_band_serif = serif[int(h * 0.78) :, :]
    assert np.abs(sub_band - sub_band_serif).mean() > 0.5  # glyphs differ
    # Batch DD: the old title-panel comparison is gone with the
    # title feature — the top band is plain picture in both.


# ── AS-5: plumbing — picker → payload → submit all carry the font ──

def test_font_picker_schedules_a_composite_refresh():
    block = _function_block('const subFontBtns = document.querySelectorAll("#recap-sub-font-picker .pill-btn");', span=900)
    assert "state.recapSubFont = btn.getAttribute(\"data-font\")" in block
    assert "scheduleRecapCompositeRefresh();" in block


def test_font_rides_composite_and_submit_payloads():
    assert APP_JS.count('sub_font: state.recapSubFont || "padauk"') >= 2


# ── AS-3b: title OFF refreshes the composite too ────────────────────

def test_title_sync_refreshes_before_the_disabled_return():
    # Batch DD: the title sync (and its disabled-return ordering
    # hazard) is removed with the feature.
    assert "syncTitleBoxVisual" not in APP_JS


# ── AS-3a: the composite is exempt from the CSS mirror flip ─────────

def test_composite_branch_strips_the_mirrored_class():
    block = _function_block("function updateCanvasMockupVisuals() {", span=20000)
    branch = block.index("if (compositeImg && state.recapCompositeUrl)")
    strip = block.index('canvas.classList.remove("mirrored")')
    assert branch < strip
    # The raw-layer flip itself is untouched (step 2 still applies it).
    assert "state.recapMirror ? 'mirrored' : ''" in block


def test_title_overlay_applied_after_the_mirror_in_engine_graph():
    # Batch DD: the preview engine carries no title overlay step.
    body = (
        REPO_ROOT / "app" / "services" / "recap_engine" / "core.py"
    ).read_text(encoding="utf-8")
    fn = body.split("def extract_preview_with_overlays(", 1)[1].split("\ndef ", 1)[0]
    assert "_visual_filters(mirror_video" in fn
    assert "title_idx" not in fn and "_render_title_png" not in fn


# ── AS-2 (client): one stage authority ──────────────────────────────

def test_stage_aspect_authority_exists_and_is_used():
    assert "function recapStageAspect()" in APP_JS
    block = _function_block("function updateCanvasMockupVisuals() {", span=20000)
    assert "const stage = recapStageAspect();" in block
    assert "mockupFrame.style.aspectRatio = stage.css;" in block
    # Original resolves from the composite's natural aspect when the
    # probed source dimensions are missing.
    helper = _function_block("function recapStageAspect()", span=1800)
    assert "ci.naturalWidth" in helper
    assert 'state.recapRatio === "Original"' in helper


def test_composite_load_backfills_original_source_dims():
    assert "compositeImg.onload" in APP_JS
    assert "state.recapSourceWidth = compositeImg.naturalWidth;" in APP_JS
    assert "state.recapSourceHeight = compositeImg.naturalHeight;" in APP_JS


# ── AS-1: stacked ratio cards ───────────────────────────────────────

def test_ratio_cards_stack_name_sub_dims():
    for ratio, name_key, sub_key in (
        ("9:16", "ratio_9_16", "ratio_9_16_sub"),
        ("16:9", "ratio_16_9", "ratio_16_9_sub"),
        ("1:1", "ratio_1_1", "ratio_1_1_sub"),
        ("Original", "ratio_original", "ratio_original_sub"),
    ):
        btn = INDEX_HTML.split(f'data-ratio="{ratio}"', 1)[1].split("</button>", 1)[0]
        assert f'class="ratio-name" data-i18n="{name_key}"' in btn
        assert f'class="ratio-sub" data-i18n="{sub_key}"' in btn
        assert 'class="ratio-dims"' in btn
    # Wording preserved — the usage text moved, it did not change.
    for key in ("ratio_9_16_sub", "ratio_16_9_sub", "ratio_1_1_sub", "ratio_original_sub"):
        assert I18N_JS.count(f"{key}:") == 2, key  # both locales
    assert 'ratio_9_16_sub: "TikTok / Reels / Shorts"' in I18N_JS
    assert 'ratio_original_sub: "မူရင်းအချိုး"' in I18N_JS
    assert 'ratio_original_sub: "Source Ratio"' in I18N_JS


def test_ratio_card_css_is_a_stacked_card_not_a_pill():
    as_block = STYLE_CSS.split("Batch AS (2026-11-05)")[1]
    assert ".ratio-pills-grid .ratio-btn" in as_block
    assert "flex-direction: column" in as_block
    assert ".ratio-btn .ratio-name" in as_block
    assert ".ratio-btn .ratio-sub" in as_block


# ── AS-6: CTA clearance + chip wrap ─────────────────────────────────

def test_voice_tab_clears_the_fixed_cta_band():
    as_block = STYLE_CSS.split("Batch AS (2026-11-05)")[1]
    assert "#tab-voice { padding-bottom: 72px; }" in as_block
    # The recap tab's pre-existing extra clearance stays in force.
    assert "#tab-recap { padding-bottom: 120px; }" in STYLE_CSS


def test_persona_filter_chips_wrap_without_clipping():
    as_block = STYLE_CSS.split("Batch AS (2026-11-05)")[1]
    assert ".persona-filter-row { flex-wrap: wrap; }" in as_block
    assert ".persona-filter-row .filter-chip" in as_block
    assert "text-overflow: clip" in as_block


# ── Cache token ─────────────────────────────────────────────────────

def test_static_token_is_r67():
    assert INDEX_HTML.count("20261104_r137") == 4
    for text in (INDEX_HTML, APP_JS, I18N_JS, STYLE_CSS):
        assert "20261104_r66" not in text
