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
    assert INDEX_HTML.count("?v=20261123_round14") == 5
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
        "left: 0;",
        "pointer-events: auto;",
        "#tab-recap #preview-blur-box.is-near-top .drag-grip-icon",
        "#tab-recap #preview-sub-box.is-near-top .sub-grip",
    ):
        assert marker in PHASE3_CSS, marker

    # Round 12 Part A: default untouched blur visual sits above subtitle (72%); slider default remains 85
    assert 'id="preview-blur-box" style="top: 72%; height: 7%;' in INDEX_HTML
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
        "syncLockedRecapDefaultsToState();",
        "updateCanvasMockupVisuals();",
        "ddFitPreviewFrame();",
        "refreshRecapStep2Summary();",
        "refreshRecapStep4Thumb();",
    ):
        assert call in restore_block, f"restoreRecapSettings missing {call}"


def test_v2_phase3_recap_studio_v2_surfaces_dark_and_lite():
    for marker in (
        "#tab-recap .recap-steps-nav",
        "#tab-recap .video-drop-zone",
        "#tab-recap .vcp-step2-cat-tabs .vcp-seg-btn.active",
        "#tab-recap .vcp-preset-card.active",
        "#tab-recap .recap-step2-summary",
        "#tab-recap .vcp-dual-quote-card",
        'html[data-theme="dark"] #tab-recap .vcp-step2-sticky-preview',
        'html[data-theme="dark"] #tab-recap .recap-dock-section',
        "html.lite #tab-recap .recap-dock-section",
        'html[data-theme="dark"].lite #tab-recap .recap-dock-section',
    ):
        assert marker in PHASE3_CSS, marker


