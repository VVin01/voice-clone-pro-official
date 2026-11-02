"""Pins for the Recap settings persistence fix (overnight hands-on, 2026-11-02).

Live-verified defect: every Recap setting lived only in the JS `state`
object, so reopening the Mini App reset ratio/fill/subtitle style/voice/
engine/split to defaults (ratio 16:9 + fill crop reverted to 9:16 + blur
after a single reload). The fix persists the settings slice to
localStorage under vcp_recap_settings and restores it (validated) at the
end of setupRecapVisualEditor().
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


def test_persistence_block_present():
    assert 'const RECAP_SETTINGS_KEY = "vcp_recap_settings";' in APP_JS
    assert "function snapshotRecapSettings()" in APP_JS
    assert "function saveRecapSettings()" in APP_JS
    assert "function restoreRecapSettings()" in APP_JS


def test_save_uses_localstorage_same_pattern():
    assert 'localStorage.setItem(RECAP_SETTINGS_KEY, JSON.stringify(snapshotRecapSettings()))' in APP_JS
    assert 'JSON.parse(localStorage.getItem(RECAP_SETTINGS_KEY) || "null")' in APP_JS


def test_restore_is_validated_not_blind():
    # enum allow-lists, boolean type checks and ranged ints gate the restore
    assert "RECAP_SETTING_ENUMS" in APP_JS
    assert 'recapRatio: ["9:16", "16:9", "1:1", "Original"]' in APP_JS
    assert 'recapTtsEngine: ["voxcpm", "gemini"]' in APP_JS
    # all seven server-side SUBTITLE_STYLE_PRESETS keys must restore
    assert 'recapSubStyle: ["yellow", "white", "boxed", "neon", "band", "heavy", "soft"]' in APP_JS
    assert "if (allowed.includes(saved[k])) state[k] = saved[k];" in APP_JS
    assert 'typeof saved[k] === "boolean"' in APP_JS
    assert "recapCropTop: [0, 35]" in APP_JS
    assert "recapSplitMinutes: [1, 10]" in APP_JS


def test_restore_called_inside_setup_after_handlers():
    setup_at = APP_JS.index("function setupRecapVisualEditor()")
    restore_call = APP_JS.index("restoreRecapSettings();", setup_at)
    # the delegated save listeners are registered in the same block
    save_wire = APP_JS.index("saveRecapSettingsSoon", setup_at)
    assert setup_at < save_wire < restore_call
    # restore happens before the initial canvas sync at the end of setup
    initial_sync = APP_JS.index("// Initial canvas mockup sync", setup_at)
    assert restore_call < initial_sync


def test_save_wired_to_recap_tab_events():
    assert 'document.getElementById("tab-recap")' in APP_JS
    assert '["click", "change", "input", "pointerup"]' in APP_JS


def test_restore_resyncs_ui_controls():
    for needle in [
        'syncPillGroup("#recap-ratio-picker .ratio-btn", "data-ratio", state.recapRatio)',
        'syncPillGroup("#recap-fill-picker .pill-btn", "data-fill", state.recapRatioFill)',
        'syncPillGroup("#recap-engine-picker .pill-btn", "data-engine", state.recapTtsEngine)',
        'syncPillGroup("#sub-style-picker .style-chip", "data-style", state.recapSubStyle)',
        "applyCropValues(state.recapCropTop || 0",
        "splitToggleEl.checked = !!saved.recapSplitEnabled",
        "syncSubtitleFontSize(state.recapFontSize || 16)",
        "syncSubtitleLines(state.recapLines || 1)",
    ]:
        assert needle in APP_JS, needle


def test_snapshot_covers_payload_fields():
    block = APP_JS[APP_JS.index('const RECAP_SETTINGS_KEY'):APP_JS.index("recapTabEl.addEventListener")]
    snap = block
    for field in ["recapRatio", "recapRatioFill", "recapMirror", "recapZoom",
                  "recapSpeed", "recapStoryStyle", "recapBurnSubs", "recapBlurSubs",
                  "recapLogo", "recapLogoPos", "recapSubStyle", "recapSubFont",
                  "recapFontSize", "recapLines", "recapSplitMinutes",
                  "recapSourceLanguage", "recapRenderQuality", "recapTtsEngine",
                  "recapGeminiVoice", "recapVoice", "recapSplitEnabled",
                  "recapBlurY", "recapBlurH", "recapSubY"]:
        assert field in snap, field


def test_statics_token_bumped():
    assert INDEX_HTML.count("?v=20261102_tb1") == 3
    assert "?v=20261102_rs1" not in INDEX_HTML
