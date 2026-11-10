"""Batch CA (2026-11-07): the Step-2 preview blur box stops washing
the subtitle, and the settled preview is the engine's true frame.

Diagnosis (owner screenshot 2026-11-07): the Mini App blur box was a
CSS overlay with ``backdrop-filter: blur(6px)`` (plus a JS inline
``style.backdropFilter`` scaled by the strength slider). A backdrop
filter frosts EVERYTHING under the rectangle — including the new
subtitle composited into the engine frame beneath it — and both
boxes default to Y=85%, so the subtitle looked blurred while
positioning (and at rest). The final render was never affected:
the engine applies blur boxes FIRST and composites subtitle PNGs
LAST. CA removes the frosting (tint + dashed outline only) and
pins the already-existing settled-truth route (Batch AK's
``/recap/preview-composite``) so it cannot silently regress:

* Item 1 — no backdrop-filter on ``.preview-blur-box`` (stylesheet
  or inline); the tint/border/label/stacking survive.
* Item 2 — the client sends FULL geometry (blur + subtitle, url
  XOR uploaded_path) to the composite endpoint on every settle
  (drag/resize/slider/seek funnel through the sync functions into
  the 600 ms-debounced, seq-guarded scheduler); the blur-strength
  slider — previously the one geometry input that refreshed
  nothing but the frosting — now schedules the composite too.
  Server pins: the composite request model carries the geometry,
  the plain-frame model stays geometry-free, and the endpoint
  routes both source kinds into ``generate_recap_preview``.
* Engine ORDER pins + one real render: in BOTH
  ``extract_preview_with_overlays`` and ``apply_production_overlays``
  the blur-box loop precedes subtitle compositing; behaviourally,
  a sample subtitle burned over a blurred zone stays sharp while
  the surrounding zone is measurably blurred.
"""
from __future__ import annotations

import inspect
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
from app.webapp import api as api_mod  # noqa: E402

INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


def _block(text: str, marker: str, span: int = 2600) -> str:
    i = text.index(marker)
    return text[i:i + span]


def _css_rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r" \{([^}]*)\}", STYLE_CSS)
    assert m, f"CSS rule {selector} present"
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)


# ── Item 1: the blur box no longer frosts what is beneath it ──────

def test_blur_box_rule_has_no_backdrop_filter():
    declarations = _css_rule(".preview-blur-box")
    assert "backdrop-filter" not in declarations
    assert "backdropFilter" not in declarations


def test_blur_box_keeps_its_region_marking():
    declarations = _css_rule(".preview-blur-box")
    # The tint (slightly stronger than the retired frosted fill)
    # plus the dashed outline carry the "this region will blur"
    # meaning now.
    assert "background: rgba(120, 130, 150, 0.45);" in declarations
    assert "border: 1.5px dashed" in declarations
    # The no-media placeholder tint is untouched.
    no_media = _css_rule(".preview-blur-box.no-media")
    assert "background: rgba(120, 130, 150, 0.16);" in no_media


def test_blur_box_stays_below_the_subtitle_handle():
    # .draggable-box (the blur box's stacking) is z-index 6; the
    # subtitle handle stays above it at 7.
    assert "z-index: 6" in _css_rule(".draggable-box")
    assert "z-index: 7" in _css_rule(".preview-sub-box")


def test_no_inline_backdrop_frosting_anywhere_in_app_js():
    # syncBlurStrengthVisual used to write style.backdropFilter on
    # the blur box, scaled by the strength slider — an inline style
    # would override the stylesheet fix, so the mechanism is gone
    # from the whole client.
    assert "backdropFilter" not in APP_JS


def test_blur_strength_schedules_the_composite():
    block = _block(APP_JS, "function syncBlurStrengthVisual() {", span=1400)
    assert "backdropFilter" not in block
    # Strength is payload geometry: a change must refresh the
    # settled engine frame (before CA it refreshed nothing).
    assert "scheduleRecapCompositeRefresh();" in block


# ── Item 2 (client): full geometry on every settle ────────────────

def test_composite_payload_carries_blur_and_subtitle_geometry():
    block = _block(APP_JS, "function recapCompositePayload() {", span=3400)
    for key in ("url:", "uploaded_path:", "blur_y:", "blur_h:",
                "blur_x:", "blur_w:", "blur_box_explicit:",
                "blur_strength:", "blur_auto_detect:",
                "sub_bottom_margin:", "sub_x:", "sub_w:",
                "sub_lines:", "sub_font_size:", "sub_style:"):
        assert key in block, f"payload key {key} missing"
    assert "/api/recap/preview-composite" in APP_JS


def test_composite_refresh_is_debounced_seq_and_hash_guarded():
    sched = _block(APP_JS, "function scheduleRecapCompositeRefresh() {", span=400)
    assert "setTimeout(refreshRecapCompositePreview, 600)" in sched
    refresh = _block(APP_JS, "async function refreshRecapCompositePreview() {", span=2400)
    assert "hash === recapCompositeHash" in refresh  # unchanged → no refetch
    assert "mySeq !== recapCompositeSeq" in refresh  # newer refresh wins
    assert "body: hash" in refresh                   # the payload IS the body


def test_geometry_settles_funnel_into_the_scheduler():
    # Drag (V/H), resize (H/W) and the blur/sub sliders all funnel
    # through the two sync functions; the seek path schedules too.
    assert "scheduleRecapCompositeRefresh();" in _block(
        APP_JS, "function syncBlurBoxVisual() {", span=2600)
    assert "scheduleRecapCompositeRefresh();" in _block(
        APP_JS, "function syncSubBoxVisual() {", span=3200)
    assert "scheduleRecapCompositeRefresh();" in _block(
        APP_JS, "function seekCanvasFrame(targetSec) {", span=700)


