"""Batch AK (2026-11-05): premium-standard fixes.

Pins: the Micro-Zoom default (1.00 everywhere a default lives);
the subtitle anchor migration to the Recapter standard (bottom-edge
margin, plate-height-aware one-time conversion of stored centre-%
values, picture-relative font); the title panel fitting its wrapped
text (Addition A); the title fallback hint (Addition B); the
live-jobs status bar + CTA hide (Addition C); preview unification
(the engine composite serves the Mini App); the colour grade
decoupled from Mirror (P5); language parity with the backend (S6/N2).
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = ROOT / "app" / "services" / "recap_engine"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ENGINE_DIR))

import core  # noqa: E402
import recap_pipeline  # noqa: E402
from app.webapp import api as api_mod  # noqa: E402

INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


def _letterbox():
    # 16:9 source in a 9:16 canvas: picture 1080x608 at oy=656.
    return core.frame_layout("9:16", "blur", 1920, 1080)


def _fullframe():
    return core.frame_layout("9:16", "blur", 1080, 1920)


# ── S1: Micro-Zoom default is 1.00 ─────────────────────────────────

def test_zoom_defaults_are_1_00():
    req = api_mod.RecapConfigRequest(url="https://example.com/v")
    assert req.zoom_factor == 1.0
    assert api_mod.DEFAULT_USER_PREFS["recapZoom"] == 1.0
    assert inspect.signature(core.extract_preview_with_overlays).parameters["zoom_factor"].default == 1.0
    assert inspect.signature(recap_pipeline.run_recap_pipeline).parameters["zoom_factor"].default == 1.0
    assert inspect.signature(recap_pipeline.generate_recap_preview).parameters["zoom_factor"].default == 1.0


def test_zoom_default_in_markup():
    m = re.search(r'id="recap-zoom-picker">(.*?)</div>', INDEX_HTML, re.S)
    assert m, "zoom picker present"
    block = m.group(1)
    active = re.search(r'class="pill-btn active" data-zoom="([\d.]+)"', block)
    assert active and active.group(1) == "1.00"


# ── S3 / Addition D: anchor migration ──────────────────────────────

def test_centre_to_margin_conversion_values():
    # 16pt/1-line at the 608px reference: font 24px, plate 47.6px
    # (7.83% of picture) → margin = 1 − 0.85 − 0.0391 ≈ 0.111.
    assert core.sub_centre_to_bottom_margin(0.85, 16, 1) == pytest.approx(0.111, abs=0.002)
    # 2-line: font 30px, plate 93px (15.3%) → ≈ 0.074.
    assert core.sub_centre_to_bottom_margin(0.85, 16, 2) == pytest.approx(0.074, abs=0.002)
    # A bottom-hugging centre converts to a small margin; a high
    # centre to a large one; output is clamped to [0, 0.95].
    assert core.sub_centre_to_bottom_margin(0.97, 16, 1) < 0.02
    assert core.sub_centre_to_bottom_margin(0.05, 16, 1) > 0.85
    assert core.sub_centre_to_bottom_margin(0.0, 16, 1) <= 0.95


def test_margin_roundtrip_keeps_visual_position():
    for lay in (_letterbox(), _fullframe()):
        cw, ch = lay["cw"], lay["ch"]
        legacy = core.subtitle_plate_box(
            cw, ch, lay, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=None)
        margin = core.sub_centre_to_bottom_margin(0.85, 16, 1)
        moved = core.subtitle_plate_box(
            cw, ch, lay, raw_font_size=16, sub_lines=1, sub_y=0.85,
            min_h=None, sub_margin=margin)
        # The plate centre after conversion is within 2px of the
        # legacy centre — existing users' subtitles do not jump.
        assert abs(moved["y"] - legacy["y"]) * ch <= 2.0


def test_margin_plate_bottom_sits_on_picture_rim():
    lay = _letterbox()
    plate = core.subtitle_plate_box(
        1080, 1920, lay, raw_font_size=16, sub_lines=1, sub_y=0.85,
        min_h=None, sub_margin=0.05)
    bottom_px = (plate["y"] + plate["h"] / 2) * 1920
    rim_px = lay["oy"] + lay["ph"] - 0.05 * lay["ph"]
    assert abs(bottom_px - rim_px) <= 2.0


def test_font_is_picture_relative():
    lb = _letterbox()
    # 4.0% of the 608px picture, not of the 1920px canvas.
    assert core.subtitle_font_px(1080, 1920, 16, 1, lb) == 24
    assert core.subtitle_font_px(1080, 1920, 16, 1, _fullframe()) == 77
    # A canvas of the picture's own height gives the same font.
    assert core.subtitle_font_px(1080, 608, 16, 1, None) == 24


def test_prefs_migration_runs_once():
    eff = api_mod._effective_user_prefs(
        {"recapSubY": 85, "recapFontSize": 16, "recapLines": 1})
    assert eff["recapSubMargin"] == 11
    assert eff["recapSettingsVersion"] == 2
    eff2 = api_mod._effective_user_prefs(
        {"recapSubY": 85, "recapFontSize": 16, "recapLines": 2})
    assert eff2["recapSubMargin"] == 7
    # Already migrated: stored margin is authoritative, untouched.
    eff3 = api_mod._effective_user_prefs(
        {"recapSubY": 50, "recapSubMargin": 30, "recapSettingsVersion": 2})
    assert eff3["recapSubMargin"] == 30
    # Never stored a position: fresh 5% default, stamped v2.
    eff4 = api_mod._effective_user_prefs({})
    assert eff4["recapSubMargin"] == 5
    assert eff4["recapSettingsVersion"] == 2


def test_sub_margin_request_field():
    req = api_mod.RecapConfigRequest(url="https://example.com/v")
    assert req.sub_bottom_margin is None
    req2 = api_mod.RecapConfigRequest(
        url="https://example.com/v", sub_bottom_margin=5)
    assert req2.sub_bottom_margin == 5
    with pytest.raises(Exception):
        api_mod.RecapConfigRequest(
            url="https://example.com/v", sub_bottom_margin=96)
    assert "sub_margin=(float(req.sub_bottom_margin)" in API_PY


# ── Addition A: the title panel fits the wrapped text ──────────────

def test_title_pillow_panel_fits_wrapped_text(tmp_path):
    title = ("အလွန်ရှည်လျားသော ရုပ်ရှင်ခေါင်းစဉ်တစ်ခုဖြစ်ပြီး "
             "စာကြောင်းများစွာ ကွဲသွားမည်ဖြစ်ပါသည် နောက်ထပ် "
             "စာသားများလည်း ပါဝင်နေပါသည် အဆုံးထိတိုင်အောင်")
    lines = core._wrap_title_lines(title, 40, 420, "")
    assert len(lines) >= 3, f"expected a multi-line wrap, got {lines!r}"
    out = tmp_path / "title.png"
    core._render_text_with_pillow(title, out, font_size=40, max_width=420)
    from PIL import Image
    img = Image.open(out).convert("RGBA")
    # Gold text pixels (255, 225, 120): the last line's ink must sit
    # INSIDE the panel — above the bottom padding, never clipped at
    # the panel's edge (the production clip this batch fixes).
    gold_rows = [y for y in range(img.height)
                 if any(img.getpixel((x, y))[:3] == (255, 225, 120)
                        for x in range(0, img.width, 3))]
    assert gold_rows, "title text rendered"
    assert max(gold_rows) < img.height - 4
    assert img.height >= len(lines) * 40


def test_title_wrap_breaks_spaceless_overwide_word():
    word = "မြန်မာဇာတ်ကားခေါင်းစဉ်အရှည်ကြီးတစ်ခုဖြစ်ပါသည်"
    lines = core._wrap_title_lines(word, 40, 200, "")
    assert len(lines) >= 2
    assert "".join(lines) == word


# ── Addition B: title fallback hint ────────────────────────────────

def test_title_fallback_hint_wiring():
    assert 'id="recap-title-fallback-hint"' in INDEX_HTML
    assert 'data-i18n="recap_title_fallback_hint"' in INDEX_HTML
    assert "syncTitleFallbackHint" in APP_JS


# ── Addition C: live-jobs bar, sheet, CTA hide ─────────────────────

def test_cta_hide_predicate_and_markup():
    assert "function recapJobActive()" in APP_JS
    assert 'src.classList.toggle("hidden", recapBlocked)' in APP_JS
    assert 'id="live-jobs-pill-dismiss"' in INDEX_HTML
    assert "dismissLiveJobsPill()" in INDEX_HTML
    assert "function dismissLiveJobsPill()" in APP_JS


def test_status_bar_and_sheet_css():
    assert ".live-jobs-pill-main" in STYLE_CSS
    assert "width: min(96vw, 480px)" in STYLE_CSS
    meta = re.search(r"\.live-job-row-meta \{([^}]*)\}", STYLE_CSS)
    assert meta and "overflow-wrap: break-word" in meta.group(1)
    assert ".live-job-row-actions" in STYLE_CSS


def test_terminal_stage_labels():
    assert 'live_job_status_success' in APP_JS
    assert 'if (liveJobsIsTerminal(job.status))' in APP_JS


# ── S2: preview unification ────────────────────────────────────────

def test_composite_preview_wiring():
    assert '"/recap/preview-composite"' in API_PY
    assert "/api/recap/preview-composite" in APP_JS
    assert 'id="canvas-composite-img"' in INDEX_HTML
    # One shared sample sentence (P2/N4).
    sig = inspect.signature(core.extract_preview_with_overlays)
    assert sig.parameters["sample_text"].default == core.PREVIEW_SAMPLE_TEXT
    # The fake demo subtitle + placeholder logo text are gone (P3/P4).
    assert 'id="mockup-orig-sub"' not in INDEX_HTML
    assert 'data-i18n="logo_label"' not in INDEX_HTML


# ── P5: colour grade is not part of the mirror ─────────────────────

def test_grade_applies_without_mirror():
    lay = {"fill": "crop", "cw": 1920, "ch": 1080, "sw": 1920, "sh": 1080}
    off, _ = core._visual_filters(False, 1.0, lay)
    on, _ = core._visual_filters(True, 1.0, lay)
    assert "eq=contrast=1.03" in off
    assert "eq=contrast=1.03" in on
    assert "hflip" not in off
    assert "hflip" in on


# ── S6/N2: language parity ─────────────────────────────────────────

def test_language_parity_with_backend():
    picker = set(re.findall(r'data-lang="([a-z]+)"', INDEX_HTML))
    assert picker == set(api_mod.RECAP_SOURCE_LANGUAGE_CODES)
    assert set(api_mod.USER_PREF_ENUMS["recapSourceLanguage"]) == picker
    enum_line = next(l for l in APP_JS.splitlines()
                     if "recapSourceLanguage: [" in l)
    for code in picker:
        assert f'"{code}"' in enum_line


# ── Cache token ────────────────────────────────────────────────────

def test_cache_token_r64():
    assert INDEX_HTML.count("?v=20261104_r75") == 4
    assert "?v=20261104_r62" not in INDEX_HTML
