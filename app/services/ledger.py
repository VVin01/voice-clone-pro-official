from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    MinuteBalance,
    MinuteLedgerEvent,
    Referral,
    ReferralReward,
    User,
    WelcomeBonusAward,
    utc_now,
)

logger = logging.getLogger(__name__)

WELCOME_BONUS_SECONDS = 300       # 5 minutes
REFERRAL_REWARD_SECONDS = 30     # 30 seconds per referral
MAX_REFERRALS_REWARDED = 20      # Max 20 eligible referrals
MAX_REFERRAL_SECONDS = 600       # Max 10 minutes total (20 * 30s)


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    language_code: str = "my",
    referral_code_arg: str | None = None,
) -> tuple[User, bool]:
    """Retrieve or register a Telegram user.

    Returns (user, is_new).
    If the user is new and was referred with a valid code, creates a PENDING referral.
    """
    stmt = select(User).where(User.id == telegram_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if user is not None:
        # Update mutable profile info if changed
        updated = False
        if username and user.username != username:
            user.username = username
            updated = True
        if first_name and user.first_name != first_name:
            user.first_name = first_name
            updated = True
        if updated:
            user.updated_at = utc_now()
            await session.flush()
        return user, False

    # Create new user
    gen_code = f"ref_{telegram_id}"
    user = User(
        id=telegram_id,
        username=username,
        first_name=first_name,
        language_code=language_code or "my",
        account_category=AccountCategory.FREE.value,
        referral_code=gen_code,
    )
    session.add(user)

    # Initialize empty balance
    balance = MinuteBalance(user_id=telegram_id)
    session.add(balance)
    await session.flush()

    # Process incoming referral if present
    if referral_code_arg:
        await _record_incoming_referral(session, new_user_id=telegram_id, raw_code=referral_code_arg)

    return user, True


async def _record_incoming_referral(
    session: AsyncSession,
    new_user_id: int,
    raw_code: str,
) -> bool:
    """Validate and record a pending referral attribution."""
    code = raw_code.strip()
    if code.startswith("ref_"):
        referrer_id_str = code[4:]
    else:
        referrer_id_str = code

    # Verify referrer exists and is not self
    stmt = select(User).where((User.referral_code == code) | (User.id == (int(referrer_id_str) if referrer_id_str.isdigit() else -1)))
    res = await session.execute(stmt)
    referrer = res.scalar_one_or_none()

    if not referrer or referrer.id == new_user_id:
        return False

    # Check if this new user already has a referral recorded
    existing_ref = await session.execute(select(Referral).where(Referral.referred_id == new_user_id))
    if existing_ref.scalar_one_or_none() is not None:
        return False

    referral = Referral(
        referrer_id=referrer.id,
        referred_id=new_user_id,
        referral_code=code,
        status="PENDING",
    )
    session.add(referral)
    await session.flush()
    return True


async def get_minute_balance(session: AsyncSession, user_id: int) -> MinuteBalance:
    """Fetch or ensure MinuteBalance row for user."""
    stmt = select(MinuteBalance).where(MinuteBalance.user_id == user_id)
    res = await session.execute(stmt)
    bal = res.scalar_one_or_none()
    if bal is None:
        bal = MinuteBalance(user_id=user_id)
        session.add(bal)
        await session.flush()
    return bal


async def award_welcome_bonus(session: AsyncSession, user_id: int) -> tuple[bool, MinuteBalance]:
    """Award exactly 5 minutes (300 seconds) once per eligible account.

    Transactional and idempotent: repeated calls return (False, balance).
    """
    stmt = select(User).where(User.id == user_id).with_for_update()
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    balance = await get_minute_balance(session, user_id)

    # Admins must not receive or require the welcome bonus
    if settings.is_admin(user_id):
        logger.info("User %s is an admin; skipping welcome bonus award.", user_id)
        return False, balance

    if user.welcome_bonus_awarded:
        return False, balance

    # Insert WelcomeBonusAward row to enforce database-level primary-key uniqueness
    award_record = WelcomeBonusAward(user_id=user_id, seconds=WELCOME_BONUS_SECONDS)
    session.add(award_record)
    try:
        await session.flush()
    except Exception:
        # Concurrent insert occurred or already exists
        return False, balance

    # Transactionally mark awarded and credit balance
    user.welcome_bonus_awarded = True
    user.updated_at = utc_now()
    balance.welcome_seconds += WELCOME_BONUS_SECONDS
    balance.updated_at = utc_now()

    event = MinuteLedgerEvent(
        user_id=user_id,
        source=LedgerSource.WELCOME.value,
        direction=LedgerDirection.CREDIT.value,
        seconds=WELCOME_BONUS_SECONDS,
        note="Welcome Bonus 5 Minutes (Once-per-account)",
    )
    session.add(event)
    await session.flush()

    logger.info("Awarded welcome bonus 300s to user %s", user_id)
    return True, balance


async def confirm_referral_and_reward(
    session: AsyncSession,
    referred_user_id: int,
) -> tuple[bool, bool, int]:
    """Confirm membership of referred user and award referrer 30s if under cap.

    Returns (was_pending: bool, reward_awarded: bool, reward_index: int).
    """
    stmt = select(Referral).where(
        Referral.referred_id == referred_user_id,
        Referral.status == "PENDING",
    ).with_for_update()
    res = await session.execute(stmt)
    ref = res.scalar_one_or_none()

    if not ref:
        return False, False, 0

    referrer_id = ref.referrer_id

    # Check how many rewards this referrer has already earned
    count_stmt = select(func.count()).select_from(ReferralReward).where(ReferralReward.referrer_id == referrer_id)
    awarded_count = (await session.execute(count_stmt)).scalar() or 0

    reward_granted = False
    reward_idx = 0

    if awarded_count < MAX_REFERRALS_REWARDED:
        reward_idx = awarded_count + 1
        reward = ReferralReward(
            referrer_id=referrer_id,
            referral_id=ref.id,
            reward_index=reward_idx,
            seconds=REFERRAL_REWARD_SECONDS,
        )
        session.add(reward)

        ref_bal = await get_minute_balance(session, referrer_id)
        ref_bal.referral_seconds += REFERRAL_REWARD_SECONDS
        ref_bal.updated_at = utc_now()

        event = MinuteLedgerEvent(
            user_id=referrer_id,
            source=LedgerSource.REFERRAL.value,
            direction=LedgerDirection.CREDIT.value,
            seconds=REFERRAL_REWARD_SECONDS,
            reference_id=str(ref.id),
            note=f"Referral reward #{reward_idx}/{MAX_REFERRALS_REWARDED} (+30s)",
        )
        session.add(event)
        reward_granted = True
        logger.info(
            "Awarded referral reward %s/20 to referrer %s for referred user %s",
            reward_idx, referrer_id, referred_user_id
        )

    ref.status = "REWARDED"
    ref.rewarded_at = utc_now()
    await session.flush()

    return True, reward_granted, reward_idx


async def credit_purchased_minutes(
    session: AsyncSession,
    user_id: int,
    minutes: int,
    payment_id: str | None = None,
    note: str | None = None,
) -> MinuteBalance:
    """Credit purchased minutes and update account status to PURCHASED."""
    if minutes <= 0:
        raise ValueError("Purchased minutes must be greater than zero")

    seconds = minutes * 60

    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(user_stmt)).scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    balance = await get_minute_balance(session, user_id)
    balance.purchased_seconds += seconds
    balance.updated_at = utc_now()

    # User receives PURCHASED status while purchased balance > 0
    user.account_category = AccountCategory.PURCHASED.value
    user.updated_at = utc_now()

    event = MinuteLedgerEvent(
        user_id=user_id,
        source=LedgerSource.PURCHASE.value,
        direction=LedgerDirection.CREDIT.value,
        seconds=seconds,
        reference_id=payment_id,
        note=note or f"Purchased {minutes} minutes ({seconds}s)",
    )
    session.add(event)
    await session.flush()

    logger.info("Credited %s minutes (%ss) to user %s (payment=%s)", minutes, seconds, user_id, payment_id)
    return balance


