from __future__ import annotations

import asyncio
import logging
import math
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, JobType, LedgerDirection, LedgerSource
from app.models.schema import (
    AuditLog,
    Job,
    JobCostTelemetry,
    MinuteBalance,
    MinuteLedgerEvent,
    RecapCreditLedgerEvent,
    User,
    WalletGrant,
    WalletReservation,
    WalletReservationLot,
    WalletTransaction,
    utc_now,
)
from app.services.ledger import (
    get_minute_balance,
    record_recap_credit_event,
)

logger = logging.getLogger(__name__)

RECAP_SECONDS_PER_CREDIT = 60
DEFAULT_PLAN_VALIDITY_DAYS = 30
REFUND_GRACE_DAYS = 7


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _grant_fefo_order():
    return (
        case((WalletGrant.expires_at.is_(None), 1), else_=0).asc(),
        WalletGrant.expires_at.asc(),
        WalletGrant.id.asc(),
    )

# Billing modes (immutable snapshot on WalletReservation & Job)
BILLING_MODE_ADMIN_FREE = "admin_free"
BILLING_MODE_VOICE_ONLY = "voice_only"
BILLING_MODE_CREDIT_ONLY = "credit_only"
BILLING_MODE_CREDIT_PLUS_VOICE = "credit_plus_voice_minutes"
BILLING_MODE_SUBTITLE_ONLY = "subtitle_only"


class BillingError(ValueError):
    """Domain error raised on invalid or unaffordable billing operations."""


class InsufficientRecapCreditsError(BillingError):
    def __init__(self, required_seconds: int, available_seconds: int):
        self.required_seconds = max(0, int(required_seconds))
        self.available_seconds = max(0, int(available_seconds))
        super().__init__(
            f"Insufficient recap credits: requested {seconds_to_credits(self.required_seconds):.2f}, "
            f"available {seconds_to_credits(self.available_seconds):.2f}"
        )


class InsufficientVoiceMinutesError(BillingError):
    def __init__(self, required_seconds: int, available_seconds: int):
        self.required_seconds = max(0, int(required_seconds))
        self.available_seconds = max(0, int(available_seconds))
        super().__init__(
            f"Insufficient balance: requested {self.required_seconds}s, available {self.available_seconds}s"
        )


