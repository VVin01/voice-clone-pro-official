"""Batch BD (2026-11-06): whole-app Glassmorphism 2.0 polish.

Spec: ~/workspace/batch-bd-spec-2026-11-06.md (§1–§14; §15 + the
persona sample-button item shipped in Batch BG and are pinned
intact below, not restyled). PRESENTATION ONLY — CSS + markup
classes; the only JS is the capsule measurement hooks. No id,
handler, data-attribute, behaviour or copy changes; no new
visible strings (the engine capsule's compact labels are the
emoji already inside its existing labels, so no new i18n keys).

BD-0  The capsule family's missing tokens: BB-2's .speed-capsule
      referenced --glass-rim / --glass-spec / --shadow-sm (never
      defined) and composed blur(var(--glass-2-blur)) — invalid.
      Its rim/shadow/blur never rendered. BD defines the tokens
      and repairs the blur line.
BD-1  §5 shared segmented capsule (.seg-capsule): the engine
      picker becomes a true one-row capsule (active segment
      expands, full label; inactive show the label's own emoji).
      The story-style picker's labels are long-form Burmese —
      PIL/Padauk proves every one exceeds the one-row 5-segment
      budget, so it takes the WRAPPED geometry of the same
      component (C1 tiles + full labels + the sliding pill).
BD-2  §5 glass switches (all four families) + blue focus fields.
BD-3  §4 secondary buttons gain the tier-2 rim; danger = red glass.
BD-4  §6 rows: 44px icon tiles (hub / admin avatar / tutorials).
BD-5  §7 one status-pill style: state dots; TEST badge tinted.
BD-6  §8 progress glow heads.
BD-7  §1 sheet interiors on the 4/8/12/16/24 rhythm.
BD-8  §9 glass skeleton + the gate-locked header shimmer (the
      r74-audit placeholder flash: FREE / 0.0m / 0c before the
      profile lands — the body starts gate-locked, so the locked
      window IS the initial-load window; CSS only, no JS change).
BD-9  §10 empty-state glass medallions.
BD-10 §11 language line-heights, tabular numerals, text-wrap
      balance on the audited orphan-syllable surfaces.
BD-11 §12 compactness: pane gaps join the scale (20px orphan ->
      s4), drop-zone + state paddings trimmed. Before/after:
      ~/workspace/fix-bd-work/measurement-heights.txt.
BD-12 Cache token r79 -> r80.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")

BD_CSS = CSS.split("BATCH BD (2026-11-06)", 1)[1]


def _rule(selector: str, css: str) -> str:
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


def _picker(sid: str, next_id: str) -> str:
    return INDEX_HTML.split(f'id="{sid}"', 1)[1].split(f'id="{next_id}"', 1)[0]


# ── BD-0: the capsule family's tokens ────────────────────────────────

def test_capsule_tokens_now_defined():
    root = _rule(":root", BD_CSS)
    assert "--glass-rim: rgba(255, 255, 255, 0.65)" in root
    assert "--glass-spec: var(--rim-shadow)" in root
    assert "--shadow-sm:" in root


def test_speed_capsule_blur_repaired():
    rule = _rule(".speed-capsule", BD_CSS)
    assert "backdrop-filter: var(--glass-2-blur)" in rule
    assert "backdrop-filter: blur(var(--glass-2-blur))" not in BD_CSS


# ── BD-1: capsule markup ─────────────────────────────────────────────

def test_engine_picker_is_a_one_row_capsule():
    seg = _picker("recap-engine-picker", "recap-gemini-voice-wrap")
    assert "seg-capsule" in INDEX_HTML.split('id="recap-engine-picker"', 1)[0].rsplit('class="', 1)[1]
    assert '<span class="seg-capsule-pill" aria-hidden="true"></span>' in seg
    assert seg.count("capsule-seg") == 3
    # Behaviour hooks untouched.
    for engine in ("voxcpm", "gemini", "edge"):
        assert f'data-engine="{engine}"' in seg
    # Full labels keep their i18n keys — on the .seg-full span, NOT
    # on the button (setLanguage's innerText pass would wipe the
    # span structure if the key sat on the button itself).
    for key in ("recap_engine_voxcpm", "recap_engine_gemini", "recap_engine_edge"):
        assert f'<span class="seg-full" data-i18n="{key}">' in seg
    assert not re.search(r'<button[^>]*data-i18n="recap_engine_', INDEX_HTML)
    # Compact labels are the labels' own emoji — no new strings.
    for glyph in ("🎙️", "✨", "🎧"):
        assert f'<span class="seg-short" aria-hidden="true">{glyph}</span>' in seg
    # The field label moved above the track (speed-row pattern).
    field = INDEX_HTML.split('<div class="capsule-field">', 1)[1].split('id="recap-gemini-voice-wrap"', 1)[0]
    assert 'data-i18n="recap_engine_label"' in field


def test_story_picker_is_the_wrapped_capsule():
    seg = _picker("story-style-picker-quick", "gemini-voice-select-quick")
    assert "style-pills-grid seg-capsule" in INDEX_HTML
    assert '<span class="seg-capsule-pill" aria-hidden="true"></span>' in seg
    assert seg.count("capsule-seg") == 5
    for story in ("viral", "dramatic", "funny", "pov", "standard"):
        assert f'data-story="{story}"' in seg
    # Full labels untouched (the wrapped geometry exists precisely
    # so no copy is shortened).
    for key in ("style_viral", "style_dramatic", "style_funny", "style_pov", "style_standard"):
        assert f'data-i18n="{key}"' in seg


def test_seg_capsule_component_css():
    track = _rule(".seg-capsule", BD_CSS)
    assert "background: var(--glass-2-bg)" in track
    assert "border: 1px solid var(--glass-rim)" in track
    assert "backdrop-filter: var(--glass-2-blur)" in track
    pill = _rule(".seg-capsule-pill", BD_CSS)
    assert "linear-gradient(90deg, #0a84ff, #38bdf8)" in pill
    assert "cubic-bezier(0.34, 1.25, 0.4, 1)" in pill  # the AZ spring
    eng = _rule("#recap-engine-picker .pill-btn.capsule-seg.active", BD_CSS)
    assert "flex-grow: 2.4" in eng
    story = _rule("#story-style-picker-quick.seg-capsule", BD_CSS)
    assert "flex-wrap: wrap" in story
    # Fallback nets learn the new surface (AZ pattern).
    assert "@supports not ((backdrop-filter: blur(2px))" in BD_CSS
    assert ".seg-capsule-pill { transition: none; }" in BD_CSS


def test_seg_capsule_movers_and_call_sites():
    assert "function moveSegPill(capsule)" in APP_JS
    assert "function moveSegPills()" in APP_JS
    assert 'capsule.querySelector(".seg-capsule-pill")' in APP_JS
    assert 'capsule.querySelector(".capsule-seg.active")' in APP_JS
    # Global lifecycle re-measures + picker-level hooks.
    assert APP_JS.count("moveSegPills()") >= 8
    assert APP_JS.count('moveSegPill(document.getElementById("recap-engine-picker"))') == 2
    assert APP_JS.count('moveSegPill(document.getElementById("story-style-picker-quick"))') == 1


# ── BD-1b: PIL/Padauk geometry proof (the BB-2 method) ───────────────

_EMOJI = ("🎙", "✨", "🎧", "🎭", "😂", "👁", "📖", "️")


def _label(key: str, lang_idx: int) -> str:
    vals = re.findall(rf'\b{key}:\s*"((?:[^"\\]|\\.)*)"', I18N_JS)
    assert len(vals) > lang_idx, key
    return vals[lang_idx]


def _width(font, s: str) -> float:
    # Conservative device accounting: PIL's fallback width for an
    # emoji glyph understates the colour-emoji advance on phones,
    # so each emoji counts a flat 19px (the BB-2 allowance).
    text = s
    n = 0
    for e in _EMOJI:
        n += text.count(e)
        text = text.replace(e, "")
    return font.getbbox(text)[2] + 19 * n


def test_capsule_width_budgets():
    imagefont = pytest.importorskip("PIL.ImageFont")
    font = imagefont.truetype(str(STATIC / "fonts" / "Padauk-Bold.ttf"), 13)
    strip3 = 360 - 32 - 32 - 12 - 2 * 4            # 276
    active3 = strip3 * 2.4 / 4.4 - 6 - 2           # 142.5 inner
    inactive3 = strip3 / 4.4 - 6 - 2               # 54.7 inner
    # Engine: every full label fits the ACTIVE segment (both langs
    # share the strings), every emoji short fits an INACTIVE one.
    for key in ("recap_engine_voxcpm", "recap_engine_gemini", "recap_engine_edge"):
        assert _width(font, _label(key, 0)) <= active3, key
    for glyph in ("🎙️", "✨", "🎧"):
        assert _width(font, glyph) <= inactive3, glyph
    # Story: EVERY Burmese full label exceeds the one-row 5-segment
    # active budget — the wrapped geometry is mathematically
    # required (shortening copy was the only alternative).
    strip5 = 360 - 32 - 32 - 12 - 4 * 4            # 268
    active5 = strip5 * 2.4 / (2.4 + 4) - 6 - 2     # 92.5 inner
    for key in ("style_viral", "style_dramatic", "style_funny", "style_pov", "style_standard"):
        assert _width(font, _label(key, 0)) > active5, key


# ── BD-2: switches + fields ──────────────────────────────────────────

def test_switches_are_glass_with_gradient_on_state():
    assert "background: var(--glass-2-bg)" in BD_CSS
    # Grouped on-state rule + luxury slider + chip dot.
    assert BD_CSS.count("linear-gradient(135deg, #0a84ff, #38bdf8)") >= 3
    for sel in (".toggle-pill-btn.active .switch-track", ".mini-toggle.active .t-val",
                '.script-review-row input[type="checkbox"]:checked',
                ".switch-luxury input:checked + .slider"):
        assert sel in BD_CSS


def test_fields_focus_blue_and_meet_the_floor():
    focus = _rule("textarea:focus, .studio-input:focus, .studio-select:focus", BD_CSS)
    assert "rgba(10, 132, 255, 0.18)" in focus
    assert "min-height: 44px" in _rule(".studio-input", BD_CSS)


# ── BD-3..BD-6: buttons, rows, status pills, progress ────────────────

def test_secondary_buttons_and_danger_glass():
    sec = _rule(".secondary-btn, .mini-btn", BD_CSS)
    assert "var(--rim-shadow)" in sec
    danger = _rule(".mini-btn.danger-btn, .adm-danger-btn", BD_CSS)
    assert "var(--red-soft)" in danger


def test_rows_join_the_44px_tile_language():
    assert "width: 44px; height: 44px" in _rule(".hub-type-icon", BD_CSS)
    assert "width: 44px; height: 44px" in _rule(".adm-avatar", BD_CSS)
    tut = _rule(".home-tut-icon", BD_CSS)
    assert "width: 44px; height: 44px" in tut
    assert "font-size: 14px" in _rule(".hub-row-title", BD_CSS)
    assert "font-size: 14px" in _rule(".home-tut-name", BD_CSS)


def test_status_pills_one_style_with_dots():
    assert ".badge-test::before" in BD_CSS
    assert ".adm-status-tag::before" in BD_CSS
    # Tinted glass, no longer BC's flat amber fill.
    assert "background: rgba(245, 158, 11, 0.16); color: #b45309" in BD_CSS
    # …and the BD override lands after BC's flat fill in the cascade.
    assert CSS.index(".badge-test {") < CSS.index("BATCH BD (2026-11-06)")


def test_progress_glow_heads():
    assert "box-shadow: 2px 0 10px rgba(10, 132, 255, 0.45)" in _rule(".progress-bar-fill", BD_CSS)
    assert ".live-jobs-pill-fill" in BD_CSS


# ── BD-7..BD-11: sheets, skeleton, empty states, type, compactness ──

def test_sheet_interiors_on_the_rhythm():
    assert "margin-bottom: var(--s2)" in _rule(".live-jobs-sheet-head", BD_CSS)
    assert "padding: var(--s4)" in _rule(".submit-confirm-card", BD_CSS)
    assert "margin-top: var(--s3)" in _rule(".submit-confirm-actions", BD_CSS)


def test_skeleton_and_gate_locked_header_shimmer():
    skel = _rule(".skeleton", BD_CSS)
    assert "border: 1px solid var(--glass-rim)" in skel
    for sel in ("body.gate-locked .user-pill .vip-badge", "body.gate-locked .user-pill .bal-chip"):
        assert sel in BD_CSS
    assert "animation: skeletonWave 1.3s ease infinite" in BD_CSS
    # CSS-only by construction: the gate still starts locked and
    # still unlocks from the same JS path (no behaviour change).
    assert "gate-locked" in INDEX_HTML.split("<body", 1)[1].split(">", 1)[0]


def test_empty_state_medallions():
    med = _rule(".hub-state-emoji, .preview-empty-icon", BD_CSS)
    assert "width: 56px; height: 56px" in med
    assert "background: var(--glass-2-bg)" in med


def test_typography_rhythm():
    assert "line-height: 1.75" in _rule("body.lang-my", BD_CSS)
    assert "line-height: 1.6" in _rule("body.lang-en", BD_CSS)
    assert "font-variant-numeric: tabular-nums" in BD_CSS
    assert "text-wrap: balance" in BD_CSS


def test_compactness_trims():
    assert "gap: var(--s4)" in _rule(".tab-pane", BD_CSS)
    assert "padding: var(--s4)" in _rule(".video-drop-zone", BD_CSS)
    assert "padding: var(--s4) var(--s3)" in _rule(".hub-state", BD_CSS)
    assert "padding: var(--s4) var(--s3)" in _rule(".adm-state", BD_CSS)


# ── §15 / BG intact + token ──────────────────────────────────────────

def test_bg_persona_work_intact_and_not_restyled():
    assert ".persona-category" in CSS                      # BG-1
    assert '"public, max-age=86400"' not in API_PY          # BG-5 stays fixed
    assert API_PY.count("no-cache") >= 2
    assert ".persona-card" not in BD_CSS                    # BD does not touch the cards
    assert ".persona-sample-listen" not in BD_CSS


def test_token_r80():
    assert INDEX_HTML.count("20261104_r83") == 4
    assert "20261104_r79" not in INDEX_HTML
