"""Phase 2 (UI Rebuild V2 — Home 4-Zone IA & Champagne Pearl VIP) verification suite."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")

HOME = INDEX_HTML.split('id="tab-home"', 1)[1].split('id="tab-voice"', 1)[0]
PHASE2_CSS = STYLE_CSS[STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 2") :]


def test_v2_phase2_cache_token_current():
    assert INDEX_HTML.count("?v=20261118_r164") == 4
    assert "20261114_r146" not in INDEX_HTML


def test_v2_phase2_four_zone_home_ia_order():
    seq = [
        'id="home-hero-card"',
        'id="home-status-strip"',
        'id="home-free-card"',
        'id="home-welcome-strip"',
        'id="home-start-card"',
        'id="home-start-recap-tile"',
        'id="home-recent-projects-card"',
        'id="home-tools-card"',
        'id="home-help-entry-card"',
        'id="home-refer-card"',
    ]
    positions = [HOME.index(marker) for marker in seq]
    assert positions == sorted(positions), "Home 4-Zone IA order mismatch"


def test_v2_phase2_zone_a_hero_balances_and_vip_tier_binding():
    hero = HOME.split('id="home-hero-card"', 1)[1].split('id="home-start-card"', 1)[0]
    assert 'class="vcp-balance-trio" id="home-free-card"' in hero
    assert hero.count('class="vcp-bal-tile"') == 3
    assert 'id="home-free-tts-value"' in hero
    assert 'id="home-free-voice-value"' in hero
    assert 'id="home-free-recap-value"' in hero
    assert 'class="welcome-strip hidden"' in hero
    assert '#tab-home .vcp-hero-card[data-tier="vip"]' in PHASE2_CSS
    assert '#tab-home .vcp-hero-card[data-tier="admin"]' in PHASE2_CSS
    fn = APP_JS.split("function paintHomeStatusStrip(data)", 1)[1].split("window.paintHomeStatusStrip", 1)[0]
    assert 'setAttribute("data-tier", tier)' in fn


def test_v2_phase2_zone_b_recap_hero_and_twin_studios():
    start = HOME.split('id="home-start-card"', 1)[1].split('id="home-recent-projects-card"', 1)[0]
    assert 'id="home-start-recap-tile"' in start
    assert "switchTab('tab-recap')" in start
    assert 'class="home-tile-grid"' in start
    assert start.count('class="home-tile"') == 2
    assert "switchTab('tab-voice')" in start
    assert "switchTab('tab-tts')" in start
    assert "#tab-home .vcp-studio-hero-tile" in PHASE2_CSS
    assert "#tab-home #home-start-card .home-tile-grid" in PHASE2_CSS


def test_v2_phase2_zone_c_designed_states_and_svg_recent_icons():
    recent = HOME.split('id="home-recent-projects-card"', 1)[1].split('id="home-tools-card"', 1)[0]
    for marker in ("vcp-skel-row", "vcp-empty-glass", "vcp-empty-medallion", "vcp-error-glass"):
        assert marker in recent, marker
    recent_fn = APP_JS.split("function renderHomeRecentProjects()", 1)[1].split("window.renderHomeRecentProjects", 1)[0]
    assert 'class="vcp-ico"' in recent_fn
    assert 'class="home-recent-footer"' in recent_fn
    assert 'vcp-job-card' in recent_fn
    assert 'vcp-job-footer-row' in recent_fn
    assert 'data-job-kind="${kind}"' in recent_fn


def test_v2_phase2_unified_queue_job_card_system_no_badge_button_overlap():
    for marker in (
        ".vcp-job-card",
        ".vcp-job-progress",
        ".vcp-job-footer-row",
        "#tab-home .vcp-job-card .home-recent-footer",
        "#tab-history .hub-row.vcp-job-card .hub-row-badges",
    ):
        assert marker in PHASE2_CSS, marker
    # Non-overlapping flex-column footer separation on both Home Recent & History & Live Sheet & Admin Queue
    live_fn = APP_JS.split("function liveJobsSheetRowHtml(job)", 1)[1].split("function renderLiveJobsSheet()", 1)[0]
    hist_fn = APP_JS.split("function hubHistoryRowHtml(j)", 1)[1].split("function renderHistoryHub()", 1)[0]
    queue_fn = APP_JS.split("function renderAdminQueueData(", 1)[1].split("window.loadAdminQueue", 1)[0]
    for name, body in (("liveJobsSheetRowHtml", live_fn), ("hubHistoryRowHtml", hist_fn), ("loadAdminQueue", queue_fn)):
        assert "vcp-job-card" in body, f"{name} must use unified .vcp-job-card"
        assert "vcp-job-footer-row" in body, f"{name} must use fixed .vcp-job-footer-row"
        assert "vcp-job-progress" in body, f"{name} must include unified .vcp-job-progress track"


def test_v2_phase2_zone_d_grouped_tools_and_dark_lite_css():
    tools = HOME.split('id="home-tools-card"', 1)[1].split('id="home-help-entry-card"', 1)[0]
    assert 'data-vcp-tools-list="1"' in tools
    assert 'data-theme="dark"' in PHASE2_CSS
    assert "html.lite" in PHASE2_CSS
    assert 'html[data-theme="dark"].lite' in PHASE2_CSS


def test_v2_phase2_i18n_new_keys_both_locales():
    for key in (
        "home_hero_tagline",
        "home_status_ready",
        "home_start_hint",
        "home_bal_tts_short",
        "home_bal_voice_short",
        "home_bal_recap_short",
        "home_recent_empty_sub",
        "home_recent_error_sub",
    ):
        assert I18N_JS.count(f"{key}:") == 2, f"{key} must exist in my and en"


_RETIRED_VIP_STATIC_IDS = {
    "card-plan-combined-starter", "card-plan-master", "card-plan-recap",
    "card-plan-recap-starter", "card-plan-voice", "card-plan-voice-packs",
    "plan-panel-combined",
    # Round 8 (B4 + B7): #tts-balance-line and #tts-gemini-key-card removed from #tab-tts
    "btn-tts-key-delete", "btn-tts-key-save", "tts-balance-line",
    "tts-byok-advanced-body", "tts-byok-advanced-toggle", "tts-gemini-key-card",
    "tts-key-active-badge", "tts-key-guide-body", "tts-key-guide-toggle",
    "tts-key-input", "tts-key-state",
}
_RETIRED_VIP_STATIC_ONCLICKS = {
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
}


def test_v2_phase2_zero_deleted_ids_or_onclicks_vs_main():
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
