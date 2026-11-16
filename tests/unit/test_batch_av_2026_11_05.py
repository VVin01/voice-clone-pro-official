"""Batch AV — Recap tab step-flow redesign (owner-approved 2026-11-05).

The Recap tab becomes four collapsible steps under a step indicator
(1 Source ထည့်သွင်းပါ → 2 Preview → 3 Voice → 4 Create). The redesign
is a DOM relocation ONLY: every pre-AV control id must still exist
(except the documented structural removals below), the submit payload
keeps its exact 42 keys, and the prefs keys are untouched.

Batch AX note (2026-11-06): the owner rejected the stacked accordion
this file's layout pins described. The pins that asserted the stacked
model (summary-strip presence, toggle/open functions, strip + label
CSS guards, token) were rewritten in place citing the AX spec; the
inventory / payload / prefs / placement pins below are UNCHANGED —
the wizard removes no control the AV inventory protected (only the
recap-summary-1..4 strips, which AX documents as its own removal).

Documented removals (spec + parent ground-truth addendum):
- tab-btn-seek / tab-btn-mask / tab-btn-style: the toolbar tab nav is
  replaced by a visible seek cluster + frequent row + the fine
  expandable; the tab PANELS' contents all survive in new homes.
- tb-panel-mask / tb-panel-style: panel wrappers dissolved so each
  control group is coherent (subtitle group, blur group, logo group);
  tb-panel-seek survives as the consolidated seek cluster.
- recap-advanced-accordion / btn-toggle-advanced-recap /
  recap-advanced-body / accordion-chevron / recap-adv-section-frame /
  recap-adv-section-speed / recap-adv-section-other: the advanced
  accordion is replaced by the step structure; every section's
  contents moved into Steps 2/3/4.
- recap-step1-tut-btn: duplicate of the header 📚 guide that exits the
  app (parent dedupe) — the header guide remains.
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

TAB = INDEX_HTML[INDEX_HTML.index('id="tab-recap"'):INDEX_HTML.index('id="tab-downloader"')]

# Every id that existed in the Recap tab before AV (measured on main
# accad5f2: 200 ids) minus the documented removals above.
REMOVED_IDS = {
    "tab-btn-seek", "tab-btn-mask", "tab-btn-style",
    "tb-panel-mask", "tb-panel-style",
    "recap-advanced-accordion", "btn-toggle-advanced-recap",
    "recap-advanced-body", "accordion-chevron",
    "recap-adv-section-frame", "recap-adv-section-speed",
    "recap-adv-section-other",
    "recap-step1-tut-btn",
    # Batch DD: the title feature's controls are removed for good.
    "preview-title-box", "title-pos-sliders", "slider-title-x",
    "val-title-x", "slider-title-y", "val-title-y",
    "recap-title-controls", "recap-title-toggle", "recap-title-text",
    "recap-title-fallback-hint",
    # Batch DD: the font-size pill toolbar became a slider row.
    "font-size-picker-toolbar",
}

SURVIVING_IDS = [
    "preview-hint-tag", "recap-preview-empty", "recap-preview-empty-inner",
    "recap-preview-loading-inner", "recap-mockup-wrap", "recap-mockup-frame",
    "video-bg-canvas", "video-backdrop-blur", "mockup-inner-movie",
    "canvas-video-player", "canvas-composite-img", "crop-shade-top",
    "crop-shade-bottom", "crop-shade-left", "crop-shade-right",
    "canvas-no-thumb", "canvas-preview-loading", "canvas-source-label",
    "preview-logo", "preview-blur-box", "preview-sub-box",
    "mockup-fill-badge",
    "recap-preview-ratio-group", "recap-preview-controls-group",
    "canvas-compact-toolbar", "tb-panel-seek", "canvas-seek-time-badge",
    "slider-canvas-seek", "btn-seek-prev-5", "btn-canvas-play-toggle",
    "btn-seek-next-5", "btn-toggle-subs", "btn-toggle-blur", "btn-toggle-logo",
    "slider-blur-y", "val-blur-y", "slider-blur-h", "val-blur-h",
    "slider-blur-x", "val-blur-x", "slider-blur-w", "val-blur-w",
    "slider-blur-strength", "val-blur-strength",
    "slider-sub-y", "val-sub-y", "slider-sub-x", "val-sub-x",
    "slider-sub-w", "val-sub-w",
    "btn-reset-canvas-layout",
    # Batch DD: font size is a full-width slider row now.
    "sub-style-picker", "sub-lines-picker-toolbar", "slider-font-size",
    "val-font-size",
    "recap-sub-font-picker", "recap-quality-picker",
    "recap-crop-presets-toolbar", "recap-logo-pos-picker",
    "recap-logo-manager", "recap-logo-controls", "recap-logo-thumb",
    "btn-logo-upload", "btn-logo-remove", "input-logo-file",
    "recap-logo-vip-hint", "recap-logo-status",
    "tab-recap", "recap-demo-card", "btn-recap-demo",
    "btn-fetch-recap-info", "btn-source-mode-file", "btn-source-mode-url",
    "panel-source-url", "panel-source-file", "recap-drop-text",
    "recap-video-drop-zone", "recap-free-terms-note",
    "recap-upload-progress", "recap-upload-bar", "recap-upload-label",
    "recap-upload-elapsed", "recap-upload-cancel", "recap-upload-hint",
    "recap-upload-error", "recap-upload-error-text", "recap-upload-error-hint",
    "recap-upload-retry", "recap-url-input",
    "recap-video-file-input", "recap-video-info-box", "recap-info-thumb",
    "recap-info-title", "recap-info-dur", "recap-info-platform",
    "recap-info-sub", "recap-limit-pill", "recap-tier-limit-text",
    "recap-ratio-picker", "recap-fill-picker", "recap-fill-note",
    "recap-engine-picker", "recap-gemini-voice-wrap",
    "recap-gemini-voice-select", "btn-preview-gemini-voice",
    "recap-persona-area", "recap-top-voices", "btn-toggle-all-voices",
    "all-voices-toggle-text", "all-voices-dropdown-wrap",
    "recap-all-voices-grid", "story-style-picker-quick",
    "recap-preview-transform-group", "btn-toggle-mirror", "recap-zoom-picker",
    "btn-toggle-crop-detail", "crop-detail-chevron", "recap-crop-detail",
    "btn-sync-crop", "slider-crop-top", "val-crop-top",
    "slider-crop-bottom", "val-crop-bottom", "slider-crop-left",
    "val-crop-left", "slider-crop-right", "val-crop-right",
    "recap-preview-speed-group", "recap-speed-picker",
    # Batch AZ-11: "recap-lang-picker" (the AX-5 pill group) is
    # retired — the surviving language control is "recap-lang-more".
    "recap-lang-more", "recap-split-toggle", "recap-split-minutes-wrap",
    "recap-split-minutes-picker", "recap-split-cost-hint",
    "recap-script-review-toggle",
    "btn-start-recap", "recap-unverified-warning", "prefs-saved-note",
    "recap-inline-status", "recap-inline-status-text",
    "recap-progress-box", "recap-spinner", "recap-status-msg",
    "recap-stage-stepper", "recap-progress-fill", "recap-step-text",
    "btn-recap-cancel", "recap-script-box", "recap-script-list",
    "btn-script-save", "btn-script-render", "btn-script-cancel",
    "recap-result-box",
    "recap-result-player", "btn-recap-dl-video", "btn-recap-send-telegram",
    "recap-oversize-note", "btn-recap-dl-audio", "btn-recap-dl-srt",
    "btn-recap-dl-script", "recap-parts-wrap", "recap-parts-list",
    "recap-split-note", "recap-blur-warning", "recap-fallback-note",
    "recap-cost-line", "recap-expiry-note", "recap-failed-box",
    "recap-failed-error", "recap-failed-refund", "btn-recap-retry",
    "recap-retry-note", "recap-retry-upload-missing",
    "studio-alert-carousel", "carousel-track", "carousel-dots",
    "btn-carousel-prev", "btn-carousel-next",
]

PAYLOAD_KEYS = [
    "url", "title", "voice_slug", "story_style", "ratio", "ratio_fill",
    "crop_top", "crop_bottom", "crop_left", "crop_right", "mirror_video",
    "zoom_factor", "voice_speed", "blur_subtitles", "blur_y", "blur_h",
    "blur_x", "blur_w", "blur_box_explicit", "blur_strength",
    # Batch BI deliberate pin update: the auto-detect blur switch
    # rides the submit payload as blur_auto_detect.
    "blur_auto_detect",
    # Batch DD deliberate pin update: the four title keys are
    # gone; sub_y joined (Top/Center position bands); the DD
    # settings ride the ddConfigExtras() spread after sub_lines.
    "burn_subtitles", "sub_bottom_margin", "sub_y", "sub_x", "sub_w", "sub_style",
    "sub_font_size", "sub_font", "sub_lines", "include_logo", "logo_pos",
    "duration_seconds", "script_review", "source_language",
    "render_quality", "tts_engine", "gemini_voice",
    # Batch BV deliberate pin update: the own-voice sample handle
    # rides the submit payload as voice_ref_id.
    "voice_ref_id",
    "split_video",
    "split_minutes",
]

NEW_I18N_KEYS = [
    "recap_step_nav_preview", "recap_step_nav_voice", "recap_step4_title",
    "recap_step_word", "recap_lang_more_label",
    "recap_fine_title", "recap_step_continue", "recap_summary_no_source",
    "recap_step4_quote_label", "recap_subtitle_group_title",
    "recap_blur_group_title",
]


def _step_slice(n: int, end: int | None = None) -> str:
    start = TAB.index(f'id="recap-step-{n}"')
    if end is None:
        end = TAB.index(f'id="recap-step-{n + 1}"') if n < 4 else len(TAB)
    return TAB[start:end]


# ── Inventory: nothing lost, removals are exactly the documented set ──

def test_av_every_surviving_control_id_still_exists():
    missing = [i for i in SURVIVING_IDS if f'id="{i}"' not in TAB]
    assert missing == [], f"controls lost in the AV restructure: {missing}"


def test_av_documented_removals_are_absent():
    present = [i for i in REMOVED_IDS if f'id="{i}"' in TAB]
    assert present == [], f"documented removals still present: {present}"


def test_av_no_duplicate_ids_in_tab():
    ids = re.findall(r'id="([^"]+)"', TAB)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert dupes == [], f"duplicate ids after restructure: {dupes}"


def test_av_submit_payload_keys_unchanged():
    i = APP_JS.index("const payload = {")
    depth, j = 0, i
    while True:
        c = APP_JS[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    keys = re.findall(r"^\s+([a-z_]+):", APP_JS[i:j], re.M)
    assert keys == PAYLOAD_KEYS


def test_av_prefs_keys_unchanged():
    # Batch AX rewrite (AX-3R): the vcp_recap_prefs key's ONLY app.js
    # reference was AV's completion read — the key was never written
    # by any code (checked: it exists in no other file either), so
    # removing that read retires a dead key, it does not change a
    # live prefs key. The live settings key is asserted in its real
    # form; the AX file pins the full story.
    assert 'RECAP_SETTINGS_KEY = "vcp_recap_settings"' in APP_JS
    assert 'localStorage.getItem("vcp_recap_prefs")' not in APP_JS


# ── Step structure and deliberate placement ──

def test_av_step_structure_and_nav():
    # Batch AX rewrite (owner direction 2026-11-06): the AV stacked
    # model — four rendered cards whose bodies collapse to summary
    # strips — is replaced by the one-step-at-a-time wizard. The
    # summary strips are REMOVED (asserted absent here and owned by
    # test_batch_ax_2026_11_06); the chips carry number/✓ only; steps
    # 2–4 start hidden so exactly one card is visible in markup state.
    for n in (1, 2, 3, 4):
        assert f'id="recap-step-{n}"' in TAB
        assert f'id="recap-step-{n}-body"' in TAB
        assert f'id="recap-summary-{n}"' not in TAB
        assert f'data-step-toggle="{n}"' in TAB
        assert f'data-step-pill="{n}"' in TAB
    assert 'id="recap-steps-nav"' in TAB
    positions = [TAB.index(f'id="recap-step-{n}"') for n in (1, 2, 3, 4)]
    assert positions == sorted(positions), "steps must render in order 1→2→3→4"
    # Step 1 carries the owner-polished name.
    assert 'data-i18n="recap_step1_title">ဗီဒီယို ထည့်သွင်းပါ<' in TAB


def test_av_step2_visible_vs_detail_split():
    step2 = _step_slice(2)
    fine_at = step2.index('id="recap-fine-body"')
    visible, detail = step2[:fine_at], step2[fine_at:]
    # Phase 4 Split-Screen 5-Tab Editor:
    # Visible before fine-body: sticky preview stage + seek bar + corner reset,
    # 5-category segmented tabs, Master Presets, and Category 2 Frame/Crop group.
    for marker in ('id="recap-step2-sticky-preview"', 'id="recap-mockup-wrap"',
                   'id="btn-preview-corner-reset"', 'id="tb-panel-seek"',
                   'id="slider-canvas-seek"', 'id="btn-seek-prev-5"',
                   'id="btn-seek-next-5"', 'class="seek-presets-pills"',
                   'id="recap-step2-cat-tabs"', 'id="recap-cat-preset"',
                   'id="recap-preview-ratio-group"', 'id="recap-crop-presets-toolbar"',
                   'id="btn-sync-crop"', 'id="slider-crop-top"'):
        assert marker in visible, marker
    # Detail (#recap-fine-body): Subtitle group (all subtitle controls together).
    sub = detail[detail.index('id="recap-subtitle-group"'):detail.index('id="recap-blur-group"')]
    for marker in ('id="btn-toggle-subs"', 'id="sub-style-cards-grid"',
                   'id="sub-style-picker"', 'id="sub-lines-picker-toolbar"',
                   'id="slider-font-size"', 'id="recap-sub-font-picker"',
                   'id="slider-sub-y"', 'id="slider-sub-x"', 'id="slider-sub-w"'):
        assert marker in sub, marker
    # Blur-box group separate from subtitle group (now holds Category 4 toggle + sliders).
    blur = detail[detail.index('id="recap-blur-group"'):detail.index('id="recap-logo-group"')]
    for marker in ('id="btn-toggle-blur"', 'id="slider-blur-y"', 'id="slider-blur-h"',
                   'id="slider-blur-x"', 'id="slider-blur-w"', 'id="slider-blur-strength"'):
        assert marker in blur, marker
    assert 'id="slider-sub-y"' not in blur
    # Batch DD: there is no title group in the detail area at all.
    for marker in ('id="recap-title-toggle"', 'id="recap-title-text"',
                   'id="slider-title-x"', 'id="slider-title-y"'):
        assert marker not in detail, marker
    # Category 5 Protection & Logo groups.
    for marker in ('id="btn-toggle-logo"', 'id="recap-logo-pos-picker"',
                   'id="recap-logo-manager"', 'id="btn-toggle-mirror"',
                   'id="recap-zoom-picker"'):
        assert marker in detail, marker
    # Reset Layout remains available.
    assert 'id="btn-reset-canvas-layout"' in detail


def test_av_step3_holds_voice_speed_language_script_review():
    step3 = _step_slice(3)
    for marker in ('id="recap-engine-picker"', 'id="recap-persona-area"',
                   'id="story-style-picker-quick"', 'id="recap-speed-picker"',
                   # AZ-11: the language control in Step 3 is the one
                   # compact dropdown (the pill group is retired).
                   'id="recap-lang-more"', 'id="recap-script-review-toggle"'):
        assert marker in step3, marker


def test_av_step4_holds_quote_quality_split_and_cta():
    step4 = _step_slice(4)
    for marker in ('id="recap-step4-thumb"', 'id="recap-step4-quote-credits"',
                   'id="recap-quality-picker"', 'id="recap-split-toggle"',
                   'id="recap-split-minutes-wrap"', 'id="recap-split-cost-hint"',
                   'id="btn-start-recap"', 'id="recap-unverified-warning"'):
        assert marker in step4, marker
    # The large quote is fed by the same function that paints the CTA.
    assert APP_JS.count("setRecapStep4Quote(") >= 3


def test_av_info_tail_below_the_step_flow():
    rights_at = TAB.index("recap_rights_note")
    carousel_at = TAB.index('id="studio-alert-carousel"')
    assert TAB.index('id="recap-step-4"') < rights_at < carousel_at


def test_av_step_module_behaviour():
    # Batch AX rewrite: toggleRecapStep/openRecapStep (the collapse
    # model) are replaced by showRecapStep — the wizard's one door.
    # The AV prefs-read marker is superseded by AX-3R: the literal
    # getItem("vcp_recap_prefs") read for completion is REMOVED (it
    # was the key's only reference in the codebase — see the prefs
    # test above).
    for marker in ("function initRecapSteps(", "function showRecapStep(",
                   "function markRecapStepDone(",
                   "function toggleRecapFineSettings(", "syncRecapStepsWithSource(ready)",
                   "initRecapSteps();"):
        assert marker in APP_JS, marker
    assert 'localStorage.getItem("vcp_recap_prefs")' not in APP_JS
    assert "function toggleRecapStep(" not in APP_JS
    assert "function openRecapStep(" not in APP_JS
    # The retired accordion toggler is gone with its markup.
    assert "window.toggleAdvancedRecapStudio = function" not in APP_JS


# ── Drag handles: differentiated and off the subtitle text ──

def test_av_grips_differentiated_and_offset():
    # Batch DD: the title grip is gone with the title box.
    assert "📝 ⠿" in TAB and "🏷️ ⠿" not in TAB and "🌫️ ⠿" in TAB
    rule = re.search(r"\.preview-sub-box \.sub-grip,\s*\.preview-title-box \.title-grip \{([^}]+)\}",
                     STYLE_CSS)
    assert rule, "grip offset rule missing"
    assert "position: absolute" in rule.group(1)
    assert "translate(-50%, calc(-100% - 3px))" in rule.group(1)


# ── i18n: new strings exist in BOTH locales ──

def test_av_new_i18n_keys_in_both_locales():
    my = I18N_JS.index("recap_step1_title")
    en = I18N_JS.index('"en"') if '"en"' in I18N_JS else I18N_JS.index("en: {")
    for key in NEW_I18N_KEYS:
        assert I18N_JS.count(f"{key}:") >= 2, f"{key} missing in one locale"
    assert 'recap_step1_title: "ဗီဒီယို ထည့်သွင်းပါ"' in I18N_JS
    assert 'recap_step1_title: "Add Source"' in I18N_JS
    assert my < en  # sanity: my block precedes en block


# ── Acceptance-criteria guards (owner, 2026-11-05 23:34) ──

def test_av_touch_targets_and_text_fit_guards():
    # Batch AX rewrite: the strip headers are gone (wizard cards have
    # plain, non-interactive headers) and the nav chips carry NO text
    # labels at all — the owner's screenshot showed them squeezed and
    # clipping. Chips are ≥44px targets (AX-2); the fine header keeps
    # its 48px target; the Step-4 choices line truncates with an
    # ellipsis instead of the removed summaries.
    assert re.search(r"\.recap-steps-nav-item \{[^}]*min-height: 44px", STYLE_CSS)
    assert re.search(r"\.recap-fine-header \{[^}]*min-height: 48px", STYLE_CSS)
    assert ".recap-steps-nav-label" not in STYLE_CSS
    assert re.search(r"\.recap-step4-choice-title \{[^}]*text-overflow: ellipsis", STYLE_CSS)
    # Step-4 quote is genuinely large.
    assert re.search(r"\.recap-step4-quote-value \{[^}]*font-size: 30px", STYLE_CSS)
    # URL field uses the card's full width (flex guard + shrink-0 button).
    assert ".input-with-btn input { flex: 1; min-width: 0; }" in STYLE_CSS
    assert ".input-with-btn .btn { min-height: 50px; flex-shrink: 0; }" in STYLE_CSS
    # Cramped spot: crop label vs sync lock pill wrap instead of colliding.
    assert re.search(r"#recap-preview-transform-group \.card-header-row \{[^}]*flex-wrap: wrap",
                     STYLE_CSS)


def test_av_cache_token_bumped():
    # Token swept per batch: r69 (AV) → r70 (AX) → r71 (AY) → r72 (AZ) → r73 (AW) → r74 (BA) → r75 (BB) → r76 (BF) → r77 (BC) → r78 (BG) → r79 (BE) → r80 (BD) → r81 (BH) → r82 (BI) → r83 (BJ) → r84 (BK) → r85 (BM) → r86 (BN) → r87 (BO) → r88 (BP) → r89 (BR) → r90 (BS) → r91 (BT) → r92 (BU) → r93 (BV) → r94 (BW) → r95 (BX) → r96 (BY) → r97 (BZ) → r98 (CA) → r99 (CB) → r100 (CF) → r101 (CC) → r102 (CD) → r103 (CE) → r104 (CG) → r105 (CH) → r106 (CI) → r107 (CJ) → r108 (CK) → r109 (CL) → r110 (CN) → r111 (CO).
    assert INDEX_HTML.count("r159") == 4
    assert "r70" not in INDEX_HTML
