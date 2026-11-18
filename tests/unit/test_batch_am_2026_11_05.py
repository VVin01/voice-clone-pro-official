"""Batch AM (2026-11-05): Mini App recap preview/settings consistency.

Built from the screenshot/settings audit
(``screenshot-settings-audit-2026-11-05.md``, mismatch register
M1-M8). Pins:

- M7: the Blur and Logo toggle handlers schedule an engine-composite
  refresh, like every other payload-affecting control.
- M8(ii): after account prefs land, the recap controls re-sync from
  state through the restore path (window hook + guarded call).
- M1: the blur drag handle has no CSS min-height floor (the 22px
  floor painted it ~1.7x taller than small H settings).
- M8(i): Reset Layout clears recapBlurBoxTouched, matching the
  per-source reset.
- M2 residual: the title drag handle is grip-only (no text span,
  no ellipsis rule) — the composite renders the real title.
- M3/M4 residuals: an explicit preparing state while the first
  composite is in flight, and a label on the raw source-video
  surface (both i18n-wired in both locales).
- S9 cosmetic: the blur sliders grey out (display-only) while the
  Blur toggle is OFF; the payload's master gate is pinned too.
- Cache token r63 -> r64.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")


def _handler_block(marker: str, span: int = 1200) -> str:
    """The click-handler region following an element lookup."""
    i = APP_JS.index(marker)
    return APP_JS[i:i + span]


def _function_block(signature: str, span: int = 2600) -> str:
    i = APP_JS.index(signature)
    return APP_JS[i:i + span]


# ── M7: toggles re-render the composite ────────────────────────────

def test_blur_toggle_schedules_composite_refresh():
    block = _handler_block('getElementById("btn-toggle-blur")')
    assert "state.recapBlurSubs = !state.recapBlurSubs;" in block
    assert "scheduleRecapCompositeRefresh();" in block


def test_logo_toggle_schedules_composite_refresh():
    # Anchor on the listener itself: an earlier getElementById for
    # the same button lives inside refreshLogoUi.
    block = _handler_block('btnToggleLogo.addEventListener("click"')
    assert "state.recapLogo = !state.recapLogo;" in block
    assert "refreshLogoUi();" in block
    assert "scheduleRecapCompositeRefresh();" in block


# ── M8(ii): controls re-sync after account prefs ───────────────────

def test_resync_hook_registered_from_restore_path():
    # Anchor on the assignment: applyAccountPrefs mentions the hook
    # name earlier in the file than the registration itself.
    block = _function_block("window.resyncRecapControlsAfterPrefs = function")
    assert "restoreRecapSettings();" in block


def test_apply_account_prefs_invokes_resync_hook():
    block = _function_block("function applyAccountPrefs(prefs) {", span=4200)
    # The hook is called only after state + the snapshot write-through
    # have both happened, and it stays guarded (boot-order safety).
    assert "cacheAccountPrefs(prefs);" in block
    assert block.index("cacheAccountPrefs(prefs);") < block.index(
        "resyncRecapControlsAfterPrefs")
    assert 'typeof window.resyncRecapControlsAfterPrefs === "function"' in block


# ── M1: no min-height floor on the blur handle ─────────────────────

def test_blur_box_has_no_min_height_floor():
    m = re.search(r"\.preview-blur-box \{([^}]*)\}", STYLE_CSS)
    assert m, "blur box rule present"
    declarations = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    assert "min-height:" not in declarations


# ── M8(i): Reset Layout clears the touched flag ────────────────────

def test_reset_layout_clears_blur_box_touched():
    block = _function_block("function resetRecapCanvasLayout() {", span=2400)
    assert "state.recapBlurBoxTouched = false;" in block


def test_per_source_reset_still_clears_blur_box_touched():
    block = _function_block("function resetBlurBoxForNewSource() {", span=1600)
    assert "state.recapBlurBoxTouched = false;" in block


# ── M2 residual: grip-only title handle ────────────────────────────

def test_title_handle_paints_no_text_span():
    assert 'id="preview-title-text-el"' not in INDEX_HTML
    assert "preview-title-text" not in INDEX_HTML
    assert ".preview-title-text" not in STYLE_CSS
    # Batch DD (Ulfa's order 2026-11-09): the title feature was removed
    # entirely — the grip box is gone too.
    assert 'id="preview-title-box"' not in INDEX_HTML
    assert "title-grip" not in INDEX_HTML


# ── M3/M4 residuals: loading state + source label ──────────────────

def test_canvas_loading_and_source_label_markup():
    assert 'id="canvas-preview-loading"' in INDEX_HTML
    assert 'id="canvas-source-label"' in INDEX_HTML
    assert 'data-i18n="recap_preview_preparing"' in INDEX_HTML
    assert 'data-i18n="recap_canvas_source_label"' in INDEX_HTML
    assert ".canvas-preview-loading" in STYLE_CSS
    assert ".canvas-source-label" in STYLE_CSS


def test_new_i18n_keys_exist_in_both_locales():
    for key in ("recap_preview_preparing", "recap_canvas_source_label"):
        assert I18N_JS.count(f"{key}:") == 2, key


def test_loading_state_wired_to_first_composite():
    block = _function_block("async function refreshRecapCompositePreview() {",
                            span=2200)
    assert "const showLoading = !state.recapCompositeUrl;" in block
    assert "setRecapPreviewLoading(true)" in block
    assert "setRecapPreviewLoading(false)" in block
    assert "function setRecapPreviewLoading(on)" in APP_JS


def test_source_label_only_on_video_branch():
    # Batch AS: span widened 14000 → 20000 — updateCanvasMockupVisuals
    # grew (AS-2 stage authority + AS-3a mirror strip + composite
    # onload); the assertions below are unchanged.
    block = _function_block("function updateCanvasMockupVisuals() {",
                            span=20000)
    assert 'getElementById("canvas-source-label")' in block
    # Default-hidden for every surface, shown only where the raw
    # <video> branch becomes the visible surface.
    assert 'sourceLabel.classList.add("hidden")' in block
    assert 'sourceLabel.classList.remove("hidden")' in block
    video_branch = block.index('canvasVideo.classList.contains("hidden")')
    shown = block.index('sourceLabel.classList.remove("hidden")')
    assert video_branch < shown


# ── S9 cosmetic: blur sliders greyed while the toggle is OFF ───────

def test_blur_sliders_grey_helper_and_callsites():
    assert "function syncBlurSlidersEnabled()" in APP_JS
    block = _function_block("function syncBlurSlidersEnabled()", span=900)
    for slider in ("slider-blur-y", "slider-blur-h", "slider-blur-x",
                   "slider-blur-w", "slider-blur-strength"):
        assert slider in block, slider
    assert 'classList.toggle("blur-off", off)' in block
    # Called from the toggle handler and from the restore sync.
    assert _handler_block('getElementById("btn-toggle-blur")').count(
        "syncBlurSlidersEnabled();") == 1
    assert APP_JS.count("syncBlurSlidersEnabled();") >= 2


def test_blur_off_grey_rule_is_display_only():
    m = re.search(r"\.slider-group\.blur-off \{([^}]*)\}", STYLE_CSS)
    assert m, "blur-off rule present"
    assert "opacity: 0.45" in m.group(1)
    assert "pointer-events: none" in m.group(1)


def test_blur_payload_master_gate_unchanged():
    # While OFF the sliders already cannot affect the job: the submit
    # payload and the composite payload both send the toggle, and the
    # values only ride along under it. AM changes display only.
    assert APP_JS.count("blur_subtitles: state.recapBlurSubs !== false,") == 2


# ── Cache token ────────────────────────────────────────────────────

def test_cache_token_r64():
    assert INDEX_HTML.count("?v=20261118_r164") == 4
    assert "r63" not in INDEX_HTML
