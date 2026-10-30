"""Recap Live Preview control-visibility tests
(fix-recap-preview-controls-2026-10-30).

Owner's complaint: the Live Preview canvas's adjustment controls (blur /
mask, subtitle position, style, seek …) were visible BEFORE any source
video was loaded, which looked untidy. Now the preview-dependent UI is
driven by a small state machine:

- NO SOURCE  -> the Step-2 card shows a tidy empty placeholder; the
  canvas mockup, the drag hint and the whole canvas-toolbar group in the
  settings accordion stay hidden.
- LOADING    -> extract/upload in flight: placeholder shows a loading
  state, controls still hidden.
- READY      -> source metadata/preview exists: placeholder hides, the
  canvas mockup + hint + toolbar group reveal.
- Clearing the source returns to NO SOURCE.

These tests pin the markup, the wiring and the copy. Visibility only —
no generation payload or default may change here.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC / "js" / "app.js").read_text(encoding="utf-8")


def _i18n() -> str:
    return (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC / "css" / "style.css").read_text(encoding="utf-8")


def test_empty_state_markup_and_preview_starts_hidden():
    html = _html()
    # Tidy placeholder with empty + loading inners.
    assert 'id="recap-preview-empty"' in html
    assert 'id="recap-preview-empty-inner"' in html
    assert 'id="recap-preview-loading-inner"' in html
    assert 'data-i18n="recap_preview_empty_title"' in html
    assert 'data-i18n="recap_preview_empty_desc"' in html
    assert 'data-i18n="recap_preview_loading"' in html
    # The canvas mockup and the drag hint ship hidden (no source yet).
    assert 'class="mockup-screen-container mt-2 hidden" id="recap-mockup-wrap"' in html
    assert 'class="hint-tag hidden" id="preview-hint-tag"' in html


def test_toolbar_group_starts_hidden_but_other_settings_do_not():
    html = _html()
    # The canvas toolbar group (seek/mask/style) is preview-dependent.
    assert 'id="recap-preview-controls-group"' in html
    group_tag = html.split('id="recap-preview-controls-group"')[0].rsplit("<div", 1)[1]
    assert "hidden" in group_tag
    # Non-preview settings stay reachable without a source: the language
    # picker group must NOT be swept into the hidden set.
    assert 'id="recap-lang-picker"' in html
    lang_card = html.split('id="recap-lang-picker"')[0].rsplit('class="card glass settings-group', 1)[1]
    assert 'recap-preview-controls-group' not in lang_card


def test_state_machine_toggles_all_preview_surfaces():
    js = _js()
    assert "function setRecapPreviewState(mode)" in js
    assert "window.setRecapPreviewState = setRecapPreviewState;" in js
    # One decision point drives every preview-dependent surface.
    for el in ("recap-preview-empty", "recap-mockup-wrap",
               "preview-hint-tag", "recap-preview-controls-group"):
        assert f'getElementById("{el}")' in js
    assert 'controlsGroup.classList.toggle("hidden", !ready)' in js
    assert 'mockupWrap.classList.toggle("hidden", !ready)' in js
    # Setup initialises the machine in the empty state.
    assert 'setRecapPreviewState("empty");' in js


def test_loading_and_ready_wired_on_extract_and_upload():
    js = _js()
    # Both source paths pass through LOADING and land on READY.
    assert js.count('setRecapPreviewState("loading");') >= 2
    assert js.count('setRecapPreviewState("ready");') >= 2
    # Extract start -> loading happens after the button spinner is set.
    assert 'btnAnalyze.innerText = "⏳...";\n            setRecapPreviewState("loading");' in js
    # Upload success -> ready right at the "Video Ready" point.
    assert '"✅ Video Ready for 1080p Recap!";\n            setRecapPreviewState("ready");' in js


def test_clearing_or_failing_source_returns_to_empty():
    js = _js()
    # Failures and URL-clearing re-evaluate from the actual source state.
    assert js.count('setRecapPreviewState(recapHasSource() ? "ready" : "empty");') >= 3
    assert "function recapHasSource()" in js
    assert "state.recapThumbnailUrl" in js and "state.uploadedVideoPath" in js


def test_empty_state_css_and_reveal_animation_exist():
    css = _css()
    assert ".preview-empty-state {" in css
    assert ".preview-empty-inner {" in css
    assert ".preview-reveal {" in css
    assert "@keyframes previewRevealIn" in css


def test_preview_state_i18n_keys_exist_in_both_languages():
    i18n = _i18n()
    for key in ("recap_preview_empty_title:", "recap_preview_empty_desc:",
                "recap_preview_loading:"):
        assert i18n.count(key) == 2, key
