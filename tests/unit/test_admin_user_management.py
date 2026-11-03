from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
# Bot cleanup 2026-11-03: this file's chat-side user-management /
# adjust-FSM / queue tests retired with the screens they pinned. What
# remains are the SERVICE-level admin_adjust_balance tests — the
# service is still live (the Mini App Admin tab calls it) — and the
# locale terminology checks.
from app.models.enums import AccountCategory, JobStatus, LedgerDirection, LedgerSource
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    User,
    utc_now,
)
from app.services.i18n import t
from app.services.ledger import (
    admin_adjust_balance,
    get_minute_balance,
    get_or_create_user,
    get_paginated_users,
    get_user_recent_ledger_events,
)


class FakeFSMContext:
    """In-memory FSMContext replica for reliable handler unit tests."""

    def __init__(self, initial_state=None, initial_data=None):
        self.current_state = initial_state
        self.data = initial_data or {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}










@pytest.mark.asyncio
async def test_admin_adjust_balance_credit_success_and_status_promotion(db_session):
    u = User(id=8801, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    assert bal.purchased_seconds == 0
    assert u.account_category == AccountCategory.FREE.value

    updated_bal = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=600,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Goodwill compensation for downtime",
        idempotency_key="ADJ-TEST-CREDIT-1",
    )
    await db_session.flush()

    assert updated_bal.purchased_seconds == 600
    assert updated_bal.purchased_minutes == 10.0
    assert u.account_category == AccountCategory.PURCHASED.value

    # Verify MinuteLedgerEvent
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == u.id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].source == LedgerSource.ADJUST.value
    assert events[0].direction == LedgerDirection.CREDIT.value
    assert events[0].seconds == 600
    assert events[0].reference_id == "ADJ-TEST-CREDIT-1"
    assert "Goodwill compensation for downtime" in events[0].note

    # Verify AuditLog
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(u.id))
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "balance.adjust.credit"
    assert logs[0].admin_id == 888111
    assert "ADJ-TEST-CREDIT-1" in logs[0].details_json


@pytest.mark.asyncio
async def test_admin_adjust_balance_debit_success_and_status_demotion(db_session):
    u = User(id=8802, account_category=AccountCategory.PURCHASED.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    bal.purchased_seconds = 600  # 10m
    await db_session.flush()

    # Debit 600s: purchased balance reaches 0, demotes to FREE
    updated_bal = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=600,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.DEBIT,
        reason="Reversing erroneous credit",
        idempotency_key="ADJ-TEST-DEBIT-1",
    )
    await db_session.flush()

    assert updated_bal.purchased_seconds == 0
    assert u.account_category == AccountCategory.FREE.value


@pytest.mark.asyncio
async def test_admin_adjust_balance_insufficient_debit_rejected(db_session):
    u = User(id=8803, account_category=AccountCategory.PURCHASED.value)
    db_session.add(u)
    await db_session.flush()

    bal = await get_minute_balance(db_session, u.id)
    bal.purchased_seconds = 180  # 3m
    await db_session.flush()

    # Attempt to debit 600s (10m)
    with pytest.raises(ValueError, match="Insufficient purchased balance"):
        await admin_adjust_balance(
            session=db_session,
            user_id=u.id,
            admin_id=888111,
            seconds=600,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.DEBIT,
            reason="Debit too large",
            idempotency_key="ADJ-OVER-DEBIT",
        )

    # Balance and account category remain intact
    assert bal.purchased_seconds == 180
    assert u.account_category == AccountCategory.PURCHASED.value


