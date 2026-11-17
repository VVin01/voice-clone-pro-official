"""Phase 3 (UI Rebuild V2 — Recap Studio & Editor MUST-FIX Polish) unit suite."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")

PHASE3_CSS = STYLE_CSS[STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 3") :]


def test_v2_phase3_cache_token_current():
    assert INDEX_HTML.count("?v=20261117_r161") == 4
    assert "20261114_r146" not in INDEX_HTML


def test_v2_phase3_zero_backdrop_filter_in_scrolling_recap_cards():
    assert "backdrop-filter" not in PHASE3_CSS
    assert "-webkit-backdrop-filter" not in PHASE3_CSS


def test_v2_phase3_must_fix_1_overlay_grip_separation_and_unclipped_stage():
    # Editor overlays separated horizontally (Blur left, Sub right) + zero corner clipping
    for marker in (
        "#tab-recap #recap-mockup-wrap.mockup-screen-container",
        "#tab-recap #recap-mockup-frame.mockup-screen",
        "border-radius: 5px;",
        "#tab-recap #preview-blur-box .drag-grip-icon",
        "left: 6px;",
        "#tab-recap #preview-sub-box {",
        "pointer-events: none;",
        "#tab-recap #preview-sub-box .sub-grip",
        "right: 6px;",
        "pointer-events: auto;",
        "#tab-recap #preview-blur-box.is-near-top .drag-grip-icon",
        "#tab-recap #preview-sub-box.is-near-top .sub-grip",
    ):
        assert marker in PHASE3_CSS, marker

    # Preview = Output defaults preserved untouched in index.html
    assert 'id="preview-blur-box" style="top: 85%; height: 7%;' in INDEX_HTML
    assert "bottom: 5%" in STYLE_CSS or "recapSubMargin" in APP_JS


def test_v2_phase3_must_fix_2_sticky_offset_stacking_and_portrait_scroll():
    for marker in (
        "--vcp-hdr-sticky-top",
        "#tab-recap .vcp-step2-sticky-preview",
        "top: var(--vcp-hdr-sticky-top, 114px);",
        "z-index: 6;",
        "#tab-recap .vcp-step2-sticky-preview.is-ratio-9-16",
        "#tab-recap .vcp-step2-sticky-preview.is-portrait-stage",
        "#tab-recap .recap-dock-section",
        "z-index: 18;",
    ):
        assert marker in PHASE3_CSS, marker

    fn = APP_JS.split("function updateCanvasMockupVisuals()", 1)[1].split("function ", 1)[0]
    for token in ("is-ratio-9-16", "is-ratio-16-9", "is-portrait-stage", "--vcp-hdr-sticky-top"):
        assert token in fn, token


def test_v2_phase3_must_fix_3_master_preset_apply_and_restore_wiring():
    assert "window.__RECAP_SETTINGS_KEY = RECAP_SETTINGS_KEY;" in APP_JS
    assert "window.restoreRecapSettings = restoreRecapSettings;" in APP_JS
    assert "window.__ddFitPreviewFrame" in APP_JS
    assert "window.refreshRecapStep2Summary" in APP_JS
    assert "window.refreshRecapStep4Output" in APP_JS

    restore_block = APP_JS.split("function restoreRecapSettings()", 1)[1].split("window.restoreRecapSettings =", 1)[0]
    for call in (
        "updateCanvasMockupVisuals();",
        "ddFitPreviewFrame();",
        "refreshRecapStep2Summary();",
        "refreshRecapStep4Thumb();",
        "refreshRecapStep4Facts();",
    ):
        assert call in restore_block, f"restoreRecapSettings missing {call}"

    preset_block = APP_JS.split("function applyRecapPresetObject(patch)", 1)[1].split("function applyRecapMasterPreset(", 1)[0]
    for call in (
        "window.restoreRecapSettings",
        "window.updateCanvasMockupVisuals",
        "window.__ddFitPreviewFrame",
        "window.refreshRecapStep2Summary",
        "window.refreshRecapStep4Output",
        "applyActiveRecapStep2Category",
    ):
        assert call in preset_block, f"applyRecapPresetObject missing {call}"


def test_v2_phase3_recap_studio_v2_surfaces_dark_and_lite():
    for marker in (
        "#tab-recap .recap-steps-nav",
        "#tab-recap .video-drop-zone",
        "#tab-recap .vcp-step2-cat-tabs .vcp-seg-btn.active",
        "#tab-recap .vcp-preset-card.active",
        "#tab-recap .vcp-sub-style-card.active",
        "#tab-recap .recap-step2-summary",
        "#tab-recap .vcp-dual-quote-card",
        'html[data-theme="dark"] #tab-recap .vcp-step2-sticky-preview',
        'html[data-theme="dark"] #tab-recap .recap-dock-section',
        "html.lite #tab-recap .recap-dock-section",
        'html[data-theme="dark"].lite #tab-recap .recap-dock-section',
    ):
        assert marker in PHASE3_CSS, marker


def test_v2_phase3_zero_deleted_ids_or_onclicks_vs_main():
    try:
        main_html = subprocess.check_output(
            ["git", "show", "main:app/webapp/static/index.html"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except Exception:
        return
    main_ids = set(re.findall(r'\bid="([^"]+)"', main_html))
    curr_ids = set(re.findall(r'\bid="([^"]+)"', INDEX_HTML))
    assert not (main_ids - curr_ids), sorted(main_ids - curr_ids)
    main_on = set(re.findall(r'\bonclick="([^"]+)"', main_html))
    curr_on = set(re.findall(r'\bonclick="([^"]+)"', INDEX_HTML))
    assert not (main_on - curr_on), sorted(main_on - curr_on)
