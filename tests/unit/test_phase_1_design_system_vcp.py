"""Phase 1 verification suite: Design System tokens, vcp-* components,
Glass performance rule (.lite mode + flat glass scrolling cards),
UI state machine (setSectionState), and Telegram native integrations.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STYLE_CSS = ROOT / "app" / "webapp" / "static" / "css" / "style.css"
APP_JS = ROOT / "app" / "webapp" / "static" / "js" / "app.js"
I18N_JS = ROOT / "app" / "webapp" / "static" / "js" / "i18n.js"


def test_phase_1_css_tokens_and_fixed_undefined_vars():
    css = STYLE_CSS.read_text(encoding="utf-8")
    # Previously undefined variables fixed in Phase 1
    for var_name in (
        "--text-muted:",
        "--text-dim:",
        "--text-1:",
        "--glass-2:",
        "--blue-soft:",
        "--danger:",
    ):
        assert var_name in css, f"Missing CSS variable definition {var_name}"

    # Phase 1 design system tokens
    for token in (
        "--vcp-sp-1: 4px",
        "--vcp-sp-4: 16px",
        "--vcp-input-min-h: 48px",
        "--vcp-btn-primary-h: 52px",
        "--vcp-touch-min: 44px",
        "--vcp-slider-thumb: 22px",
        "--vcp-lh-body: 1.7",
        "--vcp-lh-title: 1.45",
        "--vcp-ls-my: 0px",
        "--vcp-fs-input: 16px",
    ):
        assert token in css, f"Missing design token {token}"


def test_phase_1_glass_performance_and_lite_mode():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "html.lite" in css and "body.lite" in css
    assert "backdrop-filter: none !important" in css
    assert ".tab-pane .card" in css
    assert ".tab-pane .vcp-card" in css


def test_phase_1_vcp_component_classes_and_ui_state():
    css = STYLE_CSS.read_text(encoding="utf-8")
    for cls in (
        ".vcp-card",
        ".vcp-btn",
        ".vcp-btn--primary",
        ".vcp-btn-primary",
        ".vcp-btn--glass",
        ".vcp-btn-glass",
        ".vcp-btn--danger",
        ".vcp-btn-danger",
        ".vcp-chip",
        ".vcp-pill",
        ".vcp-segmented",
        ".vcp-sheet-backdrop",
        ".vcp-sheet",
        ".vcp-accordion",
        ".vcp-slider-row",
        ".vcp-badge",
        '[data-ui-state="loading"]',
        '[data-ui-state="empty"]',
        '[data-ui-state="error"]',
        '[data-ui-state="ready"]',
    ):
        assert cls in css, f"Missing vcp-* component selector {cls}"


def test_phase_1_i18n_keys_my_and_en():
    i18n = I18N_JS.read_text(encoding="utf-8")
    for key in (
        "ui_state_loading:",
        "ui_state_empty:",
        "ui_state_error:",
        "ui_state_retry:",
        "vcp_reset_default:",
        "vcp_close:",
        "vcp_advanced_settings:",
    ):
        assert i18n.count(key) >= 2, f"Key {key} must exist in both my and en in i18n.js"


def test_phase_1_js_helpers_and_telegram_integrations():
    js = APP_JS.read_text(encoding="utf-8")
    for fn in (
        "function initLitePerformanceMode(",
        "function setSectionState(",
        "function openVcpSheet(",
        "function closeVcpSheet(",
        "function toggleVcpAccordion(",
        "function resetVcpSlider(",
        "function setVcpButtonLoading(",
        "function syncTelegramBackButton(",
        "function handleTelegramBackButton(",
        "function setupViewportAndKeyboardSync(",
        "function hasUnsavedWork(",
        "function syncUnsavedWorkGuard(",
    ):
        assert fn in js, f"Missing JS helper {fn}"