# Loop-scoped per-user asyncio locks so SQLite-backed concurrent tasks serialize
# without leaking lock objects across event loops in tests.
_LOOP_USER_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _user_wallet_lock(user_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    bucket = _LOOP_USER_LOCKS.get(loop)
    if bucket is None:
        bucket = {}
        _LOOP_USER_LOCKS[loop] = bucket
    lock = bucket.get(int(user_id))
    if lock is None:
        lock = asyncio.Lock()
        bucket[int(user_id)] = lock
    return lock


def seconds_to_credits(seconds: int | float | None) -> float:
    """Convert recap output seconds into decimal credits (1 Credit = 60s)."""
    sec = max(0.0, float(seconds or 0.0))
    return round(sec / float(RECAP_SECONDS_PER_CREDIT), 4)


def credits_to_seconds(credits: int | float | None) -> int:
    """Convert decimal recap credits into whole seconds (1 Credit = 60s)."""
    return max(0, int(round(max(0.0, float(credits or 0.0)) * RECAP_SECONDS_PER_CREDIT)))


def get_recap_available_seconds(user: User) -> int:
    """Resolve user's spendable recap balance in integer seconds.

    Bridges direct ORM test fixtures that mutate ``user.recap_credits`` directly
    while preserving exact second-level precision on ``user.recap_credit_seconds``.
    """
    raw_credits = float(getattr(user, "recap_credits", 0.0) or 0.0)
    raw_sec = getattr(user, "recap_credit_seconds", None)
    if raw_sec is None:
        return max(0, int(round(raw_credits * RECAP_SECONDS_PER_CREDIT)))
    sec_int = max(0, int(raw_sec))
    if abs(raw_credits * RECAP_SECONDS_PER_CREDIT - float(sec_int)) > 1.0:
        return max(0, int(round(raw_credits * RECAP_SECONDS_PER_CREDIT)))
    return sec_int


def get_recap_reserved_seconds(user: User) -> int:
    return max(0, int(getattr(user, "reserved_recap_seconds", 0) or 0))


def sync_user_recap_balance(
    user: User,
    new_available_seconds: int,
    new_reserved_seconds: int | None = None,
) -> None:
    """Update both second-accurate recap columns and display float cleanly."""
    avail_sec = max(0, int(new_available_seconds))
    user.recap_credit_seconds = avail_sec
    user.recap_credits = seconds_to_credits(avail_sec)
    if new_reserved_seconds is not None:
        user.reserved_recap_seconds = max(0, int(new_reserved_seconds))
    user.updated_at = utc_now()


async def create_wallet_grant(
    session: AsyncSession,
    user_id: int,
    *,
    source_type: str = "PACKAGE",
    voice_seconds: int = 0,
    recap_seconds: int = 0,
    validity_days: int | None = DEFAULT_PLAN_VALIDITY_DAYS,
    payment_request_id: str | None = None,
    package_code: str | None = None,
    package_name: str | None = None,
    purchased_at: datetime | None = None,
    note: str | None = None,
) -> WalletGrant:
    """Create a per-purchase / per-grant lot with fixed validity_days (None = never expires)."""
    v_sec = max(0, int(voice_seconds or 0))
    r_sec = max(0, int(recap_seconds or 0))
    ts = _as_utc(purchased_at) or utc_now()
    v_days = int(validity_days) if (validity_days is not None and int(validity_days) > 0) else None
    exp_at = (ts + timedelta(days=v_days)) if v_days is not None else None
    grant = WalletGrant(
        user_id=user_id,
        source_type=source_type,
        payment_request_id=payment_request_id,
        package_code=package_code,
        package_name=package_name,
        validity_days=v_days,
        purchased_at=ts,
        expires_at=exp_at,
        initial_voice_seconds=v_sec,
        remaining_voice_seconds=v_sec,
        reserved_voice_seconds=0,
        initial_recap_seconds=r_sec,
        remaining_recap_seconds=r_sec,
        reserved_recap_seconds=0,
        status="ACTIVE" if (v_sec > 0 or r_sec > 0) else "DEPLETED",
        note=note,
    )
    session.add(grant)
    await session.flush()
    return grant


async def _sync_vip_tier_after_expiry(
    session: AsyncSession,
    user: User,
    bal: MinuteBalance | None,
) -> bool:
    """Downgrade PURCHASED -> FREE when all paid lots have expired and no paid balance remains."""
    if settings.is_admin(user.id):
        return False
    if user.account_category != AccountCategory.PURCHASED.value:
        return False
    active_paid = (
        await session.execute(
            select(WalletGrant).where(
                WalletGrant.user_id == user.id,
                WalletGrant.status == "ACTIVE",
            )
        )
    ).scalars().all()
    has_active_grant_bal = any(
        (int(g.remaining_voice_seconds or 0) + int(g.remaining_recap_seconds or 0)) > 0
        for g in active_paid
    )
    if has_active_grant_bal:
        return False
    if bal is not None and (int(bal.purchased_seconds or 0) > 0 or int(bal.reserved_purchased_seconds or 0) > 0):
        return False
    user.account_category = AccountCategory.FREE.value
    user.updated_at = utc_now()
    return True


async def _expire_due_grants_locked(
    session: AsyncSession,
    user_id: int,
    *,
    user: User | None = None,
    bal: MinuteBalance | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expire any due WalletGrant rows for user_id (caller holds user lock or calls via wrapper)."""
    now_utc = _as_utc(now) or utc_now()
    grants = (
        await session.execute(
            select(WalletGrant)
            .where(
                WalletGrant.user_id == user_id,
                WalletGrant.status == "ACTIVE",
                WalletGrant.expires_at.is_not(None),
            )
            .order_by(WalletGrant.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().all()

    due = [g for g in grants if (_as_utc(g.expires_at) is not None and _as_utc(g.expires_at) <= now_utc)]
    if not due:
        return {"expired_grants": 0, "expired_voice_seconds": 0, "expired_recap_seconds": 0, "downgraded_to_free": False}

    if user is None:
        user = (
            await session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    if user is None:
        return {"expired_grants": 0, "expired_voice_seconds": 0, "expired_recap_seconds": 0, "downgraded_to_free": False}

    if bal is None:
        bal = (
            await session.execute(
                select(MinuteBalance)
                .where(MinuteBalance.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if bal is None:
            bal = await get_minute_balance(session, user_id)

    total_exp_v = 0
    total_exp_r = 0
    expired_count = 0

    for g in due:
        rem_v = max(0, int(g.remaining_voice_seconds or 0))
        res_v = max(0, int(g.reserved_voice_seconds or 0))
        rem_r = max(0, int(g.remaining_recap_seconds or 0))
        res_r = max(0, int(g.reserved_recap_seconds or 0))

        unres_v = max(0, rem_v - res_v)
        unres_r = max(0, rem_r - res_r)

        ded_v = 0
        if unres_v > 0 and bal is not None:
            free_purch = max(0, int(bal.purchased_seconds or 0) - int(bal.reserved_purchased_seconds or 0))
            ded_v = min(unres_v, free_purch)
            if ded_v > 0:
                bal.purchased_seconds = max(0, int(bal.purchased_seconds or 0) - ded_v)
                bal.updated_at = now_utc
            g.remaining_voice_seconds = max(res_v, rem_v - unres_v)

        ded_r = 0
        if unres_r > 0:
            avail_r = get_recap_available_seconds(user)
            ded_r = min(unres_r, avail_r)
            if ded_r > 0:
                sync_user_recap_balance(user, max(0, avail_r - ded_r))
            g.remaining_recap_seconds = max(res_r, rem_r - unres_r)

        if res_v == 0 and res_r == 0:
            g.status = "EXPIRED"
            g.expired_at = now_utc
            expired_count += 1
        g.updated_at = now_utc

        if ded_v > 0:
            total_exp_v += ded_v
            session.add(
                MinuteLedgerEvent(
                    user_id=user_id,
                    source=LedgerSource.PURCHASE.value,
                    direction=LedgerDirection.DEBIT.value,
                    seconds=ded_v,
                    reference_id=f"EXPIRE:{g.id}",
                    note=f"Expired grant #{g.id} ({g.package_code or g.source_type})",
                )
            )
        if ded_r > 0:
            total_exp_r += ded_r
            await record_recap_credit_event(
                session,
                user_id=user_id,
                direction=LedgerDirection.DEBIT.value,
                credits=max(0.01, seconds_to_credits(ded_r)),
                seconds=ded_r,
                reference_id=f"EXPIRE:{g.id}",
                note=f"Expired grant #{g.id} ({g.package_code or g.source_type})",
            )
        if ded_v > 0 or ded_r > 0:
            await append_wallet_transaction(
                session,
                user=user,
                balance=bal,
                tx_type="EXPIRE",
                wallet_kind=_wallet_kind_for(ded_r, ded_v),
                recap_seconds_delta=-ded_r,
                voice_seconds_delta=-ded_v,
                reference_id=f"EXPIRE:{g.id}",
                note=f"Expired {g.package_name or g.package_code or g.source_type}",
            )

    downgraded = False
    if expired_count > 0 or total_exp_v > 0 or total_exp_r > 0:
        downgraded = await _sync_vip_tier_after_expiry(session, user, bal)
        await session.flush()

    return {
        "expired_grants": expired_count,
        "expired_voice_seconds": total_exp_v,
        "expired_recap_seconds": total_exp_r,
        "downgraded_to_free": downgraded,
    }


async def expire_due_grants_for_user(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Thread/task-safe lazy expiration sweep for a single user."""
    async with _user_wallet_lock(user_id):
        return await _expire_due_grants_locked(session, user_id, now=now)


async def list_user_grants(
    session: AsyncSession,
    user_id: int,
    *,
    include_expired: bool = True,
    limit: int = 30,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return user's WalletGrant lots ordered FEFO (active first, then recent expired/depleted)."""
    now_utc = _as_utc(now) or utc_now()
    q = select(WalletGrant).where(WalletGrant.user_id == user_id)
    if not include_expired:
        q = q.where(WalletGrant.status == "ACTIVE")
    q = q.order_by(
        case((WalletGrant.status == "ACTIVE", 0), else_=1).asc(),
        *_grant_fefo_order(),
    ).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    out: list[dict[str, Any]] = []
    for g in rows:
        exp_utc = _as_utc(g.expires_at)
        days_left: int | None = None
        hours_left: float | None = None
        expiring_soon = False
        if exp_utc is not None and g.status == "ACTIVE":
            diff_sec = (exp_utc - now_utc).total_seconds()
            hours_left = round(max(0.0, diff_sec / 3600.0), 1)
            days_left = max(0, int(math.ceil(diff_sec / 86400.0))) if diff_sec > 0 else 0
            expiring_soon = (
                diff_sec > 0
                and diff_sec <= 3 * 86400
                and (int(g.remaining_voice_seconds or 0) + int(g.remaining_recap_seconds or 0)) > 0
            )
        out.append({
            "id": g.id,
            "source_type": g.source_type,
            "payment_request_id": g.payment_request_id,
            "package_code": g.package_code,
            "package_name": g.package_name or g.package_code or g.source_type,
            "validity_days": g.validity_days,
            "purchased_at": (_as_utc(g.purchased_at).isoformat() if g.purchased_at else None),
            "expires_at": (exp_utc.isoformat() if exp_utc else None),
            "days_left": days_left,
            "hours_left": hours_left,
            "expiring_soon": expiring_soon,
            "never_expires": exp_utc is None,
            "initial_voice_minutes": round(int(g.initial_voice_seconds or 0) / 60.0, 2),
            "remaining_voice_minutes": round(int(g.remaining_voice_seconds or 0) / 60.0, 2),
            "initial_recap_credits": round(int(g.initial_recap_seconds or 0) / 60.0, 2),
            "remaining_recap_credits": round(int(g.remaining_recap_seconds or 0) / 60.0, 2),
            "status": g.status,
            "note": g.note,
        })
    return out


async def backfill_legacy_wallet_grants(session: AsyncSession) -> dict[str, int]:
    """Idempotently grandfather pre-existing paid balances into never-expiring LEGACY_GRANDFATHER lots."""
    users = (await session.execute(select(User).order_by(User.id.asc()))).scalars().all()
    existing_grant_uids = set(
        (await session.execute(select(WalletGrant.user_id).distinct())).scalars().all()
    )
    bals = {
        b.user_id: b
        for b in (await session.execute(select(MinuteBalance))).scalars().all()
    }
    grandfathered = 0
    for u in users:
        if u.id in existing_grant_uids:
            continue
        bal = bals.get(u.id)
        purch_v = max(0, int(getattr(bal, "purchased_seconds", 0) or 0)) if bal else 0
        recap_s = (
            get_recap_available_seconds(u)
            if u.account_category == AccountCategory.PURCHASED.value
            else 0
        )
        if purch_v <= 0 and recap_s <= 0:
            continue
        session.add(
            WalletGrant(
                user_id=u.id,
                source_type="LEGACY_GRANDFATHER",
                package_code="legacy_balance",
                package_name="Legacy Balance (No Expiry)",
                validity_days=None,
                purchased_at=u.created_at or utc_now(),
                expires_at=None,
                initial_voice_seconds=purch_v,
                remaining_voice_seconds=purch_v,
                reserved_voice_seconds=0,
                initial_recap_seconds=recap_s,
                remaining_recap_seconds=recap_s,
                reserved_recap_seconds=0,
                status="ACTIVE",
                note="Grandfathered pre-30-day transition balance",
            )
        )
        grandfathered += 1
    if grandfathered > 0:
        await session.flush()
    return {"users_scanned": len(users), "users_grandfathered": grandfathered}


async def sweep_expired_grants_and_reminders(
    session: AsyncSession,
    *,
    bot: Any = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Batch-expire all due grants across all users and send deduped 3d / 24h DM reminders."""
    now_utc = _as_utc(now) or utc_now()
    due_uids = list(
        (
            await session.execute(
                select(WalletGrant.user_id)
                .where(
                    WalletGrant.status == "ACTIVE",
                    WalletGrant.expires_at.is_not(None),
                )
                .distinct()
            )
        ).scalars().all()
    )
    expired_grants = 0
    expired_users = 0
    for uid in due_uids:
        res = await expire_due_grants_for_user(session, uid, now=now_utc)
        if res["expired_grants"] > 0 or res["expired_voice_seconds"] > 0 or res["expired_recap_seconds"] > 0:
            expired_grants += res["expired_grants"]
            expired_users += 1

    reminders_sent = 0
    if bot is not None:
        active_expiring = (
            await session.execute(
                select(WalletGrant).where(
                    WalletGrant.status == "ACTIVE",
                    WalletGrant.expires_at.is_not(None),
                )
            )
        ).scalars().all()
        for g in active_expiring:
            rem_v = int(g.remaining_voice_seconds or 0)
            rem_r = int(g.remaining_recap_seconds or 0)
            if (rem_v + rem_r) <= 0:
                continue
            exp_utc = _as_utc(g.expires_at)
            if exp_utc is None:
                continue
            diff_sec = (exp_utc - now_utc).total_seconds()
            if diff_sec <= 0:
                continue
            window: str | None = None
            if diff_sec <= 86400 and g.reminded_24h_at is None:
                window = "24h"
            elif 86400 < diff_sec <= 3 * 86400 and g.reminded_3d_at is None:
                window = "3d"
            if not window:
                continue
            u = await session.get(User, g.user_id)
            if u is None or getattr(u, "bot_blocked", False):
                continue
            lang = (getattr(u, "language_code", "my") or "my").lower()
            v_min = round(rem_v / 60.0, 1)
            r_cr = round(rem_r / 60.0, 1)
            exp_str = exp_utc.strftime("%Y-%m-%d")
            if lang == "en":
                lead = "24 hours" if window == "24h" else "3 days"
                msg = (
                    f"⏳ <b>Package Expiring Soon ({lead} left)</b>\n\n"
                    f"• Package: <b>{g.package_name or g.package_code or 'Plan'}</b>\n"
                    f"• Remaining: <b>{v_min} Voice mins</b> · <b>{r_cr} Recap credits</b>\n"
                    f"• Expires on: <b>{exp_str} (UTC)</b>\n\n"
                    "Use your remaining balance before it expires!"
                )
            else:
                lead = "၂၄ နာရီ" if window == "24h" else "၃ ရက်"
                msg = (
                    f"⏳ <b>Package သက်တမ်းကုန်ဆုံးရန် ({lead}) သာ ကျန်ပါတော့သည်</b>\n\n"
                    f"• Package: <b>{g.package_name or g.package_code or 'Plan'}</b>\n"
                    f"• လက်ကျန်: <b>Voice {v_min} မိနစ်</b> · <b>Recap {r_cr} Credit</b>\n"
                    f"• သက်တမ်းကုန်မည့်ရက်: <b>{exp_str}</b>\n\n"
                    "သက်တမ်းမကုန်မီ လက်ကျန်များကို အသုံးပြုနိုင်ပါသည်။"
                )
            try:
                await bot.send_message(chat_id=g.user_id, text=msg, parse_mode="HTML")
                if window == "24h":
                    g.reminded_24h_at = now_utc
                else:
                    g.reminded_3d_at = now_utc
                reminders_sent += 1
            except Exception as exc:
                logger.warning("Expiry reminder DM failed for user %s grant %s: %s", g.user_id, g.id, exc)
        if reminders_sent > 0:
            await session.flush()

    return {
        "expired_users": expired_users,
        "expired_grants": expired_grants,
        "reminders_sent": reminders_sent,
    }


async def get_wallet_snapshot(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """Return all 4 dual-wallet balances + active grants / earliest expiration metadata."""
    await expire_due_grants_for_user(session, user_id)
    user = await session.get(User, user_id)
    bal = await get_minute_balance(session, user_id)
    is_admin = settings.is_admin(user_id)
    recap_avail_sec = get_recap_available_seconds(user) if user else 0
    recap_res_sec = get_recap_reserved_seconds(user) if user else 0
    voice_avail_sec = int(bal.clone_spendable_seconds) if bal else 0
    voice_res_sec = int(bal.reserved_seconds) if bal else 0

    grants = await list_user_grants(session, user_id, include_expired=True, limit=25)
    active_with_bal = [
        g for g in grants
        if g["status"] == "ACTIVE" and (g["remaining_voice_minutes"] > 0 or g["remaining_recap_credits"] > 0)
    ]
    expiring_active = [g for g in active_with_bal if g["expires_at"] is not None]
    earliest = expiring_active[0] if expiring_active else None

    return {
        "user_id": user_id,
        "is_admin": is_admin,
        "account_category": (
            AccountCategory.PURCHASED.value
            if (is_admin or (user and user.account_category == AccountCategory.PURCHASED.value))
            else AccountCategory.FREE.value
        ),
        "available_recap_seconds": recap_avail_sec,
        "available_recap_credits": round(recap_avail_sec / 60.0, 2),
        "reserved_recap_seconds": recap_res_sec,
        "reserved_recap_credits": round(recap_res_sec / 60.0, 2),
        "available_voice_seconds": voice_avail_sec,
        "available_voice_minutes": round(voice_avail_sec / 60.0, 2),
        "reserved_voice_seconds": voice_res_sec,
        "reserved_voice_minutes": round(voice_res_sec / 60.0, 2),
        "tts_referral_seconds": int(bal.referral_seconds or 0) if bal else 0,
        "earliest_expires_at": earliest["expires_at"] if earliest else None,
        "earliest_days_left": earliest["days_left"] if earliest else None,
        "expiring_soon": any(g["expiring_soon"] for g in expiring_active),
        "has_grandfathered_balance": any(g["never_expires"] for g in active_with_bal),
        "active_grants_count": len(active_with_bal),
        "grants": grants,
    }


def resolve_recap_billing_mode(tts_engine: str, *, is_admin: bool = False) -> str:
    """Determine authoritative billing mode for a recap job."""
    if is_admin:
        return BILLING_MODE_ADMIN_FREE
    eng = str(tts_engine or "").strip().lower()
    if eng == "voxcpm":
        return BILLING_MODE_CREDIT_PLUS_VOICE
    if eng in ("none", "subtitle_only", "no_voice"):
        return BILLING_MODE_SUBTITLE_ONLY
    return BILLING_MODE_CREDIT_ONLY


def estimate_job_requirements(
    *,
    job_type: str,
    tts_engine: str = "voxcpm",
    duration_seconds: float | int | None = None,
    text: str = "",
    speed_preset: str = "normal",
    is_admin: bool = False,
) -> dict[str, Any]:
    """Compute pre-job estimated Recap Credits (seconds) and Voice Clone Minutes (seconds)."""
    jtype = str(job_type or JobType.VOICE_CLONE.value).upper()
    if is_admin:
        return {
            "billing_mode": BILLING_MODE_ADMIN_FREE,
            "estimated_recap_seconds": 0,
            "estimated_recap_credits": 0.0,
            "estimated_voice_seconds": 0,
            "estimated_voice_minutes": 0.0,
        }

    if jtype == JobType.VOICE_CLONE.value:
        from app.services.queue import estimate_voice_job_seconds

        est_voice = max(1, int(math.ceil(estimate_voice_job_seconds(text or "", speed_preset)))) if (text or "").strip() else 0
        return {
            "billing_mode": BILLING_MODE_VOICE_ONLY,
            "estimated_recap_seconds": 0,
            "estimated_recap_credits": 0.0,
            "estimated_voice_seconds": est_voice,
            "estimated_voice_minutes": round(est_voice / 60.0, 2),
        }

    if jtype == JobType.RECAP.value:
        mode = resolve_recap_billing_mode(tts_engine, is_admin=False)
        try:
            dur = float(duration_seconds or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
        # 1 Recap Credit = Final Output 1 Minute (exact second-level calculation).
        # Unknown remote URL duration falls back to a 60s minimum hold until completion probe.
        est_recap = max(1, int(round(dur))) if dur > 0 else 60
        est_voice = max(1, int(math.ceil(dur))) if (mode == BILLING_MODE_CREDIT_PLUS_VOICE and dur > 0) else (60 if mode == BILLING_MODE_CREDIT_PLUS_VOICE else 0)
        return {
            "billing_mode": mode,
            "estimated_recap_seconds": est_recap,
            "estimated_recap_credits": round(est_recap / 60.0, 2),
            "estimated_voice_seconds": est_voice,
            "estimated_voice_minutes": round(est_voice / 60.0, 2),
        }

    return {
        "billing_mode": BILLING_MODE_CREDIT_ONLY,
        "estimated_recap_seconds": 0,
        "estimated_recap_credits": 0.0,
        "estimated_voice_seconds": 0,
        "estimated_voice_minutes": 0.0,
    }


def _wallet_kind_for(recap_sec: int, voice_sec: int) -> str:
    if recap_sec and voice_sec:
        return "COMBO"
    if recap_sec:
        return "RECAP"
    return "VOICE"


async def append_wallet_transaction(
    session: AsyncSession,
    *,
    user: User,
    balance: MinuteBalance,
    tx_type: str,
    wallet_kind: str,
    recap_seconds_delta: int = 0,
    voice_seconds_delta: int = 0,
    reference_id: str | None = None,
    note: str | None = None,
) -> WalletTransaction:
    tx = WalletTransaction(
        user_id=user.id,
        tx_type=tx_type,
        wallet_kind=wallet_kind,
        recap_seconds_delta=int(recap_seconds_delta),
        voice_seconds_delta=int(voice_seconds_delta),
        balance_recap_seconds_after=get_recap_available_seconds(user),
        reserved_recap_seconds_after=get_recap_reserved_seconds(user),
        balance_voice_seconds_after=int(balance.clone_spendable_seconds),
        reserved_voice_seconds_after=int(balance.reserved_seconds),
        reference_id=reference_id,
        note=note,
    )
    session.add(tx)
    return tx


async def _reserve_grant_lots_fefo(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    recap_seconds: int,
    purchased_voice_seconds: int,
) -> None:
    """Reserve seconds across active WalletGrant lots in strict FEFO order (expiring earliest first)."""
    rem_r = max(0, int(recap_seconds or 0))
    rem_v = max(0, int(purchased_voice_seconds or 0))
    if rem_r <= 0 and rem_v <= 0:
        return

    # Clear any stale lot links if this job_id is being re-reserved on retry
    old_lots = (
        await session.execute(
            select(WalletReservationLot).where(WalletReservationLot.job_id == job_id)
        )
    ).scalars().all()
    for ol in old_lots:
        await session.delete(ol)

    grants = (
        await session.execute(
            select(WalletGrant)
            .where(WalletGrant.user_id == user_id, WalletGrant.status == "ACTIVE")
            .order_by(*_grant_fefo_order())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().all()

    now_utc = utc_now()
    for g in grants:
        if rem_r <= 0 and rem_v <= 0:
            break
        free_r = max(0, int(g.remaining_recap_seconds or 0) - int(g.reserved_recap_seconds or 0))
        free_v = max(0, int(g.remaining_voice_seconds or 0) - int(g.reserved_voice_seconds or 0))
        take_r = min(rem_r, free_r) if rem_r > 0 else 0
        take_v = min(rem_v, free_v) if rem_v > 0 else 0
        if take_r <= 0 and take_v <= 0:
            continue
        g.reserved_recap_seconds = int(g.reserved_recap_seconds or 0) + take_r
        g.reserved_voice_seconds = int(g.reserved_voice_seconds or 0) + take_v
        g.updated_at = now_utc
        session.add(
            WalletReservationLot(
                job_id=job_id,
                grant_id=g.id,
                reserved_recap_seconds=take_r,
                reserved_voice_seconds=take_v,
            )
        )
        rem_r -= take_r
        rem_v -= take_v


async def _settle_grant_lots(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    user: User,
    bal: MinuteBalance,
    settled_recap_seconds: int,
    settled_purchased_voice_seconds: int,
) -> None:
    """Apply final settled usage to reserved FEFO lots (plus any overrun to active FEFO lots)."""
    now_utc = utc_now()
    lots = (
        await session.execute(
            select(WalletReservationLot)
            .where(WalletReservationLot.job_id == job_id)
            .order_by(WalletReservationLot.id.asc())
        )
    ).scalars().all()

    rem_r = max(0, int(settled_recap_seconds or 0))
    rem_v = max(0, int(settled_purchased_voice_seconds or 0))

    grant_ids = [l.grant_id for l in lots]
    grants_by_id: dict[int, WalletGrant] = {}
    if grant_ids:
        g_rows = (
            await session.execute(
                select(WalletGrant)
                .where(WalletGrant.id.in_(grant_ids))
                .order_by(*_grant_fefo_order())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        grants_by_id = {g.id: g for g in g_rows}

    # Sort lots by their grant's FEFO rank
    fefo_rank = {gid: idx for idx, gid in enumerate(grants_by_id.keys())}
    lots_sorted = sorted(lots, key=lambda l: fefo_rank.get(l.grant_id, 10**9))

    for lot in lots_sorted:
        g = grants_by_id.get(lot.grant_id)
        if g is None:
            continue
        lr = int(lot.reserved_recap_seconds or 0)
        lv = int(lot.reserved_voice_seconds or 0)
        g.reserved_recap_seconds = max(0, int(g.reserved_recap_seconds or 0) - lr)
        g.reserved_voice_seconds = max(0, int(g.reserved_voice_seconds or 0) - lv)

        use_r = min(rem_r, lr, int(g.remaining_recap_seconds or 0))
        use_v = min(rem_v, lv, int(g.remaining_voice_seconds or 0))
        g.remaining_recap_seconds = max(0, int(g.remaining_recap_seconds or 0) - use_r)
        g.remaining_voice_seconds = max(0, int(g.remaining_voice_seconds or 0) - use_v)
        lot.settled_recap_seconds = use_r
        lot.settled_voice_seconds = use_v
        lot.released_recap_seconds = max(0, lr - use_r)
        lot.released_voice_seconds = max(0, lv - use_v)
        rem_r -= use_r
        rem_v -= use_v

        if (
            g.status == "ACTIVE"
            and int(g.remaining_recap_seconds or 0) == 0
            and int(g.remaining_voice_seconds or 0) == 0
            and int(g.reserved_recap_seconds or 0) == 0
            and int(g.reserved_voice_seconds or 0) == 0
        ):
            g.status = "DEPLETED"
        g.updated_at = now_utc

    # Consume any overrun from other active FEFO lots
    if rem_r > 0 or rem_v > 0:
        extra_grants = (
            await session.execute(
                select(WalletGrant)
                .where(WalletGrant.user_id == user_id, WalletGrant.status == "ACTIVE")
                .order_by(*_grant_fefo_order())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        for g in extra_grants:
            if rem_r <= 0 and rem_v <= 0:
                break
            free_r = max(0, int(g.remaining_recap_seconds or 0) - int(g.reserved_recap_seconds or 0))
            free_v = max(0, int(g.remaining_voice_seconds or 0) - int(g.reserved_voice_seconds or 0))
            take_r = min(rem_r, free_r) if rem_r > 0 else 0
            take_v = min(rem_v, free_v) if rem_v > 0 else 0
            if take_r <= 0 and take_v <= 0:
                continue
            g.remaining_recap_seconds = max(0, int(g.remaining_recap_seconds or 0) - take_r)
            g.remaining_voice_seconds = max(0, int(g.remaining_voice_seconds or 0) - take_v)
            rem_r -= take_r
            rem_v -= take_v
            if (
                g.status == "ACTIVE"
                and int(g.remaining_recap_seconds or 0) == 0
                and int(g.remaining_voice_seconds or 0) == 0
                and int(g.reserved_recap_seconds or 0) == 0
                and int(g.reserved_voice_seconds or 0) == 0
            ):
                g.status = "DEPLETED"
            g.updated_at = now_utc

    # Expire any lot whose expires_at passed while the job was running (so unused released seconds do not resurrect expired lots)
    await _expire_due_grants_locked(session, user_id, user=user, bal=bal, now=now_utc)


async def _release_or_refund_grant_lots(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    user: User | None,
    bal: MinuteBalance | None,
    from_reserved: bool,
    note: str,
) -> WalletGrant | None:
    """Release reserved holds or refund settled charges back to original lots; grant +7d REFUND_GRACE lot if expired."""
    now_utc = utc_now()
    lots = (
        await session.execute(
            select(WalletReservationLot)
            .where(WalletReservationLot.job_id == job_id)
            .order_by(WalletReservationLot.id.asc())
        )
    ).scalars().all()
    if not lots:
        return None

    grant_ids = [l.grant_id for l in lots]
    g_rows = (
        await session.execute(
            select(WalletGrant)
            .where(WalletGrant.id.in_(grant_ids))
            .order_by(WalletGrant.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    grants_by_id = {g.id: g for g in g_rows}

    expired_grace_r = 0
    expired_grace_v = 0

    for lot in lots:
        g = grants_by_id.get(lot.grant_id)
        if g is None:
            continue
        exp_utc = _as_utc(g.expires_at)
        is_expired = (g.status == "EXPIRED") or (exp_utc is not None and exp_utc <= now_utc)

        if from_reserved:
            lr = int(lot.reserved_recap_seconds or 0)
            lv = int(lot.reserved_voice_seconds or 0)
            g.reserved_recap_seconds = max(0, int(g.reserved_recap_seconds or 0) - lr)
            g.reserved_voice_seconds = max(0, int(g.reserved_voice_seconds or 0) - lv)
            lot.released_recap_seconds = lr
            lot.released_voice_seconds = lv
            if is_expired:
                # Move the refunded seconds out of the expired lot into a +7d grace lot
                g.remaining_recap_seconds = max(0, int(g.remaining_recap_seconds or 0) - lr)
                g.remaining_voice_seconds = max(0, int(g.remaining_voice_seconds or 0) - lv)
                if (
                    int(g.remaining_recap_seconds or 0) == 0
                    and int(g.remaining_voice_seconds or 0) == 0
                    and int(g.reserved_recap_seconds or 0) == 0
                    and int(g.reserved_voice_seconds or 0) == 0
                ):
                    g.status = "EXPIRED"
                    g.expired_at = g.expired_at or now_utc
                expired_grace_r += lr
                expired_grace_v += lv
            g.updated_at = now_utc
        else:
            sr = int(lot.settled_recap_seconds or 0)
            sv = int(lot.settled_voice_seconds or 0)
            if sr <= 0 and sv <= 0:
                continue
            if is_expired:
                expired_grace_r += sr
                expired_grace_v += sv
            else:
                g.remaining_recap_seconds = int(g.remaining_recap_seconds or 0) + sr
                g.remaining_voice_seconds = int(g.remaining_voice_seconds or 0) + sv
                if g.status == "DEPLETED":
                    g.status = "ACTIVE"
                g.updated_at = now_utc

    grace_grant: WalletGrant | None = None
    if expired_grace_r > 0 or expired_grace_v > 0:
        grace_grant = await create_wallet_grant(
            session,
            user_id,
            source_type="REFUND_GRACE",
            voice_seconds=expired_grace_v,
            recap_seconds=expired_grace_r,
            validity_days=REFUND_GRACE_DAYS,
            package_code="refund_grace",
            package_name=f"Refund Grace (+{REFUND_GRACE_DAYS}d)",
            purchased_at=now_utc,
            note=f"{note} (original lot expired; +{REFUND_GRACE_DAYS}d grace)",
        )
        if user is not None:
            user.account_category = AccountCategory.PURCHASED.value
            user.updated_at = now_utc
    return grace_grant


async def reserve_for_job(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    job_type: str,
    billing_mode: str,
    reserve_recap_seconds: int = 0,
    reserve_voice_seconds: int = 0,
    note: str | None = None,
) -> WalletReservation:
    """Atomically reserve Recap Credits and/or Voice Clone Minutes for a job.

    - Idempotent for an already-RESERVED job (safe on retries/double-submits).
    - Uses per-user asyncio lock + row lock so concurrent jobs never overdraft.
    """
    req_recap = max(0, int(reserve_recap_seconds))
    req_voice = max(0, int(reserve_voice_seconds))

    async with _user_wallet_lock(user_id):
        existing = (
            await session.execute(
                select(WalletReservation)
                .where(WalletReservation.job_id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.status == "RESERVED":
                return existing
            if existing.status == "SETTLED":
                raise BillingError(f"Job {job_id} already settled; cannot reserve again.")

        if settings.is_admin(user_id) or billing_mode == BILLING_MODE_ADMIN_FREE:
            if existing is None:
                res = WalletReservation(
                    job_id=job_id,
                    user_id=user_id,
                    job_type=job_type,
                    billing_mode=BILLING_MODE_ADMIN_FREE,
                    status="RESERVED",
                    note=note or f"Admin exemption ({job_id})",
                )
                session.add(res)
                await session.flush()
                return res
            existing.status = "RESERVED"
            existing.billing_mode = BILLING_MODE_ADMIN_FREE
            existing.reserved_recap_seconds = 0
            existing.reserved_voice_seconds = 0
            existing.settled_at = None
            await session.flush()
            return existing

        user = (
            await session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            raise BillingError(f"User {user_id} not found")

        bal = (
            await session.execute(
                select(MinuteBalance)
                .where(MinuteBalance.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if bal is None:
            bal = await get_minute_balance(session, user_id)

        # Enforce 30-day expiry BEFORE checking spendable balance so expired lots can never be spent.
        await _expire_due_grants_locked(session, user_id, user=user, bal=bal)

        avail_recap = get_recap_available_seconds(user)
        if req_recap > avail_recap:
            raise InsufficientRecapCreditsError(req_recap, avail_recap)

        avail_voice = int(bal.clone_spendable_seconds)
        if req_voice > avail_voice:
            raise InsufficientVoiceMinutesError(req_voice, avail_voice)

        # Hold recap seconds cleanly
        if req_recap > 0:
            sync_user_recap_balance(
                user,
                avail_recap - req_recap,
                get_recap_reserved_seconds(user) + req_recap,
            )

        # Hold voice seconds non-destructively in reserved counters (Monthly Free -> Welcome -> Purchased).
        # Gross buckets stay intact until final settle_job_reservation so reserved funds are tracked
        # via clone_spendable_seconds = (monthly + welcome + purchased) - reserved_seconds.
        free_monthly = max(
            0,
            int(bal.monthly_free_seconds or 0) - int(bal.reserved_monthly_free_seconds or 0),
        )
        free_welcome = max(
            0,
            int(bal.welcome_seconds or 0) - int(bal.reserved_welcome_seconds or 0),
        )
        free_purchased = max(
            0,
            int(bal.purchased_seconds or 0) - int(bal.reserved_purchased_seconds or 0),
        )
        res_monthly = 0
        res_welcome = 0
        res_purchased = 0
        rem_voice = req_voice
        if rem_voice > 0 and free_monthly > 0:
            d = min(free_monthly, rem_voice)
            bal.reserved_monthly_free_seconds = int(bal.reserved_monthly_free_seconds or 0) + d
            res_monthly = d
            rem_voice -= d
        if rem_voice > 0 and free_welcome > 0:
            d = min(free_welcome, rem_voice)
            bal.reserved_welcome_seconds = int(bal.reserved_welcome_seconds or 0) + d
            res_welcome = d
            rem_voice -= d
        if rem_voice > 0 and free_purchased > 0:
            d = min(free_purchased, rem_voice)
            bal.reserved_purchased_seconds = int(bal.reserved_purchased_seconds or 0) + d
            res_purchased = d
            rem_voice -= d
        if rem_voice > 0:
            # Edge guard if reserved buckets drifted from gross across legacy ops
            bal.reserved_purchased_seconds = int(bal.reserved_purchased_seconds or 0) + rem_voice
            res_purchased += rem_voice
            rem_voice = 0

        if req_voice > 0:
            bal.updated_at = utc_now()

        if existing is not None:
            res = existing
            res.billing_mode = billing_mode
            res.status = "RESERVED"
            res.reserved_recap_seconds = req_recap
            res.reserved_voice_seconds = req_voice
            res.reserved_voice_monthly = res_monthly
            res.reserved_voice_welcome = res_welcome
            res.reserved_voice_purchased = res_purchased
            res.settled_recap_seconds = 0
            res.settled_voice_seconds = 0
            res.refunded_recap_seconds = 0
            res.refunded_voice_seconds = 0
            res.partial_delivered = False
            res.settled_at = None
            res.note = note or f"Re-reserved for retry ({job_id})"
            res.updated_at = utc_now()
        else:
            res = WalletReservation(
                job_id=job_id,
                user_id=user_id,
                job_type=job_type,
                billing_mode=billing_mode,
                status="RESERVED",
                reserved_recap_seconds=req_recap,
                reserved_voice_seconds=req_voice,
                reserved_voice_monthly=res_monthly,
                reserved_voice_welcome=res_welcome,
                reserved_voice_purchased=res_purchased,
                note=note or f"Reserved for job {job_id}",
            )
            session.add(res)

        await session.flush()
        await _reserve_grant_lots_fefo(
            session,
            user_id,
            job_id,
            recap_seconds=req_recap,
            purchased_voice_seconds=res_purchased,
        )

        if req_recap > 0 or req_voice > 0:
            await append_wallet_transaction(
                session,
                user=user,
                balance=bal,
                tx_type="RESERVE",
                wallet_kind=_wallet_kind_for(req_recap, req_voice),
                recap_seconds_delta=-req_recap,
                voice_seconds_delta=-req_voice,
                reference_id=job_id,
                note=note or f"Reserved for {job_type} ({job_id})",
            )

        await session.flush()
        return res


def _clear_reserved_voice_buckets(
    bal: MinuteBalance,
    monthly: int,
    welcome: int,
    purchased: int,
) -> None:
    """Release reserved voice holds cleanly (gross buckets stayed intact during reserve)."""
    if monthly > 0:
        bal.reserved_monthly_free_seconds = max(0, int(bal.reserved_monthly_free_seconds or 0) - monthly)
    if welcome > 0:
        bal.reserved_welcome_seconds = max(0, int(bal.reserved_welcome_seconds or 0) - welcome)
    if purchased > 0:
        bal.reserved_purchased_seconds = max(0, int(bal.reserved_purchased_seconds or 0) - purchased)


async def settle_job_reservation(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    actual_recap_seconds: int = 0,
    actual_voice_seconds: int = 0,
    partial_delivered: bool = False,
    raise_on_voice_shortfall: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """Finalize a job's reservation against actual measured output durations.

    - Deducts ONLY actual used recap credits (output duration) & actual voice seconds.
    - Automatically releases any excess reserve back to the user's available wallets.
    - Prevents duplicate deduction under row lock + per-user lock.
    """
    target_recap = max(0, int(actual_recap_seconds))
    target_voice = max(0, int(actual_voice_seconds))

    async with _user_wallet_lock(user_id):
        res = (
            await session.execute(
                select(WalletReservation)
                .where(WalletReservation.job_id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

        if res is None:
            return {"found": False, "already_settled": False, "settled_recap_seconds": 0, "settled_voice_seconds": 0, "refunded_recap_seconds": 0, "refunded_voice_seconds": 0}

        if res.status != "RESERVED":
            return {
                "found": True,
                "already_settled": True,
                "status": res.status,
                "settled_recap_seconds": int(res.settled_recap_seconds or 0),
                "settled_voice_seconds": int(res.settled_voice_seconds or 0),
                "refunded_recap_seconds": int(res.refunded_recap_seconds or 0),
                "refunded_voice_seconds": int(res.refunded_voice_seconds or 0),
            }

        if settings.is_admin(user_id) or res.billing_mode == BILLING_MODE_ADMIN_FREE:
            res.status = "SETTLED"
            res.settled_at = utc_now()
            res.updated_at = utc_now()
            await session.flush()
            return {"found": True, "already_settled": False, "status": "SETTLED", "settled_recap_seconds": 0, "settled_voice_seconds": 0, "refunded_recap_seconds": 0, "refunded_voice_seconds": 0}

        user = (
            await session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            return {"found": True, "already_settled": False, "settled_recap_seconds": 0, "settled_voice_seconds": 0, "refunded_recap_seconds": 0, "refunded_voice_seconds": 0}

        bal = (
            await session.execute(
                select(MinuteBalance)
                .where(MinuteBalance.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if bal is None:
            bal = await get_minute_balance(session, user_id)

        # Enforce strict billing rule separation even if caller passed noisy values:
        if res.billing_mode in (BILLING_MODE_CREDIT_ONLY, BILLING_MODE_SUBTITLE_ONLY):
            target_voice = 0
        elif res.billing_mode == BILLING_MODE_VOICE_ONLY:
            target_recap = 0

        # ── 1. Settle Recap Credits (1 Credit = 60s output) ──
        reserved_recap = int(res.reserved_recap_seconds or 0)
        avail_recap = get_recap_available_seconds(user)
        reserved_recap_wallet = get_recap_reserved_seconds(user)

        if target_recap <= reserved_recap:
            settled_recap = target_recap
            released_recap = reserved_recap - target_recap
            new_avail_recap = avail_recap + released_recap
            new_res_recap = max(0, reserved_recap_wallet - reserved_recap)
        else:
            overrun_recap = target_recap - reserved_recap
            extra_recap = min(overrun_recap, avail_recap)
            settled_recap = reserved_recap + extra_recap
            released_recap = 0
            new_avail_recap = max(0, avail_recap - extra_recap)
            new_res_recap = max(0, reserved_recap_wallet - reserved_recap)

        sync_user_recap_balance(user, new_avail_recap, new_res_recap)

        if settled_recap > 0:
            await record_recap_credit_event(
                session,
                user_id=user_id,
                direction=LedgerDirection.DEBIT.value,
                credits=max(0.01, seconds_to_credits(settled_recap)),
                seconds=settled_recap,
                reference_id=job_id,
                note=note or f"Recap output {settled_recap}s settled ({job_id})",
            )

        # ── 2. Settle Voice Clone Minutes (actual synthesized audio seconds) ──
        r_m = int(res.reserved_voice_monthly or 0)
        r_w = int(res.reserved_voice_welcome or 0)
        r_p = int(res.reserved_voice_purchased or 0)
        reserved_voice = int(res.reserved_voice_seconds or (r_m + r_w + r_p))

        if raise_on_voice_shortfall and target_voice > 0:
            avail_for_job = (
                max(0, int(bal.monthly_free_seconds or 0) - max(0, int(bal.reserved_monthly_free_seconds or 0) - r_m))
                + max(0, int(bal.welcome_seconds or 0) - max(0, int(bal.reserved_welcome_seconds or 0) - r_w))
                + max(0, int(bal.purchased_seconds or 0) - max(0, int(bal.reserved_purchased_seconds or 0) - r_p))
            )
            if target_voice > avail_for_job:
                sync_user_recap_balance(
                    user,
                    avail_recap + reserved_recap,
                    max(0, reserved_recap_wallet - reserved_recap),
                )
                _clear_reserved_voice_buckets(bal, r_m, r_w, r_p)
                res.status = "RELEASED"
                res.refunded_recap_seconds = reserved_recap
                res.refunded_voice_seconds = reserved_voice
                res.updated_at = utc_now()
                await session.flush()
                raise ValueError(
                    f"Insufficient balance: requested {target_voice}s, available {avail_for_job}s"
                )

        # Consume order: Monthly Free -> Welcome -> Purchased
        settled_voice = 0
        rem_use = min(target_voice, reserved_voice)

        use_m = min(r_m, rem_use)
        rem_use -= use_m
        use_w = min(r_w, rem_use)
        rem_use -= use_w
        use_p = min(r_p, rem_use)
        rem_use -= use_p
        settled_voice = use_m + use_w + use_p

        rel_m = r_m - use_m
        rel_w = r_w - use_w
        rel_p = r_p - use_p
        released_voice = rel_m + rel_w + rel_p

        # Clear ALL reserved holds for this job (used + unused) from reserved counters first,
        # then deduct ONLY actual used seconds from the underlying buckets (respecting other jobs' holds).
        _clear_reserved_voice_buckets(bal, r_m, r_w, r_p)

        actual_debited: list[tuple[str, int, str]] = []
        settled_voice = 0
        rem_shortfall = 0
        for src_val, attr, res_attr, want_sec, label in (
            (LedgerSource.MONTHLY_FREE.value, "monthly_free_seconds", "reserved_monthly_free_seconds", use_m, "Monthly Free"),
            (LedgerSource.WELCOME.value, "welcome_seconds", "reserved_welcome_seconds", use_w, "Welcome"),
            (LedgerSource.PURCHASE.value, "purchased_seconds", "reserved_purchased_seconds", use_p, "Purchased"),
        ):
            want = want_sec + rem_shortfall
            if want <= 0:
                continue
            avail_b = int(getattr(bal, attr) or 0)
            free_b = max(0, avail_b - int(getattr(bal, res_attr) or 0))
            d = min(free_b, want)
            rem_shortfall = want - d
            if d > 0:
                setattr(bal, attr, avail_b - d)
                settled_voice += d
                actual_debited.append((src_val, d, label))

        # Safety net if actual exceeded reserved (never steal another concurrent job's reserve)
        extra_voice_debited = 0
        if target_voice > reserved_voice:
            overrun_v = target_voice - reserved_voice
            extra_v = min(overrun_v, int(bal.clone_spendable_seconds))
            rem_extra = extra_v
            for src_val, attr, res_attr, label in (
                (LedgerSource.MONTHLY_FREE.value, "monthly_free_seconds", "reserved_monthly_free_seconds", "Monthly Free"),
                (LedgerSource.WELCOME.value, "welcome_seconds", "reserved_welcome_seconds", "Welcome"),
                (LedgerSource.PURCHASE.value, "purchased_seconds", "reserved_purchased_seconds", "Purchased"),
            ):
                free_b = max(0, int(getattr(bal, attr) or 0) - int(getattr(bal, res_attr) or 0))
                if rem_extra > 0 and free_b > 0:
                    d = min(free_b, rem_extra)
                    setattr(bal, attr, int(getattr(bal, attr) or 0) - d)
                    rem_extra -= d
                    settled_voice += d
                    extra_voice_debited += d
                    actual_debited.append((src_val, d, f"{label} overrun"))

        debited_by_source: dict[str, int] = {}
        for src_val, sec_used, _label in actual_debited:
            if sec_used > 0:
                debited_by_source[src_val] = debited_by_source.get(src_val, 0) + sec_used
        for src_val, sec_used in debited_by_source.items():
            session.add(
                MinuteLedgerEvent(
                    user_id=user_id,
                    source=src_val,
                    direction=LedgerDirection.DEBIT.value,
                    seconds=sec_used,
                    reference_id=job_id,
                    note=note or f"Job settle ({src_val}) {sec_used}s",
                )
            )

        bal.consumed_seconds = int(bal.consumed_seconds or 0) + settled_voice
        bal.updated_at = utc_now()

        settled_purchased_voice = debited_by_source.get(LedgerSource.PURCHASE.value, 0)
        await _settle_grant_lots(
            session,
            user_id,
            job_id,
            user=user,
            bal=bal,
            settled_recap_seconds=settled_recap,
            settled_purchased_voice_seconds=settled_purchased_voice,
        )

        res.status = "SETTLED"
        res.settled_recap_seconds = settled_recap
        res.settled_voice_seconds = settled_voice
        res.refunded_recap_seconds = released_recap
        res.refunded_voice_seconds = released_voice
        res.partial_delivered = bool(partial_delivered)
        res.settled_at = utc_now()
        res.updated_at = utc_now()

        overrun_recap_debited = max(0, settled_recap - reserved_recap)
        if settled_recap > 0 or settled_voice > 0:
            await append_wallet_transaction(
                session,
                user=user,
                balance=bal,
                tx_type="DEDUCT",
                wallet_kind=_wallet_kind_for(settled_recap, settled_voice),
                recap_seconds_delta=-overrun_recap_debited,
                voice_seconds_delta=-extra_voice_debited,
                reference_id=job_id,
                note=(
                    f"Settled actual usage: recap={settled_recap}s ({seconds_to_credits(settled_recap):.2f} cr), "
                    f"voice={settled_voice}s"
                    + (" [partial]" if partial_delivered else "")
                ),
            )

        if released_recap > 0 or released_voice > 0:
            await append_wallet_transaction(
                session,
                user=user,
                balance=bal,
                tx_type="RELEASE",
                wallet_kind=_wallet_kind_for(released_recap, released_voice),
                recap_seconds_delta=released_recap,
                voice_seconds_delta=released_voice,
                reference_id=job_id,
                note=f"Released unused reserve: recap={released_recap}s, voice={released_voice}s",
            )

        await session.flush()
        return {
            "found": True,
            "already_settled": False,
            "status": "SETTLED",
            "settled_recap_seconds": settled_recap,
            "settled_voice_seconds": settled_voice,
            "refunded_recap_seconds": released_recap,
            "refunded_voice_seconds": released_voice,
        }


async def release_job_reservation(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    reason: str = "JOB_FAILED",
    note: str | None = None,
) -> dict[str, Any]:
    """Release 100% of an unused RESERVED hold back into available wallets (idempotent)."""
    async with _user_wallet_lock(user_id):
        res = (
            await session.execute(
                select(WalletReservation)
                .where(WalletReservation.job_id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if res is None:
            return {"found": False, "released": False, "released_recap_seconds": 0, "released_voice_seconds": 0}
        if res.status != "RESERVED":
            return {
                "found": True,
                "released": False,
                "status": res.status,
                "released_recap_seconds": int(res.refunded_recap_seconds or 0),
                "released_voice_seconds": int(res.refunded_voice_seconds or 0),
            }

        user = (
            await session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        bal = (
            await session.execute(
                select(MinuteBalance)
                .where(MinuteBalance.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if bal is None and user is not None:
            bal = await get_minute_balance(session, user_id)

        rel_recap = int(res.reserved_recap_seconds or 0)
        rel_m = int(res.reserved_voice_monthly or 0)
        rel_w = int(res.reserved_voice_welcome or 0)
        rel_p = int(res.reserved_voice_purchased or 0)
        rel_voice = int(res.reserved_voice_seconds or (rel_m + rel_w + rel_p))

        if user is not None:
            sync_user_recap_balance(
                user,
                get_recap_available_seconds(user) + rel_recap,
                max(0, get_recap_reserved_seconds(user) - rel_recap),
            )
        if bal is not None and rel_voice > 0:
            _clear_reserved_voice_buckets(bal, rel_m, rel_w, rel_p)
            bal.updated_at = utc_now()

        await _release_or_refund_grant_lots(
            session,
            user_id,
            job_id,
            user=user,
            bal=bal,
            from_reserved=True,
            note=note or f"Released hold on {reason} ({job_id})",
        )

        res.status = "RELEASED"
        res.refunded_recap_seconds = rel_recap
        res.refunded_voice_seconds = rel_voice
        res.settled_at = utc_now()
        res.updated_at = utc_now()

        if user is not None and bal is not None and (rel_recap > 0 or rel_voice > 0):
            await append_wallet_transaction(
                session,
                user=user,
                balance=bal,
                tx_type="REFUND",
                wallet_kind=_wallet_kind_for(rel_recap, rel_voice),
                recap_seconds_delta=rel_recap,
                voice_seconds_delta=rel_voice,
                reference_id=f"REFUND:{job_id}",
                note=note or f"Released hold on {reason} ({job_id})",
            )

        await session.flush()
        return {
            "found": True,
            "released": True,
            "status": "RELEASED",
            "released_recap_seconds": rel_recap,
            "released_voice_seconds": rel_voice,
        }


async def admin_refund_settled_job(
    session: AsyncSession,
    admin_id: int,
    job_id: str,
    *,
    reason: str = "Admin refund",
) -> dict[str, Any]:
    """Refund a SETTLED or stuck RESERVED job safely without ever double-refunding."""
    job = await session.get(Job, job_id)
    if job is None:
        raise BillingError("Job not found")

    res = (
        await session.execute(
            select(WalletReservation)
            .where(WalletReservation.job_id == job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if res is not None and res.status == "RESERVED":
        out = await release_job_reservation(
            session, job.user_id, job_id, reason="ADMIN_REFUND", note=f"Admin {admin_id}: {reason}"
        )
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="job.refund",
                target_type="job",
                target_id=job_id,
                details_json=str(out),
            )
        )
        await session.flush()
        return {
            "success": True,
            "job_id": job_id,
            "user_id": job.user_id,
            "refunded_recap_seconds": out["released_recap_seconds"],
            "refunded_voice_seconds": out["released_voice_seconds"],
        }

    # For already-SETTLED reservations: check how much settled balance remains unrefunded
    already_refunded_tx = (
        await session.execute(
            select(WalletTransaction).where(
                WalletTransaction.user_id == job.user_id,
                WalletTransaction.reference_id == f"ADMINREFUND:{job_id}",
            )
        )
    ).scalar_one_or_none()
    if already_refunded_tx is not None:
        raise BillingError("Job has already been refunded by an administrator")

    async with _user_wallet_lock(job.user_id):
        user = (
            await session.execute(
                select(User).where(User.id == job.user_id).with_for_update()
            )
        ).scalar_one_or_none()
        bal = await get_minute_balance(session, job.user_id)
        if user is None:
            raise BillingError("Job owner not found")

        recap_sec = int(res.settled_recap_seconds or 0) if res else 0
        voice_sec = int(res.settled_voice_seconds or 0) if res else 0

        if res is None:
            # Legacy fallback: inspect Job metrics / ledger rows
            from app.services.ledger import net_job_seconds_held, recap_debit_total_for_job, recap_refund_total_for_job, refund_job_seconds

            voice_net = await net_job_seconds_held(session, job.user_id, job_id)
            recap_net_cr = max(
                0.0,
                float(await recap_debit_total_for_job(session, job_id))
                - float(await recap_refund_total_for_job(session, job_id)),
            )
            recap_sec = credits_to_seconds(recap_net_cr)
            if voice_net > 0:
                await refund_job_seconds(session, job.user_id, job_id, note=f"Admin refund {admin_id}: {reason}")
                voice_sec = voice_net
            if recap_sec > 0:
                sync_user_recap_balance(user, get_recap_available_seconds(user) + recap_sec)
                await record_recap_credit_event(
                    session,
                    user_id=job.user_id,
                    direction=LedgerDirection.CREDIT.value,
                    credits=seconds_to_credits(recap_sec),
                    seconds=recap_sec,
                    reference_id=f"ADMINREFUND:{job_id}",
                    note=f"Admin refund {admin_id}: {reason}",
                )
        else:
            if recap_sec > 0:
                sync_user_recap_balance(user, get_recap_available_seconds(user) + recap_sec)
                await record_recap_credit_event(
                    session,
                    user_id=job.user_id,
                    direction=LedgerDirection.CREDIT.value,
                    credits=seconds_to_credits(recap_sec),
                    seconds=recap_sec,
                    reference_id=f"ADMINREFUND:{job_id}",
                    note=f"Admin refund {admin_id}: {reason}",
                )
            if voice_sec > 0:
                bal.purchased_seconds = int(bal.purchased_seconds or 0) + voice_sec
                bal.consumed_seconds = max(0, int(bal.consumed_seconds or 0) - voice_sec)
                bal.updated_at = utc_now()
                session.add(
                    MinuteLedgerEvent(
                        user_id=job.user_id,
                        source=LedgerSource.PURCHASE.value,
                        direction=LedgerDirection.CREDIT.value,
                        seconds=voice_sec,
                        reference_id=f"REFUND:{job_id}",
                        note=f"Admin refund {admin_id}: {reason}",
                    )
                )
            res.refunded_recap_seconds = int(res.refunded_recap_seconds or 0) + recap_sec
            res.refunded_voice_seconds = int(res.refunded_voice_seconds or 0) + voice_sec
            res.status = "RELEASED"
            res.updated_at = utc_now()
            await _release_or_refund_grant_lots(
                session,
                job.user_id,
                job_id,
                user=user,
                bal=bal,
                from_reserved=False,
                note=f"Admin {admin_id} refund: {reason}",
            )

        if recap_sec <= 0 and voice_sec <= 0:
            raise BillingError("No unrefunded charges found on this job")

        await append_wallet_transaction(
            session,
            user=user,
            balance=bal,
            tx_type="REFUND",
            wallet_kind=_wallet_kind_for(recap_sec, voice_sec),
            recap_seconds_delta=recap_sec,
            voice_seconds_delta=voice_sec,
            reference_id=f"ADMINREFUND:{job_id}",
            note=f"Admin {admin_id} refund: {reason}",
        )
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="job.refund",
                target_type="job",
                target_id=job_id,
                details_json=(
                    f'{{"user_id": {job.user_id}, "recap_seconds": {recap_sec}, '
                    f'"voice_seconds": {voice_sec}, "reason": "{reason}"}}'
                ),
            )
        )
        await session.flush()
        return {
            "success": True,
            "job_id": job_id,
            "user_id": job.user_id,
            "refunded_recap_seconds": recap_sec,
            "refunded_voice_seconds": voice_sec,
        }


async def refund_or_release_job(
    session: AsyncSession,
    user_id: int,
    job_id: str,
    *,
    reason: str = "JOB_FAILED",
    note: str | None = None,
) -> dict[str, Any]:
    """Idempotently release a RESERVED hold OR reverse a SETTLED charge on failure/cancel."""
    res = (
        await session.execute(
            select(WalletReservation).where(WalletReservation.job_id == job_id)
        )
    ).scalar_one_or_none()
    if res is None:
        return {"found": False, "refunded_recap_seconds": 0, "refunded_voice_seconds": 0}
    if res.status == "RELEASED":
        return {
            "found": True,
            "already_released": True,
            "refunded_recap_seconds": int(res.refunded_recap_seconds or 0),
            "refunded_voice_seconds": int(res.refunded_voice_seconds or 0),
        }
    if res.status == "RESERVED":
        out = await release_job_reservation(
            session, user_id, job_id, reason=reason, note=note
        )
        return {
            "found": True,
            "already_released": False,
            "refunded_recap_seconds": int(out.get("released_recap_seconds") or 0),
            "refunded_voice_seconds": int(out.get("released_voice_seconds") or 0),
        }

    # Status == SETTLED -> reverse settled charges once (only unrefunded delta)
    async with _user_wallet_lock(user_id):
        res = (
            await session.execute(
                select(WalletReservation)
                .where(WalletReservation.job_id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if res is None or res.status != "SETTLED":
            return {
                "found": bool(res),
                "already_released": True,
                "refunded_recap_seconds": 0,
                "refunded_voice_seconds": 0,
            }

        user = (
            await session.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        bal = await get_minute_balance(session, user_id)

        settled_voice = int(res.settled_voice_seconds or 0)
        settled_recap = int(res.settled_recap_seconds or 0)
        refunded_v = 0
        refunded_r = 0

        if settled_voice > 0:
            from app.services.ledger import net_job_seconds_held, refund_job_seconds

            held = await net_job_seconds_held(session, user_id, job_id)
            if held > 0:
                await refund_job_seconds(
                    session, user_id, job_id, note=note or f"Auto-refund ({reason})"
                )
                refunded_v = held

        if settled_recap > 0 and user is not None:
            from app.services.ledger import recap_debit_total_for_job, recap_refund_total_for_job

            deb_cr = float(await recap_debit_total_for_job(session, job_id))
            ref_cr = float(await recap_refund_total_for_job(session, job_id))
            rem_sec = max(0, min(settled_recap, credits_to_seconds(max(0.0, deb_cr - ref_cr))))
            if rem_sec > 0:
                sync_user_recap_balance(user, get_recap_available_seconds(user) + rem_sec)
                await record_recap_credit_event(
                    session,
                    user_id=user_id,
                    direction=LedgerDirection.CREDIT.value,
                    credits=seconds_to_credits(rem_sec),
                    seconds=rem_sec,
                    reference_id=job_id,
                    note=note or f"Auto-refund ({reason})",
                )
                refunded_r = rem_sec

        res.refunded_recap_seconds = int(res.refunded_recap_seconds or 0) + refunded_r
        res.refunded_voice_seconds = int(res.refunded_voice_seconds or 0) + refunded_v
        res.status = "RELEASED"
        res.updated_at = utc_now()
        await _release_or_refund_grant_lots(
            session,
            user_id,
            job_id,
            user=user,
            bal=bal,
            from_reserved=False,
            note=note or f"Refunded {reason} ({job_id})",
        )

        if user is not None and (refunded_r > 0 or refunded_v > 0):
            await append_wallet_transaction(
                session,
                user=user,
                balance=bal,
                tx_type="REFUND",
                wallet_kind=_wallet_kind_for(refunded_r, refunded_v),
                recap_seconds_delta=refunded_r,
                voice_seconds_delta=refunded_v,
                reference_id=f"REFUND:{job_id}",
                note=note or f"Refunded {reason} ({job_id})",
            )
        await session.flush()
        return {
            "found": True,
            "already_released": False,
            "refunded_recap_seconds": refunded_r,
            "refunded_voice_seconds": refunded_v,
        }


async def record_cost_telemetry(
    session: AsyncSession,
    *,
    job_id: str | None,
    user_id: int | None,
    provider: str,
    model_name: str = "",
    is_own_key: bool = False,
    requests_count: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    audio_seconds: float = 0.0,
    runtime_seconds: float = 0.0,
    bytes_transferred: int = 0,
    cost_basis: str | None = None,
) -> JobCostTelemetry:
    """Persist real measured API / GPU / storage / bandwidth usage telemetry."""
    prov = str(provider or "").lower()
    usd = 0.0
    basis = cost_basis or "estimated"

    if is_own_key:
        usd = 0.0
        basis = "free_tier"
    elif prov == "gemini_text":
        usd = (
            (max(0, int(input_tokens)) / 1_000_000.0) * float(settings.gemini_input_cost_per_1m_usd)
            + (max(0, int(output_tokens)) / 1_000_000.0) * float(settings.gemini_output_cost_per_1m_usd)
        )
        basis = "actual_usage_rate" if (input_tokens or output_tokens) else "estimated"
    elif prov == "gemini_tts":
        usd = (max(0.0, float(audio_seconds)) / 60.0) * float(settings.gemini_tts_cost_per_min_usd)
        basis = "actual_usage_rate" if audio_seconds > 0 else "estimated"
    elif prov in ("runpod", "voxcpm", "gpu"):
        mode = str(getattr(settings, "gpu_provider_mode", "hf_spaces") or "hf_spaces").lower()
        rate_hr = float(
            settings.runpod_gpu_cost_per_hour_usd
            if mode == "runpod"
            else settings.gpu_hourly_cost_usd
        )
        usd = (max(0.0, float(runtime_seconds)) / 3600.0) * max(0.0, rate_hr)
        basis = "actual_usage_rate" if (rate_hr > 0 and runtime_seconds > 0) else "estimated"
    elif prov == "bandwidth":
        gb = max(0, int(bytes_transferred)) / float(1024 ** 3)
        usd = gb * float(settings.bandwidth_cost_per_gb_usd)
        basis = "estimated"
    elif prov == "edge_tts":
        usd = 0.0
        basis = "free_tier"

    row = JobCostTelemetry(
        job_id=job_id,
        user_id=user_id,
        provider=prov,
        model_name=str(model_name or "")[:100],
        is_own_key=bool(is_own_key),
        requests_count=int(requests_count or 0),
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        audio_seconds=float(audio_seconds or 0.0),
        runtime_seconds=float(runtime_seconds or 0.0),
        bytes_transferred=int(bytes_transferred or 0),
        estimated_cost_usd=round(float(usd), 6),
        cost_basis=basis,
    )
    session.add(row)
    return row