# ── Item 2 (server): the composite route takes the geometry ──────

def test_composite_request_model_carries_geometry_and_both_sources():
    req = api_mod.RecapPreviewCompositeRequest()
    for field in ("url", "uploaded_path", "blur_y", "blur_h",
                  "blur_x", "blur_w", "blur_box_explicit",
                  "blur_strength", "blur_auto_detect",
                  "burn_subtitles", "sub_bottom_margin", "sub_x",
                  "sub_w", "sub_lines", "sub_font_size", "sub_style"):
        assert hasattr(req, field), f"composite field {field} missing"


def test_plain_frame_request_stays_geometry_free():
    # Recon pin: /recap/preview-frame is the plain seek frame —
    # geometry belongs to the composite endpoint alone.
    assert set(api_mod.RecapPreviewFrameRequest.model_fields) == {"url", "t"}


def test_composite_endpoint_routes_both_sources_into_the_engine():
    src = inspect.getsource(api_mod.recap_preview_composite)
    assert "generate_recap_preview" in src
    assert "req.uploaded_path" in src
    assert "req.url" in src
    assert "blur_strength=req.blur_strength" in src
    assert "blur_auto_detect=bool(req.blur_auto_detect)" in src
    assert '"/recap/preview-composite"' in API_PY


# ── Engine ORDER pins: blur first, subtitles last ─────────────────

def test_preview_engine_applies_blur_before_the_sample_subtitle():
    src = inspect.getsource(core.extract_preview_with_overlays)
    assert src.index("boxblur=luma_radius") < src.index("Add sample subtitle")


def test_production_engine_applies_blur_before_subtitles():
    src = inspect.getsource(core.apply_production_overlays)
    assert "blur boxes first, then everything else" in src
    assert src.index("boxblur=luma_radius") < src.index("PngSubtitleRenderer(")


# ── Behavioural: a subtitle over a blurred zone stays sharp ──────

def _gradient_energy(img, x0, x1, y0, y1) -> float:
    import numpy as np

    win = np.asarray(img)[y0:y1, x0:x1].astype(np.float32)
    return float(np.abs(np.diff(win, axis=1)).mean())


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)
def test_composited_preview_subtitle_stays_sharp_over_blur(tmp_path):
    from PIL import Image

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=10:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    raw = tmp_path / "raw.png"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "1.0", "-i", str(src), "-frames:v", "1", str(raw)],
        check=True,
    )

    # One wide blur box over the subtitle zone (centre 82%, height
    # 30% of the 360x640 picture → y 429..621, x 18..342).
    overlays = {
        "blur_boxes": [{"x": 0.05, "y": 0.82, "w": 0.90, "h": 0.30}],
        "blur_strength": 40,
        "sub_style": "yellow",
    }
    frames = {}
    for name, burn in (("with_sub", True), ("no_sub", False)):
        ov = dict(overlays)
        ov["burn_subtitles"] = burn
        out = core.extract_preview_with_overlays(
            ffmpeg="ffmpeg", source_video=str(src),
            output_image=str(tmp_path / f"{name}.jpg"),
            timestamp=1.0, overlays=ov, output_ratio="Original",
            frame_fill="blur", mirror_video=False, zoom_factor=1.0,
            sample_text="PREVIEW TEST", work_dir=str(tmp_path),
            ffprobe="ffprobe",
        )
        frames[name] = Image.open(out).convert("L")
    raw_img = Image.open(raw).convert("L")
    width, height = frames["with_sub"].size

    import numpy as np

    a = np.asarray(frames["with_sub"]).astype(np.int16)
    b = np.asarray(frames["no_sub"]).astype(np.int16)
    mask = np.abs(a - b) > 12
    ys, xs = np.where(mask)
    assert len(xs) > 200, "the sample subtitle was composited"
    bx0, bx1, by0, by1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    # The subtitle lands INSIDE the blurred zone…
    assert bx0 >= int(0.05 * width) - 2 and bx1 <= int(0.95 * width) + 2
    assert by0 >= int(0.67 * height) - 2 and by1 <= int(0.97 * height) + 2
    # …yet stays sharp: its edge energy dwarfs the same window in
    # the blur-only frame (text composited AFTER the blur).
    e_sub = _gradient_energy(frames["with_sub"], bx0, bx1 + 1, by0, by1 + 1)
    e_sub_blur_only = _gradient_energy(frames["no_sub"], bx0, bx1 + 1, by0, by1 + 1)
    assert e_sub > 4 * e_sub_blur_only
    assert e_sub > 8.0
    # Control: a strip inside the box but above the subtitle is
    # blurred in BOTH frames (energy well under the raw source).
    cy0, cy1 = int(0.67 * height) + 2, by0 - 6
    assert cy1 - cy0 >= 8
    e_ctrl_raw = _gradient_energy(raw_img, int(0.07 * width), int(0.93 * width), cy0, cy1)
    assert _gradient_energy(frames["with_sub"], int(0.07 * width), int(0.93 * width), cy0, cy1) < 0.55 * e_ctrl_raw
    assert _gradient_energy(frames["no_sub"], int(0.07 * width), int(0.93 * width), cy0, cy1) < 0.55 * e_ctrl_raw


# ── Cache token ────────────────────────────────────────────────────

def test_cache_token_is_r98():
    assert INDEX_HTML.count("20261104_r128") == 4
    assert ("20261104_r" + "97") not in INDEX_HTML
