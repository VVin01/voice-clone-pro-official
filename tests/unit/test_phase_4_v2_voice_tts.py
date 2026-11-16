from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")

PHASE4_MARKER = "VCP UI REBUILD V2 — PHASE 4: VOICE & TTS STUDIOS (LIGHT / DARK / LITE)"
PHASE4_CSS = STYLE_CSS.split(PHASE4_MARKER, 1)[1]
# If Phase 5 is appended later, scope PHASE4_CSS cleanly to the Phase 4 block.
if "VCP UI REBUILD V2 — PHASE 5:" in PHASE4_CSS:
    PHASE4_CSS = PHASE4_CSS.split("VCP UI REBUILD V2 — PHASE 5:", 1)[0]


def test_v2_phase4_cache_token_current():
    assert INDEX_HTML.count("?v=20261117_r159") == 4
    assert "20261114_r146" not in INDEX_HTML


def test_v2_phase4_zero_backdrop_filter_in_scrolling_surfaces():
    assert "backdrop-filter" not in PHASE4_CSS
    assert "-webkit-backdrop-filter" not in PHASE4_CSS


def test_v2_phase4_voice_studio_cobalt_and_custom_clone_rules():
    required_selectors = (
        "#tab-voice #personas-grid > [data-voice-id]",
        "#tab-voice #personas-grid > [data-voice-id].active",
        "#tab-voice #personas-grid > [data-voice-id] .persona-avatar",
        "#tab-voice #personas-grid > [data-voice-id] .persona-badge",
        "#tab-voice #personas-grid > [data-voice-id] .persona-badge:empty",
        "#tab-voice #personas-grid > [data-voice-id] .selected-indicator",
        "#tab-voice #personas-grid > [data-voice-id] .persona-footer-row",
        "#tab-voice #personas-grid > [data-voice-id] .voice-preview-btn",
        "#tab-voice .vcp-clone-steps-bar",
        "#tab-voice .vcp-clone-step",
        "#tab-voice .vcp-clone-step.is-active",
        "#tab-voice .vcp-clone-step.is-done",
        "#tab-voice .vcp-clone-req-box",
        "#tab-voice #speed-selector.speed-capsule",
        "#tab-voice .vcp-excess-banner",
    )
    for sel in required_selectors:
        assert sel in PHASE4_CSS, f"Missing Phase 4 Voice selector: {sel}"


def test_v2_phase4_tts_studio_teal_and_byok_rules():
    required_selectors = (
        "#tab-tts .vcp-excess-banner",
        "#tab-tts #tts-speed-picker.speed-capsule",
        "#tab-tts .tts-byok-advanced-head",
        "#tab-tts .tts-byok-advanced-head .card-label",
        "#tab-tts .tts-byok-advanced-body",
    )
    for sel in required_selectors:
        assert sel in PHASE4_CSS, f"Missing Phase 4 TTS selector: {sel}"


def test_v2_phase4_dark_glass_and_lite_ceramic_coverage():
    required_theme_selectors = (
        'html[data-theme="dark"] .sticky-cta-bar',
        'html[data-theme="dark"] #tab-voice #personas-grid > [data-voice-id]',
        'html[data-theme="dark"] #tab-voice #personas-grid > [data-voice-id].active',
        'html[data-theme="dark"] #tab-voice .vcp-clone-step',
        'html[data-theme="dark"] #tab-voice .vcp-clone-step.is-active',
        'html[data-theme="dark"] #tab-voice .vcp-clone-req-box',
        'html[data-theme="dark"] #tab-voice #speed-selector.speed-capsule',
        'html[data-theme="dark"] #tab-tts #tts-speed-picker.speed-capsule',
        "html.lite #tab-voice #personas-grid > [data-voice-id]",
        "html.lite #tab-voice .vcp-clone-req-box",
        'html[data-theme="dark"].lite #tab-voice #personas-grid > [data-voice-id]',
        'html[data-theme="dark"].lite #tab-voice .vcp-clone-req-box',
    )
    for sel in required_theme_selectors:
        assert sel in PHASE4_CSS, f"Missing Phase 4 Dark/Lite selector: {sel}"


def test_v2_phase4_voice_and_tts_dom_ids_and_handlers_preserved():
    voice_required = (
        'id="tab-voice"',
        'id="btn-voice-excess-trim"',
        'onclick="trimVoiceTextToLimit()"',
        'id="speed-selector"',
        'id="voice-preflight"',
        'id="btn-generate-voice"',
        'id="voice-progress-box"',
        'id="voice-result-box"',
        'id="btn-voice-play-toggle"',
        'onclick="toggleResultAudioPlay()"',
        'id="btn-download-audio"',
        'id="btn-voice-send-telegram"',
        'onclick="sendVoiceToTelegram()"',
        'id="btn-save-custom-voice-from-result"',
        'onclick="saveCurrentRefAsCustomVoice()"',
        'id="sticky-cta-voice"',
    )
    tts_required = (
        'id="tab-tts"',
        'id="tts-engine-picker"',
        'id="tts-gemini-voice-select"',
        'id="tts-edge-voice-select"',
        'id="tts-byok-advanced-toggle"',
        'onclick="toggleTtsByokAdvanced()"',
        'id="tts-key-active-badge"',
        'id="tts-key-input"',
        'id="btn-tts-key-save"',
        'id="btn-tts-key-delete"',
        'id="tts-text-input"',
        'id="tts-char-excess-banner"',
        'id="btn-tts-excess-trim"',
        'onclick="trimTtsTextToLimit()"',
        'id="tts-speed-picker"',
        'id="btn-generate-tts"',
        'onclick="generateTTS()"',
        'id="tts-progress-box"',
        'id="tts-result-box"',
        'id="btn-download-tts"',
        'id="btn-tts-send-telegram"',
        'onclick="sendTtsToTelegram()"',
    )
    for needle in voice_required + tts_required:
        assert needle in INDEX_HTML, f"Missing preserved DOM/handler marker: {needle}"
