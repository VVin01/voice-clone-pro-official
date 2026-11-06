"""Batch AH (2026-11-05): Mini App recap-form fixes from the owner's
own live check (screenshots + report, 2026-11-05 08:21).

* (1) DUPLICATED script-review toggle — Batch K had added a second
  checkbox (#recap-script-review-toggle-main) as a mirrored twin of
  the advanced-section one. AH removes the advanced copy, renames
  the surviving main-area toggle (the one with the hint) to the
  canonical id, and deletes the mirror-sync: exactly ONE toggle,
  read directly by the fingerprint, the submit payload and the
  History prefill.

* (2) TITLE overlay controls — the engine always supported
  title_text + title_position, but the Mini App/API never threaded
  a title (the worker passes no title_text and its local source
  starves the downloader's URL-title fallback), so Mini App jobs
  rendered NO title badge. AH adds title_enabled / title_text /
  title_x / title_y end-to-end (request → worker → downloader →
  pipeline overlays), a Live Canvas title box with 2-axis drag +
  X/Y sliders, and account-prefs persistence. Defaults reproduce
  the historical behaviour exactly: disabled, centre [0.5, 0.12].

* (3) PREVIEW line count — the RS-2 CSS override neutralised the
  .one-line/.two-lines line-clamps, so the canvas sample always
  wrapped naturally (2 lines at Lines=1). AH removes the override;
  the preview honours sub_lines like the render (Batch P parity).
  The server preview frame already honoured sub_lines — unchanged.

* (4) SUBTITLE drag — touch drags start only on the .drag-grip-icon
  (R40 item 5, kept: whole-box touch drags hijacked page scrolls
  and silently persisted wrong sub_y values). The grip was an 11px
  bare glyph — undiscoverable and nearly unhittable. AH makes it a
  wide pill handle; the vertical-drag clamp becomes a parameter so
  the title box can use the full [0, 100] range.

* (5) Sweep pin: index.html carries no duplicate element ids.
"""
from __future__ import annotations

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
import recap_pipeline  # noqa: E402

