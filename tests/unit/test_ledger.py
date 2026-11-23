import pytest
from sqlalchemy import select
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import MinuteLedgerEvent, Referral, ReferralReward, User
from app.services.ledger import (
    MAX_REFERRALS_REWARDED,
    REFERRAL_REWARD_SECONDS,
    WELCOME_BONUS_SECONDS,
    admin_adjust_balance,
    award_welcome_bonus,
    confirm_referral_and_reward,
    consume_seconds,
    credit_purchased_minutes,
    draw_tts_referral_seconds,
    get_minute_balance,
    get_or_create_user,
    get_referral_stats,
    grant_monthly_free_allowance,
    refund_job_seconds,
    refund_tts_referral_seconds,
    settle_recap_voice_seconds,
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

    # Simulate 25 referrals — Part I: unlimited (no 20-friend cap), 60s Voice Clone per referral
    for i in range(1, 26):
        uid = 3000 + i
        user_i, _ = await get_or_create_user(
            db_session, telegram_id=uid, referral_code_arg=referrer.referral_code
        )
        was_pending, rewarded, idx = await confirm_referral_and_reward(db_session, uid)
        assert was_pending is True
        assert rewarded is True
        assert idx == i

    # Verify referrer total referral_voice_seconds: 25 * 60s = 1500s (25 min)
    ref_bal = await get_minute_balance(db_session, referrer.id)
    assert ref_bal.referral_voice_seconds == 1500
    assert ref_bal.referral_seconds == 0
    # Welcome (300s) + Referral Voice (1500s) = 1800s (30 min)
    assert ref_bal.total_usable_seconds == 1800
    assert ref_bal.total_usable_minutes == 30.0

    # Verify referral stats (Part I: unlimited)
    stats = await get_referral_stats(db_session, referrer.id)
    assert stats["rewarded_referrals"] == 25
    assert stats["earned_seconds"] == 1500
    assert stats["remaining_slots"] is None
    assert stats["remaining_seconds"] is None
    assert stats["unlimited"] is True


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
    # User retains PURCHASED entitlement state even when purchased balance reaches 0
    assert user.account_category == AccountCategory.PURCHASED.value

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


def _derive_bucket_totals_from_ledger(events: list[MinuteLedgerEvent]) -> dict[str, int]:
    """Derive net seconds per bucket from MinuteLedgerEvent rows.

    Note: admin_adjust_balance writes source='ADJUST' with the target bucket
    tagged in ``note`` as '(PURCHASE)'/'(WELCOME)'/etc., so total usable
    seconds is exact directly from directions while per-bucket attribution for
    ADJUST rows maps that trailing bucket tag.
    """
    totals = {
        LedgerSource.MONTHLY_FREE.value: 0,
        LedgerSource.WELCOME.value: 0,
        LedgerSource.REFERRAL.value: 0,
        LedgerSource.PURCHASE.value: 0,
    }
    for ev in events:
        src = ev.source
        if src == LedgerSource.ADJUST.value and ev.note:
            for candidate in totals:
                if ev.note.endswith(f"({candidate})"):
                    src = candidate
                    break
        if src not in totals:
            totals[src] = 0
        delta = int(ev.seconds or 0)
        if ev.direction == LedgerDirection.CREDIT.value:
            totals[src] += delta
        elif ev.direction == LedgerDirection.DEBIT.value:
            totals[src] -= delta
    return totals


async def _assert_minute_balance_matches_ledger(db_session, user_id: int) -> None:
    bal = await get_minute_balance(db_session, user_id)
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent)
            .where(MinuteLedgerEvent.user_id == user_id)
            .order_by(MinuteLedgerEvent.id)
        )
    ).scalars().all()
    derived = _derive_bucket_totals_from_ledger(events)
    assert bal.monthly_free_seconds == derived[LedgerSource.MONTHLY_FREE.value]
    assert bal.welcome_seconds == derived[LedgerSource.WELCOME.value]
    assert (bal.referral_seconds + bal.referral_voice_seconds) == derived[LedgerSource.REFERRAL.value]
    assert bal.purchased_seconds == derived[LedgerSource.PURCHASE.value]
    total_from_all_events = sum(
        int(e.seconds or 0) if e.direction == LedgerDirection.CREDIT.value else -int(e.seconds or 0)
        for e in events
    )
    assert bal.total_usable_seconds == total_from_all_events


