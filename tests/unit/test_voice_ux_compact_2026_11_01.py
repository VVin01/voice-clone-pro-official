"""Voice Studio compact UX (owner complaint 2026-11-01).

The text textarea used to sit BELOW the full 12-persona grid: 2,187px
(2.59 viewports at 390x844) of scrolling before a user could type, on a
2,860px (3.39-viewport) tab. The restructure puts the flow in task
order — mode tabs, selected-voice summary chip, TEXT INPUT, then a
persona picker card that is collapsed by default, expands on demand
and collapses again as soon as a persona is chosen. No element id or
JS hook the product relies on was renamed; the generate CTA, char
counter, filters and preview buttons keep their existing wiring.

Follow-up (owner, 2026-11-01 late): tapping Change on the summary row
expanded the picker BELOW the text input card, disconnected from the
tap — the user had to scroll to find the grid that just opened. The
picker container now lives INSIDE the mode card directly beneath the
summary row and expands in place; it is still collapsed by default
(zero visual height), so the text input remains the first thing a
user can act on. The standalone picker toggle row is gone — the
summary row's Change button is the single toggle.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def test_picker_expands_in_place_under_summary_row():
    # Supersedes the original "text input before persona grid" DOM
    # order pin: the grid now sits in the DOM directly beneath the
    # summary row (owner's in-place requirement), still collapsed by
    # default so it costs no visual height and the text input stays
    # the first actionable element on screen.
    row = INDEX_HTML.index('id="voice-selected-row"')
    picker = INDEX_HTML.index('id="persona-picker-card"')
    grid = INDEX_HTML.index('id="personas-grid"')
    text_input = INDEX_HTML.index('id="voice-text-input"')
    assert row < picker < grid < text_input
    # The picker is nested inside the mode card: it appears before the
    # card's custom-mode section and before the input card entirely.
    assert picker < INDEX_HTML.index('id="ref-audio-section"')


def test_summary_chip_and_picker_card_present_collapsed_by_default():
    assert 'id="voice-selected-row"' in INDEX_HTML
    assert 'id="voice-selected-name"' in INDEX_HTML
    assert 'id="voice-selected-avatar"' in INDEX_HTML
    assert 'id="btn-change-voice"' in INDEX_HTML
    assert 'id="persona-picker-card"' in INDEX_HTML
    assert "persona-picker-card picker-collapsed" in INDEX_HTML
    # The Change button is the picker's only toggle now (the standalone
    # toggle row below the text input was removed in the move).
    assert 'id="btn-toggle-picker"' not in INDEX_HTML
    assert 'aria-controls="persona-picker-card"' in INDEX_HTML
    # Exactly one grid instance — the move must not duplicate state.
    assert INDEX_HTML.count('id="personas-grid"') == 1
    assert INDEX_HTML.count('id="persona-picker-card"') == 1
    assert INDEX_HTML.count('id="curated-personas-card"') == 1


def test_picker_collapse_css_hides_only_the_grid_body():
    assert ".persona-picker-card.picker-collapsed #curated-personas-card" in STYLE_CSS


def test_js_summary_picker_and_collapse_on_select():
    assert "function updateSelectedVoiceSummary()" in APP_JS
    assert "function setPersonaPickerOpen(open)" in APP_JS
    assert "function togglePersonaPicker()" in APP_JS
    # Choosing a persona renders (which refreshes the summary) and collapses.
    select_slice = APP_JS[APP_JS.index("state.selectedPersona = v.id;"):]
    select_slice = select_slice[: select_slice.index("});")]
    assert "renderCuratedVoices();" in select_slice
    assert "setPersonaPickerOpen(false);" in select_slice
    # renderCuratedVoices keeps the summary chip in sync.
    assert "updateSelectedVoiceSummary();" in APP_JS


def test_picker_aria_lives_on_change_button():
    fn = APP_JS[APP_JS.index("function setPersonaPickerOpen(open)"):]
    fn = fn[: fn.index("\n}\n")]
    assert 'getElementById("btn-change-voice")' in fn
    assert "aria-expanded" in fn


def test_custom_mode_hides_picker_and_summary():
    fn = APP_JS[APP_JS.index("function switchVoxCPMSubMode"):]
    fn = fn[: fn.index("\n}\n")]
    assert 'getElementById("persona-picker-card")' in fn
    assert 'getElementById("voice-selected-row")' in fn
    # The pre-existing curated/custom toggling is untouched.
    assert 'getElementById("curated-personas-card")' in fn
    assert 'getElementById("ref-audio-section")' in fn


def test_existing_voice_hooks_preserved():
    for hook in (
        'id="voice-text-input"',
        'id="char-count"',
        'id="btn-generate-voice"',
        'id="personas-grid"',
        'id="personas-count"',
        'id="curated-personas-card"',
        'id="ref-audio-section"',
        'id="btn-submode-curated"',
        'id="btn-submode-custom"',
    ):
        assert hook in INDEX_HTML, hook
    assert "function playVoicePreview(" in APP_JS
    assert "function filterPersonas(" in APP_JS


def test_summary_chip_shows_short_persona_name_only():
    # Persona picker labels carry a parenthetical descriptor
    # ("မင်းမင်း (ရုပ်ရှင်ဇာတ်လမ်းပြောသူ)") which truncated mid-parenthesis in the
    # compact summary row; the chip renders the short name only.
    assert "function personaShortName(" in APP_JS
    fn = APP_JS[APP_JS.index("function updateSelectedVoiceSummary()"):]
    fn = fn[: fn.index("\n}\n")]
    assert "personaShortName(v.name)" in fn


def test_new_i18n_keys_have_my_en_parity():
    for key in ("voice_selected_label", "voice_change", "voice_pick_title"):
        assert I18N_JS.count(f"{key}:") == 2, key