STATIC = ROOT / "app" / "webapp" / "static"
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
DOWNLOADER_PY = (ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")
PIPELINE_PY = (ENGINE_DIR / "recap_pipeline.py").read_text(encoding="utf-8")

FFMPEG_BIN = shutil.which("ffmpeg")


def _req(**kw):
    import app.webapp.api as api_mod

    return api_mod.RecapConfigRequest(url="https://example.com/v", **kw)


# ── 1. Script-review de-duplication ────────────────────────────────

def test_exactly_one_script_review_toggle():
    assert INDEX_HTML.count('id="recap-script-review-toggle"') == 1
    # The advanced-section copy is gone; the split toggle next to it
    # is untouched and also unique.
    assert INDEX_HTML.count('id="recap-split-toggle"') == 1


def test_twin_toggle_fully_removed():
    assert "recap-script-review-toggle-main" not in INDEX_HTML
    assert "recap-script-review-toggle-main" not in APP_JS
    assert "srMainToggle" not in APP_JS
    assert "srAdvToggle" not in APP_JS


def test_surviving_toggle_has_the_hint_and_feeds_the_payload():
    # The kept row is the one with the helpful description, sitting
    # in the main area (before the CTA).
    assert 'data-i18n="recap_script_review_hint"' in INDEX_HTML
    toggle_at = INDEX_HTML.index('id="recap-script-review-toggle"')
    cta_at = INDEX_HTML.index('id="btn-start-recap"')
    assert toggle_at < cta_at
    # The payload's script_review reads that very element.
    assert (
        'script_review: !!((document.getElementById("recap-script-review-toggle") || {}).checked)'
        in APP_JS
    )


# ── 2. Title: API model ────────────────────────────────────────────

def test_title_fields_default_to_historical_behaviour():
    req = _req()
    assert req.title_enabled is False
    assert req.title_text is None
    assert req.title_x == 50
    assert req.title_y == 12


def test_title_fields_round_trip_through_persisted_config():
    req = _req(title_enabled=True, title_text="My Recap", title_x=30, title_y=70)
    dump = req.model_dump()
    dump.pop("url", None)
    rebuilt = _req(**dump)
    assert rebuilt.title_enabled is True
    assert rebuilt.title_text == "My Recap"
    assert (rebuilt.title_x, rebuilt.title_y) == (30, 70)


def test_title_bounds():
    from pydantic import ValidationError

    for kw in ({"title_x": -1}, {"title_x": 101}, {"title_y": -1}, {"title_y": 101},
               {"title_text": "x" * 201}):
        with pytest.raises(ValidationError):
            _req(**kw)
    _req(title_x=0, title_y=0)
    _req(title_x=100, title_y=100)
    _req(title_text="x" * 200)


def test_title_worker_threading():
    assert (
        "title_text=((req.title_text or req.title) if req.title_enabled else None),"
        in API_PY
    )
    assert "title_position=[req.title_x / 100.0, req.title_y / 100.0]," in API_PY


# ── 2b. Title: prefs allowlist ─────────────────────────────────────

def test_title_prefs_defaults_and_sanitizer():
    import app.webapp.api as api_mod

    defaults = api_mod.DEFAULT_USER_PREFS
    assert defaults["recapTitleEnabled"] is False
    assert defaults["recapTitleText"] == ""
    assert defaults["recapTitleX"] == 50
    assert defaults["recapTitleY"] == 12

    out = api_mod._sanitize_user_prefs({
        "recapTitleEnabled": True,
        "recapTitleText": "Hello",
        "recapTitleX": 250,          # out of range → dropped (the
        "recapTitleY": -5,           # sanitizer never clamps ints)
        "recapTitleText2": "nope",   # unknown keys are dropped
    })
    assert out["recapTitleEnabled"] is True
    assert out["recapTitleText"] == "Hello"
    assert "recapTitleX" not in out
    assert "recapTitleY" not in out
    assert "recapTitleText2" not in out
    # In-range values pass through untouched.
    out3 = api_mod._sanitize_user_prefs({"recapTitleX": 0, "recapTitleY": 100})
    assert out3["recapTitleX"] == 0 and out3["recapTitleY"] == 100
    # Over-long free text is dropped whole, never stored.
    out2 = api_mod._sanitize_user_prefs({"recapTitleText": "y" * 500})
    assert "recapTitleText" not in out2


# ── 2c. Title: downloader + pipeline threading ─────────────────────

def test_title_threading_signatures():
    import inspect

    from app.services.downloader import MediaDownloader

    dl_params = inspect.signature(MediaDownloader.generate_recap_video).parameters
    assert "title_text" in dl_params and "title_position" in dl_params
    pl_params = inspect.signature(recap_pipeline.run_recap_pipeline).parameters
    assert "title_text" in pl_params and "title_position" in pl_params
    assert pl_params["title_position"].default is None
    assert "title_position=title_position," in DOWNLOADER_PY


def _capture_pipeline_overlays(monkeypatch, tmp_path, **kwargs):
    """Run run_recap_pipeline with the planner + renderer stubbed;
    capture the overlays dict handed to EdgeSmartSync."""
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=320x240:rate=30:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    captured = {}

    class _FakePlanner:
        def __init__(self, *a, **kw):
            pass

        def build(self, source, job_dir, source_title=None, reuse=True):
            plan_path = Path(job_dir) / "plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("{}", encoding="utf-8")
            return plan_path, {"segments": []}

    class _FakeSync:
        def __init__(self, *a, **kw):
            captured["overlays"] = kw.get("overlays") or (a[0] if a else {})

        def run(self):
            return {"final": str(src), "audio": None, "segments": 0}

    monkeypatch.setattr(recap_pipeline, "RecapPlanBuilder", _FakePlanner)
    monkeypatch.setattr(recap_pipeline, "EdgeSmartSync", _FakeSync)
    recap_pipeline.run_recap_pipeline(src, job_dir=tmp_path / "job", **kwargs)
    return captured["overlays"]


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_pipeline_no_title_by_default(monkeypatch, tmp_path):
    overlays = _capture_pipeline_overlays(monkeypatch, tmp_path)
    assert "title_text" not in overlays
    assert "title_position" not in overlays


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_pipeline_title_default_position_is_historical(monkeypatch, tmp_path):
    overlays = _capture_pipeline_overlays(
        monkeypatch, tmp_path, title_text="My Title"
    )
    assert overlays["title_text"] == "My Title"
    assert overlays["title_position"] == [0.5, 0.12]


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_pipeline_title_custom_position_and_clamping(monkeypatch, tmp_path):
    overlays = _capture_pipeline_overlays(
        monkeypatch, tmp_path, title_text="T", title_position=[0.3, 0.7]
    )
    assert overlays["title_position"] == [0.3, 0.7]
    overlays = _capture_pipeline_overlays(
        monkeypatch, tmp_path, title_text="T", title_position=[1.4, -0.2]
    )
    assert overlays["title_position"] == [1.0, 0.0]
    overlays = _capture_pipeline_overlays(
        monkeypatch, tmp_path, title_text="T", title_position="bogus"
    )
    assert overlays["title_position"] == [0.5, 0.12]


# ── 2d. Title: the engine burns it where title_position says ───────

def _make_dark_src(tmp_path):
    # Temporal noise keeps the (still dark) clip above the overlay
    # pass's 5 KB output sanity floor without lifting any pixel
    # near the title-brightness threshold used below.
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x101018:size=320x240:rate=30:duration=3,noise=alls=18:allf=t",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    return src


def _title_centroid_y(tmp_path, out_video):
    frame = tmp_path / "frame.png"
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(out_video), "-frames:v", "1", str(frame)],
        check=True, capture_output=True,
    )
    from PIL import Image

    with Image.open(frame) as im:
        px = im.convert("L")
        w, h = px.size
        data = px.load()
        total = 0.0
        weight = 0.0
        for yy in range(h):
            for xx in range(w):
                v = data[xx, yy]
                if v > 120:
                    total += yy * v
                    weight += v
    assert weight > 0, "no title pixels found"
    return (total / weight) / h


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
@pytest.mark.parametrize("ty,lo,hi", [(0.12, 0.0, 0.35), (0.88, 0.65, 1.0)])
def test_engine_title_lands_at_title_position(tmp_path, ty, lo, hi):
    src = _make_dark_src(tmp_path)
    out = tmp_path / "out.mp4"
    core.apply_production_overlays(
        FFMPEG_BIN, src, out,
        {"title_text": "TITLE TEST", "title_position": [0.5, ty]},
        "libx264", tmp_path / "w", frame_size=(320, 240),
        layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=3,
    )
    assert out.is_file()
    cy = _title_centroid_y(tmp_path, out)
    assert lo <= cy <= hi, f"title centroid y={cy:.3f} not in [{lo}, {hi}] for ty={ty}"


