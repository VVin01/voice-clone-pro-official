"""Phase 2 — Home Dashboard, Thin Status Strip, Quick Create (Voice/Recap/TTS),
Free Allowances with Period Badges, Recent Projects (24h Retention) & Unified Help Hub.
"""
from __future__ import annotations

from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")

HOME = INDEX_HTML[INDEX_HTML.index('id="tab-home"'):INDEX_HTML.index('id="tab-voice"')]


def test_phase2_thin_status_strip_taps_to_profile():
    # Round 13 wired #home-status-strip to openProfileView() (Profile houses tier, key & refer).
    assert 'id="home-status-strip"' in HOME
    assert 'id="home-greeting-text"' in HOME
    assert 'id="home-tier-badge"' in HOME
    assert 'id="home-bal-summary"' in HOME
    strip_slice = HOME.split('id="home-status-strip"', 1)[1].split('id="home-welcome-strip"', 1)[0]
    assert "openProfileView()" in strip_slice
    assert "function paintHomeStatusStrip(data)" in APP_JS
    assert "paintHomeStatusStrip(data);" in APP_JS


def test_phase2_welcome_strip_plays_pregenerated_sample_without_api_generation():
    # Round 13 decoupled welcome sample playback from retired #home-samples-card using _welcomeTryAudio.
    strip_slice = HOME.split('id="home-welcome-strip"', 1)[1].split('id="home-start-card"', 1)[0]
    assert 'id="welcome-try-sample-btn"' in strip_slice
    assert 'onclick="playWelcomeTrySample()"' in strip_slice
    body = APP_JS.split("function playWelcomeTrySample()", 1)[1].split("window.playWelcomeTrySample", 1)[0]
    assert "_welcomeTryAudio" in body
    assert "/api/voices/synthesize" not in body
    assert "/api/tts/generate" not in body


def test_phase2_quick_create_has_all_three_studios():
    start_slice = HOME.split('id="home-start-card"', 1)[1].split('id="home-tools-card"', 1)[0]
    assert "onclick=\"switchTab('tab-voice')\"" in start_slice
    assert "onclick=\"switchTab('tab-recap')\"" in start_slice
    assert "onclick=\"switchTab('tab-tts')\"" in start_slice
    assert 'id="home-start-tts-tile"' in start_slice


def test_phase2_free_today_card_has_unit_and_reset_period_badges():
    free_slice = HOME.split('id="home-free-card"', 1)[1].split('id="home-welcome-strip"', 1)[0]
    for period_key in ("home_free_tts_period", "home_free_voice_period", "home_free_recap_period"):
        assert f'data-i18n="{period_key}"' in free_slice, period_key


def test_phase2_retention_badge_helper_preserved():
    # Round 13 retired #home-recent-projects-card from Home (History Hub owns recent jobs);
    # 24h retention badge helper stays live for History Hub.
    assert 'id="home-recent-projects-card"' not in HOME
    assert "function computeRetentionBadgeHtml(j)" in APP_JS


def test_phase2_single_help_entry_point_controls_tutorials():
    assert 'id="home-help-entry-card"' in HOME
    assert 'id="btn-home-help-hub"' in HOME
    assert 'onclick="toggleVcpHelpHub()"' in HOME
    assert "function toggleVcpHelpHub(" in APP_JS
    assert "#tab-home:not(.vcp-help-hub-open) #home-tutorials-card" in STYLE_CSS


def test_phase2_i18n_keys_present_in_both_locales():
    keys = [
        "home_greeting_default",
        "home_greeting_named",
        "welcome_try_sample_btn",
        "welcome_try_sample_playing",
        "home_start_tts_name",
        "home_start_tts_desc",
        "home_free_tts_period",
        "home_free_voice_period",
        "home_free_recap_period",
        "home_recent_time_left_hours",
        "home_recent_action_dl",
        "home_help_entry_title",
        "home_help_entry_desc",
    ]
    for key in keys:
        assert I18N_JS.count(f"{key}:") == 2, f"Missing or duplicate i18n key: {key}"


def test_phase2_paint_home_status_strip_uses_state_tg_without_reference_error():
    """Regression (Phase 8 QA): paintHomeStatusStrip must read state.tg, not
    an undeclared bare `tg` identifier (which threw ReferenceError inside
    loadUserProfile() at runtime).
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    fn_src = "function paintHomeStatusStrip(data)" + APP_JS.split(
        "function paintHomeStatusStrip(data)", 1
    )[1].split("window.paintHomeStatusStrip", 1)[0]
    script = (
        f"const state = {{ tg: {{ initDataUnsafe: {{ user: {{ first_name: 'Su' }} }} }} }};\n"
        f"const els = {{ 'home-greeting-text': {{}}, 'home-tier-badge': {{}}, 'home-bal-summary': {{}} }};\n"
        f"const document = {{ getElementById: (id) => els[id] || null }};\n"
        f"const t = (k) => k;\n"
        f"const tf = (k, p) => k + ':' + (p && p.name || '');\n"
        f"{fn_src}\n"
        f"paintHomeStatusStrip({{ balance_minutes: 3, recap_credits: 3, is_vip: false, is_admin: false }});\n"
        f"if (els['home-greeting-text'].textContent !== 'home_greeting_named:Su') throw new Error('greet fail');\n"
        f"console.log('OK');\n"
    )
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert res.stdout.strip() == "OK"

