"""Premium polish pass (branch feat-premium-polish-2026-10-31).

Static/behaviour pins for:
- A1 staged progress steppers wired to real job states;
- A3 haptics routed to the correct Telegram APIs, feature-detected;
- A4 samples card (komin voice + mahnin recap narration, app routes);
- A5 fetchWithTimeout on the data loaders + Home error/retry strip;
- A6 sticky create CTA bars mirroring the real buttons;
- P8 i18n keys for persona filter chips / voices expander;
- P10 slip upload styled label over the same file input;
- P12 dedicated Danger-Zone ban reason field feeding the ban payload;
- T1–T4 tutorial registry (msgs 28–38), dead-code removal, and no stale
  /16 fallback anywhere;
- Home-tab restructure (owner, 2026-10-31): the samples card, the full
  tutorials list and the start-creating rows live on a dedicated first
  Home tab; the studios keep only their pre-polish tutorial entries
  (the T3 per-screen banners and the T2 card were removed again).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
SETTINGS_PY = (ROOT / "app/config/settings.py").read_text(encoding="utf-8")


def _locale_block(lang: str) -> str:
    m = re.search(rf"\n    {lang}: \{{(.*?)\n    \}}[,;]?\n", I18N_JS, re.S)
    assert m, f"locale block {lang} not found"
    return m.group(1)


def _keys(lang: str) -> set[str]:
    return set(re.findall(r"^\s{8}(\w+):", _locale_block(lang), re.M))


# ── A1: staged steppers ────────────────────────────────────────────────

def test_stage_steppers_present_and_wired():
    assert 'id="voice-stage-stepper"' in INDEX_HTML
    assert 'id="recap-stage-stepper"' in INDEX_HTML
    assert "function setVoiceStage" in APP_JS
    assert "function setRecapStage" in APP_JS
    # voice polling maps QUEUED/RUNNING/SUCCESS to stages
    assert 'setVoiceStage("working")' in APP_JS
    assert 'setVoiceStage("success")' in APP_JS
    # recap stepper is driven by the server's real progress_step
    assert "setRecapStage(data.progress_step, data.status)" in APP_JS


# ── A3: haptics routing ────────────────────────────────────────────────

def test_haptic_routes_notification_and_selection():
    assert "notificationOccurred" in APP_JS
    assert "selectionChanged" in APP_JS
    assert "impactOccurred" in APP_JS
    # feature-detected: guarded access to the HapticFeedback object
    assert "state.tg?.HapticFeedback" in APP_JS


def _pane(html: str, pane_id: str) -> str:
    start = html.index(f'id="{pane_id}"')
    nxt = html.find('<section id="tab-', start + 1)
    return html[start:] if nxt == -1 else html[start:nxt]


# ── A4: Home samples ───────────────────────────────────────────────────

def test_home_samples_use_komin_and_mahnin_routes():
    assert 'id="home-samples-card"' in INDEX_HTML
    # Hunt #2 V2: /api/voices/sample now requires the Telegram auth
    # header, which an <audio src> load cannot send — the players carry
    # the route in data-sample-url and setupSamplePlayers() fetches it
    # with auth (see test_hunt2_small_criticals_2026_10_31.py).
    assert 'data-sample-url="/api/voices/sample/komin"' in INDEX_HTML
    assert 'data-sample-url="/api/voices/sample/mahnin"' in INDEX_HTML
    # kozayar must never be a demo sample (owner verdict 2026-10-31)
    assert "sample/kozayar" not in INDEX_HTML
    # Placement (owner restructure 2026-10-31): the samples card lives
    # on the Home tab; the Voice studio pane stays uncluttered.
    assert 'id="home-samples-card"' in _pane(INDEX_HTML, "tab-home")
    assert 'id="home-samples-card"' not in _pane(INDEX_HTML, "tab-voice")


# ── A5: timeouts + error states ────────────────────────────────────────

def test_fetch_with_timeout_used_by_loaders():
    assert "async function fetchWithTimeout" in APP_JS
    assert "AbortController" in APP_JS
    for needle in [
        'fetchWithTimeout("/api/user/me"',
        'fetchWithTimeout("/api/voices/curated"',
        'fetchWithTimeout("/api/history"',
        'fetchWithTimeout("/api/vip/plans"',
        'fetchWithTimeout("/api/admin/overview"',
        'fetchWithTimeout("/api/admin/pricing"',
        'fetchWithTimeout("/api/admin/audit-log?limit=50"',
    ]:
        assert needle in APP_JS, needle


def test_home_load_error_strip_with_retry():
    assert 'id="home-load-error"' in INDEX_HTML
    assert "retryHomeLoad()" in INDEX_HTML
    assert "function retryHomeLoad" in APP_JS
    assert "setHomeLoadErrorVisible(true)" in APP_JS


# ── A6: sticky CTAs ────────────────────────────────────────────────────

def test_sticky_cta_bars_mirror_real_buttons():
    assert 'id="sticky-cta-voice"' in INDEX_HTML
    assert 'id="sticky-cta-recap"' in INDEX_HTML
    assert 'id="btn-generate-voice-sticky"' in INDEX_HTML
    assert 'id="btn-start-recap-sticky"' in INDEX_HTML
    assert "IntersectionObserver" in APP_JS
    assert "setupStickyCtas" in APP_JS
    # clearance above the floating nav is encoded in CSS
    assert ".sticky-cta-bar" in STYLE_CSS
    assert "64px + 10px + 12px" in STYLE_CSS


# ── P8/P9/P10/P12 ──────────────────────────────────────────────────────

def test_p8_filter_chips_and_expander_i18n():
    for key in ("persona_filter_all", "persona_filter_male", "persona_filter_female",
                "voices_show_all", "voices_hide_all"):
        assert f'data-i18n="{key}"' in INDEX_HTML or key in APP_JS
    assert 'data-i18n="persona_filter_all"' in INDEX_HTML
    assert 'data-i18n="voices_show_all"' in INDEX_HTML
    assert 't("voices_hide_all")' in APP_JS


def test_p9_ratio_labels_have_no_embedded_pin_emoji():
    for key in ("ratio_9_16", "ratio_16_9", "ratio_1_1", "ratio_original"):
        for lang in ("my", "en"):
            m = re.search(rf"{key}: \"([^\"]*)\"", _locale_block(lang))
            assert m and "📐" not in m.group(1), (key, lang)


def test_p10_slip_upload_styled_label_same_input():
    assert 'for="slip-file-input"' in INDEX_HTML
    assert 'id="slip-file-input"' in INDEX_HTML
    assert 'id="slip-file-name"' in INDEX_HTML
    assert 'class="hidden-file-input"' in INDEX_HTML


def test_p12_dedicated_ban_reason_field():
    assert 'id="adm-ban-reason"' in INDEX_HTML
    assert 'data-i18n-ph="admin_ban_reason_ph"' in INDEX_HTML
    assert "function _adminBanReason" in APP_JS
    assert "_adminRequireBanReason()" in APP_JS
    # ban call no longer reads the Adjust card's field
    ban_fn = APP_JS.split("async function _adminBanCall")[1].split("\n}")[0]
    assert "adm-adjust-reason" not in ban_fn
    # Adjust flow keeps its own reason field (unchanged behaviour)
    assert 'id="adm-adjust-reason"' in INDEX_HTML


# ── Tutorials T1–T4 ────────────────────────────────────────────────────

def test_t1_registry_links_28_to_38():
    for n in range(28, 39):
        assert f"https://t.me/VoiceClonePro/{n}" in APP_JS, n
    assert "const TUTORIAL_LINKS" in APP_JS
    for key in ("index", "plans", "customvoice", "downloader",
                "scriptedit", "recapadvanced", "refer", "support"):
        assert f"{key}:" in APP_JS.split("const TUTORIAL_LINKS")[1].split("};")[0]


def test_t2_home_tutorials_list_covers_all_ten():
    # The T2 "All Tutorials" card on the Voice tab is superseded by the
    # Home tutorials card (owner restructure): full list + index action.
    assert 'id="btn-all-tutorials"' not in INDEX_HTML
    assert 'id="home-tutorials-card"' in _pane(INDEX_HTML, "tab-home")
    assert "openTutorialVideo('index')" in INDEX_HTML
    for key in ("bot", "voice", "recap", "plans", "customvoice",
                "downloader", "scriptedit", "recapadvanced", "refer",
                "support"):
        assert f"openTutorialVideo('{key}')" in INDEX_HTML, key
    # 10 tutorial rows + 2 start-creating rows share the row pattern
    assert INDEX_HTML.count('class="home-tut-row"') == 12


def test_t3_studio_banners_removed_originals_kept():
    # The polish T3 per-screen banners cluttered the studios (owner
    # verdict) and are gone; each tutorial is reached from Home instead.
    for bid in ("tut-banner-plans", "tut-banner-downloader",
                "tut-banner-customvoice", "tut-banner-scriptedit",
                "tut-banner-recapadvanced", "tut-banner-support"):
        assert f'id="{bid}"' not in INDEX_HTML, bid
    # Pre-polish originals survive untouched: Voice banner + Step-1 btn.
    assert 'id="voice-tutorial-banner"' in INDEX_HTML
    assert 'id="recap-step1-tut-btn"' in INDEX_HTML


def test_t4_no_stale_16_and_dead_code_removed():
    for name, blob in [("app.js", APP_JS), ("api.py", API_PY),
                       ("settings.py", SETTINGS_PY)]:
        assert "VoiceClonePro/16" not in blob, name
    # dead #recap-tutorial-banner management is gone
    assert "recap-tutorial-banner" not in APP_JS
    # Step-1 button has an id, uses configured title + enabled flag
    assert 'id="recap-step1-tut-btn"' in INDEX_HTML
    assert "tutorialDisplayTitle" in APP_JS
    # server defaults point at the current posts
    assert 'default="https://t.me/VoiceClonePro/28"' in SETTINGS_PY
    assert 'default="https://t.me/VoiceClonePro/29"' in SETTINGS_PY
    assert 'default="https://t.me/VoiceClonePro/30"' in SETTINGS_PY


def test_home_tab_is_first_nav_and_landing_pane():
    nav_block = INDEX_HTML.split('<nav class="bottom-nav">')[1].split("</nav>")[0]
    order = re.findall(r'data-tab="(tab-[a-z]+)"', nav_block)
    assert order == ["tab-home", "tab-voice", "tab-recap", "tab-downloader",
                     "tab-vip", "tab-history", "tab-admin"]
    # Home is the landing pane; Voice stays directly selectable.
    assert '<section id="tab-home" class="tab-pane active">' in INDEX_HTML
    assert '<section id="tab-voice" class="tab-pane">' in INDEX_HTML
    assert 'data-i18n="tab_home"' in nav_block
    # start-creating rows jump into the studios via switchTab
    assert 'id="home-start-card"' in _pane(INDEX_HTML, "tab-home")
    assert "switchTab('tab-voice')" in INDEX_HTML
    assert "switchTab('tab-recap')" in INDEX_HTML


def test_new_i18n_keys_exist_in_both_locales():
    new_keys = [
        "stage_script", "stage_voice", "stage_video", "stage_ready",
        "samples_title", "sample_voice_name", "sample_recap_name",
        "home_load_error",
        "tut_all_title", "tut_plans_title", "tut_downloader_title",
        "tut_custom_title", "tut_script_title", "tut_advanced_title",
        "tut_support_title", "persona_filter_all", "persona_filter_male",
        "persona_filter_female", "voices_show_all", "voices_hide_all",
        "slip_file_btn", "slip_file_none", "admin_ban_reason_ph",
        "tab_home", "tutorials_card_title", "tut_bot_title",
        "tut_voice_title", "tut_recap_title", "tut_refer_title",
        "home_start_title", "home_start_voice_name", "home_start_voice_desc",
        "home_start_recap_name", "home_start_recap_desc",
    ]
    my, en = _keys("my"), _keys("en")
    for key in new_keys:
        assert key in my, f"my missing {key}"
        assert key in en, f"en missing {key}"