# ── 2e. Title: frontend wiring ─────────────────────────────────────

def test_title_controls_markup():
    # Batch AM (M2 residual): the title drag handle is grip-only —
    # 'id="preview-title-text-el"' was removed from this list because
    # the ellipsized text span inside the handle is gone; the real
    # title renders in the composited frame.
    for el in ('id="recap-title-toggle"', 'id="recap-title-text"',
               'id="preview-title-box"',
               'id="slider-title-x"', 'id="slider-title-y"',
               'id="val-title-x"', 'id="val-title-y"'):
        assert el in INDEX_HTML, el
    assert 'maxlength="200"' in INDEX_HTML


def test_title_frontend_wiring():
    assert "function syncTitleBoxVisual()" in APP_JS
    assert "function titleCentreCanvasFrac(" in APP_JS
    # Payload carries all four fields.
    assert "title_enabled: !!state.recapTitleEnabled," in APP_JS
    assert 'title_text: (state.recapTitleText || "").trim() || null,' in APP_JS
    assert "title_x: sliderTitleXSubmit ? parseInt(sliderTitleXSubmit.value)" in APP_JS
    assert "title_y: sliderTitleYSubmit ? parseInt(sliderTitleYSubmit.value)" in APP_JS
    # Both drag helpers are attached to the title box.
    assert "enableVerticalDrag(previewTitle," in APP_JS
    assert "enableHorizontalDrag(previewTitle," in APP_JS
    # Prefs collect carries the four keys; prefs APPLY carries only
    # text/position since Batch BR (the toggle is session-truth —
    # account prefs must not resurrect it on load).
    assert "recapTitleEnabled: !!state.recapTitleEnabled," in APP_JS
    assert '"recapTitleText", "recapTitleX", "recapTitleY",' in APP_JS
    assert '"recapTitleEnabled", "recapTitleText"' not in APP_JS
    # Snapshot round-trip carries text/position; the enabled bool is
    # session-truth since Batch BR (absent from RECAP_SETTING_BOOLS).
    assert '"recapTitleEnabled"]' not in APP_JS
    assert "recapTitleX: [0, 100], recapTitleY: [0, 100]," in APP_JS
    # History prefill maps the stored config back in.
    assert "recapTitleEnabled: cfg.title_enabled === true," in APP_JS
    assert "recapTitleX: cfg.title_x, recapTitleY: cfg.title_y," in APP_JS


