"""Batch AY — Home tab rearrangement + beautification (owner
direction 2026-11-06 00:37; spec ~/workspace/batch-ay-spec-2026-11-06.md).

The primary actions were buried fifth on Home. AY-1 reorders the tab
(welcome strip → Start hero tiles → Tools tiles → Samples →
Tutorials → Refer → Support) and restyles in the AX design language.
Layout + styling ONLY: every id, link, button, audio element and
navigation action behaves exactly as before.

Invariants pinned here:
- All 12 pre-AY Home ids survive exactly once.
- Card order inside the pane is the AY-1 order.
- Start/Tools entries are tiles carrying the SAME onclick actions
  and the SAME name/desc i18n keys the rows used.
- Samples: the same two <audio> elements (ids + data-sample-url),
  native controls gone, branded buttons wired in app.js to the same
  elements; the one-at-a-time rule in setupSamplePlayers stands.
- Tutorials: all 11 openTutorialVideo calls (index chip + 10 rows)
  survive — owner addendum 2026-11-06 01:18 collapses the card to
  ONE header row (title + count + chevron toggle); every link sits
  inside the in-card accordion body #home-tut-more.
- Refer card copy/bindings untouched (the +30s wording is Batch
  AW's to change); the Support row lives in its own card now.
- New strings exist in BOTH i18n locales; cache token r71.
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

HOME = INDEX_HTML[INDEX_HTML.index('id="tab-home"'):INDEX_HTML.index('id="tab-voice"')]

# Round 13 simplified Home: samples/refer/support cards moved out; Hero + Start + Tools + Help/Tutorials remain.
HOME_IDS = ["tab-home", "home-welcome-strip", "welcome-strip-text",
            "home-start-card", "home-tools-card", "home-help-entry-card",
            "home-tutorials-card", "home-tut-more"]

TUT_KEYS = ["bot", "voice", "recap", "plans", "customvoice", "downloader",
            "scriptedit", "recapadvanced", "refer", "support"]


def _card(card_id: str) -> str:
    start = HOME.index(f'id="{card_id}"')
    nxt = HOME.find('<div class="card glass', start + 1)
    return HOME[start:] if nxt == -1 else HOME[start:nxt]


# ── Id survival + AY-1 order ─────────────────────────────────────

def test_ay_home_ids_survive_exactly_once():
    for hid in HOME_IDS:
        assert INDEX_HTML.count(f'id="{hid}"') == 1, hid


def test_ay_home_card_order():
    seq = ["home-welcome-strip", "home-start-card", "home-tools-card",
           "home-help-entry-card", "home-tutorials-card"]
    pos = [HOME.index(f'id="{i}"') for i in seq]
    assert pos == sorted(pos), "Home card order"


# ── Start + Tools tiles ──────────────────────────────────────────

def test_ay_start_hero_tiles():
    card = _card("home-start-card")
    assert 'class="home-tile-grid"' in card
    assert card.count('class="home-tile"') == 2
    assert "onclick=\"switchTab('tab-voice')\"" in card
    assert "onclick=\"switchTab('tab-recap')\"" in card
    for key in ("home_start_title", "home_start_voice_name",
                "home_start_voice_desc", "home_start_recap_name",
                "home_start_recap_desc"):
        assert f'data-i18n="{key}"' in card, key
    # The rows are gone from this card (tiles replaced them).
    assert 'class="home-tut-row"' not in card


def test_ay_tools_tiles():
    card = _card("home-tools-card")
    assert 'class="home-tile-grid"' in card
    assert card.count('class="home-tile"') == 2
    assert "onclick=\"switchTab('tab-downloader')\"" in card
    assert 'onclick="openHistoryHub()"' in card
    for key in ("home_tools_title", "home_tools_dl_name", "home_tools_dl_desc",
                "home_tools_history_name", "home_tools_history_desc"):
        assert f'data-i18n="{key}"' in card, key
    assert 'class="home-tut-row"' not in card


def test_ay_tiles_share_one_language_css():
    assert ".home-tile-grid" in STYLE_CSS
    assert "grid-template-columns: 1fr 1fr" in STYLE_CSS
    tile = STYLE_CSS.split("#tab-home .home-tile {", 1)[1].split("}", 1)[0]
    assert "linear-gradient(135deg, #2563eb, #60a5fa)" in STYLE_CSS  # AX language
    assert "border-radius: var(--r-lg)" in tile
    # AZ-8: Myanmar line-heights for tile text mirror the row rules.
    assert "body.lang-my .home-tile-name { line-height: 1.7; }" in STYLE_CSS
    assert "body.lang-my .home-tile-hint { line-height: 1.8; }" in STYLE_CSS


# ── Samples card retired in Round 13 (moved to Voice Studio & Welcome Try) ──

def test_ay_samples_card_retired_welcome_try_exists():
    assert 'id="home-samples-card"' not in HOME
    assert 'id="welcome-try-sample-btn"' in HOME


# ── Tutorials expandable ─────────────────────────────────────────

def test_ay_tutorials_all_links_survive_in_accordion():
    card = _card("home-tutorials-card")
    assert card.count("openTutorialVideo(") == 11  # index chip + 10 rows
    more_at = card.index('id="home-tut-more"')
    assert "openTutorialVideo(" not in card[:more_at]
    for key in ["index"] + TUT_KEYS:
        assert card.index(f"openTutorialVideo('{key}')") > more_at, key


def test_ay_tutorials_accordion_header_toggle():
    # Round 13 moved the Home Help/Tutorials toggle onto #btn-home-help-hub.
    assert 'id="btn-home-help-hub"' in HOME
    assert 'id="home-tutorials-card"' in HOME
    assert 'id="home-tut-more"' in HOME


def test_ay_refer_and_support_moved_out_of_home():
    # Round 13 moved Refer & Support into Profile / Bot /start Refer.
    assert 'id="home-refer-card"' not in HOME
    assert "openSupportContact()" in INDEX_HTML
    assert 'data-i18n="support_contact_title"' in INDEX_HTML


# ── i18n + token + strip ─────────────────────────────────────────

def test_ay_new_strings_both_locales():
    assert 'home_tut_count: "၁၀ ခု"' in I18N_JS
    assert 'home_tut_count: "10 guides"' in I18N_JS


def test_ay_welcome_strip_restyled_scoped():
    strip = STYLE_CSS.split("#tab-home .welcome-strip {", 1)[1].split("}", 1)[0]
    assert "linear-gradient(135deg" in strip
    # Strip markup + behaviour unchanged.
    home_strip = HOME.split('id="home-welcome-strip"', 1)[1].split('id="home-start-card"', 1)[0]
    assert 'onclick="dismissWelcomeStrip()"' in home_strip
    assert "switchTab('tab-recap')" in home_strip


def test_ay_cache_token_r79():
    assert INDEX_HTML.count("20261123_round15") == 5
    assert "20261104_r70" not in INDEX_HTML