_RETIRED_VIP_STATIC_IDS = {
    "card-plan-combined-starter", "card-plan-master", "card-plan-recap",
    "card-plan-recap-starter", "card-plan-voice", "card-plan-voice-packs",
    "plan-panel-combined",
    # Round 8 (B4 + B7): #tts-balance-line and #tts-gemini-key-card removed from #tab-tts
    "btn-tts-key-delete", "btn-tts-key-save", "tts-balance-line",
    "tts-byok-advanced-body", "tts-byok-advanced-toggle", "tts-gemini-key-card",
    "tts-key-active-badge", "tts-key-guide-body", "tts-key-guide-toggle",
    "tts-key-input", "tts-key-state",
    # Round 8.1: #tts-edge-voice-hint removed from #tab-tts
    "tts-edge-voice-hint",
    # Round 10 B3–B7: retired Recap Step 2 controls & strips
    "btn-canvas-play-toggle", "btn-reset-canvas-layout", "btn-save-my-preset",
    "btn-seek-next-5", "btn-seek-prev-5", "btn-sync-crop", "btn-toggle-crop-detail",
    "btn-toggle-mirror", "canvas-seek-time-badge", "crop-detail-chevron",
    "input-my-preset-name", "recap-cat-preset", "recap-crop-detail",
    "recap-crop-presets-toolbar", "recap-fill-note", "recap-fill-picker",
    "recap-master-presets-grid", "recap-my-presets-list", "recap-my-presets-section",
    "recap-my-presets-vip-badge", "recap-preview-transform-group", "recap-protection-picker",
    "recap-step2-summary", "recap-zoom-picker", "slider-canvas-seek",
    "slider-crop-bottom", "slider-crop-left", "slider-crop-right", "slider-crop-top",
    "val-crop-bottom", "val-crop-left", "val-crop-right", "val-crop-top",
    # Round 11: retired 12-card subtitle style grid (dropdown-only selector)
    "sub-style-cards-grid",
    # Round 12 (B3, C3, D2, D6): custom-font inline row, voice billing badge,
    # secondary Step-4 fact chips, and split-video UI removed.
    "btn-sub-font-clear", "btn-sub-font-upload",
    "recap-fact-blur", "recap-fact-mirror", "recap-fact-quality",
    "recap-fact-speed", "recap-fact-subs", "recap-fact-title",
    "recap-split-cost-hint", "recap-split-minutes-picker",
    "recap-split-minutes-wrap", "recap-split-note", "recap-split-toggle",
    "recap-sub-font-custom-row", "recap-voice-billing-badge",
    "sub-font-custom-status",
    # Post-combined follow-up (2026-11-23 Parts C, E, G)
    "recap-unverified-warning",
    "adm-ai-thumbnail-card", "adm-set-thumb-cap", "adm-set-thumb-model",
    "adm-set-thumb-prompt", "adm-thumb-status-badge", "adm-thumb-usage-box",
    "adm-thumb-usage-stats", "adm-thumb-usage-title", "btn-recap-ai-thumbnail",
    "btn-recap-dl-thumbnail", "btn-save-ai-thumbnail-settings", "recap-ai-thumbnail-box",
    "recap-thumb-headline-input", "recap-thumb-preview-img", "recap-thumb-status-note",
    "adm-set-daily-job-limit", "adm-set-group-ai", "adm-set-tiered-pricing",
    "adm-set-tutorial-video-enabled", "adm-set-voice-daily-free",
    "adm-set-voice-daily-paid", "adm-tut-recap-title",
}
_RETIRED_VIP_STATIC_ONCLICKS = {
    "applyRecapMasterPreset('fb_story')",
    "applyRecapMasterPreset('strict_safe')",
    "applyRecapMasterPreset('tiktok')",
    "applyRecapMasterPreset('yt_16_9')",
    "saveCurrentAsMyRecapPreset()",
    "toggleRecapCropDetail()",
    "selectRecapSubStyleCard('cinematic')",
    "selectRecapSubStyleCard('glass')",
    "selectRecapSubStyleCard('yellow')",
    "selectRecapSubStyleCard('white')",
    "selectRecapSubStyleCard('boxed')",
    "selectRecapSubStyleCard('neon')",
    "selectRecapSubStyleCard('band')",
    "selectRecapSubStyleCard('heavy')",
    "selectRecapSubStyleCard('soft')",
    "selectRecapSubStyleCard('rose')",
    "selectRecapSubStyleCard('orange')",
    "selectRecapSubStyleCard('mint')",
    "copyAccount('09790760003')",
    "openPaymentDrawer('MIN_10', 8000, '🎙️ Voice Pack 10 Minutes')",
    "openPaymentDrawer('MIN_30', 20000, '🎙️ Voice Pack 30 Minutes')",
    "openPaymentDrawer('MIN_60', 38000, '🎙️ Voice Pack 60 Minutes')",
    "openPaymentDrawer('all_in_one_master', 180000, '👑 Master')",
    "openPaymentDrawer('combined_starter', 30000, '🎙️🎬 Combined Starter')",
    "openPaymentDrawer('recap_starter', 20000, '🎬 Recap Starter')",
    "openPaymentDrawer('recap_studio_pro', 90000, '🎬 Recap Pro')",
    "openPaymentDrawer('voice_studio_pro', 75000, '🎙️ Voice Pro')",
    "switchPlanCategory('combined')",
    "toggleTtsByokAdvanced()",
    # Post-combined follow-up (2026-11-23 Parts D, E, G)
    "previewSelectedGeminiVoice()",
    "generateRecapAiThumbnail()",
    "saveAiThumbnailSettings()",
    "filterAdminPricing('topup')",
}


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
    assert not ((main_ids - curr_ids) - _RETIRED_VIP_STATIC_IDS), sorted((main_ids - curr_ids) - _RETIRED_VIP_STATIC_IDS)
    main_on = set(re.findall(r'\bonclick="([^"]+)"', main_html))
    curr_on = set(re.findall(r'\bonclick="([^"]+)"', INDEX_HTML))
    assert not ((main_on - curr_on) - _RETIRED_VIP_STATIC_ONCLICKS), sorted((main_on - curr_on) - _RETIRED_VIP_STATIC_ONCLICKS)
