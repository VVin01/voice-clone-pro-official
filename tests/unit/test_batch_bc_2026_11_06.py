"""Batch BC (2026-11-06): Admin income completeness (owner request).

1. BC-1 TEST-ACCOUNT FLAG — a durable per-user boolean
   (users.is_test_account, repo migration pattern in app/db/session.py),
   toggled from the Admin user detail view via
   POST /admin/users/{id}/test-flag, audit-logged (user.test_flag).
   No user IDs are hardcoded anywhere.
2. BC-2 INCOME EXCLUSION — GET /admin/overview income (all four
   windows, sums AND counts) excludes payments whose user is
   test-flagged; read-side only, unflagging re-includes. Mirrors the
   owner's live case: real 20,000 + 30,000 = 50,000 vs the 78,000 the
   card showed when two test payments (8,000 + 20,000) were included.
3. BC-3 INCOME DETAIL — GET /admin/income-detail returns the
   per-payment rows behind the card (date/time, user name + @username,
   package, amount), newest first; its totals (non-test rows only)
   reconcile with the card for every period, with or without the
   "test လည်း ပြမယ်" display toggle.
4. BC-4 AUDIT TRAIL — payment approve/reject audit entries carry
   admin id, target user, amount and the decision. (Approve already
   did; the reject entry previously recorded ONLY the reason and
   hand-built its JSON, so a quoted reason broke it.)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, PaymentStatus
from app.models.schema import AuditLog, PaymentRequest, User
from app.services.ledger import get_or_create_user
from app.services.payments import (
    approve_payment_request,
    create_payment_request,
    get_active_pricing_plans,
    reject_payment_request,
)
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999202


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


def _pay(pid: str, user_id: int, amount: int, status: str, **kw) -> PaymentRequest:
    p = PaymentRequest(
        id=pid,
        user_id=user_id,
        package_code=kw.pop("package_code", "VOICE_30"),
        minutes=kw.pop("minutes", 30),
        amount_mmk=amount,
        status=status,
    )
    if "reviewed_at" in kw:
        p.reviewed_at = kw["reviewed_at"]
    if "created_at" in kw:
        p.created_at = kw["created_at"]
    return p


# ── Token ────────────────────────────────────────────────────────────

def test_bc_cache_token_is_r79():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert html.count("20261125_admin_r1") == 5
    assert "20261104_r76" not in html


# ── BC-1: flag storage, toggle endpoint, audit ───────────────────────

def test_bc_flag_column_and_migration_exist():
    from app.models.schema import User as UserModel

    assert "is_test_account" in UserModel.__table__.columns
    session_src = (ROOT / "app/db/session.py").read_text(encoding="utf-8")
    # PostgreSQL branch + SQLite branch of the repo migration pattern.
    assert "ADD COLUMN IF NOT EXISTS is_test_account BOOLEAN DEFAULT FALSE" in session_src
    assert '"is_test_account" not in cols_users' in session_src


@pytest.mark.asyncio
async def test_bc_test_flag_toggle_persists_and_is_audited(db_session):
    admin = _user(ADMIN_ID)
    target = _user(700201, first_name="Milo", username="Milo_N4U")
    await _seed(db_session, admin, target)

    res = await api_mod.set_admin_user_test_flag(
        target.id, api_mod.TestFlagRequest(is_test_account=True), admin, db_session
    )
    assert res == {"success": True, "is_test_account": True}

    fresh = await db_session.get(User, target.id)
    assert fresh.is_test_account is True

    # The detail payload (what the Mini App toggle renders from)
    # carries the flag.
    detail = await api_mod.get_admin_user_detail(target.id, admin, db_session)
    assert detail["is_test_account"] is True

    logs = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "user.test_flag",
            AuditLog.target_id == str(target.id),
        )
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].admin_id == ADMIN_ID
    assert json.loads(logs[0].details_json)["is_test_account"] is True

    # Unflagging persists too and leaves a second audit entry.
    res = await api_mod.set_admin_user_test_flag(
        target.id, api_mod.TestFlagRequest(is_test_account=False), admin, db_session
    )
    assert res == {"success": True, "is_test_account": False}
    await db_session.refresh(fresh)
    assert fresh.is_test_account is False
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "user.test_flag")
    )).scalars().all()
    assert len(logs) == 2


# ── BC-2/BC-3 seed: the owner's live numbers ─────────────────────────

async def _seed_owner_case(db):
    """Real income 20,000 (today) + 30,000 (10 days ago); test-account
    payments 8,000 + 20,000 (today) — the 78,000 the card used to show."""
    admin = _user(ADMIN_ID)
    buyer = _user(700211, first_name="Real Buyer", username="realbuyer")
    tester = _user(700212, first_name="Milo", username="Milo_N4U",
                   is_test_account=True)
    await _seed(db, admin, buyer, tester)
    now = datetime.now(timezone.utc)
    A = PaymentStatus.APPROVED.value
    rows = [
        _pay("bc-real-today", buyer.id, 20000, A, reviewed_at=now),
        _pay("bc-real-10d", buyer.id, 30000, A,
             reviewed_at=now - timedelta(days=10)),
        _pay("bc-test-a", tester.id, 8000, A, reviewed_at=now),
        _pay("bc-test-b", tester.id, 20000, A, reviewed_at=now),
        # Noise: never income regardless of flags.
        _pay("bc-real-pending", buyer.id, 999000,
             PaymentStatus.PENDING.value, created_at=now),
        _pay("bc-test-rejected", tester.id, 888000,
             PaymentStatus.REJECTED.value, reviewed_at=now),
    ]
    await _seed(db, *rows)
    return admin, buyer, tester


@pytest.mark.asyncio
async def test_bc_overview_income_excludes_test_accounts(db_session):
    admin, _buyer, _tester = await _seed_owner_case(db_session)

    inc = (await api_mod.get_admin_overview(admin, db_session))["income"]
    assert inc["today_mmk"] == 20000 and inc["today_count"] == 1
    assert inc["last_7d_mmk"] == 20000 and inc["last_7d_count"] == 1
    assert inc["last_30d_mmk"] == 50000 and inc["last_30d_count"] == 2
    assert inc["all_time_mmk"] == 50000 and inc["all_time_count"] == 2


@pytest.mark.asyncio
async def test_bc_overview_income_unflag_reincludes(db_session):
    admin, _buyer, tester = await _seed_owner_case(db_session)

    await api_mod.set_admin_user_test_flag(
        tester.id, api_mod.TestFlagRequest(is_test_account=False),
        admin, db_session,
    )
    inc = (await api_mod.get_admin_overview(admin, db_session))["income"]
    # Back to the pre-BC figure: exclusion is a read-side filter on
    # the flag, the payment rows were never altered.
    assert inc["all_time_mmk"] == 78000 and inc["all_time_count"] == 4
    assert inc["today_mmk"] == 48000 and inc["today_count"] == 3


@pytest.mark.asyncio
async def test_bc_income_detail_reconciles_with_card(db_session):
    admin, _buyer, _tester = await _seed_owner_case(db_session)
    overview = (await api_mod.get_admin_overview(admin, db_session))["income"]

    detail = await api_mod.get_admin_income_detail(
        period="all", include_test=False, admin=admin, db=db_session
    )
    assert detail["total_mmk"] == overview["all_time_mmk"] == 50000
    assert detail["count"] == overview["all_time_count"] == 2
    assert [p["id"] for p in detail["payments"]] == [
        "bc-real-today", "bc-real-10d",
    ]  # newest first, test rows hidden by default
    row = detail["payments"][0]
    assert row["user_name"] == "Real Buyer"
    assert row["username"] == "realbuyer"
    assert row["package_code"] == "VOICE_30"
    assert row["amount_mmk"] == 20000
    assert row["is_test"] is False
    assert row["paid_at"] is not None

    # Per-period reconciliation with the card windows.
    today = await api_mod.get_admin_income_detail(
        period="today", include_test=False, admin=admin, db=db_session
    )
    assert today["total_mmk"] == overview["today_mmk"] == 20000
    assert today["count"] == 1
    d30 = await api_mod.get_admin_income_detail(
        period="30d", include_test=False, admin=admin, db=db_session
    )
    assert d30["total_mmk"] == overview["last_30d_mmk"] == 50000
    assert d30["count"] == 2


@pytest.mark.asyncio
async def test_bc_income_detail_test_toggle_is_display_only(db_session):
    admin, _buyer, _tester = await _seed_owner_case(db_session)

    detail = await api_mod.get_admin_income_detail(
        period="all", include_test=True, admin=admin, db=db_session
    )
    # Test payments now SHOW, badged — but the totals do not move.
    assert detail["total_mmk"] == 50000
    assert detail["count"] == 2
    assert len(detail["payments"]) == 4
    test_rows = [p for p in detail["payments"] if p["is_test"]]
    assert {p["id"] for p in test_rows} == {"bc-test-a", "bc-test-b"}
    assert {p["amount_mmk"] for p in test_rows} == {8000, 20000}
    shown_sum = sum(
        p["amount_mmk"] for p in detail["payments"] if not p["is_test"]
    )
    assert shown_sum == detail["total_mmk"]


# ── BC-4: approve/reject audit entries ───────────────────────────────

@pytest.mark.asyncio
async def test_bc_approve_audit_entry_carries_user_and_amount(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=700221)
    await get_active_pricing_plans(db_session)
    payment = await create_payment_request(
        session=db_session, user_id=user.id,
        package_code="MIN_10", receipt_file_id="slip-approve",
    )
    ok, _pay_row, msg = await approve_payment_request(
        session=db_session, payment_id=payment.id, admin_id=ADMIN_ID,
    )
    assert ok is True and msg == "APPROVED"
    await db_session.commit()

    logs = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "payment.approve",
            AuditLog.target_id == payment.id,
        )
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].admin_id == ADMIN_ID
    details = json.loads(logs[0].details_json)
    assert details["user_id"] == user.id
    assert details["amount_mmk"] == payment.amount_mmk == 8000
    assert details["minutes"] == 10


@pytest.mark.asyncio
async def test_bc_reject_audit_entry_carries_user_amount_and_reason(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=700222)
    await get_active_pricing_plans(db_session)
    payment = await create_payment_request(
        session=db_session, user_id=user.id,
        package_code="MIN_30", receipt_file_id="slip-reject",
    )
    # A reason containing a double quote broke the old hand-built
    # JSON details string outright; json.dumps must survive it.
    ok, _pay_row, msg = await reject_payment_request(
        session=db_session, payment_id=payment.id, admin_id=ADMIN_ID,
        reason='Slip "unreadable" — blurry',
    )
    assert ok is True and msg == "REJECTED"
    await db_session.commit()

    logs = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "payment.reject",
            AuditLog.target_id == payment.id,
        )
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].admin_id == ADMIN_ID
    details = json.loads(logs[0].details_json)
    assert details["user_id"] == user.id
    assert details["amount_mmk"] == payment.amount_mmk
    assert details["reason"] == 'Slip "unreadable" — blurry'


# ── Frontend wiring pins ─────────────────────────────────────────────

BC_I18N_KEYS = (
    "admin_income_details_btn", "admin_income_detail_title",
    "admin_income_show_test", "admin_income_test_badge",
    "admin_income_empty", "admin_income_minutes",
    "admin_test_account", "admin_test_account_hint",
    "admin_test_flag_on", "admin_test_flag_off",
)


def test_bc_frontend_wiring_and_i18n():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    i18n = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    css = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")

    # BC-3 sheet + BC-1 toggle markup.
    assert 'id="income-detail-sheet"' in html
    assert 'id="income-detail-show-test"' in html
    assert 'id="income-detail-list"' in html
    assert 'id="adm-test-flag-toggle"' in html
    assert 'onclick="openIncomeDetail(' in html
    assert 'onchange="adminToggleTestFlag(this.checked)"' in html

    # JS calls the two new endpoints.
    assert "/api/admin/income-detail" in js
    assert "/test-flag" in js
    assert "function adminToggleTestFlag" in js
    assert "function loadIncomeDetail" in js

    # Muted TEST treatment exists in CSS.
    assert ".income-detail-row.is-test" in css
    assert ".badge-test" in css

    # AZ-12: every new string is an i18n key in BOTH locales (my + en
    # blocks each define the key exactly once).
    for key in BC_I18N_KEYS:
        assert i18n.count(f"{key}:") == 2, key
