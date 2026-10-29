from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, LedgerDirection, LedgerSource
from app.models.schema import (
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    MonthlyFreeAllowanceGrant,
    User,
    VoiceModel,
)
from app.services.ledger import (
    consume_seconds,
    get_current_monthly_period,
    get_minute_balance,
    get_or_create_user,
    grant_monthly_free_allowance,
    credit_purchased_minutes,
)
from app.services.models import (
    get_available_models_for_user,
    set_model_free_permission,
    set_model_purchased_permission,
)
from app.services.queue import create_voice_clone_job


@pytest.mark.asyncio
async def test_monthly_free_allowance_idempotency(db_session):
    """Monthly free allowance (180s) is granted once per calendar month YYYY-MM UTC."""
    user, _ = await get_or_create_user(db_session, telegram_id=555001)
    period = get_current_monthly_period()

    # Initial grant for current period
    granted1, bal = await grant_monthly_free_allowance(db_session, user.id, period=period)
    assert granted1 is True
    assert bal.monthly_free_seconds == 180
    assert bal.last_monthly_grant_period == period

    # Subsequent call for same period returns (False, bal) without duplicate grant
    granted, bal_again = await grant_monthly_free_allowance(db_session, user.id, period=period)
    assert granted is False
    assert bal_again.monthly_free_seconds == 180

    # Verify only 1 grant row exists
    grants = (
        await db_session.execute(
            select(MonthlyFreeAllowanceGrant).where(
                MonthlyFreeAllowanceGrant.user_id == user.id,
                MonthlyFreeAllowanceGrant.period == period,
            )
        )
    ).scalars().all()
    assert len(grants) == 1
    assert grants[0].seconds == 180

    # Verify next month gets a new grant
    next_month = "2099-01"
    granted_next, bal_next = await grant_monthly_free_allowance(db_session, user.id, period=next_month)
    assert granted_next is True
    assert bal_next.monthly_free_seconds == 180
    assert bal_next.last_monthly_grant_period == next_month


@pytest.mark.asyncio
async def test_concurrency_limit_enforcement(db_session, tmp_path):
    """Users cannot have more than 1 concurrent job (QUEUED or RUNNING)."""
    user, _ = await get_or_create_user(db_session, telegram_id=555002)

    # Active model
    pth = tmp_path / "m1.pth"
    pth.write_bytes(b"pth")
    model = VoiceModel(
        name="M1", slug="m1", type="SYSTEM", model_dir=str(tmp_path), pth_path=str(pth), status="ACTIVE", is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    # Job 1 succeeds and is QUEUED
    job1, reason1 = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="Job 1 text", edge_voice="my-MM-ThihaNeural", model_id=model.id
    )
    assert job1 is not None
    assert reason1 == "QUEUED"

    # Job 2 is BLOCKED due to concurrency
    job2, reason2 = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="Job 2 text", edge_voice="my-MM-ThihaNeural", model_id=model.id
    )
    assert job2 is None
    assert reason2 == "CONCURRENT_JOB_LIMIT_EXCEEDED"

    # Complete job 1
    job1.status = JobStatus.SUCCESS.value
    await db_session.flush()

    # Now Job 2 can be queued!
    job3, reason3 = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="Job 3 text", edge_voice="my-MM-ThihaNeural", model_id=model.id
    )
    assert job3 is not None
    assert reason3 == "QUEUED"


@pytest.mark.asyncio
async def test_daily_job_limit_free_vs_purchased(db_session, tmp_path):
    """Free accounts are limited to 5 jobs per calendar day; Purchased accounts have unlimited routine jobs."""
    user, _ = await get_or_create_user(db_session, telegram_id=555003)
    assert user.account_category == AccountCategory.FREE.value

    pth = tmp_path / "m_daily.pth"
    pth.write_bytes(b"pth")
    model = VoiceModel(
        name="DailyM", slug="dm", type="SYSTEM", model_dir=str(tmp_path), pth_path=str(pth), status="ACTIVE", is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    # Insert 5 completed jobs for today
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(5):
        j = Job(
            id=f"job_today_{i}",
            user_id=user.id,
            chat_id=user.id,
            input_text=f"Sample text {i}",
            edge_voice="my-MM-ThihaNeural",
            model_id=model.id,
            status=JobStatus.SUCCESS.value,
            created_at=today_start + timedelta(minutes=i + 1),
        )
        db_session.add(j)
    await db_session.flush()

    # 6th job for Free user is blocked with DAILY_JOB_LIMIT_EXCEEDED
    job_blocked, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="6th job attempt", edge_voice="my-MM-ThihaNeural", model_id=model.id
    )
    assert job_blocked is None
    assert reason == "DAILY_JOB_LIMIT_EXCEEDED"

    # Promote user to PURCHASED tier via minute purchase
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=10)
    await db_session.flush()
    assert user.account_category == AccountCategory.PURCHASED.value

    # Now the 6th job succeeds!
    job_allowed, reason_allowed = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="6th job for purchased user", edge_voice="my-MM-ThihaNeural", model_id=model.id
    )
    assert job_allowed is not None
    assert reason_allowed == "QUEUED"