@pytest.mark.asyncio
async def test_part_c_minute_balance_derivable_from_ledger_events(db_session):
    """Part C Area 4: after every grant, purchase, voice consume/refund,
    Option B hold/under-run/overrun settle, TTS referral draw/refund, and admin
    adjustment, per-bucket SUM(CREDIT)-SUM(DEBIT) matches MinuteBalance exactly."""
    user, _ = await get_or_create_user(db_session, telegram_id=5099)
    await award_welcome_bonus(db_session, user.id)
    await grant_monthly_free_allowance(db_session, user.id)
    await credit_purchased_minutes(db_session, user.id, minutes=10, payment_id="PAY-PARTC-1")

    ref_user, _ = await get_or_create_user(
        db_session, telegram_id=5100, referral_code_arg=user.referral_code
    )
    assert ref_user.id == 5100
    await confirm_referral_and_reward(db_session, ref_user.id)
    await _assert_minute_balance_matches_ledger(db_session, user.id)

    # 1) Voice clone consume + full refund symmetry
    await consume_seconds(db_session, user.id, 350, job_id="vc-partc-1")
    await _assert_minute_balance_matches_ledger(db_session, user.id)
    await refund_job_seconds(db_session, user.id, "vc-partc-1")
    await _assert_minute_balance_matches_ledger(db_session, user.id)

    # 2) Option B recap hold + under-run settle
    await consume_seconds(db_session, user.id, 200, job_id="rc_partc_under")
    await _assert_minute_balance_matches_ledger(db_session, user.id)
    await settle_recap_voice_seconds(
        db_session, user.id, "rc_partc_under", actual_seconds=110, note="Recap clone settle (rc_partc_under)"
    )
    await _assert_minute_balance_matches_ledger(db_session, user.id)

    # 3) Option B recap hold + overrun settle + replay
    await consume_seconds(db_session, user.id, 80, job_id="rc_partc_over")
    await settle_recap_voice_seconds(
        db_session, user.id, "rc_partc_over", actual_seconds=120, note="Recap clone settle (rc_partc_over)"
    )
    await settle_recap_voice_seconds(
        db_session, user.id, "rc_partc_over", actual_seconds=120, note="Recap clone settle (rc_partc_over)"
    )
    await _assert_minute_balance_matches_ledger(db_session, user.id)

    # 4) Grandfathered TTS referral draw + settle delta (Part I: past referral_seconds still spendable on TTS)
    bal_for_tts = await get_minute_balance(db_session, user.id)
    bal_for_tts.referral_seconds += 120
    db_session.add(
        MinuteLedgerEvent(
            user_id=user.id,
            source=LedgerSource.REFERRAL.value,
            direction=LedgerDirection.CREDIT.value,
            seconds=120,
            reference_id="LEGACY_TTS_REF",
            note="Grandfathered TTS referral balance",
        )
    )
    await db_session.flush()
    drawn = await draw_tts_referral_seconds(db_session, user.id, "tts-partc-1", 80)
    assert drawn == 80
    await _assert_minute_balance_matches_ledger(db_session, user.id)
    await refund_tts_referral_seconds(db_session, user.id, "tts-partc-1", keep_seconds=50)
    await _assert_minute_balance_matches_ledger(db_session, user.id)

    # 5) Admin manual adjustments (+purchase and -purchase)
    await admin_adjust_balance(
        db_session,
        user_id=user.id,
        admin_id=1,
        seconds=90,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.CREDIT,
        reason="Topup",
        idempotency_key="partc-adj-1",
    )
    await admin_adjust_balance(
        db_session,
        user_id=user.id,
        admin_id=1,
        seconds=30,
        source=LedgerSource.PURCHASE,
        direction=LedgerDirection.DEBIT,
        reason="Trim",
        idempotency_key="partc-adj-2",
    )
    await _assert_minute_balance_matches_ledger(db_session, user.id)
