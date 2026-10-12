import pytest
from sqlalchemy import select
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import MinuteLedgerEvent, Referral, ReferralReward, User
from app.services.ledger import (
    MAX_REFERRALS_REWARDED,
    REFERRAL_REWARD_SECONDS,
    WELCOME_BONUS_SECONDS,
    award_welcome_bonus,
    confirm_referral_and_reward,
    consume_seconds,
    credit_purchased_minutes,
    get_minute_balance,
    get_or_create_user,
    get_referral_stats,
)


@pytest.mark.asyncio
async def test_welcome_bonus_idempotent(db_session):
    # Register user
    user, is_new = await get_or_create_user(db_session, telegram_id=1001)
    assert is_new is True
    assert user.welcome_bonus_awarded is False

    # Award welcome bonus first time
    awarded, bal = await award_welcome_bonus(db_session, user.id)
    assert awarded is True
    assert bal.welcome_seconds == WELCOME_BONUS_SECONDS  # 300s
    assert bal.total_usable_seconds == 300
    assert bal.total_usable_minutes == 5.0
    assert user.welcome_bonus_awarded is True

    # Check ledger event
    events = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user.id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].source == LedgerSource.WELCOME.value
    assert events[0].direction == LedgerDirection.CREDIT.value
    assert events[0].seconds == 300

    # Repeat award (concurrent/retry call)
    awarded_second, bal_second = await award_welcome_bonus(db_session, user.id)
    assert awarded_second is False
    assert bal_second.welcome_seconds == 300
    assert bal_second.total_usable_seconds == 300

    # Verify no second ledger entry
    events_after = (await db_session.execute(
        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user.id)
    )).scalars().all()
    assert len(events_after) == 1


@pytest.mark.asyncio
async def test_referral_attribution_and_capping(db_session):
    # Referrer user
    referrer, _ = await get_or_create_user(db_session, telegram_id=2001)
    await award_welcome_bonus(db_session, referrer.id)

    # Self-referral rejected
    self_referred, _ = await get_or_create_user(
        db_session, telegram_id=2002, referral_code_arg="ref_2002"
    )
    ref_row = (await db_session.execute(
        select(Referral).where(Referral.referred_id == 2002)
    )).scalar_one_or_none()
    assert ref_row is None

    # Simulate 25 referrals to test the 20-referral cap
    for i in range(1, 26):
        uid = 3000 + i
        user_i, _ = await get_or_create_user(
            db_session, telegram_id=uid, referral_code_arg=referrer.referral_code
        )
        was_pending, rewarded, idx = await confirm_referral_and_reward(db_session, uid)
        assert was_pending is True

        if i <= MAX_REFERRALS_REWARDED:  # 1 to 20
            assert rewarded is True
            assert idx == i
        else:  # 21 to 25 (over cap)
            assert rewarded is False
            assert idx == 0

    # Verify referrer total referral seconds: exactly 20 * 30s = 600s (10 min)
    ref_bal = await get_minute_balance(db_session, referrer.id)
    assert ref_bal.referral_seconds == 600
    assert ref_bal.referral_minutes == 10.0
    # Welcome (300s) + Referral (600s) = 900s (15 min)
    assert ref_bal.total_usable_seconds == 900
    assert ref_bal.total_usable_minutes == 15.0

    # Verify referral stats
    stats = await get_referral_stats(db_session, referrer.id)
    assert stats["rewarded_referrals"] == 20
    assert stats["earned_seconds"] == 600
    assert stats["remaining_slots"] == 0
    assert stats["remaining_seconds"] == 0


@pytest.mark.asyncio
async def test_balance_consumption_and_status_transitions(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=4001)
    await award_welcome_bonus(db_session, user.id)  # +300s welcome
    assert user.account_category == AccountCategory.FREE.value

    # Purchase 10 minutes (+600s)
    await credit_purchased_minutes(db_session, user.id, minutes=10, payment_id="PAY-TEST-001")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 600
    assert bal.welcome_seconds == 300
    assert bal.total_usable_seconds == 900
    # Status promoted to PURCHASED
    assert user.account_category == AccountCategory.PURCHASED.value

    # Consume 200s: should consume from welcome balance first
    bal = await consume_seconds(db_session, user.id, seconds_to_consume=200, job_id="JOB-1")
    assert bal.welcome_seconds == 100
    assert bal.purchased_seconds == 600
    assert bal.total_usable_seconds == 700
    assert user.account_category == AccountCategory.PURCHASED.value

    # Consume 200s more: 100 from welcome (reaches 0), 100 from purchased (leaves 500)
    bal = await consume_seconds(db_session, user.id, seconds_to_consume=200, job_id="JOB-2")
    assert bal.welcome_seconds == 0
    assert bal.purchased_seconds == 500
    assert bal.total_usable_seconds == 500
    assert user.account_category == AccountCategory.PURCHASED.value

    # Consume remaining 500s purchased: purchased balance reaches 0!
    bal = await consume_seconds(db_session, user.id, seconds_to_consume=500, job_id="JOB-3")
    assert bal.purchased_seconds == 0
    assert bal.total_usable_seconds == 0
    # Status reverts back to FREE
    assert user.account_category == AccountCategory.FREE.value

    # Attempt to consume more than available raises ValueError
    with pytest.raises(ValueError, match="Insufficient balance"):
        await consume_seconds(db_session, user.id, seconds_to_consume=10, job_id="JOB-4")

    # Purchasing minutes again restores PURCHASED status
    await credit_purchased_minutes(db_session, user.id, minutes=5, payment_id="PAY-TEST-002")
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 300
    assert user.account_category == AccountCategory.PURCHASED.value


@pytest.mark.asyncio
async def test_ledger_events_audit_trail(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=5001)
    await award_welcome_bonus(db_session, user.id)
    await credit_purchased_minutes(db_session, user.id, minutes=2, payment_id="PAY-100")
    await consume_seconds(db_session, user.id, seconds_to_consume=60, job_id="JOB-TEST")

    events = (await db_session.execute(
        select(MinuteLedgerEvent)
        .where(MinuteLedgerEvent.user_id == user.id)
        .order_by(MinuteLedgerEvent.id)
    )).scalars().all()

    # 1: Welcome credit 300
    # 2: Purchase credit 120
    # 3: Welcome debit 60
    assert len(events) == 3
    assert events[0].source == "WELCOME" and events[0].direction == "CREDIT" and events[0].seconds == 300
    assert events[1].source == "PURCHASE" and events[1].direction == "CREDIT" and events[1].seconds == 120
    assert events[2].source == "WELCOME" and events[2].direction == "DEBIT" and events[2].seconds == 60
