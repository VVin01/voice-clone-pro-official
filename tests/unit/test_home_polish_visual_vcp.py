"""Unit guards for the Home Polish + Visual Identity Pass (feat/home-polish-visual)."""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")

PASS_MARKER = "HOME POLISH + VISUAL IDENTITY PASS"
PASS_CSS = STYLE_CSS.split(PASS_MARKER, 1)[1].split("VCP UI REBUILD V2", 1)[0]


def test_cache_token_bumped_to_r142():
    assert INDEX_HTML.count("?v=20261117_r160") == 4
    assert "_r141" not in INDEX_HTML


def test_a1_welcome_strip_wraps_and_respects_hidden():
    assert "#tab-home .welcome-strip.hidden" in PASS_CSS
    assert "#tab-home .welcome-strip:not(.hidden)" in PASS_CSS
    assert "flex-wrap: wrap !important;" in PASS_CSS
    assert "#tab-home .welcome-strip-sample-btn" in PASS_CSS
    assert "rgba(15, 23, 42, 0.4" not in PASS_CSS
    assert "rgba(15, 23, 42, 0.7" not in PASS_CSS


def test_a2_recent_projects_light_glass_and_full_width_name():
    assert ".home-recent-row" in PASS_CSS
    assert "-webkit-line-clamp: 2 !important;" in PASS_CSS
    assert 'class="home-recent-footer"' in APP_JS
    assert 'data-job-kind="${kind}"' in APP_JS
    assert "#tab-home .home-tile-tts" in PASS_CSS


def test_a3_see_all_chip_never_wraps():
    see_all_block = PASS_CSS.split("#home-recent-see-all {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap !important;" in see_all_block
    assert "flex: 0 0 auto !important;" in see_all_block


def test_a4_free_today_rows_align_with_first_line():
    free_row_block = PASS_CSS.split("#tab-home .home-free-row", 1)[1].split("}", 1)[0]
    assert "align-items: flex-start !important;" in free_row_block


def test_a5_vip_current_plan_units_use_i18n():
    assert 'id="profile-balance-min">—<' in INDEX_HTML
    assert 'id="profile-balance-credits">—<' in INDEX_HTML
    assert "function paintVipPlanBalances(data)" in APP_JS
    fn = APP_JS.split("function paintVipPlanBalances(data)", 1)[1].split("window.paintVipPlanBalances", 1)[0]
    assert 'fmt("home_free_value_min"' in fn
    assert 'fmt("home_free_value_credits"' in fn
    app_without_helper = APP_JS.replace(fn, "")
    assert "`${data.balance_minutes} mins`" not in app_without_helper
    assert "`${data.recap_credits || 0} credits`" not in app_without_helper


def test_a6_help_entry_header_and_accordion_rhythm():
    assert 'class="home-help-entry-head"' in INDEX_HTML
    assert "#tab-home .home-help-entry-head" in PASS_CSS
    assert "#tab-home #home-tutorials-card .home-tut-head" in PASS_CSS


def test_b1_header_brand_gradient_covers_lite_mode():
    normalized = PASS_CSS.replace("\r\n", "\n")
    assert ".top-nav,\nhtml.lite .top-nav,\nbody.lite .top-nav" in normalized
    assert "linear-gradient(135deg, #0b1630" in PASS_CSS


def test_b2_b3_b4_b5_visual_identity_and_glass_perf():
    # B2: dedicated studio accent blocks
    for studio_sel in (
        "#tab-voice .step-num-pill",
        "#tab-recap .step-num-pill",
        "#tab-tts .step-num-pill",
        "#tab-downloader #btn-extract-dl",
        "#tab-vip .plan-tab-btn.active",
    ):
        assert studio_sel in PASS_CSS
    # B3: brand hero strip with white ink
    assert "#tab-home .home-status-strip" in PASS_CSS
    assert "color: #ffffff !important;" in PASS_CSS
    # B4: dual-layer shadow + gradient border + filled bottom-nav pill
    assert "background-clip: padding-box, border-box !important;" in PASS_CSS
    assert '.bottom-nav[data-active-tab="tab-recap"] .nav-pill' in PASS_CSS
    assert 'nav.setAttribute("data-active-tab", active.dataset.tab)' in APP_JS
    # B5: balance numbers semibold + tabular-nums
    assert "font-variant-numeric: tabular-nums;" in PASS_CSS
    # Glass perf rule: zero backdrop-filter rules in pass CSS
    css_no_comments = re.sub(r"/\*.*?\*/", "", "/*" + PASS_CSS, flags=re.DOTALL)
    assert "backdrop-filter" not in css_no_comments