@pytest.mark.asyncio
async def test_admin_adjust_balance_validation_errors(db_session):
    u = User(id=8804, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    # Empty reason
    with pytest.raises(ValueError, match="mandatory and cannot be empty"):
        await admin_adjust_balance(
            session=db_session,
            user_id=u.id,
            admin_id=888111,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="   ",
        )

    # Negative seconds
    with pytest.raises(ValueError, match="must be positive"):
        await admin_adjust_balance(
            session=db_session,
            user_id=u.id,
            admin_id=888111,
            seconds=-60,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Valid reason",
        )

    # Missing user
    with pytest.raises(ValueError, match="User 999988 not found"):
        await admin_adjust_balance(
            session=db_session,
            user_id=999988,
            admin_id=888111,
            seconds=300,
            source=LedgerSource.PURCHASE,
            direction=LedgerDirection.CREDIT,
            reason="Valid reason",
        )


@pytest.mark.asyncio
async def test_admin_adjust_balance_idempotency_duplicate_suppression(db_session):
    u = User(id=8805, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    token = "IDEMPOTENT-NONCE-ABC"

    # First execution: +300s
    bal1 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Test idempotency",
        idempotency_key=token,
    )
    assert bal1.purchased_seconds == 300

    # Second execution with same key: must NOT add 300s again!
    bal2 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Test idempotency replay",
        idempotency_key=token,
    )
    assert bal2.purchased_seconds == 300  # Exactly 300, never 600!

    # Exactly one ledger event with that reference_id
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == u.id,
            MinuteLedgerEvent.reference_id == token,
        )
    )).scalars().all()
    assert len(events) == 1

    # Exactly one AdjustmentIdempotencyRecord with that idempotency_key
    records = (await db_session.execute(
        select(AdjustmentIdempotencyRecord).where(
            AdjustmentIdempotencyRecord.idempotency_key == token,
        )
    )).scalars().all()
    assert len(records) == 1
    assert records[0].user_id == u.id
    assert records[0].seconds == 300

    # Exactly one AuditLog with that idempotency_key
    audits = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.target_id == str(u.id),
        )
    )).scalars().all()
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_admin_adjust_balance_concurrent_duplicate_multi_session(tmp_path):
    """Test concurrent duplicate adjustments across separate sessions on an isolated database."""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from app.db.base import Base

    db_file = tmp_path / "concurrent_test.db"
    test_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(test_url, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as setup_session:
        u = User(id=7777, account_category=AccountCategory.FREE.value)
        setup_session.add(u)
        await setup_session.commit()

    token = "CONCURRENT-NONCE-XYZ"

    async def execute_adjust(worker_id: int):
        async with session_factory() as session:
            try:
                bal = await admin_adjust_balance(
                    session=session,
                    user_id=7777,
                    admin_id=888111,
                    seconds=300,
                    source=LedgerSource.PURCHASE,
                    direction=LedgerDirection.CREDIT,
                    reason=f"Concurrent adjust from worker {worker_id}",
                    idempotency_key=token,
                )
                await session.commit()
                return "ok", bal.purchased_seconds
            except Exception:
                await session.rollback()
                return "conflict", 0

    results = await asyncio.gather(execute_adjust(1), execute_adjust(2))
    assert len(results) == 2

    # Verify database state
    async with session_factory() as verify_session:
        final_bal = await get_minute_balance(verify_session, 7777)
        assert final_bal.purchased_seconds == 300  # Exactly 300s, NEVER 600s!

        events = (await verify_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == 7777,
                MinuteLedgerEvent.reference_id == token,
            )
        )).scalars().all()
        assert len(events) == 1

        idemp_recs = (await verify_session.execute(
            select(AdjustmentIdempotencyRecord).where(
                AdjustmentIdempotencyRecord.idempotency_key == token,
            )
        )).scalars().all()
        assert len(idemp_recs) == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_adjust_balance_different_keys_allowed(db_session):
    u = User(id=8809, account_category=AccountCategory.FREE.value)
    db_session.add(u)
    await db_session.flush()

    bal1 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Adjustment 1",
        idempotency_key="ADJ-DIFF-KEY-1",
    )
    assert bal1.purchased_seconds == 300

    bal2 = await admin_adjust_balance(
        session=db_session,
        user_id=u.id,
        admin_id=888111,
        seconds=300,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Adjustment 2",
        idempotency_key="ADJ-DIFF-KEY-2",
    )
    assert bal2.purchased_seconds == 600  # Both legitimate adjustments applied

    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == u.id)
    )).scalars().all()
    assert len(events) == 2














@pytest.mark.asyncio
async def test_localization_parity_and_terminology():
    # Verify terminology in en and my messages.json
    en_desc = t("buy_minutes_desc", "en")
    assert "VIP" in en_desc  # R40 canon: user-facing paid status is VIP

    long_err_en = t("tts_text_too_long", "en", max_chars=500, curr_chars=600)
    assert "account type" in long_err_en
    assert "tier" not in long_err_en

    admin_tts_en = t("tts_admin_enter_text", "en", max_chars=5000)
    assert "User account" in admin_tts_en
    assert "tier" not in admin_tts_en

    pay_approve_en = t("payment_approved_user_notify", "en", minutes=10, recap_credits=5)
    # R40: user-facing paid status is "VIP" (PURCHASED is admin/DB only)
    # and the notice states BOTH minutes and recap credits.
    assert "Account status: <b>VIP</b>" in pay_approve_en
    assert "Recap Credits: <b>5</b>" in pay_approve_en
    assert "tier" not in pay_approve_en.lower()

    pricing_title_en = t("admin_pricing_title", "en")
    assert "Active Minute Packages" in pricing_title_en

    pricing_title_my = t("admin_pricing_title", "my")
    assert "မိနစ်ပက်ကေ့ဂျ်များ" in pricing_title_my
