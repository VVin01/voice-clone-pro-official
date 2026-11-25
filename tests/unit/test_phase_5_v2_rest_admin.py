from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")

PHASE5_MARKER = "VCP UI REBUILD V2 — PHASE 5: DOWNLOADER, VIP, HISTORY, MODALS & ADMIN CONSOLE"
PHASE5_CSS = STYLE_CSS.split(PHASE5_MARKER, 1)[1]


def test_v2_phase5_cache_token_current():
    assert INDEX_HTML.count("?v=20261125_admin_r1") == 5
    assert "20261114_r146" not in INDEX_HTML


def test_v2_phase5_zero_backdrop_filter_in_scrolling_surfaces():
    assert "backdrop-filter" not in PHASE5_CSS
    assert "-webkit-backdrop-filter" not in PHASE5_CSS


def test_v2_phase5_admin_4x2_nav_triage_and_collapsible_settings():
    assert "#tab-admin #adm-nav" in PHASE5_CSS
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in PHASE5_CSS
    assert "#tab-admin .adm-triage-banner" in PHASE5_CSS
    assert "#tab-admin details.adm-accordion" in PHASE5_CSS
    assert 'id="adm-triage-card"' in INDEX_HTML
    assert 'onclick="showAdminSection(\'payments\')"' in INDEX_HTML
    for acc_id in (
        'id="adm-acc-pay-maint"',
        'id="adm-acc-ai-bots"',
        'id="adm-acc-quotas-gates"',
        'id="adm-acc-tutorials"',
        'id="adm-acc-pricing"',
        'id="adm-acc-gemini"',
        'id="adm-tts-chain-card"',
    ):
        assert acc_id in INDEX_HTML, f"Missing collapsible settings accordion: {acc_id}"
    assert 'id="btn-save-admin-settings"' in INDEX_HTML
    assert 'onclick="saveAdminSettings()"' in INDEX_HTML
    assert 'adm-triage-card' in APP_JS


def test_v2_phase5_downloader_vip_history_modals_and_dark_lite_rules():
    for sel in (
        "#tab-downloader .dl-search-card",
        "#tab-downloader .dl-formats-grid .vcp-dl-format-card.active",
        "#tab-vip .pricing-card",
        "#tab-vip #payment-box",
        "#tab-history .vcp-history-retention-bar",
        ".submit-confirm-card",
        ".live-jobs-sheet-card",
        ".gate-lock-card",
        'html[data-theme="dark"] #tab-admin #adm-nav',
        'html[data-theme="dark"] #tab-admin details.adm-accordion',
        'html[data-theme="dark"] .submit-confirm-card',
        "html.lite #tab-admin #adm-nav",
        'html[data-theme="dark"].lite #tab-admin details.adm-accordion',
    ):
        assert sel in PHASE5_CSS, f"Missing Phase 5 CSS selector: {sel}"


def test_v2_phase5_new_i18n_keys_in_both_languages():
    for key in (
        "adm_triage_checking",
        "adm_triage_action_needed",
        "adm_triage_clear",
        "adm_triage_sub",
        "adm_triage_btn",
        "adm_set_grp_pay_maint",
        "adm_set_pill_pay_maint",
        "adm_set_grp_ai_bots",
        "adm_set_pill_ai_bots",
        "adm_set_grp_quotas_gates",
        "adm_set_pill_quotas",
    ):
        assert I18N_JS.count(f"{key}:") >= 2, f"Missing bilingual i18n key: {key}"
