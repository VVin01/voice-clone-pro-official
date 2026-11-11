"""Batch AZ — Glassmorphism 2.0 / Liquidmorphism whole-app upgrade
(owner direction 2026-11-06 00:42; spec
~/workspace/batch-az-spec-2026-11-06.md, AZ-1 … AZ-12).

Client-only batch: depth-tiered glass tokens with specular rims, a
slow liquid ambient, fluid motion (data-driven sliding bottom-nav
pill, spring entrances), tinted-glass accents, a rebuilt toast, the
Recap wizard indicator as a liquid segmented capsule (AZ-9,
superseding the owner-rejected AX-2R circles), the AZ-10
text-integrity fixes, the AZ-11 compact-dropdown conversions and
the AZ-12 i18n conversions. No ids, behaviours, payloads or
navigation change except the specced control swaps (AZ-9/AZ-11),
whose write paths are pinned unchanged here and in the AX/AV files.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
APP_JS = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

AZ_CSS = STYLE_CSS[STYLE_CSS.index("BATCH AZ — GLASSMORPHISM 2.0"):]
TAB = INDEX_HTML[INDEX_HTML.index('id="tab-recap"'):INDEX_HTML.index('id="tab-downloader"')]


def _rule(selector: str, css: str = AZ_CSS) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
    assert m, f"rule not found: {selector}"
    return m.group(1)


# ── AZ-1/AZ-3: depth tiers + liquid ambient ─────────────────────

def test_az_glass_tier_tokens_and_legacy_remap():
    assert "--glass-1-bg: rgba(255, 255, 255, 0.50);" in AZ_CSS
    assert "--glass-2-bg: rgba(255, 255, 255, 0.65);" in AZ_CSS
    assert "--glass-3-bg: rgba(255, 255, 255, 0.78);" in AZ_CSS
    # Blur radii capped at the tier values 18 / 22 / 28.
    assert "--glass-1-blur: blur(18px)" in AZ_CSS
    assert "--glass-2-blur: blur(22px)" in AZ_CSS
    assert "--glass-3-blur: blur(28px)" in AZ_CSS
    # Every legacy surface resolves to a tier (no surface left flat).
    assert "--surface: var(--glass-1-bg);" in AZ_CSS
    assert "--glass-strong: var(--glass-3-bg);" in AZ_CSS
    assert "--blur-card: var(--glass-1-blur);" in AZ_CSS
    assert "--blur-chrome: var(--glass-2-blur);" in AZ_CSS
    assert "--blur-overlay: var(--glass-3-blur);" in AZ_CSS


def test_az_liquid_ambient_drifts_and_respects_reduced_motion():
    assert "@keyframes azAmbientDrift" in AZ_CSS
    before = _rule("body::before")
    assert "inset: -8%" in before  # oversized so the drift never bares an edge
    assert "azAmbientDrift 56s ease-in-out infinite alternate" in before
    rm = AZ_CSS[AZ_CSS.index("@media (prefers-reduced-motion: reduce)",
                              AZ_CSS.index("AZ fallbacks")):]
    assert "body::before { animation: none; }" in rm


# ── AZ-2: specular gradient rims ────────────────────────────────

def test_az_rim_recipe_covers_the_surface_set():
    rim = re.search(
        r"(\.card, \.top-nav,.*?\.studio-alert-carousel) \{([^}]+)\}", AZ_CSS, re.S)
    assert rim, "rim surface group not found"
    assert "border: 1px solid transparent" in rim.group(2)
    assert "linear-gradient(180deg, var(--rim-hi), var(--rim-lo)) border-box" in rim.group(2)
    assert "padding-box" in rim.group(2)
    assert "var(--az-bg" in rim.group(2)
    # Overlay surfaces sit on the most opaque tier (one group rule).
    tier3 = re.search(
        r"\.modal-content, \.studio-confirm-card, \.gate-lock-card, \.studio-toast,\s*"
        r"\.floating-alert-card, \.submit-confirm-card, \.live-jobs-sheet-card \{([^}]+)\}",
        AZ_CSS)
    assert tier3 and "--az-bg: var(--glass-3-bg);" in tier3.group(1)
    # Chrome keeps its readability floor (deliberate, see report).
    assert ".bottom-nav { --az-bg: rgba(255, 255, 255, 0.88); }" in AZ_CSS


# ── AZ-4: liquid motion — nav pill, pane enter, springs ─────────

def test_az_nav_pill_element_and_data_driven_mover():
    nav = INDEX_HTML[INDEX_HTML.index('<nav class="bottom-nav"'):]
    nav = nav[:nav.index("</nav>")]
    # The pill is the nav's first child so it slides behind the items.
    assert nav.index('id="nav-active-pill"') < nav.index('class="nav-item')
    # Data-driven: the mover reads whichever item is .active — no
    # tab list is hardcoded, so a future 5th tab joins automatically.
    body = APP_JS[APP_JS.index("function moveNavPill()"):APP_JS.index("function markPaneEnter")]
    assert 'document.querySelector(".bottom-nav .nav-item.active")' in body
    assert "active.offsetLeft" in body and "active.offsetWidth" in body
    assert "window.moveNavPill = moveNavPill;" in APP_JS
    assert "moveNavPill();" in APP_JS  # wired into switchTab + setup
    assert "function markPaneEnter(pane)" in APP_JS
    assert "@keyframes azPaneIn" in AZ_CSS
    assert ".tab-pane.tab-enter { animation: azPaneIn 0.22s ease; }" in AZ_CSS
    # The item itself no longer paints the active fill — the pill does.
    assert ".nav-item.active { background: transparent; box-shadow: none; }" in AZ_CSS


def test_az_spring_entrances_and_press_feedback():
    assert "@keyframes azSheetIn" in AZ_CSS and "@keyframes azPopIn" in AZ_CSS
    assert ".live-jobs-sheet-card, .submit-confirm-card { animation: azSheetIn" in AZ_CSS
    assert ".modal-content, .studio-confirm-card, .gate-lock-card { animation: azPopIn" in AZ_CSS
    press = re.search(r"\.pill-btn:active,.*?\.style-select-pill:active \{([^}]+)\}", AZ_CSS, re.S)
    assert press and "transform: scale(0.97);" in press.group(1)


# ── AZ-5: tinted-glass accents ──────────────────────────────────

def test_az_tinted_accents():
    # The featured pricing tier reads as the gold (paid) tier.
    assert ".pricing-card.featured-master {" in AZ_CSS
    assert "rgba(245, 185, 33, 0.14)" in AZ_CSS
    # AU's gold render-quality selection survives, rim added.
    assert "#recap-quality-picker .pill-btn.active {" in AZ_CSS
    assert "var(--rim-shadow)" in _rule("#recap-quality-picker .pill-btn.active")


# ── AZ-7: toasts ────────────────────────────────────────────────

def test_az_toast_structure_and_timing():
    toast = _rule(".studio-toast")
    assert "overflow: hidden" in toast and "studioToastIn" in toast
    assert ".studio-toast.leaving {" in AZ_CSS
    assert ".studio-toast .toast-progress {" in AZ_CSS
    assert "var(--toast-life, 3500ms)" in AZ_CSS
    # The JS sets the lifetime custom property from the call's own
    # duration and starts the exit 200 ms before removal — the
    # total on-screen time is unchanged from the pre-AZ toast.
    body = APP_JS[APP_JS.index("function showStudioToast("):APP_JS.index("function showPrefsSavedNote")]
    assert "toast.style.setProperty('--toast-life', duration + 'ms');" in body
    assert "duration - 200" in body and "classList.add('leaving')" in body
    assert '<span class="toast-progress" aria-hidden="true"></span>' in body
    # The close button's label resolves through i18n (AZ-12), with
    # the pre-AZ literal kept only as the i18n-failed fallback.
    assert 'const closeLabel = typeof t === "function" ? t("toast_close") : "Close";' in body
    assert 'aria-label="${closeLabel}"' in body


# ── AZ-9: liquid segmented capsule ──────────────────────────────

def test_az9_capsule_markup_and_css():
    nav = TAB[TAB.index('id="recap-steps-nav"'):TAB.index('id="prefs-saved-note"')]
    assert 'class="recap-steps-capsule"' in nav
    assert nav.count('class="recap-steps-nav-item') == 4
    assert nav.count('class="recap-steps-nav-name"') == 4
    capsule = _rule(".recap-steps-capsule")
    assert "padding: 6px" in capsule and "border-radius: 999px" in capsule
    item = _rule(".recap-steps-nav-item")
    assert "height: 44px" in item and "min-height: 44px" in item
    active = _rule(".recap-steps-nav-item.is-active")
    assert "flex-grow: 2.4" in active
    assert "linear-gradient(90deg, #0a84ff, #38bdf8)" in active
    assert "color: #ffffff" in active
    done = _rule(".recap-steps-nav-item.is-done")
    assert "rgba(10, 132, 255, 0.12)" in done
    # Session-truth is untouched: no segment is pre-marked done in
    # markup; only step 1 starts active (the boot state).
    assert "is-done" not in nav
    assert nav.count("is-active") == 1


# ── AZ-10: text-integrity fixes ─────────────────────────────────

def test_az10_pill_and_chip_line_heights():
    assert ".pill-btn { line-height: 1.6; }" in AZ_CSS
    assert "word-break: keep-all" in AZ_CSS
    chip = _rule(".bal-chip")
    assert "min-height: 34px" in chip and "line-height: 1.6" in chip
    # Batch BB-2 re-pin: the Voice speed pills became liquid-capsule
    # segments (.speed-seg); the ≥44px touch height carries over.
    assert "min-height: 44px" in _rule(".speed-seg")
    assert "body.lang-my .nav-label { line-height: 1.75; }" in AZ_CSS
    assert "#tab-voice { padding-bottom: 72px; }" in AZ_CSS
    assert "min-height: 44px" in _rule(".welcome-strip-btn")


def test_az10_prefs_confirmation_is_a_toast_not_the_wizard_caption():
    body = APP_JS[APP_JS.index("function showPrefsSavedNote"):APP_JS.index("function showPrefsSavedNote") + 700]
    assert 'showStudioToast(t("live_prefs_saved"), "success", 2200);' in body


# ── AZ-11: compact dropdowns ────────────────────────────────────

def test_az11_language_is_one_dropdown_with_one_write_path():
    assert 'id="recap-lang-picker"' not in INDEX_HTML
    assert "#recap-lang-picker" not in STYLE_CSS
    assert "#recap-lang-picker" not in APP_JS
    m = re.search(r'<select id="recap-lang-more"[^>]*>(.*?)</select>', TAB, re.S)
    assert m and "glass-dropdown" in m.group(0)
    assert len(re.findall(r"<option ", m.group(1))) == 18
    # The single write path is identical in shape to AX's.
    assert 'langSel.addEventListener("change", () => {' in APP_JS
    assert "setRecapSourceLanguage(langSel.value);" in APP_JS
    assert 'state.recapSourceLanguage = code || "auto";' in APP_JS


def test_az11_subtitle_style_is_one_dropdown():
    m = re.search(r'<select id="sub-style-picker"[^>]*>(.*?)</select>', INDEX_HTML, re.S)
    assert m and "glass-dropdown" in m.group(0)
    values = re.findall(r'<option value="([a-z]+)"', m.group(1))
    assert values == ["yellow", "white", "boxed", "neon", "band", "heavy", "soft", "rose", "orange", "mint"]
    body = APP_JS[APP_JS.index('const styleSel = document.getElementById("sub-style-picker");'):]
    body = body[:body.index("// 10. Subtitle Live Preview Renderer")]
    assert 'styleSel.addEventListener("change", () => {' in body
    assert 'state.recapSubStyle = styleSel.value || "yellow";' in body
    assert "scheduleRecapCompositeRefresh();" in body
    gem = re.search(r'<select id="recap-gemini-voice-select"[^>]*>', INDEX_HTML)
    assert gem and "glass-dropdown" in gem.group(0)


def test_az11_glass_dropdown_pattern():
    dd = _rule(".glass-dropdown")
    assert "appearance: none" in dd and "-webkit-appearance: none" in dd
    assert "min-height: 50px" in dd and "background-image:" in dd  # chevron


# ── AZ-12: i18n conversions ─────────────────────────────────────

AZ_NEW_KEYS = ["toast_close", "gate_chip_channel", "gate_chip_group",
               "recap_gemini_auto", "recap_step4_quote_unit",
               "sub_lines_one", "sub_lines_two",
               "bal_chip_voice_title", "bal_chip_recap_title"]


def test_az12_new_keys_exist_in_both_locales():
    for key in AZ_NEW_KEYS:
        assert I18N_JS.count(f"{key}:") == 2, key


def test_az12_converted_surfaces_carry_the_keys():
    assert 'data-i18n="gate_chip_channel"' in INDEX_HTML
    assert 'data-i18n="gate_chip_group"' in INDEX_HTML
    assert 'paintGateChip(document.getElementById("gate-chip-channel"), t("gate_chip_channel"), channelOk)' in APP_JS
    assert '"📢 Channel", channelOk' not in APP_JS
    assert 'data-i18n="recap_step4_quote_unit"' in INDEX_HTML
    assert 'data-i18n="sub_lines_one"' in INDEX_HTML
    assert 'data-i18n="sub_lines_two"' in INDEX_HTML
    assert 'data-i18n="recap_gemini_auto"' in INDEX_HTML
    assert 'data-i18n-title="bal_chip_voice_title"' in INDEX_HTML
    assert 'data-i18n="recap_info_ready"' in INDEX_HTML


# ── fallbacks + token ───────────────────────────────────────────

def test_az_fallbacks_cover_the_new_layers():
    supports = AZ_CSS[AZ_CSS.index("@supports not ((backdrop-filter: blur(2px))"):]
    # Batch BB-2 re-pin: the speed capsule joins the no-backdrop net
    # (AZ's own discipline — every new glass layer enters the nets),
    # so the selector list now ends with .speed-capsule.
    assert ".studio-alert-carousel, .recap-steps-capsule,\n    .speed-capsule {" in supports
    assert "background: #ffffff" in supports
    rt = AZ_CSS[AZ_CSS.index("prefers-reduced-transparency: reduce"):]
    assert "backdrop-filter: none" in rt
    assert ".recap-steps-capsule { background: rgba(255, 255, 255, 0.97); }" in rt


def test_az_cache_token_is_r79():
    assert INDEX_HTML.count("20261104_r138") == 4
    assert "20261104_r71" not in INDEX_HTML
