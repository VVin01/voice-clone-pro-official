"""Batch R (2026-11-04): admin fixes approved by Ulfa.

1. TRUTHFUL BLOCKED COUNT — the Admin overview "ပိတ်ထားသူများ" card
   counted ONLY admin bans (status == BLOCKED), so it never moved
   when a broadcast discovered users who had blocked the bot
   (bot_blocked flag). The headline is now the combined OR-count,
   with banned_by_admin / bot_blocked_users exposed for the split
   sub-line. Counting/presentation only — no semantics change.
2. DASHBOARD BUTTONS on the payment result card — the result card
   lives on the slip PHOTO; handle_admin_payments' empty branch used
   a raw edit_text ("there is no text in the message to edit" →
   generic error popup) and handle_admin_dashboard's raw edit_text
   was swallowed (dead button). Both now go through the media-aware
   safe_edit_message, and the error string joined the global
   harmless-error suppression list as defence in depth.
3. INCOME (ဝင်ငွေ) CARD — GET /admin/overview carries an `income`
   object summing APPROVED payment_requests (amount_mmk is the
   server-derived plan price) over Rangoon-today / rolling 7d /
   rolling 30d / all-time, bucketed on COALESCE(reviewed_at,
   created_at).
"""
from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize
from aiogram.types import User as TgUser

from app.config import settings
from app.models.enums import AccountCategory, PaymentStatus, UserStatus
from app.models.schema import PaymentRequest, User
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999101
RANGOON = timezone(timedelta(hours=6, minutes=30))


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


# ── 1. Blocked counts ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_overview_blocked_headline_combines_admin_bans_and_bot_blockers(db_session):
    admin = _user(ADMIN_ID)
    banned_a = _user(700001, status=UserStatus.BLOCKED.value)
    banned_b = _user(700002, status=UserStatus.BLOCKED.value)
    flagged_a = _user(700003, bot_blocked=True)
    flagged_b = _user(700004, bot_blocked=True)
    flagged_c = _user(700005, bot_blocked=True)
    # Overlap: admin-banned AND has blocked the bot — one person,
    # must count once in the headline, once in each split figure.
    both = _user(700006, status=UserStatus.BLOCKED.value, bot_blocked=True)
    normal = _user(700007)
    await _seed(db_session, admin, banned_a, banned_b, flagged_a,
                flagged_b, flagged_c, both, normal)

    data = await api_mod.get_admin_overview(admin, db_session)

    assert data["banned_by_admin"] == 3       # banned_a, banned_b, both
    assert data["bot_blocked_users"] == 4     # flagged_a/b/c, both
    assert data["banned_users"] == 6          # OR-count: overlap once


@pytest.mark.asyncio
async def test_overview_blocked_counts_zero_on_empty_db(db_session):
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin)
    data = await api_mod.get_admin_overview(admin, db_session)
    assert data["banned_users"] == 0
    assert data["banned_by_admin"] == 0
    assert data["bot_blocked_users"] == 0


# ── 3. Income windows ────────────────────────────────────────────────

def _pay(pid: str, user_id: int, amount: int, status: str, **kw) -> PaymentRequest:
    p = PaymentRequest(
        id=pid,
        user_id=user_id,
        package_code="VOICE_30",
        minutes=30,
        amount_mmk=amount,
        status=status,
    )
    if "reviewed_at" in kw:
        p.reviewed_at = kw["reviewed_at"]
    if "created_at" in kw:
        p.created_at = kw["created_at"]
    return p


def _rangoon_midnight_utc(now: datetime) -> datetime:
    return now.astimezone(RANGOON).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_overview_income_windows_and_boundaries(db_session):
    admin = _user(ADMIN_ID)
    buyer = _user(700111)
    await _seed(db_session, admin, buyer)

    now = datetime.now(timezone.utc)
    midnight = _rangoon_midnight_utc(now)
    mid_today = midnight + (now - midnight) / 2  # safely inside today

    A = PaymentStatus.APPROVED.value
    rows = [
        # today (mid-day Rangoon)
        _pay("inc-today", buyer.id, 1000, A, reviewed_at=mid_today),
        # Rangoon-midnight boundary: +1s is today, −1s is yesterday
        _pay("inc-bound-in", buyer.id, 2000, A, reviewed_at=midnight + timedelta(seconds=1)),
        _pay("inc-bound-out", buyer.id, 8000, A, reviewed_at=midnight - timedelta(seconds=1)),
        # legacy row: reviewed_at NULL → created_at fallback (today)
        _pay("inc-null-reviewed", buyer.id, 4000, A, reviewed_at=None, created_at=now),
        # rolling windows
        _pay("inc-3d", buyer.id, 16000, A, reviewed_at=now - timedelta(days=3)),
        _pay("inc-10d", buyer.id, 32000, A, reviewed_at=now - timedelta(days=10)),
        _pay("inc-40d", buyer.id, 64000, A, reviewed_at=now - timedelta(days=40)),
        # never income: not APPROVED
        _pay("inc-rejected", buyer.id, 128000, PaymentStatus.REJECTED.value, reviewed_at=now),
        _pay("inc-pending", buyer.id, 256000, PaymentStatus.PENDING.value, created_at=now),
    ]
    await _seed(db_session, *rows)

    data = await api_mod.get_admin_overview(admin, db_session)
    inc = data["income"]

    assert inc["today_mmk"] == 1000 + 2000 + 4000
    assert inc["today_count"] == 3
    assert inc["last_7d_mmk"] == 7000 + 8000 + 16000
    assert inc["last_7d_count"] == 5
    assert inc["last_30d_mmk"] == 31000 + 32000
    assert inc["last_30d_count"] == 6
    assert inc["all_time_mmk"] == 63000 + 64000
    assert inc["all_time_count"] == 7