async def consume_seconds(
    session: AsyncSession,
    user_id: int,
    seconds_to_consume: int,
    job_id: str | None = None,
    note: str | None = None,
) -> MinuteBalance:
    """Deduct seconds using a deterministic consumption policy.

    Order:
    1. Welcome seconds
    2. Referral seconds
    3. Purchased seconds

    Returns updated MinuteBalance.
    Raises ValueError if balance is insufficient.
    Updates account category back to FREE when purchased balance reaches zero.
    """
    if seconds_to_consume <= 0:
        return await get_minute_balance(session, user_id)

    # Admin unlimited access: admins never consume minutes and have no ledger debits
    if settings.is_admin(user_id):
        logger.info("User %s is an admin; skipping minute consumption for job %s.", user_id, job_id)
        return await get_minute_balance(session, user_id)

    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(user_stmt)).scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    # Idempotent deduction protection: if this job_id was already debited, skip
    if job_id:
        existing_debit = (
            await session.execute(
                select(MinuteLedgerEvent).where(
                    MinuteLedgerEvent.user_id == user_id,
                    MinuteLedgerEvent.reference_id == job_id,
                    MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
                )
            )
        ).first()
        if existing_debit:
            logger.info("Job %s was already deducted for user %s; skipping duplicate deduction.", job_id, user_id)
            return await get_minute_balance(session, user_id)

    balance = await get_minute_balance(session, user_id)
    if balance.total_usable_seconds < seconds_to_consume:
        raise ValueError(
            f"Insufficient balance: requested {seconds_to_consume}s, available {balance.total_usable_seconds}s"
        )

    remaining_to_deduct = seconds_to_consume

    # 1. Deduct from welcome balance
    if remaining_to_deduct > 0 and balance.welcome_seconds > 0:
        welcome_deduct = min(balance.welcome_seconds, remaining_to_deduct)
        balance.welcome_seconds -= welcome_deduct
        remaining_to_deduct -= welcome_deduct
        session.add(
            MinuteLedgerEvent(
                user_id=user_id,
                source=LedgerSource.WELCOME.value,
                direction=LedgerDirection.DEBIT.value,
                seconds=welcome_deduct,
                reference_id=job_id,
                note=note or "Voice generation deduction (Welcome)",
            )
        )

    # 2. Deduct from referral balance
    if remaining_to_deduct > 0 and balance.referral_seconds > 0:
        referral_deduct = min(balance.referral_seconds, remaining_to_deduct)
        balance.referral_seconds -= referral_deduct
        remaining_to_deduct -= referral_deduct
        session.add(
            MinuteLedgerEvent(
                user_id=user_id,
                source=LedgerSource.REFERRAL.value,
                direction=LedgerDirection.DEBIT.value,
                seconds=referral_deduct,
                reference_id=job_id,
                note=note or "Voice generation deduction (Referral)",
            )
        )

    # 3. Deduct from purchased balance
    if remaining_to_deduct > 0 and balance.purchased_seconds > 0:
        purchased_deduct = min(balance.purchased_seconds, remaining_to_deduct)
        balance.purchased_seconds -= purchased_deduct
        remaining_to_deduct -= purchased_deduct
        session.add(
            MinuteLedgerEvent(
                user_id=user_id,
                source=LedgerSource.PURCHASE.value,
                direction=LedgerDirection.DEBIT.value,
                seconds=purchased_deduct,
                reference_id=job_id,
                note=note or "Voice generation deduction (Purchased)",
            )
        )

    balance.consumed_seconds += seconds_to_consume
    balance.updated_at = utc_now()

    # Free vs Purchased status policy:
    # A user with a positive purchased-minute balance receives PURCHASED status.
    # When purchased minutes reach zero, the user returns to FREE status.
    if balance.purchased_seconds > 0:
        user.account_category = AccountCategory.PURCHASED.value
    else:
        user.account_category = AccountCategory.FREE.value
    user.updated_at = utc_now()

    await session.flush()
    return balance


