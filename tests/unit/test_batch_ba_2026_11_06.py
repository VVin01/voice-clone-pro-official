"""Batch BA — Header phone-width fit (owner standard 2026-11-06
00:48/01:23: no cramped/cut/broken text anywhere; spec
~/workspace/batch-ba-spec-2026-11-06.md, BA-1).

Found by the parent's own eyes-on of the live r72 build: at phone
width (~380px) the .top-nav overflowed — the 🎬 balance chip was
half-cut at the right edge, the language button was cut mid-text,
and the guide button collapsed to its icon. The fix is CSS
compaction inside one @media (max-width: 430px) block (plus the
language button's label becoming flag + text spans so the text can
hide WHOLE at narrow width — a flat text node cannot be partially
hidden without cutting glyphs). No element removed; no id,
behaviour, payload or navigation changes; the full labelled form
returns above 430px.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.webapp import api as api_mod


STATIC_DIR = Path(api_mod.__file__).resolve().parent / "static"
I18N_JS = (STATIC_DIR / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (STATIC_DIR / "css" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

BA_CSS = STYLE_CSS[STYLE_CSS.index("BATCH BA (2026-11-06)"):]
HEADER = INDEX_HTML[INDEX_HTML.index('<header class="top-nav">'):INDEX_HTML.index("</header>")]


def _ba_media() -> str:
    m = re.search(r"@media \(max-width: 430px\) \{(.*)\n\}", BA_CSS, re.S)
    assert m, "BA @media (max-width: 430px) block missing"
    return m.group(1)


def _rule(selector: str, css: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
    assert m, f"rule not found: {selector}"
    return m.group(1)


# ── BA-1: the compaction lives in one ≤430px block ──────────────

def test_ba_media_block_compacts_the_header():
    block = _ba_media()
    # Brand compacts: STUDIO hides, wordmark/logo/PRO stay (markup
    # untouched — the AD brand pins still hold above).
    assert "display: none" in _rule(".sub-brand", block)
    # Header padding tightens one spacing step, nothing more.
    assert "padding-inline: var(--s2)" in _rule(".top-nav", block)
    assert "padding: var(--s2)" in _rule(".user-pill", block)


def test_ba_guide_and_language_become_icon_buttons():
    block = _ba_media()
    btns = _rule(".tutorial-pill-btn, .lang-pill-btn", block)
    # ≥40px hit areas in both dimensions, icons centred and whole.
    assert "width: 40px" in btns
    assert "min-height: 40px" in btns
    assert "justify-content: center" in btns
    # Text spans hide WHOLE (never clipped mid-glyph).
    texts = _rule(".tutorial-pill-btn .tutorial-btn-text,\n    .lang-pill-btn .lang-btn-text", block)
    assert "display: none" in texts


def test_ba_badge_live_and_chips_tighten_but_stay_whole():
    block = _ba_media()
    badge = _rule(".vip-badge", block)
    # Badge text may shrink only to the specced 10px floor.
    assert "font-size: 10px" in badge
    assert "padding: 2px 6px" in _rule(".live-dot", block)
    chip = _rule(".bal-chip", block)
    assert "padding: 6px 8px" in chip
    # The AZ-10.1 glass-pill form itself is untouched (this block
    # only tightens inline padding; min-height/line-height stand).
    az_chip = _rule(".bal-chip", STYLE_CSS[STYLE_CSS.index("BATCH AZ — GLASSMORPHISM 2.0"):])
    assert "min-height: 34px" in az_chip
    assert "line-height: 1.6" in az_chip


def test_ba_full_labels_return_above_430():
    # The old ≤480px tutorial collapse is gone, and nothing before
    # the BA block hides either header text span — so the full
    # labelled form is what renders above 430px.
    pre_ba = STYLE_CSS[:STYLE_CSS.index("BATCH BA (2026-11-06)")]
    assert ".tutorial-pill-btn { width: 38px" not in pre_ba
    assert "tutorial-btn-text { display: none" not in pre_ba
    assert "lang-btn-text" not in pre_ba
    # Base button rules keep their labelled geometry.
    base = _rule(".tutorial-pill-btn, .lang-pill-btn", STYLE_CSS)
    assert "min-height: 38px" in base
    assert "padding: 5px 10px" in base


# ── The language button's two-span label ────────────────────────

def test_ba_lang_button_label_is_flag_plus_text_spans():
    assert 'id="btn-lang-toggle"' in HEADER
    assert '<span class="lang-flag" aria-hidden="true">🇲🇲</span>' in HEADER
    assert '<span class="lang-btn-text" id="lang-btn-text">မြန်မာ</span>' in HEADER
    # The label keeps a reachable name when icon-only: the title
    # follows the UI language through the existing engine hook.
    assert 'data-i18n-title="lang_toggle_title"' in HEADER


def test_ba_setlanguage_writes_the_same_two_span_structure():
    assert "lang-flag" in I18N_JS
    assert "lang-btn-text" in I18N_JS
    # Visible text is byte-identical to the pre-BA flat label.
    assert '"🇲🇲"' in I18N_JS and '"🇬🇧"' in I18N_JS
    assert '"မြန်မာ"' in I18N_JS and '"EN"' in I18N_JS
    assert 'langBtn.innerText = lang === "my"' not in I18N_JS


def test_ba_lang_toggle_title_key_in_both_locales():
    assert I18N_JS.count("lang_toggle_title:") == 2
    assert 'lang_toggle_title: "ဘာသာစကား ပြောင်းရန်"' in I18N_JS
    assert 'lang_toggle_title: "Switch language"' in I18N_JS


# ── No element removed, token swept ─────────────────────────────

def test_ba_no_header_element_removed():
    for needle in ('id="btn-tutorial-top"', 'id="btn-lang-toggle"',
                   'id="user-pill"', 'id="vip-status-badge"',
                   'id="user-voice-bal"', 'id="user-recap-bal"',
                   'class="brand-crest"', 'class="pro-tag"',
                   'class="sub-brand"', 'class="live-dot"'):
        assert needle in HEADER, needle


def test_ba_cache_token_is_r79():
    assert INDEX_HTML.count("20261104_r116") == 4
    assert "20261104_r73" not in INDEX_HTML
