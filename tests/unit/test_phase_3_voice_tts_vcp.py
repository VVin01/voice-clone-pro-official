"""Phase 3 — Voice & TTS Studios: Text-First Flow (Addendum A #1), Non-Destructive
Character Excess Handling, Custom Clone 5-Step Guided Flow, BYOK Advanced Accordion
with Active Key Badge & Live Estimated Cost Badges.
"""
from __future__ import annotations

from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")


def test_phase3_text_first_visual_order_in_css():
    assert "#tab-voice > #voice-input-card { order: 3; }" in STYLE_CSS
    assert "#tab-voice > #voxcpm-mode-section { order: 4; }" in STYLE_CSS
    assert "#tab-voice > #voice-cta-block { order: 5; }" in STYLE_CSS
    assert "#tab-tts > #tts-text-card { order: 2; }" in STYLE_CSS
    assert "#tab-tts > .input-card:not(#tts-text-card) { order: 3; }" in STYLE_CSS
    assert "#tab-tts > #tts-cta-block { order: 4; }" in STYLE_CSS


def test_phase3_non_destructive_character_excess_banners_and_actions():
    assert 'id="voice-char-excess-banner"' in INDEX_HTML
    assert 'id="btn-voice-excess-trim"' in INDEX_HTML
    assert 'id="btn-voice-excess-vip"' in INDEX_HTML
    assert 'onclick="trimVoiceTextToLimit()"' in INDEX_HTML
    assert 'id="tts-char-excess-banner"' in INDEX_HTML
    assert 'id="btn-tts-excess-trim"' in INDEX_HTML
    assert 'id="btn-tts-excess-vip"' in INDEX_HTML
    assert 'onclick="trimTtsTextToLimit()"' in INDEX_HTML
    assert "function syncVoiceExcessAndCost(" in APP_JS
    assert "function trimVoiceTextToLimit(" in APP_JS
    assert "function syncTtsExcessAndCost(" in APP_JS
    assert "function trimTtsTextToLimit(" in APP_JS


def test_phase3_custom_clone_file_requirements_and_save_flow():
    # Round 13 retired #custom-clone-stepper & #custom-clone-req-box; check status & save-from-result stay live.
    assert 'id="custom-clone-check-status"' in INDEX_HTML
    assert 'id="btn-save-custom-voice-from-result"' in INDEX_HTML
    assert "function saveCurrentRefAsCustomVoice(" in APP_JS


def test_phase3_tts_byok_moved_to_profile_in_round8():
    # Round 8 B7: Gemini Key BYOK card lives in Profile only (removed from TTS tab)
    assert 'id="tts-gemini-key-card"' not in INDEX_HTML
    assert 'id="tts-byok-advanced-toggle"' not in INDEX_HTML
    assert 'id="profile-gemini-key-card"' in INDEX_HTML
    assert 'id="profile-key-active-badge"' in INDEX_HTML
    assert '"profile-key-active-badge"' in APP_JS


def test_phase3_generate_buttons_show_estimated_minutes():
    assert 'id="voice-btn-est-cost"' in INDEX_HTML
    assert 'id="tts-btn-est-cost"' in INDEX_HTML
    assert "function estimateSpeechMinutesFromText(" in APP_JS


def test_phase3_i18n_keys_present_in_both_locales():
    keys = [
        "clone_req_duration",
        "clone_req_format",
        "clone_req_clean",
        "clone_check_ok",
        "clone_step5_save_btn",
        "char_excess_warning",
        "char_excess_use_free_part",
        "char_excess_upgrade_vip",
        "est_cost_min_badge",
        "tts_key_active_badge",
    ]
    for key in keys:
        assert I18N_JS.count(f"{key}:") == 2, f"Missing or duplicate i18n key: {key}"