def test_title_i18n_keys_both_locales():
    for key in ("recap_title_toggle_label", "recap_title_hint",
                "recap_title_placeholder", "recap_title_sample",
                "slider_title_x", "slider_title_y"):
        assert I18N_JS.count(f"{key}:") == 2, key


# ── 3. Preview line count honoured ─────────────────────────────────

def test_line_clamps_stand_and_rs2_override_is_gone():
    # Batch AK rewrite: the CSS sample-text preview is retired (the
    # engine-composited frame renders the real line count), so the
    # clamp rules and the RS-2 override question are gone together —
    # a 1-line setting can no longer paint 2 lines because no CSS
    # text paints at all.
    assert ".preview-sub-box.one-line .preview-sub-text" not in STYLE_CSS
    assert ".preview-sub-box.two-lines .preview-sub-text" not in STYLE_CSS
    assert ".preview-sub-text" not in STYLE_CSS


def test_preview_toggles_line_class_from_setting():
    # Batch AK rewrite: line count reaches the preview through the
    # composite payload (the engine renders 1 vs 2 lines); the JS
    # class toggling on a CSS text box no longer exists.
    assert 'previewBox.classList.add("one-line")' not in APP_JS
    assert 'previewBox.classList.add("two-lines")' not in APP_JS
    assert "sub_lines:" in APP_JS  # composite payload + submit payload


# ── 4. Drag affordances ────────────────────────────────────────────

def test_grip_is_a_real_touch_target():
    grip = STYLE_CSS.index(".drag-grip-icon { touch-action: none;")
    block = STYLE_CSS[grip:grip + 300]
    assert "min-width: 30px" in block
    assert "border-radius: 999px" in block
    assert "background: rgba(10, 12, 18, 0.55)" in block


def test_vertical_drag_clamp_is_a_parameter():
    assert (
        "function enableVerticalDrag(boxElement, onUpdateY, getInitialY, getDeltaScale, getClampRange)"
        in APP_JS
    )
    # Default range unchanged for subtitle/blur (their slider range).
    assert "getClampRange ? getClampRange() : [10, 95]" in APP_JS


# ── 5. Sweep: no duplicate ids anywhere in the form ────────────────

def test_index_html_has_no_duplicate_ids():
    ids = re.findall(r'id="([^"]+)"', INDEX_HTML)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert dupes == [], f"duplicate ids: {dupes}"