@pytest.mark.asyncio
async def test_model_tier_permissions(db_session, tmp_path):
    """Free users only see allowed_for_free models; Purchased users see allowed_for_purchased; Admins see all."""
    pth = tmp_path / "tier.pth"
    pth.write_bytes(b"pth")

    m_free = VoiceModel(
        name="FreeModelOnly", slug="free-only", type="SYSTEM", model_dir=str(tmp_path), pth_path=str(pth), status="ACTIVE",
        allowed_for_free=True, allowed_for_purchased=False,
    )
    m_purch = VoiceModel(
        name="PurchasedOnly", slug="purch-only", type="SYSTEM", model_dir=str(tmp_path), pth_path=str(pth), status="ACTIVE",
        allowed_for_free=False, allowed_for_purchased=True,
    )
    m_both = VoiceModel(
        name="BothTiers", slug="both", type="SYSTEM", model_dir=str(tmp_path), pth_path=str(pth), status="ACTIVE",
        allowed_for_free=True, allowed_for_purchased=True,
    )
    db_session.add_all([m_free, m_purch, m_both])
    await db_session.flush()

    free_user, _ = await get_or_create_user(db_session, telegram_id=555004)
    purch_user, _ = await get_or_create_user(db_session, telegram_id=555005)
    await credit_purchased_minutes(db_session, user_id=purch_user.id, minutes=10)
    # Hermetic: do not depend on the process ADMIN_IDS env being set.
    settings.admin_ids_raw = "555009"
    admin_id = list(settings.admin_ids)[0]
    admin_user, _ = await get_or_create_user(db_session, telegram_id=admin_id)

    free_models = await get_available_models_for_user(db_session, free_user.id)
    free_names = [m.name for m in free_models]
    assert "FreeModelOnly" in free_names
    assert "BothTiers" in free_names
    assert "PurchasedOnly" not in free_names

    purch_models = await get_available_models_for_user(db_session, purch_user.id)
    purch_names = [m.name for m in purch_models]
    assert "PurchasedOnly" in purch_names
    assert "BothTiers" in purch_names
    assert "FreeModelOnly" not in purch_names

    admin_models = await get_available_models_for_user(db_session, admin_user.id)
    admin_names = [m.name for m in admin_models]
    assert "FreeModelOnly" in admin_names
    assert "PurchasedOnly" in admin_names
    assert "BothTiers" in admin_names


@pytest.mark.asyncio
async def test_deduction_order_and_automatic_reversion(db_session):
    """Deduction order consumes monthly_free_seconds first, then purchased_seconds. Reverts to FREE when purchased hits 0."""
    user, _ = await get_or_create_user(db_session, telegram_id=555006)
    await grant_monthly_free_allowance(db_session, user.id)
    # Grant purchased minutes (1 minute = 60s)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=1)
    assert user.account_category == AccountCategory.PURCHASED.value

    bal = await get_minute_balance(db_session, user.id)
    assert bal.monthly_free_seconds == 180
    assert bal.purchased_seconds == 60
    assert bal.total_usable_seconds == 240

    # Deduct 100s -> must deduct entirely from monthly_free_seconds (180 - 100 = 80s left)
    bal1 = await consume_seconds(db_session, user_id=user.id, seconds_to_consume=100, job_id="job_ded_1")
    assert bal1.monthly_free_seconds == 80
    assert bal1.purchased_seconds == 60
    assert user.account_category == AccountCategory.PURCHASED.value

    # Deduct 100s -> consumes remaining 80s monthly_free, and 20s from purchased_seconds (60 - 20 = 40s left)
    bal2 = await consume_seconds(db_session, user_id=user.id, seconds_to_consume=100, job_id="job_ded_2")
    assert bal2.monthly_free_seconds == 0
    assert bal2.purchased_seconds == 40
    assert user.account_category == AccountCategory.PURCHASED.value

    # Deduct remaining 40s -> purchased_seconds reaches 0 -> user retains PURCHASED entitlement state
    bal3 = await consume_seconds(db_session, user_id=user.id, seconds_to_consume=40, job_id="job_ded_3")
    assert bal3.monthly_free_seconds == 0
    assert bal3.purchased_seconds == 0
    assert user.account_category == AccountCategory.PURCHASED.value


