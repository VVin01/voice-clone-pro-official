"""Recap tab tidy-up (branch fix/recap-tab-tidy, 2026-11-01).

Owner verdict after using the Recap tab himself: messy, not premium.
The audit inventory (recap-tab-audit-2026-11-01/AUDIT_INVENTORY.md) +
the owner's premium/hierarchy directive + the adopted Antigravity
second opinion drive this batch. Presentation only — every control
keeps its id, handler and submitted values; no pipeline/API change.

Pins:
- the calm layout: engine in Step 3, canvas toolbar + ratio docked in
  Step 2, ONE story-style picker, ONE crop-presets set, Advanced as
  flattened hairline sections in one fixed order;
- all 12 persona voices as cards (no duplicated <select> picker);
- the single visible CTA (sticky) with the in-flow button retired to
  a hidden mirror source once the observer is live;
- one switch component, uniform pill tiles, Western digits for split
  minutes, clean subtitle sample, single-icon preview labels;
- i18n completeness for the new keys in BOTH languages.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")

VOICE_SLUGS = ["komin", "kozayar", "koaung", "mahnin", "kothura", "komyat",
               "kophone", "manadi", "mamay", "mayoon", "machaw", "mathiri"]


def _translations() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = """
global.localStorage = { getItem: () => null, setItem: () => {} };
global.document = { querySelectorAll: () => [], documentElement: {}, body: { classList: { toggle: () => {} } }, getElementById: () => null };
global.window = {};
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const translations = eval(src + '; translations;');
console.log(JSON.stringify(translations));
"""
    out = subprocess.run([node, "-e", script, str(STATIC / "js" / "i18n.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


# ── Layout: the calm 3-step path ─────────────────────────────────────

def test_engine_picker_lives_in_step3_voice_card():
    step3 = HTML.index('data-i18n="recap_step2_title"')
    advanced = HTML.index('id="recap-advanced-accordion"')
    engine = HTML.index('id="recap-engine-picker"')
    gemini = HTML.index('id="recap-gemini-voice-wrap"')
    assert step3 < engine < advanced
    assert step3 < gemini < advanced
    assert 'id="recap-persona-area"' in HTML


def test_canvas_toolbar_and_ratio_docked_in_step2():
    step2 = HTML.index('data-i18n="preview_label"')
    step3 = HTML.index('data-i18n="recap_step2_title"')
    for el in ('id="recap-preview-ratio-group"', 'id="recap-mockup-wrap"',
               'id="recap-preview-controls-group"', 'id="canvas-no-thumb"'):
        assert step2 < HTML.index(el) < step3, el
    # Logo picker moved into the toolbar Style tab (one home, by the canvas).
    assert step2 < HTML.index('id="recap-logo-pos-picker"') < step3
    assert 'data-i18n="recap_specs_title"' in HTML


def test_advanced_sections_fixed_order_and_flattened():
    frame = HTML.index('id="recap-adv-section-frame"')
    speed = HTML.index('id="recap-adv-section-speed"')
    other = HTML.index('id="recap-adv-section-other"')
    assert frame < speed < other
    assert HTML.index('id="recap-preview-transform-group"') > frame
    assert HTML.index('id="recap-lang-picker"') > other
    assert HTML.index('id="recap-split-toggle"') > other
    # No nested .card.glass groups remain inside the accordion body.
    body = HTML.split('id="recap-advanced-body"', 1)[1].split('id="btn-start-recap"', 1)[0]
    assert "card glass" not in body


def test_story_style_picker_has_one_home_in_step3():
    assert HTML.count('id="story-style-picker-quick"') == 1
    step3 = HTML.index('data-i18n="recap_step2_title"')
    advanced = HTML.index('id="recap-advanced-accordion"')
    assert step3 < HTML.index('id="story-style-picker-quick"') < advanced


def test_crop_presets_have_one_home_in_toolbar():
    assert 'id="recap-crop-presets"' not in HTML
    assert HTML.count('id="recap-crop-presets-toolbar"') == 1
    assert "recap-crop-presets-toolbar" in APP_JS  # sync still targets the survivor


# ── Voices: all 12 as cards, no duplicated select ────────────────────

def test_all_voices_expanded_is_twelve_cards_no_select():
    assert 'id="recap-voice-select"' not in HTML
    assert 'id="btn-preview-recap-voice"' not in HTML
    grid = HTML.split('id="recap-all-voices-grid"', 1)[1].split('id="recap-advanced-accordion"', 1)[0]
    slugs = re.findall(r'data-voice="(\w+)"', grid)
    assert sorted(slugs) == sorted(VOICE_SLUGS)
    for slug in VOICE_SLUGS:
        assert f"previewVoiceSlug('{slug}', event)" in grid
    # Top-4 grid unchanged.
    top = HTML.split('id="recap-top-voices"', 1)[1].split('id="all-voices-dropdown-wrap"', 1)[0]
    assert sorted(re.findall(r'data-voice="(\w+)"', top)) == sorted(["komin", "kozayar", "koaung", "mahnin"])
    # One JS handler wires both grids and syncs by slug.
    assert '"#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card"' in APP_JS
    assert 'getElementById("recap-persona-area")' in APP_JS


def test_gemini_select_full_width_with_auto_hint():
    m = re.search(r'<select id="recap-gemini-voice-select"[^>]*>(.*?)</select>', HTML, re.S)
    assert m
    first = re.search(r'<option value="">([^<]*)</option>', m.group(1))
    assert first and first.group(1) == "Auto"
    assert 'data-i18n="recap_gemini_auto_hint"' in HTML
    assert 'class="studio-select recap-gemini-select"' in HTML


# ── CTA: one visible (sticky), in-flow retired by the observer ───────

def test_single_visible_cta_mechanism():
    assert 'id="btn-start-recap"' in HTML
    assert 'id="btn-start-recap-sticky"' in HTML
    assert 'src.classList.add("cta-source-hidden")' in APP_JS
    assert ".cta-source-hidden { display: none !important; }" in CSS
    assert "#tab-recap { padding-bottom: 120px; }" in CSS
    # Rights note sits below the CTA wrap as a small muted note (class, no inline style).
    assert 'class="rights-note recap-rights-note"' in HTML
    assert "opacity:0.85; text-align:left" not in HTML


# ── Canvas: placeholder + broken-image guards ────────────────────────

def test_canvas_no_thumbnail_placeholder_wiring():
    assert 'id="canvas-no-thumb"' in HTML
    assert 'data-i18n="recap_canvas_no_thumb"' in HTML
    assert "thumbImg.onerror" in APP_JS
    # A thumbnail URL that fails to load repaints as no-media, so the
    # placeholder (not the letterbox seams) shows in every ratio.
    assert 'thumbImg.dataset.failed = "1"' in APP_JS
    assert 'getElementById("canvas-no-thumb")' in APP_JS
    assert ".canvas-no-thumb {" in CSS


def test_checking_state_has_shimmer_and_unverified_hint():
    assert 'class="preview-progress-shimmer"' in HTML
    assert 'data-i18n="recap_preview_unverified_hint"' in HTML
    assert "@keyframes previewShimmer" in CSS


# ── Components: one switch, uniform pills, compact ratio ─────────────

def test_one_switch_component():
    assert '<span class="switch-track" aria-hidden="true"></span>' in HTML
    assert ".toggle-pill-btn.active .switch-track" in CSS
    assert '.script-review-row input[type="checkbox"]' in CSS
    assert "appearance: none" in CSS
    assert ".mini-toggle.active .t-val" in CSS
    # Mirror no longer uses the rogue green active accent.
    assert ".toggle-pill-btn.active { background: var(--gold-soft)" in CSS


def test_uniform_pill_tiles_and_compact_ratio():
    for sel in ("#story-style-picker-quick .style-select-pill",
                "#recap-logo-pos-picker .pill-btn",
                "#recap-crop-presets-toolbar .pill-btn",
                "#sub-style-picker .style-chip"):
        assert sel in CSS, sel
    assert "flex: 1 1 42%" in CSS and "flex: 1 1 26%" in CSS
    assert ".ratio-btn { min-height: 44px" in CSS


def test_step1_tutorial_pill_has_own_row():
    assert 'class="step1-tut-row"' in HTML
    i = HTML.index('data-i18n="recap_step1_title"')
    j = HTML.index('id="recap-step1-tut-btn"')
    assert i < j
    header = HTML[i:j]
    assert "step1-tut-row" in header  # button no longer shares the title row


def test_crop_sliders_nested_one_level_deeper():
    assert 'id="recap-crop-detail"' in HTML
    assert 'onclick="toggleRecapCropDetail()"' in HTML
    assert "window.toggleRecapCropDetail = function()" in APP_JS
    assert HTML.index('id="recap-crop-detail"') < HTML.index('id="slider-crop-top"')


# ── Copy / i18n hygiene ──────────────────────────────────────────────

def test_new_i18n_keys_exist_in_both_languages():
    tr = _translations()
    for key in ("recap_preview_unverified_hint", "recap_gemini_auto_hint",
                "recap_canvas_no_thumb", "recap_specs_title",
                "recap_sub_lines_label", "recap_sub_size_label",
                "recap_crop_presets_label", "recap_adv_other_title"):
        assert tr["my"][key] and tr["en"][key], key
        assert tr["my"][key] != tr["en"][key], key


def test_single_speaker_icon_and_clean_sample():
    tr = _translations()
    for lang in ("my", "en"):
        assert not tr[lang]["btn_preview_voice"].startswith("🔊")
        sample = tr[lang]["sub_sample_text"]
        assert '"' not in sample and "…" not in sample
    # The canvas preview renderer reads the sample from i18n now.
    assert APP_JS.count('previewText.innerHTML = t("sub_sample_text");') == 2


def test_split_minutes_use_western_digits():
    tr = _translations()
    assert tr["my"]["recap_split_min_2"] == "2 မိနစ်"
    assert tr["my"]["recap_split_min_3"] == "3 မိနစ်"
    assert tr["my"]["recap_split_min_5"] == "5 မိနစ်"
    assert ">2 မိနစ်<" in HTML and ">3 မိနစ်<" in HTML and ">5 မိနစ်<" in HTML


def test_cache_buster_bumped_for_tidy():
    # rt4 = full-app polish batch (2026-11-01) superseding rt3.
    # fp1 = admin-demote + voice-picker batch (2026-11-01 late)
    # superseding rt4 (index.html / app.js / style.css changed again).
    assert HTML.count("?v=20261102_rs2") == 3
    assert "?v=20261101_rt4" not in HTML
    assert "?v=20261101_rt3" not in HTML
    assert "?v=20261101_rt2" not in HTML
    assert "?v=20261101_rt1" not in HTML
    assert "?v=20261101_gv1" not in HTML


# ── Return pass: no-media blur label + toolbar tab fit ──────────────

def test_no_media_blur_box_is_label_less():
    # updateCanvasMockupVisuals tracks whether real media is actually
    # displayed; only the video and live-thumbnail branches count.
    assert APP_JS.count("mediaVisible = true;") == 2
    # In no-media states (empty preview / dead thumbnail) the blur box
    # renders as a slim, dimmed outline (the F1 scheme below hides its
    # label by default in every state): at the shared default Y=85%
    # its label text otherwise stacks onto the subtitle sample.
    assert 'blurBoxEl.classList.toggle("no-media", !mediaVisible)' in APP_JS
    assert ".preview-blur-box.no-media { background: rgba(120, 130, 150, 0.16); }" in CSS
    # Presentation only: the box and its state defaults are untouched.
    assert 'id="preview-blur-box" style="top: 85%; height: 7%;' in HTML
    assert 'data-i18n="blur_box_label"' in HTML


# ── Return pass 2 (real-source findings F1–F4) ──────────────────────

def test_blur_label_paints_only_while_editing():
    # F1: the collision was a MEDIA-state defect too — the label is an
    # editing affordance, hidden by default in every state...
    m = re.search(r"\.preview-blur-box \.blur-label \{([^}]*)\}", CSS)
    assert m and "display: none" in m.group(1)
    # ...and shown only in its editing contexts: Mask toolbar tab
    # active, the box being dragged/resized, or hover.
    for sel in (".preview-blur-box.label-on .blur-label",
                ".preview-blur-box.is-dragging .blur-label",
                ".preview-blur-box:hover .blur-label"):
        assert sel in CSS, sel
    # Shown, it rides just above the blur band so it never shares the
    # subtitle sample's centred text line at the shared default Y=85%.
    shown = re.search(
        r"\.preview-blur-box\.label-on \.blur-label,\s*"
        r"\.preview-blur-box\.is-dragging \.blur-label,\s*"
        r"\.preview-blur-box:hover \.blur-label \{([^}]*)\}", CSS)
    assert shown and "position: absolute" in shown.group(1)
    assert shown and "bottom: calc(100% + 1px)" in shown.group(1)
    # The Mask tab handler drives .label-on; resizing reuses the
    # drag class so the label paints for the duration.
    assert 'previewBlur.classList.toggle("label-on", target === "mask")' in APP_JS
    assert 'previewBlur.classList.add("is-dragging")' in APP_JS
    assert 'previewBlur.classList.remove("is-dragging")' in APP_JS


def test_thumbnail_branch_uses_blurred_backdrop_layer():
    # F2/F3: the thumbnail branch fills the SAME blurred backdrop layer
    # as the video branch (one centred inset over a blurred cover
    # backdrop in every ratio) — never the sharp canvas-cover slices.
    assert APP_JS.count("backdropEl.style.backgroundImage = `url('${state.recapThumbnailUrl}')`") == 2
    assert "canvas.style.backgroundImage = `url(" not in APP_JS


def test_thumbnail_aspect_fit_and_refit_on_load():
    # F2: with no reported source dimensions, the inset fits the loaded
    # thumbnail's natural aspect instead of the canvas-opposite guess.
    assert "loadedThumb.naturalWidth / loadedThumb.naturalHeight" in APP_JS
    assert "const rCropped = rFit * (keepW / keepH);" in APP_JS
    # ...and refits exactly once per image when it finishes loading.
    assert "thumbImg.dataset.aspect" in APP_JS


def test_mirror_flips_backdrop_and_inset_together():
    # F3: the render applies hflip BEFORE its bg/fg split
    # (core.py _visual_filters), so the mockup flips both layers with
    # Mirror on and neither with it off; overlays stay unflipped.
    assert ".video-bg-placeholder.mirrored .mockup-inner-movie" in CSS
    assert ".video-bg-placeholder.mirrored .video-backdrop-blur { transform: scaleX(-1); }" in CSS


def test_placeholder_composes_in_short_frames():
    # F4: in 16:9 (and the Original fallback shape) the no-thumbnail
    # placeholder shrinks and lifts off the drag-box band.
    assert ".mockup-screen.ratio-16-9 .canvas-no-thumb" in CSS
    assert ".mockup-screen.ratio-16-9 .canvas-no-thumb-icon { font-size: 24px; }" in CSS


def test_toolbar_tabs_single_line_equal_height():
    btn = re.search(r"\.tb-tab-btn \{([^}]*)\}", CSS)
    assert btn and "white-space: nowrap" in btn.group(1)
    assert btn and "align-items: center" in btn.group(1)
    # Mask label shortened so all three tabs fit one line at 390px
    # (no ellipsis-clipped Burmese anywhere).
    assert "<span>🌫️ Blur စာတန်း</span>" in HTML
    assert "Blur & စာတန်း" not in HTML