async def refund_job_seconds(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    note: str | None = None,
) -> MinuteBalance:
    """Idempotently refund seconds previously deducted for a job."""
    # Find all debits for this job
    debits = list((
        await session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user_id,
                MinuteLedgerEvent.reference_id == job_id,
                MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
            )
        )
    ).scalars().all())
    if not debits:
        return await get_minute_balance(session, user_id)

    # Check if already refunded
    refund_ref = f"REFUND:{job_id}"
    existing_refund = (
        await session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user_id,
                MinuteLedgerEvent.reference_id == refund_ref,
                MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
            )
        )
    ).first()
    if existing_refund:
        logger.info("Job %s was already refunded for user %s; skipping duplicate refund.", job_id, user_id)
        return await get_minute_balance(session, user_id)

    balance = await get_minute_balance(session, user_id)
    user = await session.get(User, user_id)

    for debit in debits:
        if debit.source == LedgerSource.WELCOME.value:
            balance.welcome_seconds += debit.seconds
        elif debit.source == LedgerSource.REFERRAL.value:
            balance.referral_seconds += debit.seconds
        elif debit.source == LedgerSource.PURCHASE.value:
            balance.purchased_seconds += debit.seconds
            if user:
                user.account_category = AccountCategory.PURCHASED.value

        session.add(
            MinuteLedgerEvent(
                user_id=user_id,
                source=debit.source,
                direction=LedgerDirection.CREDIT.value,
                seconds=debit.seconds,
                reference_id=refund_ref,
                note=note or f"Refund for failed job {job_id} ({debit.source})",
            )
        )

    balance.updated_at = utc_now()
    await session.flush()
    return balance


