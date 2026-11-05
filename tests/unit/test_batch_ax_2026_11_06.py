"""Batch AX — Recap wizard: one step at a time + compact language
picker (owner direction 2026-11-06 00:21).

The owner rejected Batch AV's stacked scroll accordion from his own
screenshots of the live build: completed steps must DISAPPEAR, exactly
ONE step card shows at a time, and going back happens by tapping the
1-2-3-4 indicator. Separately: the source-language picker must not
show the full long list of 18 pills.

Invariants pinned here (spec ~/workspace/batch-ax-spec-2026-11-06.md):
- Markup state: steps 2–4 start hidden, step 1 visible — exactly one
  step card visible; showRecapStep is the one door in app.js.
- Chips carry number/✓ only — no text labels, no AV summary strips.
- Language: Auto + the six frequent languages as pills; all 18 values
  in ONE dropdown; both views write the same single state key.
- Payload (42 keys) and prefs keys unchanged (asserted in the AV file
  and re-asserted here); api.py untouched by the batch.
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
NAV = TAB[TAB.index('id="recap-steps-nav"'):TAB.index('id="prefs-saved-note"')]

ALL_LANGS = ["auto", "en", "zh", "ko", "th", "ja", "hi", "my",
             "es", "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr"]
FREQUENT_LANGS = ["my", "en", "th", "zh", "ko", "ja"]  # AX-5's six


def _card_tag(n: int) -> str:
    m = re.search(r'<div class="[^"]*" id="recap-step-%d"' % n, TAB)
    assert m, f"step card {n} missing"
    return m.group(0)


# ── AX-1: the wizard visibility model ──

def test_ax_exactly_one_step_visible_in_markup_state():
    # Step 1 is the landing card; 2–4 are hidden in markup. Navigation
    # (showRecapStep) is the only thing that changes which is shown.
    assert "hidden" not in _card_tag(1)
    for n in (2, 3, 4):
        assert "hidden" in _card_tag(n), f"step {n} must start hidden"
    # Bodies are never the hidden layer any more (AV collapsed them).
    for n in (1, 2, 3, 4):
        m = re.search(r'<div class="recap-step-body[^"]*" id="recap-step-%d-body"' % n, TAB)
        assert m and "hidden" not in m.group(0), f"step {n} body must not be hidden"


def test_ax_show_step_is_the_one_door():
    for marker in ("function showRecapStep(", "window.showRecapStep = showRecapStep;",
                   "function markRecapStepDone(", "function initRecapSteps(",
                   "function isRecapStepOpen(", "initRecapSteps();"):
        assert marker in APP_JS, marker
    # The AV collapse model is gone, not bypassed.
    for dead in ("function toggleRecapStep(", "function openRecapStep(",
                 "function setRecapStepOpen(", "function refreshRecapStepSummary(",
                 "recap-summary-"):
        assert dead not in APP_JS, dead
    # Chip taps and Continue buttons route through the one door.
    assert 'showRecapStep(parseInt(el.getAttribute("data-step-toggle"), 10))' in APP_JS
    assert "showRecapStep(target);" in APP_JS
    # Source resolve marks Step 1 complete and auto-shows Step 2.
    assert "syncRecapStepsWithSource(ready)" in APP_JS
    assert "showRecapStep(2);" in APP_JS
    # Returning users land on Step 1 (init shows it last).
    assert "showRecapStep(1);" in APP_JS


# ── AX-2: indicator chips — number/✓ only, no text labels ──

def test_ax_chips_carry_number_or_check_only():
    # No label spans, no separator glyphs from the AV nav.
    assert "recap-steps-nav-label" not in TAB
    assert "recap-steps-nav-sep" not in TAB
    assert "recap-steps-nav-label" not in STYLE_CSS
    # Chips: four real buttons, each with exactly one pill span whose
    # entire text is its step number.
    chips = re.findall(
        r'<button[^>]*data-step-toggle="(\d)"[^>]*>\s*<span class="recap-steps-nav-num" data-step-pill="\d">(\d)</span>\s*</button>',
        NAV)
    assert chips == [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4")], chips
    # No step NAME renders inside any chip (that was the cramping);
    # the names live in the card headers (4× recap_step_word) plus the
    # single indicator caption (1×).
    assert TAB.count('data-i18n="recap_step_word"') == 5
    # The ✓ state is painted by markRecapStepDone on the same spans.
    assert 'pill.textContent = done ? "✓" : String(n);' in APP_JS


def test_ax_indicator_css_states_and_touch_targets():
    item = re.search(r"\.recap-steps-nav-item \{([^}]+)\}", STYLE_CSS)
    assert item and "min-width: 44px" in item.group(1) and "min-height: 44px" in item.group(1)
    num = re.search(r"\.recap-steps-nav-num \{([^}]+)\}", STYLE_CSS)
    assert num and "border-radius: 999px" in num.group(1)
    # The AV stacked-model CSS is gone with its markup.
    for dead in (".recap-step-strip", ".recap-step-summary", ".recap-step-chevron",
                 ".recap-step-collapsed", ".recap-steps-nav-line"):
        assert dead not in STYLE_CSS, dead


# ── AX-2R: premium progress-track indicator ──

def test_ax2r_track_nodes_and_single_caption():
    # Track + gradient fill elements exist behind the four nodes.
    assert 'id="recap-steps-track-fill"' in NAV
    assert 'class="recap-steps-track-bg"' in NAV
    # ONLY the current step's name renders in the indicator area: one
    # caption line, holding word + position + name spans.
    assert 'id="recap-steps-caption"' in NAV
    assert 'id="recap-steps-caption-pos"' in NAV
    assert 'id="recap-steps-caption-name"' in NAV
    assert NAV.count("recap-steps-caption") >= 3
    # The caption is driven from showRecapStep's one door.
    assert "function refreshRecapStepIndicator()" in APP_JS
    assert 'RECAP_STEP_NAME_KEYS' in APP_JS
    assert 'name.setAttribute("data-i18n", RECAP_STEP_NAME_KEYS[recapCurrentStep]);' in APP_JS
    assert 'pos.textContent = recapCurrentStep + "/4";' in APP_JS


def test_ax2r_indicator_design_css():
    fill = re.search(r"\.recap-steps-track-fill \{([^}]+)\}", STYLE_CSS)
    assert fill and "linear-gradient(90deg, #2563eb, #60a5fa)" in fill.group(1)
    assert fill and "transition: width 0.25s ease" in fill.group(1)
    # Nodes: 36–40px circles with shadow + ring.
    num = re.search(r"\.recap-steps-nav-num \{([^}]+)\}", STYLE_CSS)
    assert num and "width: 38px" in num.group(1) and "height: 38px" in num.group(1)
    assert num and "box-shadow:" in num.group(1) and "border:" in num.group(1)
    # Completed + current = gradient fill, white glyph.
    both = re.search(r"\.recap-steps-nav-item\.is-done \.recap-steps-nav-num,\s*\.recap-steps-nav-item\.is-active \.recap-steps-nav-num \{([^}]+)\}", STYLE_CSS)
    assert both and "linear-gradient(135deg, #2563eb, #60a5fa)" in both.group(1)
    # Current step pulses gently (transform/opacity-family only: the
    # halo animates box-shadow); advance pops the node once.
    assert "@keyframes recap-step-halo" in STYLE_CSS
    assert "@keyframes recap-step-pop" in STYLE_CSS
    assert re.search(r"\.recap-steps-nav-item\.pop \.recap-steps-nav-num \{[^}]*animation: recap-step-pop", STYLE_CSS)
    # Tasteful limits: reduced-motion users get no halo/pop/fill motion.
    assert "prefers-reduced-motion: reduce" in STYLE_CSS


# ── AX-3R: completion is session-truth ──

def test_ax3r_no_completion_on_load_even_with_prefs():
    # (a) NO ✓ on load with prefs present: initRecapSteps reads no
    # prefs for completion and marks nothing done — the AV behaviour
    # (mark 2/3/4 done from vcp_recap_prefs) is gone entirely. (A
    # call-site scan over the WHOLE file, not an init slice: the
    # earners live outside init and are pinned separately below.)
    assert "markRecapStepDone(2, true)" in APP_JS  # sanity: scan works
    callers = re.findall(r"markRecapStepDone\((\d), true\)", APP_JS)
    assert sorted(callers) == ["1", "2", "3"], callers
    assert APP_JS.count('localStorage.getItem("vcp_recap_prefs")') == 0
    # Step 4 can never show ✓ — enforced inside markRecapStepDone.
    assert "if (n === 4) done = false;" in APP_JS


def test_ax3r_completion_earned_in_session_only():
    # (b) Step 1 earns ✓ ONLY from the source-resolve path this session.
    sync = APP_JS[APP_JS.index("function syncRecapStepsWithSource"):APP_JS.index("function setRecapStep4Quote")]
    assert "markRecapStepDone(1, true)" in sync
    # Steps 2/3 earn ✓ ONLY from their Continue buttons; Step 2's
    # Continue additionally requires a source present this session.
    assert "if (from === 2 && recapHasSource() && !RECAP_STEP_DONE[2]) markRecapStepDone(2, true);" in APP_JS
    assert "if (from === 3 && !RECAP_STEP_DONE[3]) markRecapStepDone(3, true);" in APP_JS
    # Every markRecapStepDone call site is one of those three earners.
    sites = re.findall(r"markRecapStepDone\((\d), true\)", APP_JS)
    assert sorted(sites) == ["1", "2", "3"], sites
    # (c) Values still pre-fill from prefs: the settings restore is
    # intact and reads the same key. (The vcp_recap_prefs key string
    # now appears only in the AV payload/prefs assertions' history —
    # the app never referenced it outside AV's removed completion
    # read; see the report's code-reality note.)
    assert "function restoreRecapSettings()" in APP_JS
    assert 'localStorage.getItem(RECAP_SETTINGS_KEY)' in APP_JS
    assert 'RECAP_SETTINGS_KEY = "vcp_recap_settings"' in APP_JS


# ── AV summary strips: removed, role moved ──

def test_ax_summary_strips_removed():
    for n in (1, 2, 3, 4):
        assert f'id="recap-summary-{n}"' not in TAB
        assert f'id="recap-summary-{n}"' not in INDEX_HTML
    # Their information role moved to the Step 4 choices line (AX-4).
    assert 'id="recap-step4-choices"' in TAB


# ── AX-4: Step 4 choices line ──

def test_ax_step4_choices_line():
    step4 = TAB[TAB.index('id="recap-step-4"'):]
    choices_at = step4.index('id="recap-step4-choices"')
    quote_at = step4.index('id="recap-step4-quote-credits"')
    assert choices_at < quote_at, "choices line sits above the credit quote"
    for marker in ('id="recap-step4-choice-title"', 'id="recap-step4-choice-voice"',
                   'id="recap-step4-choice-ratio"'):
        assert marker in step4, marker
    # Fed from the same live sources the AV summaries read (info title,
    # active voice card, active ratio card) and refreshed with the quote.
    assert "function refreshRecapStep4Choices()" in APP_JS
    assert "refreshRecapStep4Choices();" in APP_JS
    # Never overflows: flex line, title segment ellipsises.
    assert re.search(r"\.recap-step4-choices \{[^}]*overflow: hidden", STYLE_CSS)
    assert re.search(r"\.recap-step4-choice-title \{[^}]*min-width: 0", STYLE_CSS)
    assert re.search(r"\.recap-step4-choice-title \{[^}]*text-overflow: ellipsis", STYLE_CSS)


# ── AX-5: compact source-language picker ──

def _lang_block() -> str:
    return TAB[TAB.index('id="recap-lang-picker"'):TAB.index('id="recap-script-review-toggle"')]


def test_ax_language_pills_are_auto_plus_the_frequent_six():
    block = _lang_block()
    pills = block[:block.index('id="recap-lang-more"')]
    codes = re.findall(r'data-lang="([a-z]+)"', pills)
    assert codes == ["auto"] + FREQUENT_LANGS, codes
    # None of the other 11 languages renders as a pill any more.
    for code in ALL_LANGS:
        if code not in FREQUENT_LANGS and code != "auto":
            assert f'data-lang="{code}"' not in pills, code


def test_ax_language_dropdown_lists_all_eighteen():
    block = _lang_block()
    select = block[block.index('id="recap-lang-more"'):]
    values = re.findall(r'<option value="([a-z]*)"', select)
    assert values[0] == "", "first option is the affordance placeholder"
    assert values[1:] == ["auto", "my", "en", "th", "zh", "ko", "ja", "hi",
                           "es", "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr"]
    assert sorted(values[1:]) == sorted(ALL_LANGS)
    assert 'data-i18n="recap_lang_more_label"' in select


def test_ax_language_two_views_one_state():
    # Both controls write the same single state via one function; the
    # sync repaints BOTH views (pill highlight + dropdown selection),
    # so a saved out-of-frequent value displays on load.
    assert "function setRecapSourceLanguage(code)" in APP_JS
    assert "function syncRecapLangViews()" in APP_JS
    assert 'state.recapSourceLanguage = code || "auto";' in APP_JS
    assert 'langSel.addEventListener("change"' in APP_JS
    assert "setRecapSourceLanguage(langSel.value);" in APP_JS
    # The prefs-restore path re-syncs the two views too.
    assert "if (window.syncRecapLangViews) syncRecapLangViews();" in APP_JS
    # The enum of restorable values is unchanged (all 18 still valid).
    assert 'recapSourceLanguage: ["auto", "en", "zh", "ko", "th", "ja", "hi", "my", "es", "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr"]' in APP_JS
    # Payload still reads the same state field.
    assert 'source_language: state.recapSourceLanguage || "auto"' in APP_JS


def test_ax_new_i18n_keys_in_both_locales():
    for key in ("recap_step_word", "recap_lang_more_label"):
        assert I18N_JS.count(f"{key}:") >= 2, f"{key} missing in one locale"
    assert 'recap_step_word: "အဆင့်"' in I18N_JS
    assert 'recap_step_word: "Step"' in I18N_JS
    assert 'recap_lang_more_label: "More languages…"' in I18N_JS


# ── Hard invariants: payload + prefs + token ──

def test_ax_payload_and_prefs_invariants_hold():
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
    assert len(keys) == 42, keys
    assert keys[0] == "url" and keys[-1] == "split_minutes"
    assert 'RECAP_SETTINGS_KEY = "vcp_recap_settings"' in APP_JS


def test_ax_cache_token_r70():
    assert INDEX_HTML.count("20261104_r70") == 4
    assert "20261104_r69" not in INDEX_HTML


# ── AX-4R: Step 4 shows the expected OUTPUT before submit ──────────

def test_ax4r_output_card_is_step4s_opening_surface():
    step4 = TAB[TAB.index('id="recap-step-4"'):]
    out_at = step4.index('id="recap-step4-output"')
    choices_at = step4.index('id="recap-step4-choices"')
    quote_at = step4.index('id="recap-step4-quote-credits"')
    # Order per AX-4R §4 + AX-6: preview → facts → choices → quote.
    assert out_at < choices_at < quote_at
    for marker in ('id="recap-step4-output-frame"', 'id="recap-step4-thumb"',
                   'id="recap-step4-output-empty"', 'id="recap-step4-facts"',
                   'data-i18n="recap_output_caption"'):
        assert marker in step4, marker
    # Facts chips: canvas · duration · subtitles · title · blur · quality.
    for fid in ("recap-fact-canvas", "recap-fact-duration", "recap-fact-subs",
                "recap-fact-title", "recap-fact-blur", "recap-fact-quality"):
        assert f'id="{fid}"' in step4, fid
    # The no-source frame reuses Step 2's designed empty state language.
    assert 'id="recap-step4-output-empty"' in step4
    assert step4.count('data-i18n="recap_preview_empty_title"') >= 1


def test_ax4r_refresh_on_entry_and_swap_without_flash():
    assert "function refreshRecapStep4Output()" in APP_JS
    assert "if (n === 4) { refreshRecapStep4Output(); }" in APP_JS
    # Re-request uses the EXISTING composite helper (no new API).
    assert "window.refreshRecapCompositePreview()" in APP_JS
    assert APP_JS.count('fetch("/api/recap/preview-composite"') == 1
    # The composite success path swaps the Step 4 frame when it lands.
    assert "isRecapStepOpen(4)" in APP_JS
    assert "window.refreshRecapStep4Thumb()" in APP_JS
    # The Step 4 frame prefers the engine composite URL of record.
    assert "state.recapCompositeUrl ||" in APP_JS


def test_ax4r_facts_are_derived_from_live_config():
    assert "function refreshRecapStep4Facts()" in APP_JS
    for marker in ('"9:16": "1080×1920 · 9:16"', '"16:9": "1920×1080 · 16:9"',
                   "state.recapSourceDuration", "state.recapBurnSubs",
                   "state.recapTitleEnabled", "state.recapBlurSubs",
                   "#recap-quality-picker .pill-btn.active",
                   "formatRecapQuoteDuration(dur)"):
        assert marker in APP_JS, marker


def test_ax4r_caption_and_fact_keys_in_both_locales():
    for key in ("recap_output_caption", "recap_fact_on", "recap_fact_off"):
        assert I18N_JS.count(key + ":") == 2, key


# ── AX-6: design polish pass ───────────────────────────────────────

def test_ax6_card_header_badge_matches_indicator_nodes():
    assert re.search(r"\.recap-step-head \.step-num-pill \{[^}]*width: 30px", STYLE_CSS)
    assert re.search(r"\.recap-step-head \.step-num-pill \{[^}]*"
                     r"linear-gradient\(135deg, #2563eb, #60a5fa\)", STYLE_CSS)


def test_ax6_continue_is_full_width_primary_with_arrow():
    assert re.search(r"\.recap-step-next \{[^}]*width: 100%", STYLE_CSS)
    assert re.search(r"\.recap-step-next \{[^}]*"
                     r"linear-gradient\(135deg, #2563eb, #60a5fa\)", STYLE_CSS)
    # The → affordance ships in the markup of both Continue buttons.
    assert TAB.count("recap_step_continue") == 2
    assert TAB.count("→</button>") >= 2


def test_ax6_one_selected_pill_language_au_lock_excluded():
    blue = re.search(r"#tab-recap \.pill-btn\.active[^{]*\{[^}]*"
                     r"linear-gradient\(135deg, #2563eb, #60a5fa\)", STYLE_CSS)
    assert blue
    # The AU quality lock's shipped gold is restated AFTER the blue
    # rule, so the quality pills keep their look (AX-6 exclusion).
    gold = re.search(r"#recap-quality-picker \.pill-btn\.active \{[^}]*#ffd968", STYLE_CSS)
    assert gold and gold.start() > blue.start()


def test_ax6_toggles_sliders_groups_empty_state():
    assert re.search(r"#tab-recap \.toggle-pill-btn\.active \.switch-track \{[^}]*"
                     r"linear-gradient\(135deg, #2563eb, #60a5fa\)", STYLE_CSS)
    assert re.search(r"#tab-recap \.slider-val \{[^}]*min-width: 58px", STYLE_CSS)
    assert re.search(r"#tab-recap \.slider-val \{[^}]*text-align: right", STYLE_CSS)
    assert re.search(r"\.recap-detail-group-title \{[^}]*border-bottom: 1px solid", STYLE_CSS)
    assert re.search(r"\.preview-empty-state \{[^}]*dashed", STYLE_CSS)


def test_ax6_step4_chips_quote_card_and_cta():
    assert re.search(r"\.recap-step4-choices \{[^}]*flex-wrap: wrap", STYLE_CSS)
    assert re.search(r"\.recap-step4-choices > span \{[^}]*border-radius: 999px", STYLE_CSS)
    assert re.search(r"\.recap-step4-quote \{[^}]*border-box", STYLE_CSS)
    assert re.search(r"#btn-start-recap \{[^}]*"
                     r"linear-gradient\(135deg, #2563eb, #60a5fa\)", STYLE_CSS)
    # CTA behaviour/ids untouched: the button still starts the recap.
    assert 'id="btn-start-recap"' in TAB
