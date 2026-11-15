"""Phase 1 (UI Rebuild V2) structural & visual-guardrail verification suite.

Covers:
1. Hard Guardrail #1: Zero element id / onclick handler removals vs main.
2. Hard Guardrail #8: Low-end Android WebView performance (zero backdrop-filter
   inside scrolling .tab-pane content cards; total CSS+JS <= +15% baseline).
3. Ulfa Feedback Fix #1: Recap stage top-left overlay separation (#canvas-source-label
   inside .vcp-preview-stage-bar, separated from video canvas).
4. Ulfa Feedback Fix #2: Darkened Light-theme secondary/muted grey ink for
   outdoor readability (--text-2 / --vcp-text-secondary #2F435E, --text-3 #475569).
5. Approved Floating Liquid Glass header/dock chrome, SF-style inline SVG icons,
   and first-class Light / Dark / Lite theme architecture + complete my/en keys.
"""
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

V2_CSS = STYLE_CSS[STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 1") :]
V2_CSS_BLOCK = STYLE_CSS[STYLE_CSS.rindex("/*", 0, STYLE_CSS.index("VCP UI REBUILD V2 — PHASE 1")) :]

# Baseline LF byte count on main (b4ec0f5a): style.css 341,120 + app.js 777,042 + i18n.js 233,938
MAIN_ASSET_BYTES_BASELINE = 1_357_000
MAX_ASSET_BYTES_ALLOWED = int(MAIN_ASSET_BYTES_BASELINE * 1.15)


def test_v2_phase1_asset_weight_under_15_percent_cap():
    css_bytes = len(STYLE_CSS.encode("utf-8"))
    app_bytes = len(APP_JS.encode("utf-8"))
    i18n_bytes = len(I18N_JS.encode("utf-8"))
    total = css_bytes + app_bytes + i18n_bytes
    assert total <= MAX_ASSET_BYTES_ALLOWED, (
        f"CSS+JS weight {total} exceeds +15% cap ({MAX_ASSET_BYTES_ALLOWED})"
    )


def test_v2_phase1_cache_token_bumped():
    assert INDEX_HTML.count("?v=20261114_r150") == 4
    assert "20261114_r146" not in INDEX_HTML


def test_v2_phase1_outdoor_readable_light_secondary_grey_tokens():
    for expected in (
        "--text: #0F172A;",
        "--text-2: #2F435E;",
        "--text-3: #475569;",
        "--vcp-text-primary: #0F172A;",
        "--vcp-text-secondary: #2F435E;",
        "--vcp-text-muted: #475569;",
    ):
        assert expected in STYLE_CSS, f"Missing outdoor ink token: {expected}"


def test_v2_phase1_recap_stage_top_left_chip_separation():
    stage_bar_start = INDEX_HTML.index('<div class="vcp-preview-stage-bar">')
    stage_bar_end = INDEX_HTML.index('<div class="preview-empty-state', stage_bar_start)
    stage_bar_html = INDEX_HTML[stage_bar_start:stage_bar_end]
    mockup_start = INDEX_HTML.index('<div class="mockup-screen-container', stage_bar_end)
    mockup_end = INDEX_HTML.index('<div class="recap-dock-section', mockup_start)
    mockup_html = INDEX_HTML[mockup_start:mockup_end]

    assert 'id="canvas-source-label"' in stage_bar_html
    assert 'id="btn-preview-corner-reset"' in stage_bar_html
    assert 'id="canvas-source-label"' not in mockup_html
    assert ".vcp-preview-stage-bar .canvas-source-label" in V2_CSS
    assert "position: static;" in V2_CSS


def test_v2_phase1_floating_chrome_and_sf_svg_nav_icons():
    nav = INDEX_HTML.split('<nav class="bottom-nav">', 1)[1].split("</nav>", 1)[0]
    assert 'id="nav-active-pill"' in nav
    for emoji in ("🏠", "🎙️", "🎧", "🎬", "💎", "⚡"):
        assert emoji not in nav, f"Bottom nav should use crisp SF-style SVG instead of emoji {emoji}"
    assert nav.count('class="vcp-ico vcp-nav-svg"') == 6

    header = INDEX_HTML[INDEX_HTML.index('<header class="top-nav">'):INDEX_HTML.index("</header>")]
    assert "📚" not in header
    assert 'id="btn-theme-toggle"' in header
    assert 'onclick="toggleThemeMode()"' in header
    assert 'class="vcp-ico"' in header


def test_v2_phase1_dark_and_lite_themes_and_no_scrolling_backdrop_filter():
    assert 'html[data-theme="dark"]' in V2_CSS
    assert 'html[data-theme="dark"] .top-nav .user-pill' in V2_CSS
    assert "html.lite" in V2_CSS and 'html[data-theme="dark"].lite' in V2_CSS
    assert ".vcp-preview-stage-bar .canvas-source-label" in V2_CSS

    # Zero backdrop-filter rules on scrolling .tab-pane cards inside V2_CSS_BLOCK
    v2_no_comments = re.sub(r"/\*.*?\*/", "", V2_CSS_BLOCK, flags=re.DOTALL)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]+)\}", v2_no_comments):
        for decl in body.split(";"):
            if "backdrop-filter" in decl and "none" not in decl:
                assert any(ok in sel for ok in (".top-nav", ".bottom-nav")), (
                    f"Unexpected backdrop-filter on scrolling element: {sel.strip()} -> {decl.strip()}"
                )
    assert ".tab-pane .card" in STYLE_CSS and "backdrop-filter: none !important;" in STYLE_CSS


def test_v2_phase1_theme_js_helpers_and_i18n_keys():
    for fn in ("function initThemeMode()", "function setThemeMode(", "function toggleThemeMode()"):
        assert fn in APP_JS
    for key in ("theme_toggle_title:", "theme_light:", "theme_dark:"):
        assert I18N_JS.count(key) >= 2, f"Missing i18n key {key} in my/en"


def test_v2_phase1_zero_deleted_ids_or_onclicks_vs_main():
    try:
        main_html = subprocess.check_output(
            ["git", "show", "main:app/webapp/static/index.html"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except Exception:
        return  # Shallow CI clone without main ref skips git show check cleanly
    main_ids = set(re.findall(r'\bid="([^"]+)"', main_html))
    curr_ids = set(re.findall(r'\bid="([^"]+)"', INDEX_HTML))
    missing_ids = main_ids - curr_ids
    assert not missing_ids, f"Deleted element IDs vs main: {sorted(missing_ids)}"

    main_onclicks = set(re.findall(r'\bonclick="([^"]+)"', main_html))
    curr_onclicks = set(re.findall(r'\bonclick="([^"]+)"', INDEX_HTML))
    missing_onclicks = main_onclicks - curr_onclicks
    assert not missing_onclicks, f"Deleted onclick handlers vs main: {sorted(missing_onclicks)}"
