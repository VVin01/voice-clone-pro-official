"""Batch BG (2026-11-06): persona-card tidy fix (BD spec §15).

Owner's real-phone screenshot on live r76 ("ဖုန်းမှာမသေသပ်"): the
Voice-tab persona cards wrapped their titles mid-parenthesis
("ကိုသူရ (သတင်းနှင့် / Documentary)"), rows filled unevenly, and the
preview button's icon+label group overflowed its pill at phone
widths (PIL/Padauk measured slack −13.6px at 360px, my). BG is
presentation only — CSS + the card template's structure:

BG-1  The card renders the SHORT name on its own line
      (personaShortName) and the parenthetical descriptor as a
      separate .persona-category line under it (personaCategoryName).
      No id, handler or data-attribute changes; the summary chip and
      every JS hook (playVoicePreview queries) are untouched.
BG-2  Equal-height rows: .persona-card fills its grid row and the
      footer button pins to one shared baseline.
BG-3  The footer bleeds into the card's side padding so the button
      spans the card's full inner width; the button gains the §4
      44px floor, §1 8px padding and the 12px label step (up from
      the BB V-3 11.5px shrink). Fit proven by the PIL/Padauk sweep
      (~/workspace/fix-bg-work/measure_persona_cards.py) at
      360/390/430, my+en, all 12 personas, all 3 label states.
BG-4  Typography rhythm: name at the 15px card-title step on one
      line; Burmese description line-height completes §11 at 1.75.
BG-5  Sample-cache fix (owner-reported live defect, same problem
      area): phones kept hearing the OLD persona samples ~1h after
      the BF deploy because /api/voices/sample/{slug} returned
      Cache-Control: public, max-age=86400. Both sample FileResponses
      (static return + komin fallback) now send no-cache; ETag
      revalidation stays FileResponse's own.
BG-6  Recap persona pickers use a SEPARATE component (.voice-card)
      and are deliberately untouched by this batch.
BG-7  Cache token r76 -> r77 (BG on the BF base); renumbered
      r77 -> r78 when ported onto the BC tree (fix-bg2-work).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.responses import FileResponse

from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")

BG_CSS = CSS.split("BATCH BG (2026-11-06)", 1)[1]


def _rule(selector: str, css: str) -> str:
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


# ── BG-1: card structure ─────────────────────────────────────────────

def test_card_name_is_short_name_on_its_own_line():
    render = APP_JS.split("function renderCuratedVoices()", 1)[1].split(
        "function personaShortName", 1)[0]
    assert "personaShortName(v.name) || v.name" in render
    # The full (parenthesised) label no longer renders as the name.
    assert '<span class="persona-name">${v.name}</span>' not in render


def test_card_category_is_a_separate_line():
    render = APP_JS.split("function renderCuratedVoices()", 1)[1].split(
        "function personaShortName", 1)[0]
    assert "personaCategoryName(v.name)" in render
    assert 'class="persona-category"' in render
    # Category sits between name and badge inside the meta column.
    assert render.index('class="persona-name"') \
        < render.index('class="persona-category"') \
        < render.index('class="persona-badge"')


def test_category_helper_extracts_the_parenthetical():
    assert "function personaCategoryName(" in APP_JS
    fn = APP_JS.split("function personaCategoryName(", 1)[1].split("\n}", 1)[0]
    assert "match(" in fn and "m[1].trim()" in fn


def test_card_hooks_and_attributes_unchanged():
    render = APP_JS.split("function renderCuratedVoices()", 1)[1].split(
        "function personaShortName", 1)[0]
    assert 'card.setAttribute("data-voice-id", v.id)' in render
    assert "playVoicePreview('${v.id}', this)" in render
    assert 'class="voice-preview-btn"' in render
    assert 'class="preview-btn-icon"' in render
    assert 'class="preview-btn-label"' in render


# ── BG-2 / BG-3 / BG-4: the CSS ──────────────────────────────────────

def test_cards_fill_the_row_and_buttons_share_a_baseline():
    assert "height: 100%" in _rule(".persona-card", BG_CSS)


def test_name_one_line_at_the_card_title_step():
    rule = _rule(".persona-name", BG_CSS)
    assert "font-size: 15px" in rule
    assert "white-space: nowrap" in rule


def test_category_line_style():
    rule = _rule(".persona-category", BG_CSS)
    assert "font-size: 12px" in rule
    assert "text-wrap: balance" in rule


def test_burmese_description_line_height_completes_s11():
    assert "line-height: 1.75" in _rule("body.lang-my .persona-desc", BG_CSS)


def test_footer_bleeds_and_button_gains_room():
    assert "margin-inline: calc(-1 * var(--s3))" in _rule(".persona-footer-row", BG_CSS)
    btn = _rule(".voice-preview-btn", BG_CSS)
    assert "min-height: 44px" in btn
    assert "padding: var(--s2)" in btn
    assert "font-size: 12px" in btn
    label = _rule(".voice-preview-btn .preview-btn-label", BG_CSS)
    # Ellipsis is the ONLY permitted shrink, for the transient
    # loading label at 360px (sweep-documented).
    assert "min-width: 0" in label and "text-overflow: ellipsis" in label


# ── BG-5: sample responses revalidate instead of pinning for 24h ────

def test_sample_endpoint_no_longer_sends_day_long_cache():
    assert "public, max-age=86400" not in API_PY
    sample_fn = API_PY.split("async def get_voice_sample_audio", 1)[1].split(
        "\n@api_router", 1)[0]
    # Both persona-sample returns (static + komin fallback) revalidate.
    assert sample_fn.count('headers={"Cache-Control": "no-cache"}') == 2


@pytest.mark.asyncio
async def test_sample_response_cache_control_is_no_cache(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=882601)
    await db_session.commit()
    resp = await api_mod.get_voice_sample_audio("komin", user=user, db=db_session)
    assert isinstance(resp, FileResponse)
    assert resp.headers["cache-control"] == "no-cache"
    # ETag handling stays FileResponse's own: Starlette computes it
    # from the file stat when the response is sent (set_stat_headers),
    # so clients revalidate cheaply instead of re-downloading.


@pytest.mark.asyncio
async def test_sample_fallback_response_cache_control_is_no_cache(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=882602)
    await db_session.commit()
    # An unknown slug resolves through to the komin fallback return.
    resp = await api_mod.get_voice_sample_audio(
        "no-such-persona-bg", user=user, db=db_session)
    assert isinstance(resp, FileResponse)
    assert str(resp.path).endswith("komin.mp3")
    assert resp.headers["cache-control"] == "no-cache"


# ── BG-6: recap component untouched ──────────────────────────────────

def test_recap_voice_cards_not_restructured():
    # Batch BV deliberate pin update: 13 — the 12 persona cards plus
    # BV's own-voice card, added inside the same persona area without
    # restructuring any persona card.
    assert INDEX_HTML.count('class="voice-card') == 13
    assert "persona-category" not in INDEX_HTML


# ── BG-7: token ──────────────────────────────────────────────────────

def test_cache_token_bumped():
    assert INDEX_HTML.count("?v=20261104_r102") == 4
    assert "20261104_r76" not in INDEX_HTML