async def admin_adjust_balance(
    session: AsyncSession,
    user_id: int,
    admin_id: int,
    seconds: int,
    source: LedgerSource,
    direction: LedgerDirection,
    reason: str,
    idempotency_key: str | None = None,
) -> MinuteBalance:
    """Audited balance adjustment by an administrator with idempotency protection.

    - Validates positive seconds and mandatory non-empty reason.
    - Prevents debits that would result in a negative balance.
    - Idempotent: duplicate calls with the same idempotency_key return existing balance without double-adjusting.
    - Updates account category (promotes to PURCHASED on credit; demotes to FREE when purchased seconds reach 0).
    - Records MinuteLedgerEvent and AuditLog transactionally.
    """
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValueError("Adjustment reason is mandatory and cannot be empty")

    if seconds <= 0:
        raise ValueError("Adjustment seconds must be positive")

    # Row-lock user and balance to prevent concurrent race conditions
    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user_res = await session.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    balance = await get_minute_balance(session, user_id)

    # Idempotency check: if an adjustment with this idempotency_key was already recorded, return current balance
    if idempotency_key:
        existing_rec = await session.get(AdjustmentIdempotencyRecord, idempotency_key)
        if existing_rec:
            logger.info(
                "Idempotent adjustment duplicate detected for user %s with key %s; suppressing replay.",
                user_id, idempotency_key,
            )
            return balance

        # Also fallback check MinuteLedgerEvent for events created before migration
        existing_event = (
            await session.execute(
                select(MinuteLedgerEvent).where(
                    MinuteLedgerEvent.user_id == user_id,
                    MinuteLedgerEvent.reference_id == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing_event:
            logger.info(
                "Idempotent adjustment duplicate detected in ledger events for user %s with key %s; suppressing replay.",
                user_id, idempotency_key,
            )
            return balance

    try:
        async with session.begin_nested():
            if idempotency_key:
                session.add(
                    AdjustmentIdempotencyRecord(
                        idempotency_key=idempotency_key,
                        user_id=user_id,
                        admin_id=admin_id,
                        seconds=seconds,
                        direction=direction.value,
                    )
                )

            if direction == LedgerDirection.CREDIT:
                if source == LedgerSource.PURCHASE:
                    balance.purchased_seconds += seconds
                    user.account_category = AccountCategory.PURCHASED.value
                elif source == LedgerSource.REFERRAL:
                    balance.referral_seconds += seconds
                elif source == LedgerSource.WELCOME:
                    balance.welcome_seconds += seconds
            else:
                if source == LedgerSource.PURCHASE:
                    if balance.purchased_seconds < seconds:
                        raise ValueError(
                            f"Insufficient purchased balance ({balance.purchased_seconds}s) to debit {seconds}s"
                        )
                    balance.purchased_seconds -= seconds
                    if balance.purchased_seconds == 0:
                        user.account_category = AccountCategory.FREE.value
                elif source == LedgerSource.REFERRAL:
                    if balance.referral_seconds < seconds:
                        raise ValueError(
                            f"Insufficient referral balance ({balance.referral_seconds}s) to debit {seconds}s"
                        )
                    balance.referral_seconds -= seconds
                elif source == LedgerSource.WELCOME:
                    if balance.welcome_seconds < seconds:
                        raise ValueError(
                            f"Insufficient welcome balance ({balance.welcome_seconds}s) to debit {seconds}s"
                        )
                    balance.welcome_seconds -= seconds

            balance.updated_at = utc_now()
            user.updated_at = utc_now()

            event = MinuteLedgerEvent(
                user_id=user_id,
                source=LedgerSource.ADJUST.value,
                direction=direction.value,
                seconds=seconds,
                reference_id=idempotency_key,
                note=f"Admin {admin_id} adjustment: {clean_reason} ({source.value})",
            )
            session.add(event)

            audit_details = {
                "seconds": seconds,
                "minutes": round(seconds / 60.0, 2),
                "source": source.value,
                "direction": direction.value,
                "reason": clean_reason,
                "idempotency_key": idempotency_key,
            }
            audit = AuditLog(
                admin_id=admin_id,
                action=f"balance.adjust.{direction.value.lower()}",
                target_type="user",
                target_id=str(user_id),
                details_json=json.dumps(audit_details),
            )
            session.add(audit)
            await session.flush()
    except IntegrityError as exc:
        # The savepoint has been rolled back automatically by begin_nested().__aexit__.
        # Determine whether this IntegrityError is a confirmed idempotency-key PK collision
        # or an unexpected constraint failure (FK violation, CHECK constraint, etc.).
        #
        # After savepoint rollback the outer session is still usable. We perform a fresh
        # SELECT to see whether a committed AdjustmentIdempotencyRecord for this key exists.
        # If it does:  a racing session already applied the adjustment → suppress replay.
        # If it does not: some other constraint was violated → re-raise so the caller
        #                 and the outer transaction handle it correctly.
        if idempotency_key:
            # Use populate_existing=True so the identity map returns a freshly-loaded row
            # rather than any stale cached object that was expired by the savepoint rollback.
            confirmed_rec = await session.get(
                AdjustmentIdempotencyRecord,
                idempotency_key,
                populate_existing=True,
            )
            if confirmed_rec is not None:
                logger.info(
                    "Idempotency PK collision confirmed for key %s (user %s); "
                    "suppressing duplicate adjustment.",
                    idempotency_key, user_id,
                )
                await session.expire(user)
                await session.expire(balance)
                return await get_minute_balance(session, user_id)

        # Not a confirmed idempotency collision — re-raise the original exception unchanged
        # so FK violations, CHECK constraints, and other unexpected errors are not swallowed.
        logger.error(
            "Unexpected IntegrityError during balance adjustment (user=%s, key=%s): %s",
            user_id, idempotency_key, exc,
        )
        raise

    return balance


async def get_paginated_users(
    session: AsyncSession,
    page: int = 1,
    page_size: int = 5,
) -> tuple[list[User], int, int]:
    """Retrieve users with pagination. Returns (users, total_users, total_pages)."""
    count_stmt = select(func.count()).select_from(User)
    total_users = (await session.execute(count_stmt)).scalar() or 0
    total_pages = max(1, (total_users + page_size - 1) // page_size) if total_users > 0 else 1

    clamped_page = max(1, min(page, total_pages))
    stmt = (
        select(User)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((clamped_page - 1) * page_size)
        .limit(page_size)
    )
    users = list((await session.execute(stmt)).scalars().all())
    return users, total_users, total_pages


async def get_user_recent_ledger_events(
    session: AsyncSession,
    user_id: int,
    limit: int = 3,
) -> list[MinuteLedgerEvent]:
    """Fetch recent minute ledger events for a user."""
    stmt = (
        select(MinuteLedgerEvent)
        .where(MinuteLedgerEvent.user_id == user_id)
        .order_by(MinuteLedgerEvent.created_at.desc(), MinuteLedgerEvent.id.desc())
        .limit(limit)
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_referral_stats(session: AsyncSession, user_id: int) -> dict:
    """Return rewarded referrals count, earned minutes, and remaining capacity."""
    count_stmt = select(func.count()).select_from(ReferralReward).where(ReferralReward.referrer_id == user_id)
    rewarded_count = (await session.execute(count_stmt)).scalar() or 0

    pending_stmt = select(func.count()).select_from(Referral).where(
        Referral.referrer_id == user_id,
        Referral.status == "PENDING",
    )
    pending_count = (await session.execute(pending_stmt)).scalar() or 0

    earned_seconds = rewarded_count * REFERRAL_REWARD_SECONDS
    remaining_slots = max(0, MAX_REFERRALS_REWARDED - rewarded_count)
    remaining_seconds = remaining_slots * REFERRAL_REWARD_SECONDS

    return {
        "rewarded_referrals": rewarded_count,
        "max_referrals": MAX_REFERRALS_REWARDED,
        "pending_referrals": pending_count,
        "earned_seconds": earned_seconds,
        "earned_minutes": earned_seconds / 60.0,
        "remaining_slots": remaining_slots,
        "remaining_seconds": remaining_seconds,
        "remaining_minutes": remaining_seconds / 60.0,
    }
