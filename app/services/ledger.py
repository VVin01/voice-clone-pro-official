from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, LedgerDirection, LedgerSource
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    ImportedAudience,
    MinuteBalance,
    MinuteLedgerEvent,
    MonthlyFreeAllowanceGrant,
    RecapCreditLedgerEvent,
    Referral,
    ReferralReward,
    User,
    WelcomeBonusAward,
    utc_now,
)

logger = logging.getLogger(__name__)

WELCOME_BONUS_SECONDS = 300       # 5 minutes
# Batch AW (owner decision 2026-11-05 23:57): the referral reward is
# now 2 minutes of TTS-only time per rewarded referral (was 30s of
# clone-spendable time). New rewards only — existing balances keep
# their value and simply become TTS-spendable under the re-scope.
REFERRAL_REWARD_SECONDS = 120    # 2 minutes of TTS time per referral
MAX_REFERRALS_REWARDED = 20      # Max 20 eligible referrals
MAX_REFERRAL_SECONDS = 2400      # Max 40 minutes total (20 * 120s)


async def _find_available_referral_code(
    session: AsyncSession,
    telegram_id: int,
    max_attempts: int = 5,
) -> str:
    """Generate a unique referral code that does not collide with existing users."""
    candidate = f"ref_{telegram_id}"
    stmt = select(User.id).where(User.referral_code == candidate, User.id != telegram_id)
    existing_owner = (await session.execute(stmt)).scalar_one_or_none()
    if existing_owner is None:
        return candidate

    for attempt in range(1, max_attempts + 1):
        suffix = secrets.token_hex(3)
        candidate = f"ref_{telegram_id}_{suffix}"[:32]
        stmt = select(User.id).where(User.referral_code == candidate, User.id != telegram_id)
        if (await session.execute(stmt)).scalar_one_or_none() is None:
            return candidate

    suffix = uuid.uuid4().hex[:8]
    return f"r{telegram_id}_{suffix}"[:32]


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
    Safely handles concurrent registrations, unique constraint collisions, and rollbacks.
    """
    # Profile fields are truncated to their column widths at this
    # single intake choke point (hunt #4 A2): Telegram display names
    # can exceed String(150)/String(100), which used to surface as a
    # Postgres 500 on registration/profile refresh. Every caller
    # (bot /start, webapp auth) funnels through here.
    if username:
        username = username[:100]
    if first_name:
        first_name = first_name[:150]
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
        await _sync_imported_and_allowance(session, telegram_id)
        return user, False

    # User not found in current snapshot; attempt insertion with retry and collision handling
    max_insert_attempts = 5
    last_error: Exception | None = None

    for attempt in range(max_insert_attempts):
        if attempt == 0:
            gen_code = await _find_available_referral_code(session, telegram_id)
        else:
            suffix = secrets.token_hex(3 + attempt)
            gen_code = f"ref_{telegram_id}_{suffix}"[:32]

        try:
            async with session.begin_nested():
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

            # Insert succeeded!
            if referral_code_arg:
                await _record_incoming_referral(session, new_user_id=telegram_id, raw_code=referral_code_arg)

            await _sync_imported_and_allowance(session, telegram_id)
            return user, True

        except IntegrityError as exc:
            last_error = exc
            # Check if user was inserted concurrently
            check_user = (await session.execute(select(User).where(User.id == telegram_id))).scalar_one_or_none()
            if check_user is not None:
                logger.info("Resolved concurrent registration for user %s", telegram_id)
                updated = False
                if username and check_user.username != username:
                    check_user.username = username
                    updated = True
                if first_name and check_user.first_name != first_name:
                    check_user.first_name = first_name
                    updated = True
                if updated:
                    check_user.updated_at = utc_now()
                    await session.flush()
                await _sync_imported_and_allowance(session, telegram_id)
                return check_user, False

            # The collision was on referral_code with another user or concurrent commit pending
            logger.debug(
                "Referral code collision in get_or_create_user for user %s (attempt %s/%s), retrying...",
                telegram_id,
                attempt + 1,
                max_insert_attempts,
            )
            await asyncio.sleep(0.02 * (attempt + 1))
            check_user = (await session.execute(select(User).where(User.id == telegram_id))).scalar_one_or_none()
            if check_user is not None:
                await _sync_imported_and_allowance(session, telegram_id)
                return check_user, False

    if last_error:
        raise last_error

    raise RuntimeError(f"Failed to get_or_create_user for {telegram_id} after {max_insert_attempts} attempts")


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

    # Extract base numeric ID if code has a collision suffix (e.g. ref_12345_a1b2c3)
    base_numeric_str = referrer_id_str.split("_")[0] if "_" in referrer_id_str else referrer_id_str
    possible_id = int(base_numeric_str) if base_numeric_str.isdigit() else -1

    # Verify referrer exists and is not self
    stmt = select(User).where((User.referral_code == code) | (User.id == possible_id))
    res = await session.execute(stmt)
    referrer = res.scalar_one_or_none()

    if not referrer or referrer.id == new_user_id:
        return False

    # Check if this new user already has a referral recorded
    existing_ref = await session.execute(select(Referral).where(Referral.referred_id == new_user_id))
    if existing_ref.scalar_one_or_none() is not None:
        return False

    try:
        async with session.begin_nested():
            referral = Referral(
                referrer_id=referrer.id,
                referred_id=new_user_id,
                referral_code=code,
                status="PENDING",
            )
            session.add(referral)
            await session.flush()
            return True
    except IntegrityError:
        logger.info("Concurrent referral attribution for user %s already exists", new_user_id)
        return False


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


def get_current_monthly_period() -> str:
    """Return the current calendar month in UTC as 'YYYY-MM'."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def grant_monthly_free_allowance(
    session: AsyncSession,
    user_id: int,
    period: str | None = None,
) -> tuple[bool, MinuteBalance]:
    """Grant 3 minutes (180 seconds) free allowance once per calendar month.

    - Free users receive 180 seconds free allowance once per calendar month (YYYY-MM UTC).
    - Admins skip balance allowances since they have unlimited access.
    - Idempotent: protected by MonthlyFreeAllowanceGrant unique constraint (user_id, period).
    - Deductions use monthly_free_seconds first before purchased_seconds.
    """
    # Batch AJ (L22): serialise on the User row FIRST (the same lock
    # consume_seconds and award_welcome_bonus take). The grant writes
    # the monthly bucket ABSOLUTELY from the balance it loaded; a
    # consume committing between that load and this flush used to be
    # overwritten by the stale absolute value, resurrecting seconds
    # the user had already spent.
    _locked_user = (await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )).scalar_one_or_none()
    balance = await get_minute_balance(session, user_id)

    if settings.is_admin(user_id):
        return False, balance

    current_period = period or get_current_monthly_period()

    # Check if already granted for this period
    stmt = select(MonthlyFreeAllowanceGrant).where(
        MonthlyFreeAllowanceGrant.user_id == user_id,
        MonthlyFreeAllowanceGrant.period == current_period,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return False, balance

    allowance_seconds = settings.free_monthly_allowance_seconds

    try:
        async with session.begin_nested():
            grant = MonthlyFreeAllowanceGrant(
                user_id=user_id,
                period=current_period,
                seconds=allowance_seconds,
            )
            session.add(grant)
            await session.flush()
    except IntegrityError:
        # Concurrent grant attempt
        return False, balance

    # Set / replenish monthly free seconds for the new period
    balance.monthly_free_seconds = allowance_seconds
    balance.last_monthly_grant_period = current_period
    balance.updated_at = utc_now()

    event = MinuteLedgerEvent(
        user_id=user_id,
        source=LedgerSource.MONTHLY_FREE.value,
        direction=LedgerDirection.CREDIT.value,
        seconds=allowance_seconds,
        reference_id=current_period,
        note=f"Monthly Free Allowance {current_period} (3 minutes)",
    )
    session.add(event)
    await session.flush()

    logger.info("Granted monthly free allowance (%ss) to user %s for period %s", allowance_seconds, user_id, current_period)
    return True, balance


async def _sync_imported_and_allowance(session: AsyncSession, user_id: int) -> None:
    """Mark user as registered in ImportedAudience if applicable."""
    imported_row = (
        await session.execute(
            select(ImportedAudience).where(ImportedAudience.telegram_id == user_id)
        )
    ).scalar_one_or_none()
    if imported_row is not None and not imported_row.is_registered_in_new_system:
        imported_row.is_registered_in_new_system = True
        await session.flush()


async def award_welcome_bonus(
    session: AsyncSession,
    user_id: int,
    seconds: int | None = None,
) -> tuple[bool, MinuteBalance]:
    """Award welcome bonus once per eligible account if enabled.

    Transactional and idempotent: repeated calls return (False, balance).
    """
    bonus_sec = seconds if seconds is not None else WELCOME_BONUS_SECONDS
    if bonus_sec <= 0:
        balance = await get_minute_balance(session, user_id)
        return False, balance

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
    award_record = WelcomeBonusAward(user_id=user_id, seconds=bonus_sec)
    session.add(award_record)
    try:
        await session.flush()
    except Exception:
        # Concurrent insert occurred or already exists
        return False, balance

    # Transactionally mark awarded and credit balance
    user.welcome_bonus_awarded = True
    user.updated_at = utc_now()
    balance.welcome_seconds += bonus_sec
    balance.updated_at = utc_now()

    event = MinuteLedgerEvent(
        user_id=user_id,
        source=LedgerSource.WELCOME.value,
        direction=LedgerDirection.CREDIT.value,
        seconds=bonus_sec,
        note=f"Welcome Bonus {bonus_sec // 60} Minutes (Once-per-account)",
    )
    session.add(event)
    await session.flush()

    logger.info("Awarded welcome bonus %ss to user %s", bonus_sec, user_id)
    return True, balance


async def confirm_referral_and_reward(
    session: AsyncSession,
    referred_user_id: int,
) -> tuple[bool, bool, int]:
    """Confirm membership of referred user and award referrer 120s TTS-only if under cap.

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

    # Serialize reward grants per referrer (hunt #7): two friends of
    # the same referrer confirming membership at the same moment each
    # counted the existing rewards and inserted the SAME reward_index
    # - the unique constraint then failed one confirm with an
    # IntegrityError inside the referred user's gate verification
    # (reproduced live on the SQLite test harness), and the balance
    # credit was a stale read-modify-write that could lose one grant.
    # Locking the referrer's User row makes count + insert + credit
    # atomic per referrer on Postgres - the same pattern the payment
    # approval and purchased-credit paths already rely on. (On
    # SQLite FOR UPDATE is a no-op, so the harness pins the lock
    # ordering and the sequential end state instead.)
    await session.execute(
        select(User).where(User.id == referrer_id).with_for_update()
    )

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
            note=f"Referral reward #{reward_idx}/{MAX_REFERRALS_REWARDED} (+{REFERRAL_REWARD_SECONDS}s TTS)",
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

    This is the VOICE CLONE billing cascade. Order:
    1. Monthly free seconds
    2. Welcome seconds
    3. Purchased seconds

    Batch AW re-scope: referral seconds are TTS-only now and are
    NEVER deducted here (nor counted toward the balance check) —
    clone-spendable means monthly_free + welcome + purchased. TTS
    jobs draw the referral bucket through draw_tts_referral_seconds
    instead.

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

    # Idempotent deduction protection — Batch AJ (H6): NET-aware.
    # The old check skipped whenever ANY debit row existed for the
    # job, which also covered a job whose charge had been REFUNDED:
    # an admin retry of a refunded job then re-ran and completed
    # without ever being billed again. What matters is the unrefunded
    # remainder: fully billed (net >= request) → skip as before;
    # partially billed → charge only the missing delta; fully
    # refunded (net == 0) → charge in full, like a first run.
    seconds_to_charge = seconds_to_consume
    if job_id:
        billed_rows = (await session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user_id,
                MinuteLedgerEvent.reference_id == job_id,
                MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
            )
        )).scalars().all()
        if billed_rows:
            refunded_rows = (await session.execute(
                select(MinuteLedgerEvent).where(
                    MinuteLedgerEvent.user_id == user_id,
                    MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
                    MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
                )
            )).scalars().all()
            net_billed = sum(int(r.seconds or 0) for r in billed_rows) - sum(
                int(r.seconds or 0) for r in refunded_rows
            )
            if net_billed >= seconds_to_consume:
                logger.info("Job %s was already deducted for user %s; skipping duplicate deduction.", job_id, user_id)
                return await get_minute_balance(session, user_id)
            if net_billed > 0:
                seconds_to_charge = seconds_to_consume - net_billed

    balance = await get_minute_balance(session, user_id)
    if balance.clone_spendable_seconds < seconds_to_charge:
        raise ValueError(
            f"Insufficient balance: requested {seconds_to_charge}s, available {balance.clone_spendable_seconds}s"
        )

    remaining_to_deduct = seconds_to_charge

    # 1. Deduct from monthly free balance
    if remaining_to_deduct > 0 and balance.monthly_free_seconds > 0:
        monthly_deduct = min(balance.monthly_free_seconds, remaining_to_deduct)
        balance.monthly_free_seconds -= monthly_deduct
        remaining_to_deduct -= monthly_deduct
        session.add(
            MinuteLedgerEvent(
                user_id=user_id,
                source=LedgerSource.MONTHLY_FREE.value,
                direction=LedgerDirection.DEBIT.value,
                seconds=monthly_deduct,
                reference_id=job_id,
                note=note or "Voice generation deduction (Monthly Free)",
            )
        )

    # 2. Deduct from welcome balance (legacy)
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

    # Batch AW: the referral bucket is skipped entirely — it is
    # TTS-only balance now (see the docstring above).

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

    balance.consumed_seconds += seconds_to_charge
    balance.updated_at = utc_now()

    # Purchased entitlement policy:
    # A user who has purchased minutes holds the persistent PURCHASED entitlement state.
    # Consuming minutes does not demote or erase their PURCHASED entitlement.
    if balance.purchased_seconds > 0:
        user.account_category = AccountCategory.PURCHASED.value
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

    # Batch AJ (H6 + L3): refund only the UNREFUNDED delta, per
    # source. The old shape returned early when ANY refund row
    # existed, so a job that was refunded, retried (and re-billed by
    # the now net-aware consume_seconds) could never be refunded for
    # its second charge. And MONTHLY_FREE debits are refundable only
    # into the period they were charged in: last month's free bucket
    # no longer exists, so crediting it back this month minted free
    # seconds on top of this month's fresh allowance (L3).
    refund_ref = f"REFUND:{job_id}"
    prior_refunds = (await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == refund_ref,
            MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
        )
    )).scalars().all()
    refunded_by_source: dict[str, int] = {}
    for r in prior_refunds:
        refunded_by_source[r.source] = refunded_by_source.get(r.source, 0) + int(r.seconds or 0)

    _now = utc_now()
    current_period = (_now.year, _now.month)
    debit_by_source: dict[str, int] = {}
    for debit in debits:
        if debit.source == LedgerSource.MONTHLY_FREE.value and debit.created_at is not None:
            if (debit.created_at.year, debit.created_at.month) != current_period:
                continue
        debit_by_source[debit.source] = debit_by_source.get(debit.source, 0) + int(debit.seconds or 0)

    refundable = {
        source: total - refunded_by_source.get(source, 0)
        for source, total in debit_by_source.items()
    }
    refundable = {s: v for s, v in refundable.items() if v > 0}
    if not refundable:
        logger.info("Job %s has no unrefunded balance for user %s; skipping refund.", job_id, user_id)
        return await get_minute_balance(session, user_id)

    balance = await get_minute_balance(session, user_id)
    user = await session.get(User, user_id)

    for source, seconds in refundable.items():
        if source == LedgerSource.MONTHLY_FREE.value:
            balance.monthly_free_seconds += seconds
        elif source == LedgerSource.WELCOME.value:
            balance.welcome_seconds += seconds
        elif source == LedgerSource.REFERRAL.value:
            balance.referral_seconds += seconds
        elif source == LedgerSource.PURCHASE.value:
            balance.purchased_seconds += seconds
            if user:
                user.account_category = AccountCategory.PURCHASED.value

        session.add(
            MinuteLedgerEvent(
                user_id=user_id,
                source=source,
                direction=LedgerDirection.CREDIT.value,
                seconds=seconds,
                reference_id=refund_ref,
                note=note or f"Refund for failed job {job_id} ({source})",
            )
        )

    balance.updated_at = utc_now()
    await session.flush()
    return balance


_CLONE_LEDGER_SOURCES = (
    LedgerSource.MONTHLY_FREE.value,
    LedgerSource.WELCOME.value,
    LedgerSource.PURCHASE.value,
)
_RECAP_VOICE_SETTLE_OVERRUN_PREFIX = "Recap clone settle overrun"


async def net_job_seconds_held(
    session: AsyncSession,
    user_id: int,
    job_id: str,
) -> int:
    """Net clone-spendable seconds currently held by ``job_id``:
    clone-bucket DEBITs minus REFUND:<job_id> CREDITs.
    """
    debits = (await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == job_id,
            MinuteLedgerEvent.source.in_(_CLONE_LEDGER_SOURCES),
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
    )).scalars().all()
    if not debits:
        return 0
    credits = (await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
            MinuteLedgerEvent.source.in_(_CLONE_LEDGER_SOURCES),
            MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
        )
    )).scalars().all()
    net = sum(int(r.seconds or 0) for r in debits) - sum(
        int(r.seconds or 0) for r in credits
    )
    return max(0, net)


async def settle_recap_voice_seconds(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    actual_seconds: int,
    *,
    note: str | None = None,
) -> dict:
    """Settle a recap clone-voice hold against actual synthesized narration duration.

    Option B billing semantics:
    - Submit holds ``estimated_voice_seconds`` via ``consume_seconds``.
    - On completion, we keep ``actual_seconds``:
      * If ``actual_seconds < net_held``, refund the unused delta back
        into the original buckets (reverse cascade: PURCHASE -> WELCOME ->
        MONTHLY_FREE, respecting MONTHLY_FREE current-period safety) and
        record ``REFUND:<job_id>`` CREDIT rows.
      * If ``actual_seconds > net_held``, charge ONLY up to
        ``balance.clone_spendable_seconds`` as a final safety net — a
        rendered job NEVER fails over a small overrun.
    - Idempotent under row lock: repeat calls return the already-settled
      summary without double-refunding or double-charging overrun.
    """
    target_actual = max(0, int(actual_seconds))
    if settings.is_admin(user_id):
        return {
            "held_seconds": 0,
            "actual_seconds": target_actual,
            "settled_seconds": 0,
            "refunded_seconds": 0,
            "overrun_charged_seconds": 0,
            "overrun_uncovered_seconds": 0,
        }

    user_stmt = select(User).where(User.id == user_id).with_for_update()
    user = (await session.execute(user_stmt)).scalar_one_or_none()
    if not user:
        return {
            "held_seconds": 0,
            "actual_seconds": target_actual,
            "settled_seconds": 0,
            "refunded_seconds": 0,
            "overrun_charged_seconds": 0,
            "overrun_uncovered_seconds": 0,
        }

    debits = list((await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == job_id,
            MinuteLedgerEvent.source.in_(_CLONE_LEDGER_SOURCES),
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
    )).scalars().all())
    if not debits:
        return {
            "held_seconds": 0,
            "actual_seconds": target_actual,
            "settled_seconds": 0,
            "refunded_seconds": 0,
            "overrun_charged_seconds": 0,
            "overrun_uncovered_seconds": 0,
        }

    refund_ref = f"REFUND:{job_id}"
    prior_refunds = list((await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == refund_ref,
            MinuteLedgerEvent.source.in_(_CLONE_LEDGER_SOURCES),
            MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
        )
    )).scalars().all())

    overrun_prefix = f"{_RECAP_VOICE_SETTLE_OVERRUN_PREFIX} ({job_id})"
    prior_overrun_charged = sum(
        int(r.seconds or 0)
        for r in debits
        if (r.note or "").startswith(overrun_prefix)
    )
    initial_held = sum(
        int(r.seconds or 0)
        for r in debits
        if not (r.note or "").startswith(overrun_prefix)
    )
    already_refunded = sum(int(r.seconds or 0) for r in prior_refunds)

    debit_all_by_source: dict[str, int] = {}
    debit_refundable_by_source: dict[str, int] = {}
    _now = utc_now()
    current_period = (_now.year, _now.month)
    for debit in debits:
        sec = int(debit.seconds or 0)
        debit_all_by_source[debit.source] = debit_all_by_source.get(debit.source, 0) + sec
        if debit.source == LedgerSource.MONTHLY_FREE.value and debit.created_at is not None:
            if (debit.created_at.year, debit.created_at.month) != current_period:
                continue
        debit_refundable_by_source[debit.source] = debit_refundable_by_source.get(debit.source, 0) + sec

    refunded_by_source: dict[str, int] = {}
    for ref in prior_refunds:
        refunded_by_source[ref.source] = refunded_by_source.get(ref.source, 0) + int(ref.seconds or 0)

    net_held = max(0, sum(debit_all_by_source.values()) - already_refunded)

    # Idempotency when overrun settlement already ran once
    if prior_overrun_charged > 0 and target_actual >= net_held:
        return {
            "held_seconds": initial_held,
            "actual_seconds": target_actual,
            "settled_seconds": net_held,
            "refunded_seconds": already_refunded,
            "overrun_charged_seconds": prior_overrun_charged,
            "overrun_uncovered_seconds": max(0, target_actual - net_held),
        }

    balance = await get_minute_balance(session, user_id)

    if target_actual <= net_held:
        give_back = net_held - target_actual
        if give_back <= 0:
            return {
                "held_seconds": initial_held,
                "actual_seconds": target_actual,
                "settled_seconds": net_held,
                "refunded_seconds": already_refunded,
                "overrun_charged_seconds": prior_overrun_charged,
                "overrun_uncovered_seconds": 0,
            }
        refunded_now = 0
        for src in (
            LedgerSource.PURCHASE.value,
            LedgerSource.WELCOME.value,
            LedgerSource.MONTHLY_FREE.value,
        ):
            if give_back <= 0:
                break
            avail_bucket = max(
                0,
                debit_refundable_by_source.get(src, 0) - refunded_by_source.get(src, 0),
            )
            if avail_bucket <= 0:
                continue
            give = min(give_back, avail_bucket)
            if give <= 0:
                continue
            if src == LedgerSource.PURCHASE.value:
                balance.purchased_seconds += give
                user.account_category = AccountCategory.PURCHASED.value
            elif src == LedgerSource.WELCOME.value:
                balance.welcome_seconds += give
            elif src == LedgerSource.MONTHLY_FREE.value:
                balance.monthly_free_seconds += give
            balance.consumed_seconds = max(0, int(balance.consumed_seconds or 0) - give)
            session.add(
                MinuteLedgerEvent(
                    user_id=user_id,
                    source=src,
                    direction=LedgerDirection.CREDIT.value,
                    seconds=give,
                    reference_id=refund_ref,
                    note=note or f"Recap clone settle refund ({job_id})",
                )
            )
            give_back -= give
            refunded_now += give

        if refunded_now > 0:
            balance.updated_at = utc_now()
            user.updated_at = utc_now()
            await session.flush()

        total_refunded = already_refunded + refunded_now
        settled = max(0, net_held - refunded_now)
        return {
            "held_seconds": initial_held,
            "actual_seconds": target_actual,
            "settled_seconds": settled,
            "refunded_seconds": total_refunded,
            "overrun_charged_seconds": prior_overrun_charged,
            "overrun_uncovered_seconds": 0,
        }

    # target_actual > net_held -> charge overrun capped at available clone balance (safety net)
    overrun_needed = target_actual - net_held
    charge = min(overrun_needed, int(balance.clone_spendable_seconds))
    charged_now = 0
    if charge > 0:
        rem = charge
        if rem > 0 and balance.monthly_free_seconds > 0:
            d = min(balance.monthly_free_seconds, rem)
            balance.monthly_free_seconds -= d
            rem -= d
            charged_now += d
            session.add(
                MinuteLedgerEvent(
                    user_id=user_id,
                    source=LedgerSource.MONTHLY_FREE.value,
                    direction=LedgerDirection.DEBIT.value,
                    seconds=d,
                    reference_id=job_id,
                    note=note or f"{overrun_prefix} (Monthly Free)",
                )
            )
        if rem > 0 and balance.welcome_seconds > 0:
            d = min(balance.welcome_seconds, rem)
            balance.welcome_seconds -= d
            rem -= d
            charged_now += d
            session.add(
                MinuteLedgerEvent(
                    user_id=user_id,
                    source=LedgerSource.WELCOME.value,
                    direction=LedgerDirection.DEBIT.value,
                    seconds=d,
                    reference_id=job_id,
                    note=note or f"{overrun_prefix} (Welcome)",
                )
            )
        if rem > 0 and balance.purchased_seconds > 0:
            d = min(balance.purchased_seconds, rem)
            balance.purchased_seconds -= d
            rem -= d
            charged_now += d
            session.add(
                MinuteLedgerEvent(
                    user_id=user_id,
                    source=LedgerSource.PURCHASE.value,
                    direction=LedgerDirection.DEBIT.value,
                    seconds=d,
                    reference_id=job_id,
                    note=note or f"{overrun_prefix} (Purchased)",
                )
            )
        balance.consumed_seconds = int(balance.consumed_seconds or 0) + charged_now
        balance.updated_at = utc_now()
        user.updated_at = utc_now()
        await session.flush()

    settled_total = net_held + charged_now
    uncovered = max(0, overrun_needed - charged_now)
    return {
        "held_seconds": initial_held,
        "actual_seconds": target_actual,
        "settled_seconds": settled_total,
        "refunded_seconds": already_refunded,
        "overrun_charged_seconds": prior_overrun_charged + charged_now,
        "overrun_uncovered_seconds": uncovered,
    }


async def draw_tts_referral_seconds(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    seconds: int,
) -> int:
    """Batch AW: draw up to ``seconds`` from the referral bucket for a
    TTS job (the referral bucket is the TTS tab's balance).

    Partial coverage is allowed and expected: the caller counts only
    the remainder against the daily TTS cap. The draw is CLAIMED at
    submit time (mirroring how the recap worker claims its credit
    before rendering), recorded as a REFERRAL DEBIT under the job's
    own reference so the failure/settle refunds below can find it.
    Returns the amount actually drawn (0 when the bucket is empty or
    the request is non-positive). The caller owns the transaction and
    the per-user row lock (create_tts_job holds it).
    """
    if seconds <= 0:
        return 0
    balance = await get_minute_balance(session, user_id)
    drawn = min(int(balance.referral_seconds or 0), int(seconds))
    if drawn <= 0:
        return 0
    balance.referral_seconds -= drawn
    balance.updated_at = utc_now()
    session.add(
        MinuteLedgerEvent(
            user_id=user_id,
            source=LedgerSource.REFERRAL.value,
            direction=LedgerDirection.DEBIT.value,
            seconds=drawn,
            reference_id=job_id,
            note="TTS generation deduction (Referral)",
        )
    )
    await session.flush()
    return drawn


async def tts_referral_net_draw(
    session: AsyncSession,
    user_id: int,
    job_id: str,
) -> int:
    """Net referral seconds a TTS job currently holds: its REFERRAL
    debits minus the REFUND:<job_id> credits already returned. This is
    the figure the daily-cap accounting subtracts from a job's cost
    and the settle/refund paths reconcile against."""
    debits = (await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == job_id,
            MinuteLedgerEvent.source == LedgerSource.REFERRAL.value,
            MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
    )).scalars().all()
    credits = (await session.execute(
        select(MinuteLedgerEvent).where(
            MinuteLedgerEvent.user_id == user_id,
            MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
            MinuteLedgerEvent.source == LedgerSource.REFERRAL.value,
            MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
        )
    )).scalars().all()
    net = sum(int(r.seconds or 0) for r in debits) - sum(
        int(r.seconds or 0) for r in credits
    )
    return max(0, net)


async def refund_tts_referral_seconds(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    keep_seconds: int = 0,
    note: str | None = None,
) -> int:
    """Batch AW: return a TTS job's referral draw, keeping
    ``keep_seconds`` of it (the settled charge on success).

    Failure/cancel passes keep_seconds=0 and gets the full draw back;
    success passes keep_seconds=min(net draw, actual seconds) so an
    over-estimate hands its unused referral seconds back. Idempotent
    by construction: it reconciles against tts_referral_net_draw, so
    repeat calls (a settle racing a failure refund) can never credit
    the same seconds twice. Returns the amount credited back.
    """
    net = await tts_referral_net_draw(session, user_id, job_id)
    give_back = net - max(0, int(keep_seconds))
    if give_back <= 0:
        return 0
    balance = await get_minute_balance(session, user_id)
    balance.referral_seconds += give_back
    balance.updated_at = utc_now()
    session.add(
        MinuteLedgerEvent(
            user_id=user_id,
            source=LedgerSource.REFERRAL.value,
            direction=LedgerDirection.CREDIT.value,
            seconds=give_back,
            reference_id=f"REFUND:{job_id}",
            note=note or f"Refund for TTS job {job_id} (Referral)",
        )
    )
    await session.flush()
    return give_back


def recap_refund_reference(job_id: str) -> str:
    """The ledger reference_id a recap job's refund is recorded under
    (the voice path's ``REFUND:<job_id>`` convention)."""
    return f"REFUND:{job_id}"


async def record_recap_credit_event(
    session: AsyncSession,
    *,
    user_id: int,
    direction: str,
    credits: int,
    reference_id: str | None = None,
    note: str | None = None,
) -> None:
    """Append one recap-credit ledger event (Batch O, billing option C).

    The caller owns the transaction: the event is added to `session`
    alongside the balance change it describes, so the two commit (or
    roll back) together.
    """
    if credits <= 0:
        return
    session.add(
        RecapCreditLedgerEvent(
            user_id=user_id,
            source="RECAP",
            direction=direction,
            credits=int(credits),
            reference_id=reference_id,
            note=note,
        )
    )


async def recap_debit_total_for_job(session: AsyncSession, job_id: str) -> int:
    """Batch AJ (H11): total recap credits EVER charged for a job,
    from the durable ledger (submit DEBIT reference = the bare job
    id; top-ups and retry charges share it). Summed in Python — the
    codebase bans func.sum under app/."""
    rows = (await session.execute(
        select(RecapCreditLedgerEvent).where(
            RecapCreditLedgerEvent.reference_id == job_id,
            RecapCreditLedgerEvent.direction == LedgerDirection.DEBIT.value,
        )
    )).scalars().all()
    return sum(int(r.credits or 0) for r in rows)


async def recap_refund_family(
    session: AsyncSession, job_id: str
) -> tuple[int, int]:
    """Batch DC: (total credits, row count) across a job's refund
    family — the plain ``REFUND:<job_id>`` row plus any
    ``REFUND:<job_id>#<n>`` top-up rows. A job re-charged by an
    admin retry (Batch AJ H6) can legitimately be refunded more
    than once, but the partial unique index
    ``uq_recap_ledger_refund_ref`` allows only one CREDIT row per
    exact reference, so later refunds take a ``#<n>`` suffix and
    every total must sum the whole family."""
    base = recap_refund_reference(job_id)
    like_base = (
        base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    rows = (await session.execute(
        select(RecapCreditLedgerEvent).where(
            RecapCreditLedgerEvent.direction == LedgerDirection.CREDIT.value,
            or_(
                RecapCreditLedgerEvent.reference_id == base,
                RecapCreditLedgerEvent.reference_id.like(
                    like_base + "#%", escape="\\"
                ),
            ),
        )
    )).scalars().all()
    rows = [
        r for r in rows
        if r.reference_id == base
        or (r.reference_id or "").startswith(base + "#")
    ]
    return sum(int(r.credits or 0) for r in rows), len(rows)


async def recap_refund_total_for_job(session: AsyncSession, job_id: str) -> int:
    """Batch AJ (H11): total recap credits ever refunded for a job
    (CREDIT events under the REFUND:<job_id> reference; Batch DC:
    the whole refund family, including #<n> top-up rows)."""
    total, _count = await recap_refund_family(session, job_id)
    return total


async def recap_next_refund_reference(
    session: AsyncSession, job_id: str
) -> str:
    """Batch DC: the reference the job's NEXT refund row must use —
    the plain ``REFUND:<job_id>`` for the first refund, then
    ``REFUND:<job_id>#<n>``, so the partial unique index on exact
    references never blocks a legitimate later refund."""
    _total, count = await recap_refund_family(session, job_id)
    base = recap_refund_reference(job_id)
    return base if count == 0 else f"{base}#{count + 1}"


async def recap_refund_event_exists(
    session: AsyncSession, user_id: int, job_id: str
) -> bool:
    """True when a refund CREDIT for this job is already on the ledger
    — the durable cross-check behind the persisted refund claim."""
    existing = (
        await session.execute(
            select(RecapCreditLedgerEvent.id).where(
                RecapCreditLedgerEvent.user_id == user_id,
                RecapCreditLedgerEvent.reference_id == recap_refund_reference(job_id),
                RecapCreditLedgerEvent.direction == LedgerDirection.CREDIT.value,
            )
        )
    ).first()
    return existing is not None


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
                elif source == LedgerSource.MONTHLY_FREE:
                    balance.monthly_free_seconds += seconds
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
                elif source == LedgerSource.MONTHLY_FREE:
                    if balance.monthly_free_seconds < seconds:
                        raise ValueError(
                            f"Insufficient monthly free balance ({balance.monthly_free_seconds}s) to debit {seconds}s"
                        )
                    balance.monthly_free_seconds -= seconds
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