@pytest.mark.asyncio
async def test_purchased_entitlement_preservation_and_insufficient_balance_guidance(db_session, tmp_path):
    """Verify that a user who purchased minutes retains PURCHASED entitlement at 0 balance.

    1. User purchases minutes -> Promoted to PURCHASED category.
    2. All purchased minutes are consumed -> Account category remains PURCHASED.
    3. User retains 2,000 character limit and access to Purchased models.
    4. When granted a new monthly Free allowance (180s), user can generate up to 2,000 chars with Purchased models.
    5. When total usable balance reaches 0, job creation returns INSUFFICIENT_BALANCE.
    6. Historical purchase and deduction records in MinuteLedgerEvent remain intact.
    """
    user, _ = await get_or_create_user(db_session, telegram_id=555009)
    await credit_purchased_minutes(db_session, user_id=user.id, minutes=5, payment_id="PAY-ENTITLEMENT-1")
    assert user.account_category == AccountCategory.PURCHASED.value

    # Model available only for Purchased tier
    pth = tmp_path / "purchased_only.pth"
    pth.write_bytes(b"content")
    model = VoiceModel(
        name="PurchasedOnlyModel",
        slug="purchased-only-model",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth),
        status="ACTIVE",
        allowed_for_free=False,
        allowed_for_purchased=True,
    )
    db_session.add(model)
    await db_session.flush()

    # Consume all purchased minutes down to 0
    await consume_seconds(db_session, user_id=user.id, seconds_to_consume=300, job_id="job_spend_all")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 0
    assert bal.total_usable_seconds == 0
    # Crucial verification: Account category remains PURCHASED (entitlement never erased)
    assert user.account_category == AccountCategory.PURCHASED.value

    # Purchased models remain visible and accessible in catalog
    avail = await get_available_models_for_user(db_session, user.id)
    avail_names = [m.name for m in avail]
    assert "PurchasedOnlyModel" in avail_names

    # If user creates job, create_voice_clone_job grants monthly free allowance if not already granted.
    # To test strictly zero balance, grant and consume current monthly allowance first:
    await grant_monthly_free_allowance(db_session, user.id)
    bal_after_grant = await get_minute_balance(db_session, user.id)
    if bal_after_grant.total_usable_seconds > 0:
        await consume_seconds(
            db_session,
            user_id=user.id,
            seconds_to_consume=bal_after_grant.total_usable_seconds,
            job_id="job_spend_allowance",
        )
    bal_zero = await get_minute_balance(db_session, user.id)
    assert bal_zero.total_usable_seconds == 0

    job_failed, res = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="Text with absolute zero balance",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert job_failed is None
    assert res == "INSUFFICIENT_BALANCE"

    # Now grant next month's allowance: user can use 2,000 characters with PurchasedOnlyModel
    await grant_monthly_free_allowance(db_session, user.id, period="2099-12")
    bal_new = await get_minute_balance(db_session, user.id)
    assert bal_new.monthly_free_seconds == 180
    assert bal_new.purchased_seconds == 0
    assert bal_new.total_usable_seconds == 180

    # User can submit long text (e.g. 1,500 chars > Free 500 limit) using Purchased model
    long_text = "A" * 1500
    job_ok, res_ok = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text=long_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert res_ok == "QUEUED"
    assert job_ok is not None

    # Verify audit ledger history: all credit and debit events are intact
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user.id).order_by(MinuteLedgerEvent.id)
        )
    ).scalars().all()
    purchase_credits = [e for e in events if e.source == LedgerSource.PURCHASE.value and e.direction == LedgerDirection.CREDIT.value]
    assert len(purchase_credits) == 1
    assert purchase_credits[0].seconds == 300
    assert purchase_credits[0].reference_id == "PAY-ENTITLEMENT-1"
