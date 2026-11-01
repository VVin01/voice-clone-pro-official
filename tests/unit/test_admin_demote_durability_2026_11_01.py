"""Admin demote durability (owner report 2026-11-01, late evening).

The owner demoted a paying user back to Free from the Mini App Admin
("Free ပြန်ချ Crd နှုတ်တာမှာ VIP ကကျန်ခဲ့တယ်") — the VIP tier stayed /
came back. Root cause: ``account_category`` is entitlement-derived in
``app/services/ledger.py`` — ``consume_seconds`` re-asserts PURCHASED
on EVERY job while ``purchased_seconds > 0``, and a PURCHASE-source
refund does the same, so flipping the field alone never sticks while
the user still holds purchased seconds.

Fix (in ``adjust_user_status``): a PURCHASED -> FREE category change
first drains the purchased bucket to zero through the audited
``admin_adjust_balance`` path (post-delta balance, admin's own reason),
then sets the category — no entitlement fuel remains, and the drain is
recorded in the ledger, the ledger's audit log and the endpoint's own
``user.adjust`` audit details (``purchased_seconds_zeroed``).

These pins cover: (a) demote with a purchased balance, (a2) the
owner's exact composite case (demote + partial deduction), (b) no
revival on a later consume, (c) demote with a zero balance, and
(d) promote / plain adjustments behaving exactly as before.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import AuditLog, MinuteBalance, MinuteLedgerEvent, User
from app.services.ledger import consume_seconds
from app.webapp import api as api_mod

ADMIN_ID = 999001
USER_ID = 700111
OTHER_ID = 700222


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
    # USER_ID must NOT be an admin: consume_seconds skips admins, which
    # would hide the revival path this file exists to pin down.
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    yield


async def _seed(db, *objs):
    for o in objs:
        db.add(o)
    await db.commit()


async def _events(db, uid):
    return (await db.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == uid)
    )).scalars().all()


async def _audits(db, uid, action=None):
    stmt = select(AuditLog).where(AuditLog.target_id == str(uid))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return (await db.execute(stmt)).scalars().all()


# ── (a) Demote with purchased balance: zeroed, categorised, audited ──

@pytest.mark.asyncio
async def test_demote_zeroes_purchased_balance_and_audits(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID, account_category=AccountCategory.PURCHASED.value)
    bal = MinuteBalance(user_id=USER_ID, purchased_seconds=1800)
    await _seed(db_session, admin, target, bal)

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=USER_ID,
            account_category=AccountCategory.FREE.value,
            reason="refund issued - demote to free",
        ),
        admin, db_session,
    )

    assert res["success"] is True
    assert res["purchased_seconds_zeroed"] == 1800

    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.account_category == AccountCategory.FREE.value
    refreshed_bal = await db_session.get(MinuteBalance, USER_ID)
    assert refreshed_bal.purchased_seconds == 0

    # Ledger trail: one audited ADJUST debit of exactly the balance.
    events = await _events(db_session, USER_ID)
    debits = [e for e in events if e.direction == LedgerDirection.DEBIT.value]
    assert len(debits) == 1
    assert debits[0].seconds == 1800
    assert debits[0].source == LedgerSource.ADJUST.value

    # Ledger's own audit row + the endpoint's user.adjust details.
    ledger_audits = await _audits(db_session, USER_ID, "balance.adjust.debit")
    assert len(ledger_audits) == 1
    endpoint_audits = await _audits(db_session, USER_ID, "user.adjust")
    assert len(endpoint_audits) == 1
    details = json.loads(endpoint_audits[0].details_json)
    assert details["account_category"] == AccountCategory.FREE.value
    assert details["purchased_seconds_zeroed"] == 1800


# ── (a2) The owner's exact case: demote + partial deduction ──────────

@pytest.mark.asyncio
async def test_demote_with_partial_deduction_zeroes_remainder(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID, account_category=AccountCategory.PURCHASED.value)
    bal = MinuteBalance(user_id=USER_ID, purchased_seconds=3600)
    await _seed(db_session, admin, target, bal)

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=USER_ID,
            account_category=AccountCategory.FREE.value,
            add_minutes=-10,
            reason="demote and partial clawback",
        ),
        admin, db_session,
    )

    assert res["success"] is True
    # The explicit -600s delta lands first; the demote drains the rest.
    assert res["purchased_seconds_zeroed"] == 3000
    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.account_category == AccountCategory.FREE.value
    refreshed_bal = await db_session.get(MinuteBalance, USER_ID)
    assert refreshed_bal.purchased_seconds == 0
    events = await _events(db_session, USER_ID)
    total_debited = sum(
        e.seconds for e in events if e.direction == LedgerDirection.DEBIT.value
    )
    assert total_debited == 3600


# ── (b) A later consume of free minutes must NOT revive PURCHASED ────

@pytest.mark.asyncio
async def test_consume_after_demote_does_not_revive_purchased(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID, account_category=AccountCategory.PURCHASED.value)
    bal = MinuteBalance(
        user_id=USER_ID, purchased_seconds=1800, monthly_free_seconds=3600
    )
    await _seed(db_session, admin, target, bal)

    await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=USER_ID,
            account_category=AccountCategory.FREE.value,
            reason="demote to free",
        ),
        admin, db_session,
    )

    # A normal voice job drawing on the free bucket: before the fix,
    # consume_seconds re-asserted PURCHASED here because purchased
    # seconds were still on the books.
    await consume_seconds(db_session, USER_ID, 600, job_id="job-after-demote")
    await db_session.commit()

    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.account_category == AccountCategory.FREE.value
    refreshed_bal = await db_session.get(MinuteBalance, USER_ID)
    assert refreshed_bal.purchased_seconds == 0
    assert refreshed_bal.monthly_free_seconds == 3000


# ── (c) Demote when there is nothing to drain ────────────────────────

@pytest.mark.asyncio
async def test_demote_with_zero_purchased_balance(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID, account_category=AccountCategory.PURCHASED.value)
    bal = MinuteBalance(user_id=USER_ID, purchased_seconds=0)
    await _seed(db_session, admin, target, bal)

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=USER_ID,
            account_category=AccountCategory.FREE.value,
            reason="tier cleanup",
        ),
        admin, db_session,
    )

    assert res["success"] is True
    assert res["purchased_seconds_zeroed"] == 0
    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.account_category == AccountCategory.FREE.value
    # No ledger call was needed, so no ledger event exists.
    assert await _events(db_session, USER_ID) == []


# ── (d) Promote + plain adjustments: behaviour exactly as before ─────

@pytest.mark.asyncio
async def test_promote_without_credit_unchanged(db_session):
    admin = _user(ADMIN_ID)
    target = _user(USER_ID)  # FREE
    bal = MinuteBalance(user_id=USER_ID, purchased_seconds=0)
    await _seed(db_session, admin, target, bal)

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=USER_ID,
            account_category=AccountCategory.PURCHASED.value,
            reason="manual vip grant",
        ),
        admin, db_session,
    )

    assert res["success"] is True
    assert res["purchased_seconds_zeroed"] == 0
    refreshed = await db_session.get(User, USER_ID)
    assert refreshed.account_category == AccountCategory.PURCHASED.value
    refreshed_bal = await db_session.get(MinuteBalance, USER_ID)
    assert refreshed_bal.purchased_seconds == 0
    assert await _events(db_session, USER_ID) == []


@pytest.mark.asyncio
async def test_plain_credit_and_partial_debit_unchanged(db_session):
    admin = _user(ADMIN_ID)
    target = _user(OTHER_ID)  # FREE
    bal = MinuteBalance(user_id=OTHER_ID, purchased_seconds=0)
    await _seed(db_session, admin, target, bal)

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=OTHER_ID, add_minutes=30, reason="support goodwill"
        ),
        admin, db_session,
    )
    assert res["purchased_seconds_zeroed"] == 0
    refreshed_bal = await db_session.get(MinuteBalance, OTHER_ID)
    assert refreshed_bal.purchased_seconds == 1800
    # Ledger credit promotion semantics are untouched.
    refreshed = await db_session.get(User, OTHER_ID)
    assert refreshed.account_category == AccountCategory.PURCHASED.value

    res = await api_mod.adjust_user_status(
        api_mod.AdjustUserRequest(
            user_id=OTHER_ID, add_minutes=-10, reason="partial clawback"
        ),
        admin, db_session,
    )
    assert res["purchased_seconds_zeroed"] == 0
    refreshed_bal = await db_session.get(MinuteBalance, OTHER_ID)
    assert refreshed_bal.purchased_seconds == 1200
    # A plain debit that does not empty the bucket never demoted before
    # this fix and must not demote now either.
    refreshed = await db_session.get(User, OTHER_ID)
    assert refreshed.account_category == AccountCategory.PURCHASED.value
