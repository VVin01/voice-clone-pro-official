"""QA fix batch (2026-11-02) — Q1 + Q2 + F4.

Pins for the fixes built from the first weekly QA cycle
(qa-cycle-2026-11-02/REPORT.md):

- Q1 (visual): the Recap canvas's dashed band plate was sized from
  the blur-band % alone (default 7%, clamped to a 22px min-height)
  while the two-line subtitle sample sharing its centre anchor is
  ~47px tall — the caption straddled the plate at EVERY ratio
  (DOM-measured: 12.4px spill top and bottom at 16:9 / 1:1 /
  Original, 4.8px at 9:16). The plate now grows in the preview
  (fitBlurPlateToCaption) until it contains the caption; the blur_h
  value sent to the render is untouched. Geometry is proven by DOM
  measurement in the fix record; these pins hold the wiring.
- Q2 (functional): the Recap Analyze extract call for a Short died
  at the network level (ERR_CONNECTION_CLOSED, no response) while
  the paste-time source thumbnail was already on the canvas; the
  catch's generic error toast then read as "the loaded frame
  failed". A network-level failure with a preview on the canvas no
  longer toasts (the catch's honest unverified info card remains
  the signal); failures with no preview still toast.
- F4 (server): GET /api/admin/users silently ignored the `search`
  query param. The list route now honours it with the SAME matching
  semantics as /admin/users/search (username / first name contains,
  "@" stripped, numeric = exact Telegram ID), composed with the
  category filter; absent/blank search changes nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import User
from app.webapp import api as api_mod
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")

ADMIN_ID = 999001


# ── Statics token ───────────────────────────────────────────────────

def test_statics_token_bumped_to_qa1():
    assert HTML.count("?v=20261115_r152") == 4
    assert "20261102_tb1" not in HTML


# ── Q1: plate grows to contain the caption ──────────────────────────

def _js_slice(start_marker: str, end_marker: str) -> str:
    i = APP_JS.index(start_marker)
    j = APP_JS.index(end_marker, i + len(start_marker))
    return APP_JS[i:j]


def test_q1_fit_function_defined_with_containment_rule():
    # Batch AK rewrite: the JS fit function is retired — containment
    # is now the ENGINE's, in core.subtitle_plate_box: the plate
    # always stays inside the canvas (its clamp), at any sub_y /
    # margin, and the Mini App shows that very plate via the
    # composited preview frame.
    assert "function fitBlurPlateToCaption()" not in APP_JS
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app" / "services" / "recap_engine"))
    import core as _core
    lay = _core.frame_layout("9:16", "blur", 1920, 1080)
    for margin in (0.0, 0.05, 0.5, 0.95):
        plate = _core.subtitle_plate_box(
            1080, 1920, lay, raw_font_size=22, sub_lines=2,
            sub_y=0.85, min_h=None, sub_margin=margin)
        assert 0.0 <= plate["y"] - plate["h"] / 2
        assert plate["y"] + plate["h"] / 2 <= 1.0


def test_q1_blur_sync_always_refits():
    body = _js_slice("function syncBlurBoxVisual()",
                     "function syncSubBoxVisual()")
    # Batch AK: no JS plate to refit — a blur change re-renders the
    # composited frame instead.
    assert "scheduleRecapCompositeRefresh();" in body
    # The slider height is still applied first (the fit only grows).
    # (Batch AA deliberate pin update: the height is now painted
    # picture-relative via blurBoxCanvasGeom — see the Batch AA
    # test file.)
    assert "previewBlur.style.height = `${(geom.height * 100).toFixed(3)}%`;" in body


def test_q1_caption_moves_and_preview_updates_refit():
    sub = _js_slice("function syncSubBoxVisual()",
                    "if (sliderBlurY && previewBlur && valBlurY)")
    assert "syncBlurBoxVisual();" in sub
    # Batch AK: updateSubtitleVisualPreview is a retired stub; the
    # caption-move path is syncSubBoxVisual, which still funnels
    # through the blur sync (asserted above) and now also schedules
    # the composited frame.
    assert "scheduleRecapCompositeRefresh();" in sub


def test_q1_render_payload_untouched():
    # The growth is preview-only: the blur values posted to the
    # render still come from the sliders, never from the plate's
    # (grown) inline style.
    assert "blur_y: sliderBlurY ? parseInt(sliderBlurY.value) : 85" in APP_JS
    assert "blur_h: sliderBlurH ? parseInt(sliderBlurH.value) : 7" in APP_JS


# ── Q2: no error toast when a preview frame is already up ──────────

def test_q2_network_error_classifier():
    body = _js_slice("function isNetworkLevelError(err)",
                     "// ── Home load error strip")
    assert 'err.name === "AbortError"' in body
    assert "err instanceof TypeError" in body
    assert "failed to fetch|networkerror|load failed" in body


def test_q2_guarded_branch_precedes_generic_toast():
    handler = _js_slice('btnAnalyze.addEventListener("click"',
                        'urlInput.addEventListener("input"')
    guard = "} else if (isNetworkLevelError(err) && recapHasSource()) {"
    assert guard in handler
    # The specific branches stay ahead of the guard…
    assert handler.index('msg.includes("Douyin")') < handler.index(guard)
    assert handler.index("blockedLinkInfo(msg).blocked") < handler.index(guard)
    # …and the generic toast still exists AFTER it (failures with no
    # preview on the canvas must keep toasting honestly).
    assert handler.index(guard) < handler.index("showErrorToast(err, 'generic');")
    # The catch still paints the honest unverified info card.
    assert "metadata_verified: false" in handler


# ── F4: /admin/users honours `search` ───────────────────────────────

def _user(uid: int, **kw) -> User:
    return User(
        id=uid,
        first_name=kw.pop("first_name", "Test"),
        username=kw.pop("username", None),
        account_category=kw.pop("account_category", AccountCategory.FREE.value),
        language_code="my",
        **kw,
    )


@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    yield


async def _seed(db, *objs):
    for o in objs:
        db.add(o)
    await db.commit()


def _at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=timezone.utc)


async def _seed_roster(db):
    admin = _user(ADMIN_ID, created_at=_at(1))
    alice = _user(700401, username="alice_mm", first_name="Alice",
                  created_at=_at(2))
    alina = _user(700402, username="alina", first_name="Aye Chan",
                  created_at=_at(3))
    bob = _user(700403, username="bobby", first_name="Bob",
                account_category=AccountCategory.PURCHASED.value,
                created_at=_at(4))
    await _seed(db, admin, alice, alina, bob)
    return admin


@pytest.mark.asyncio
async def test_f4_search_matches_username_contains(db_session):
    admin = await _seed_roster(db_session)
    res = await api_mod.list_admin_users(30, 0, admin, db_session, search="ali")
    ids = {u["id"] for u in res["users"]}
    assert ids == {700401, 700402}  # alice_mm + alina, not bob/admin
    assert res["total"] == 2


@pytest.mark.asyncio
async def test_f4_search_is_case_insensitive_and_matches_first_name(db_session):
    admin = await _seed_roster(db_session)
    by_name = await api_mod.list_admin_users(30, 0, admin, db_session, search="AYE")
    assert [u["id"] for u in by_name["users"]] == [700402]
    by_user = await api_mod.list_admin_users(30, 0, admin, db_session, search="BOBBY")
    assert [u["id"] for u in by_user["users"]] == [700403]


@pytest.mark.asyncio
async def test_f4_search_strips_at_prefix(db_session):
    admin = await _seed_roster(db_session)
    res = await api_mod.list_admin_users(30, 0, admin, db_session, search="@alice_mm")
    assert [u["id"] for u in res["users"]] == [700401]


@pytest.mark.asyncio
async def test_f4_numeric_search_matches_exact_id(db_session):
    admin = await _seed_roster(db_session)
    res = await api_mod.list_admin_users(30, 0, admin, db_session, search="700403")
    assert [u["id"] for u in res["users"]] == [700403]
    assert res["total"] == 1


@pytest.mark.asyncio
async def test_f4_search_composes_with_category(db_session):
    admin = await _seed_roster(db_session)
    paid = await api_mod.list_admin_users(
        30, 0, admin, db_session, category="PURCHASED", search="a")
    assert [u["id"] for u in paid["users"]] == []  # bob has no "a" match
    paid_bob = await api_mod.list_admin_users(
        30, 0, admin, db_session, category="PURCHASED", search="bob")
    assert [u["id"] for u in paid_bob["users"]] == [700403]
    free_ali = await api_mod.list_admin_users(
        30, 0, admin, db_session, category="FREE", search="ali")
    assert {u["id"] for u in free_ali["users"]} == {700401, 700402}


@pytest.mark.asyncio
async def test_f4_blank_or_absent_search_is_plain_browse(db_session):
    admin = await _seed_roster(db_session)
    plain = await api_mod.list_admin_users(30, 0, admin, db_session)
    blank = await api_mod.list_admin_users(30, 0, admin, db_session, search="  ")
    assert plain["total"] == blank["total"] == 4
    assert [u["id"] for u in plain["users"]] == [u["id"] for u in blank["users"]]


@pytest.mark.asyncio
async def test_f4_no_match_returns_empty_with_zero_total(db_session):
    admin = await _seed_roster(db_session)
    res = await api_mod.list_admin_users(30, 0, admin, db_session, search="zzz-nope")
    assert res["users"] == []
    assert res["total"] == 0
    assert res["count"] == 0