def test_overview_income_uses_per_row_aggregation_not_sql_sum():
    # Codebase convention (premium-round D1 sweep): no SQL SUM under
    # app/ — the admin API aggregates per-row. Batch R's income must
    # honour it; this pins the api module specifically.
    src = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    assert "func.sum(" not in src


@pytest.mark.asyncio
async def test_overview_income_empty_db_is_zeros(db_session):
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin)
    data = await api_mod.get_admin_overview(admin, db_session)
    assert data["income"] == {
        "today_mmk": 0, "today_count": 0,
        "last_7d_mmk": 0, "last_7d_count": 0,
        "last_30d_mmk": 0, "last_30d_count": 0,
        "all_time_mmk": 0, "all_time_count": 0,
    }


# ── 2. Handler source pins ───────────────────────────────────────────

def _func_source(rel_path: str, name: str) -> str:
    src = (ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(src, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found in {rel_path}")


def test_dashboard_and_payments_handlers_have_no_raw_edit_text():
    for name in ("handle_admin_dashboard", "handle_admin_payments"):
        body = _func_source("app/handlers/admin.py", name)
        assert ".edit_text(" not in body, name
        assert "safe_edit_message(" in body, name


def test_result_card_buttons_and_safe_edit_survive():
    src = (ROOT / "app/handlers/admin.py").read_text(encoding="utf-8")
    # The buttons stay, wired to the same callbacks.
    assert 'callback_data="admin:payments"' in src
    assert 'callback_data="admin:dashboard"' in src
    for name in ("handle_admin_pay_approve_confirm", "handle_admin_pay_reject_confirm"):
        body = _func_source("app/handlers/admin.py", name)
        assert "safe_edit_message(" in body, name
        assert ".edit_text(" not in body, name


def test_no_text_to_edit_error_is_suppressed_globally():
    src = (ROOT / "app/handlers/__init__.py").read_text(encoding="utf-8")
    assert '"there is no text in the message to edit"' in src


# ── 2. Behaviour: the buttons work from the slip PHOTO card ─────────

def _photo_message() -> Message:
    return Message(
        message_id=42,
        date=datetime.now(timezone.utc),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Boss"),
        photo=[PhotoSize(file_id="ph", file_unique_id="phu", width=10, height=10)],
        caption="payment slip",
    )


def _admin_callback(data: str, message: Message) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="Boss")
    cb.data = data
    cb.message = message
    cb.bot = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_payments_button_after_last_approval_edits_photo_caption(db_session):
    """The exact failure Ulfa hit: approve the last slip, tap
    '💳 Payments Dashboard' on the photo result card → previously the
    generic error popup; now the caption is edited via safe path."""
    from app.handlers import admin as admin_handlers

    settings.admin_ids_raw = str(ADMIN_ID)

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    cb = _admin_callback("admin:payments", _photo_message())
    with patch.object(Message, "edit_caption", new=AsyncMock()) as edit_caption, \
         patch.object(admin_handlers, "AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await admin_handlers.handle_admin_payments(cb)

    edit_caption.assert_awaited_once()
    assert edit_caption.await_args.kwargs["caption"]  # non-empty text
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_dashboard_button_on_photo_card_edits_caption(db_session):
    """'🛠️ Admin Dashboard' on the same photo card was silently dead
    (raw edit_text, exception swallowed); it must now navigate."""
    from app.handlers import admin as admin_handlers

    settings.admin_ids_raw = str(ADMIN_ID)
    cb = _admin_callback("admin:dashboard", _photo_message())
    with patch.object(Message, "edit_caption", new=AsyncMock()) as edit_caption:
        await admin_handlers.handle_admin_dashboard(cb, session=db_session)

    edit_caption.assert_awaited_once()
    cb.answer.assert_awaited()


# ── Frontend wiring + cache pin ──────────────────────────────────────

def test_frontend_income_and_banned_split_wiring():
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    i18n_js = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    index_html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")

    # Banned split sub-line.
    assert 'id="adm-banned-split"' in index_html
    assert "stats.banned_by_admin" in app_js
    assert "stats.bot_blocked_users" in app_js
    assert "admin_stat_banned_split" in app_js

    # Income card + wiring.
    assert 'id="admin-income-card"' in index_html
    for el in ("adm-income-today", "adm-income-7d", "adm-income-30d", "adm-income-all"):
        assert f'id="{el}"' in index_html
        assert f'id="{el}-count"' in index_html
        assert el in app_js
    assert "stats.income" in app_js
    assert ".toLocaleString() + \" MMK\"" in app_js

    # i18n parity: every new key exists in both locales.
    for key in (
        "admin_stat_banned_split", "admin_stat_income", "admin_income_today",
        "admin_income_7d", "admin_income_30d", "admin_income_all",
        "admin_income_count",
    ):
        assert i18n_js.count(key + ":") >= 2, key


def test_cache_version_r52():
    index_html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert index_html.count("?v=20261104_r129") == 4
