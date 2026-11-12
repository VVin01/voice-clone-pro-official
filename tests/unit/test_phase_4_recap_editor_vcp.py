"""Phase 4 — Recap Step 2 Split-Screen 5-Tab Editor, Master Presets, VIP My Presets,
10 Visual Subtitle Sample Cards, L1/L2 Progressive Disclosure & Downloader Handoff.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")


def test_phase4_sticky_preview_corner_reset_and_five_category_tabs():
    assert 'id="recap-step2-sticky-preview"' in INDEX_HTML
    assert 'id="btn-preview-corner-reset"' in INDEX_HTML
    assert 'onclick="resetRecapCanvasLayout()"' in INDEX_HTML
    assert ".vcp-step2-sticky-preview" in STYLE_CSS
    assert "position: sticky;" in STYLE_CSS

    assert 'id="recap-step2-cat-tabs"' in INDEX_HTML
    for cat in ("preset", "frame", "subtitle", "blur", "protection"):
        assert f'data-step2-cat="{cat}"' in INDEX_HTML
        assert f"switchRecapStep2Category('{cat}')" in INDEX_HTML
    assert "function switchRecapStep2Category(" in APP_JS
    assert "function applyActiveRecapStep2Category(" in APP_JS


def test_phase4_master_presets_preserve_defaults_and_ui():
    assert 'id="recap-cat-preset"' in INDEX_HTML
    assert 'id="recap-master-presets-grid"' in INDEX_HTML
    for key in ("tiktok", "fb_story", "yt_16_9", "strict_safe"):
        assert f'data-master-preset="{key}"' in INDEX_HTML
        assert f"applyRecapMasterPreset('{key}')" in INDEX_HTML

    assert "const RECAP_MASTER_PRESETS =" in APP_JS
    assert "function applyRecapMasterPreset(" in APP_JS
    assert "function syncRecapMasterPresetActive(" in APP_JS
    # Shipped defaults remain unchanged for primary tiktok preset and state init
    assert 'recapRatio: "9:16"' in APP_JS
    assert 'recapRatioFill: "blur"' in APP_JS
    assert 'recapSubStyle: "yellow"' in APP_JS
    assert "recapBlurAutoDetect: true" in APP_JS


def test_phase4_my_presets_ui_and_backend_recap_presets_sanitization():
    for marker in (
        'id="recap-my-presets-section"',
        'id="recap-my-presets-vip-badge"',
        'id="input-my-preset-name"',
        'id="btn-save-my-preset"',
        'id="recap-my-presets-list"',
        'onclick="saveCurrentAsMyRecapPreset()"',
    ):
        assert marker in INDEX_HTML

    for fn in (
        "function parseMyRecapPresets(",
        "function serializeMyRecapPresets(",
        "function renderMyRecapPresets(",
        "function saveCurrentAsMyRecapPreset(",
        "function applyMyRecapPreset(",
        "function deleteMyRecapPreset(",
    ):
        assert fn in APP_JS

    # Approved Phase 4 backend extension in USER_PREF_STRINGS & _sanitize_user_prefs
    assert api_mod.USER_PREF_STRINGS.get("recap_presets") == 4000
    assert api_mod.DEFAULT_USER_PREFS.get("recap_presets") == ""

    sample_list = [
        {"name": f"Preset {i}", "settings": {"recapRatio": "9:16", "recapSubStyle": "yellow"}}
        for i in range(1, 5)
    ]
    cleaned = api_mod._sanitize_user_prefs({"recap_presets": sample_list})
    assert "recap_presets" in cleaned
    parsed = json.loads(cleaned["recap_presets"])
    assert len(parsed) == 3  # capped at max 3 presets
    assert [p["name"] for p in parsed] == ["Preset 1", "Preset 2", "Preset 3"]

    cleaned_empty = api_mod._sanitize_user_prefs({"recap_presets": "not json"})
    assert cleaned_empty["recap_presets"] == "not json"[:4000]


def test_phase4_all_ten_subtitle_visual_sample_cards_and_sync():
    assert 'id="sub-style-cards-grid"' in INDEX_HTML
    styles = ("yellow", "white", "boxed", "neon", "band", "heavy", "soft", "rose", "orange", "mint")
    for style in styles:
        assert f'data-style-card="{style}"' in INDEX_HTML
        assert f"selectRecapSubStyleCard('{style}')" in INDEX_HTML
        assert f"sample-{style}" in INDEX_HTML
        assert f".vcp-sub-sample.sample-{style}" in STYLE_CSS

    assert "function syncRecapSubStyleCards(" in APP_JS
    assert "function selectRecapSubStyleCard(" in APP_JS


def test_phase4_crop_bounds_l2_accordions_and_logo_vip_upsell():
    for cid in ("slider-crop-top", "slider-crop-bottom", "slider-crop-left", "slider-crop-right"):
        m = re.search(rf'<[^>]*id="{cid}"[^>]*>', INDEX_HTML)
        assert m, f"Missing crop slider {cid}"
        assert 'max="35"' in m.group(0), f"{cid} should keep max=35"

    for marker in (
        'id="recap-sub-l2-accordion"',
        'id="btn-toggle-sub-l2-detail"',
        'id="recap-sub-l2-body"',
        'id="recap-blur-l2-accordion"',
        'id="btn-toggle-blur-l2-detail"',
        'id="recap-blur-l2-body"',
        'id="recap-logo-vip-badge"',
        'id="recap-logo-vip-hint"',
        'id="btn-logo-vip-upsell"',
    ):
        assert marker in INDEX_HTML

    assert "function toggleRecapSubL2Detail(" in APP_JS
    assert "function toggleRecapBlurL2Detail(" in APP_JS


def test_phase4_downloader_to_recap_step1_handoff():
    assert "extractData: data" in APP_JS
    assert "state.recapExtractHandoff =" in APP_JS
    assert "const handoff = state.recapExtractHandoff;" in APP_JS


def test_phase4_i18n_keys_complete_in_my_and_en():
    required_keys = [
        "btn_reset_layout_short",
        "recap_cat_preset",
        "recap_cat_frame",
        "recap_cat_subtitle",
        "recap_cat_blur",
        "recap_cat_protection",
        "recap_master_presets_title",
        "preset_tiktok_name",
        "preset_tiktok_desc",
        "preset_fb_story_name",
        "preset_fb_story_desc",
        "preset_yt_16_9_name",
        "preset_yt_16_9_desc",
        "preset_strict_safe_name",
        "preset_strict_safe_desc",
        "recap_master_preset_applied_toast",
        "recap_my_presets_title",
        "recap_my_preset_ph",
        "btn_save_my_preset",
        "recap_my_preset_empty",
        "recap_my_preset_vip_only",
        "recap_my_preset_max_toast",
        "recap_my_preset_too_large",
        "recap_my_preset_default_name",
        "recap_my_preset_apply",
        "recap_my_preset_delete",
        "recap_my_preset_saved_toast",
        "recap_my_preset_applied_toast",
        "recap_my_preset_deleted_toast",
        "recap_sub_l2_title",
        "recap_blur_l2_title",
        "btn_logo_vip_upsell",
        "dl_use_in_recap_ready_toast",
    ]
    for key in required_keys:
        matches = re.findall(rf"\b{re.escape(key)}\s*:", I18N_JS)
        assert len(matches) == 2, f"i18n key {key} expected in both my & en, found {len(matches)}"
