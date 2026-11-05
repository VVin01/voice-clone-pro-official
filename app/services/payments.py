from __future__ import annotations

import logging
import uuid
from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, PaymentStatus
from app.models.schema import AuditLog, BotSetting, PaymentIntent, PaymentRequest, PricingPlan, User, utc_now
from app.services.ledger import credit_purchased_minutes, get_or_create_user

logger = logging.getLogger(__name__)


class SlipAlreadyUsedError(ValueError):
    """Batch AJ (H1): this payment slip was already submitted and is
    live (PENDING under another account) or already APPROVED — one
    bank slip buys exactly one credit, globally. Subclasses ValueError
    so the pre-existing error handling in the slip handlers (which
    treat ValueError as "this submission cannot proceed") keeps
    working; handlers catch this type specifically to show the
    dedicated already-used message."""


# ── Edit-and-approve claim TTL ───────────────────────────────────────
# Moved here from payment_review (Batch AJ, M3): the claim must be
# enforced inside approve/reject_payment_request themselves, and
# payment_review already imports this module — keeping the TTL and
# the staleness rule here avoids a circular import. payment_review
# re-exports both names, so existing import sites are unchanged.

from datetime import timedelta, timezone

# An edit-and-approve claim older than this is STALE: the claiming
# admin has presumably walked away, so the claim stops blocking the
# other admins (the same lazy-expiry idea as the group-AI escalations'
# 24h expiry). Live claims (≤ TTL) keep the exact batch-I behaviour —
# this weakens nothing for an admin who is actively editing.
PAYMENT_REVIEW_CLAIM_TTL = timedelta(minutes=60)


def _claim_as_utc(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to aware UTC (naive reads as UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def payment_claim_is_stale(
    payment: PaymentRequest, *, now: datetime | None = None
) -> bool:
    """True when the row carries a claim older than the TTL.

    Unclaimed rows are never stale, and neither is a claim with no
    timestamp (pre-TTL legacy rows): only a positively-aged claim
    expires.
    """
    if getattr(payment, "review_claimed_by", None) is None:
        return False
    claimed_at = _claim_as_utc(getattr(payment, "review_claimed_at", None))
    if claimed_at is None:
        return False
    moment = _claim_as_utc(now) if now is not None else utc_now()
    return moment - claimed_at > PAYMENT_REVIEW_CLAIM_TTL


def _claimed_by_other(payment: PaymentRequest, admin_id: int) -> bool:
    """Batch AJ (M3): True when a LIVE claim on this request belongs
    to a different admin. Enforced by approve/reject themselves, so
    every surface (bot handlers, Mini App endpoints, future callers)
    gets the same protection the bot UI always had."""
    claimed_by = getattr(payment, "review_claimed_by", None)
    if claimed_by is None or claimed_by == admin_id:
        return False
    return not payment_claim_is_stale(payment)

# Single source of truth for ALL paid plans, in three separate categories.
# Voice-only: minute packs + Voice Pro. Recap-only: Recap Starter + Recap
# Pro. Combined Voice+Recap: Combined Starter + Master. Free tier is
# separate from all three paid categories and is NOT in this catalogue.
# Per-unit prices fall as tiers rise within each category:
#   voice: 800 -> 667 -> 633 -> 300 MMK/min
#   recap: 4,000 -> 3,600 MMK/credit
#   combined: Starter is 30,000 vs 40,000 bought separately (25% off);
#   Master is 180,000 vs 366,000 at bundle rates (voice 500x300 +
#   recap 60x3,600), so the bundle discount deepens with size.
# API /vip/plans, Mini App buttons and the bot buy menu all read this
# catalogue (directly or through the helpers below) — do not change a
# price in only one place.
PLAN_CATALOGUE = [
    {"code": "MIN_10", "category": "voice", "minutes": 10, "recap_credits": 0, "price_mmk": 8000},
    {"code": "MIN_30", "category": "voice", "minutes": 30, "recap_credits": 0, "price_mmk": 20000},
    {"code": "MIN_60", "category": "voice", "minutes": 60, "recap_credits": 0, "price_mmk": 38000},
    {"code": "voice_studio_pro", "category": "voice", "minutes": 250, "recap_credits": 0, "price_mmk": 75000},
    {"code": "recap_starter", "category": "recap", "minutes": 0, "recap_credits": 5, "price_mmk": 20000},
    {"code": "recap_studio_pro", "category": "recap", "minutes": 0, "recap_credits": 25, "price_mmk": 90000},
    {"code": "combined_starter", "category": "combined", "minutes": 30, "recap_credits": 5, "price_mmk": 30000},
    {"code": "all_in_one_master", "category": "combined", "minutes": 500, "recap_credits": 60, "price_mmk": 180000},
]


def get_plan_by_code(code: str) -> dict | None:
    """Return the catalogue entry for a plan code, or None if unknown."""
    for entry in PLAN_CATALOGUE:
        if entry["code"] == code:
            return entry
    return None


def package_recap_credits(package_code: str | None) -> int:
    """Recap credits granted by a package code (0 for voice-only packs).

    PaymentRequest rows store minutes only, so every payment message
    that states what the buyer received must resolve the credit side
    from the catalogue — printing minutes alone told recap-pack buyers
    "0 minutes credited" for a 5-credit purchase (R40 item 2).
    """
    cat = get_plan_by_code(package_code or "")
    return int(cat["recap_credits"]) if cat else 0


DEFAULT_PRICING_PLANS = [
    {"code": p["code"], "minutes": p["minutes"], "price_mmk": p["price_mmk"], "sort_order": i + 1}
    for i, p in enumerate([e for e in PLAN_CATALOGUE if e["code"].startswith("MIN_")])
]


async def get_active_pricing_plans(session: AsyncSession) -> list[PricingPlan]:
    """Retrieve active pricing plans, seeding defaults if none exist."""
    stmt = select(PricingPlan).where(PricingPlan.is_active.is_(True)).order_by(PricingPlan.sort_order)
    res = await session.execute(stmt)
    plans = list(res.scalars().all())

    if not plans:
        # Seed defaults
        for p_data in DEFAULT_PRICING_PLANS:
            plan = PricingPlan(
                code=p_data["code"],
                minutes=p_data["minutes"],
                price_mmk=p_data["price_mmk"],
                sort_order=p_data["sort_order"],
                is_active=True,
            )
            session.add(plan)
        await session.flush()
        plans = (await session.execute(stmt)).scalars().all()

    return list(plans)


async def create_payment_request(
    session: AsyncSession,
    user_id: int,
    package_code: str,
    receipt_file_id: str | None = None,
    slip_fingerprint: str | None = None,
) -> PaymentRequest:
    """Create a new pending payment request."""
    # Duplicate-slip guard (hunt #7): the SAME receipt evidence sent
    # twice (a double-tap, a Telegram resend, or the after-restart
    # resume racing the live FSM handler) used to create two PENDING
    # requests for one real payment — approve both and the user is
    # credited twice. Telegram file_ids are stable per photo/document,
    # so an identical file_id already PENDING for this user means
    # this submission is the same slip: return the existing request
    # instead of minting a second one.
    if receipt_file_id:
        existing = (await session.execute(
            select(PaymentRequest).where(
                PaymentRequest.user_id == user_id,
                PaymentRequest.receipt_file_id == receipt_file_id,
                PaymentRequest.status == PaymentStatus.PENDING.value,
            )
        )).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "Duplicate slip submission by user %s ignored; existing request %s still pending",
                user_id, existing.id,
            )
            return existing
    # Batch AJ (H1): one bank slip = one credit, GLOBALLY. The hunt-#7
    # guard above only covers the same user while PENDING — but the
    # slip file is deleted at approval and APPROVED rows were never
    # matched, so re-sending the same slip after approval (or sending
    # it from a second account) minted a second credit for one
    # transfer. The durable fingerprint closes both: bot submissions
    # fingerprint as tg:<file_id> (stable per photo for this bot —
    # the same photo re-sent or forwarded IS the same slip), Mini App
    # uploads as sha256:<image bytes>. Any fingerprint already carried
    # by a PENDING request of another user, or by ANY APPROVED
    # request, refuses the submission. REJECTED rows do not block: an
    # admin already judged that attempt, and the legitimate owner may
    # resubmit (a later approval then blocks everyone, as it should).
    fingerprint = slip_fingerprint or (f"tg:{receipt_file_id}" if receipt_file_id else None)
    if fingerprint:
        clash = (await session.execute(
            select(PaymentRequest).where(
                PaymentRequest.slip_fingerprint == fingerprint,
                PaymentRequest.status != PaymentStatus.REJECTED.value,
            )
        )).scalars().all()
        for prior in clash:
            if prior.status == PaymentStatus.APPROVED.value:
                raise SlipAlreadyUsedError(
                    "This payment slip has already been used for an approved payment."
                )
            if prior.user_id != user_id and prior.status == PaymentStatus.PENDING.value:
                raise SlipAlreadyUsedError(
                    "This payment slip is already under review from another submission."
                )
            if prior.user_id == user_id and prior.status == PaymentStatus.PENDING.value:
                logger.info(
                    "Duplicate slip submission by user %s ignored; existing request %s still pending",
                    user_id, prior.id,
                )
                return prior
    # Resolve package details: DB minute packs first, then the plan
    # catalogue (bundles/recap/combined codes are not DB PricingPlan rows,
    # but they are sellable through the bot buy menu and Mini App).
    stmt = select(PricingPlan).where(PricingPlan.code == package_code)
    res = await session.execute(stmt)
    plan = res.scalar_one_or_none()
    if not plan:
        cat = get_plan_by_code(package_code)
        if not cat:
            raise ValueError(f"Pricing plan code '{package_code}' not found")
        plan = PricingPlan(
            code=cat["code"],
            minutes=cat["minutes"],
            price_mmk=cat["price_mmk"],
            sort_order=0,
            is_active=True,
        )

    # 12 hex chars (48 bits) instead of 6: the 6-char space (~16.7M/day)
    # made birthday collisions realistic at a few thousand slips/day,
    # and a collision 500s the submission on IntegrityError (hunt F13).
    pay_id = f"PAY-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:12].upper()}"
    payment = PaymentRequest(
        id=pay_id,
        user_id=user_id,
        package_code=plan.code,
        minutes=plan.minutes,
        amount_mmk=plan.price_mmk,
        receipt_file_id=receipt_file_id,
        slip_fingerprint=fingerprint,
        status=PaymentStatus.PENDING.value,
    )
    session.add(payment)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Race backstop (Batch AJ, H1): the defensive partial unique
        # index fired — a concurrent submission with the same slip
        # fingerprint committed first. Translate to the domain error
        # when the winner is a live/approved row; re-raise otherwise.
        await session.rollback()
        if fingerprint:
            winner = (await session.execute(
                select(PaymentRequest).where(
                    PaymentRequest.slip_fingerprint == fingerprint,
                    PaymentRequest.status != PaymentStatus.REJECTED.value,
                )
            )).scalars().first()
            if winner is not None:
                if winner.user_id == user_id and winner.status == PaymentStatus.PENDING.value:
                    return winner
                raise SlipAlreadyUsedError(
                    "This payment slip has already been submitted."
                ) from exc
        raise
    logger.info("Created payment request %s for user %s (%s min)", pay_id, user_id, plan.minutes)
    return payment


# ── Payment intents: the buy-flow selection, persisted (hunt B2) ─────

async def save_payment_intent(session: AsyncSession, user_id: int, package_code: str) -> None:
    """Record (or overwrite) the user's selected package at tap time,
    so the selection survives a bot restart before the slip arrives."""
    await session.merge(
        PaymentIntent(user_id=user_id, package_code=package_code, chosen_at=utc_now())
    )
    await session.flush()


async def get_recent_payment_intent(
    session: AsyncSession, user_id: int, max_age_hours: int = 24
) -> PaymentIntent | None:
    """The user's persisted selection when it is fresh enough to
    resume a slip flow from; stale intents are cleared and ignored."""
    intent = await session.get(PaymentIntent, user_id)
    if intent is None:
        return None
    chosen_at = intent.chosen_at
    if chosen_at is None:
        return None
    if chosen_at.tzinfo is None:  # SQLite hands back naive datetimes
        from datetime import timezone

        chosen_at = chosen_at.replace(tzinfo=timezone.utc)
    from datetime import timedelta

    if utc_now() - chosen_at > timedelta(hours=max_age_hours):
        await clear_payment_intent(session, user_id)
        return None
    return intent


async def clear_payment_intent(session: AsyncSession, user_id: int) -> None:
    """Drop the persisted selection (slip created / flow abandoned)."""
    intent = await session.get(PaymentIntent, user_id)
    if intent is not None:
        await session.delete(intent)
        await session.flush()


def _delete_slip_file(payment: PaymentRequest) -> None:
    """Delete the locally stored slip image at a terminal transition
    (hunt L1). Slip images are bank screenshots (PII) and previously
    accumulated on disk forever. The PaymentRequest row and the
    AuditLog entry keep the full record — only the image goes.
    Bot-flow requests store a Telegram file_id instead of a local
    file (slip_url is None), so there is nothing to delete for them.
    """
    slip_url = payment.slip_url or ""
    if not slip_url.startswith("/api/receipts/"):
        return
    try:
        from pathlib import Path

        from app.config import settings

        path = settings.storage_root / "receipts" / Path(slip_url).name
        if path.is_file():
            path.unlink()
    except Exception as exc:  # never fail the review over cleanup
        logger.warning("Could not delete slip file for payment %s: %s", payment.id, exc)


async def approve_payment_request(
    session: AsyncSession,
    payment_id: str,
    admin_id: int,
) -> tuple[bool, PaymentRequest | None, str]:
    """Approve payment request and credit purchased minutes idempotently.

    Prevents duplicate approval and duplicate crediting via row locks and state check.
    """
    stmt = select(PaymentRequest).where(PaymentRequest.id == payment_id).with_for_update()
    res = await session.execute(stmt)
    payment = res.scalar_one_or_none()

    if not payment:
        return False, None, "PAYMENT_NOT_FOUND"

    if payment.status != PaymentStatus.PENDING.value:
        return False, payment, f"ALREADY_{payment.status}"

    # Batch AJ (M3): the edit-and-approve claim is enforced HERE, in
    # the service — previously only the bot handlers checked it, so
    # the Mini App approve endpoint could approve straight over
    # another admin's in-progress edit. A live claim held by a
    # different admin blocks the decision; the claim holder and stale
    # claims proceed exactly as before.
    if _claimed_by_other(payment, admin_id):
        return False, payment, "CLAIMED_BY_OTHER"

    # Mark approved transactionally
    payment.status = PaymentStatus.APPROVED.value
    payment.reviewed_by = admin_id
    payment.reviewed_at = utc_now()
    # Terminal transition releases any edit-and-approve claim in the
    # same transaction, whichever surface approved (bot / Mini App).
    payment.review_claimed_by = None
    payment.review_claimed_at = None

    # Credit purchased minutes. Recap-only plans carry minutes == 0 and
    # credit_purchased_minutes() rejects non-positive amounts, so it is
    # only called when the plan actually bears minutes — recap-only
    # approvals are credited by the recap block below instead.
    if payment.minutes > 0:
        await credit_purchased_minutes(
            session=session,
            user_id=payment.user_id,
            minutes=payment.minutes,
            payment_id=payment.id,
            note=f"Approved by admin {admin_id}: {payment.minutes} minutes ({payment.package_code})",
        )

    # Credit recap credits for recap/combined plans. This is the single
    # crediting path: the bot approval handler and the Mini App approval
    # endpoint both call this service, so both credit the same amounts
    # for the same package code.
    # Batch AJ (M4): an edit-and-approve correction of the recap side
    # (edited_recap_credits, recorded by apply_edited_approval) wins
    # over the catalogue amount, exactly like the corrected minutes
    # above win over the plan minutes.
    cat = get_plan_by_code(payment.package_code)
    _edited_recap = getattr(payment, "edited_recap_credits", None)
    _recap_to_credit = (
        int(_edited_recap)
        if _edited_recap is not None
        else (cat["recap_credits"] if cat else 0)
    )
    if _recap_to_credit:
        # Atomic increment (hunt F5): a conditional UPDATE instead of
        # read-modify-write, so a concurrent charge/refund on the same
        # user can no longer be silently overwritten by this approval.
        # A recap-only approval must also upgrade the account, the same
        # way credit_purchased_minutes() does for minute plans.
        result = await session.execute(
            update(User)
            .where(User.id == payment.user_id)
            .values(
                recap_credits=User.recap_credits + _recap_to_credit,
                account_category=AccountCategory.PURCHASED.value,
            )
        )
        if result.rowcount:
            target = await session.get(User, payment.user_id)
            if target is not None:
                # Keep the in-session copy consistent with the UPDATE.
                await session.refresh(target)

    # Record audit log
    session.add(AuditLog(
        admin_id=admin_id,
        action="payment.approve",
        target_type="payment_request",
        target_id=str(payment.id),
        details_json=f'{{"user_id": {payment.user_id}, "minutes": {payment.minutes}, "amount_mmk": {payment.amount_mmk}}}',
    ))

    _delete_slip_file(payment)
    await session.flush()
    logger.info("Payment %s approved by admin %s (credited %s min to user %s)", payment.id, admin_id, payment.minutes, payment.user_id)
    return True, payment, "APPROVED"


async def reject_payment_request(
    session: AsyncSession,
    payment_id: str,
    admin_id: int,
    reason: str | None = None,
) -> tuple[bool, PaymentRequest | None, str]:
    """Reject payment request."""
    stmt = select(PaymentRequest).where(PaymentRequest.id == payment_id).with_for_update()
    res = await session.execute(stmt)
    payment = res.scalar_one_or_none()

    if not payment:
        return False, None, "PAYMENT_NOT_FOUND"

    if payment.status != PaymentStatus.PENDING.value:
        return False, payment, f"ALREADY_{payment.status}"

    # Batch AJ (M3): same claim enforcement as approve (above).
    if _claimed_by_other(payment, admin_id):
        return False, payment, "CLAIMED_BY_OTHER"

    payment.status = PaymentStatus.REJECTED.value
    payment.reviewed_by = admin_id
    payment.reviewed_at = utc_now()
    payment.rejection_reason = reason or "Payment evidence unverified"
    # Terminal transition releases any edit-and-approve claim (above).
    payment.review_claimed_by = None
    payment.review_claimed_at = None

    session.add(AuditLog(
        admin_id=admin_id,
        action="payment.reject",
        target_type="payment_request",
        target_id=str(payment.id),
        details_json=f'{{"reason": "{payment.rejection_reason}"}}',
    ))

    _delete_slip_file(payment)
    await session.flush()
    logger.info("Payment %s rejected by admin %s", payment.id, admin_id)
    return True, payment, "REJECTED"


async def get_payment_account_info(session: AsyncSession) -> dict[str, str]:
    """Retrieve active payment account numbers and names.

    The bot_settings rows (written from the admin surfaces) win, field
    by field; any field they leave missing or empty falls back to the
    env-backed settings values — the same values the Mini App plans
    screen displays — so a bot invoice can never show a blank account
    number while the Mini App shows the real one (hunt #3 A1).
    """
    rows = (await session.execute(select(BotSetting).where(BotSetting.key.like("payment_%")))).scalars().all()
    d = {r.key: r.value for r in rows}

    def _pick(key: str) -> str:
        return d.get(key) or getattr(settings, key, "") or ""

    return {
        "kpay_number": _pick("payment_kpay_number"),
        "kpay_name": _pick("payment_kpay_name"),
        "wave_number": _pick("payment_wave_number"),
        "wave_name": _pick("payment_wave_name"),
    }


# The one price range every surface must respect (hunt #4 B3): the
# webapp endpoint and the bot typed-input path already enforced
# 100 – 10,000,000 MMK at their edges, but the bot PRESET path called
# the service directly and the service itself accepted anything —
# a crafted preset callback could store a 0 or absurd price.
PRICE_MMK_MIN = 100
PRICE_MMK_MAX = 10_000_000


async def update_pricing_plan_price(
    session: AsyncSession,
    code: str,
    new_price_mmk: int,
    admin_id: int | None = None,
) -> tuple[bool, PricingPlan | None, str]:
    """Update price of an existing pricing plan."""
    if not (PRICE_MMK_MIN <= int(new_price_mmk) <= PRICE_MMK_MAX):
        return False, None, "PRICE_OUT_OF_RANGE"
    stmt = select(PricingPlan).where(PricingPlan.code == code)
    res = await session.execute(stmt)
    plan = res.scalar_one_or_none()
    if not plan:
        return False, None, "PLAN_NOT_FOUND"

    old_price = plan.price_mmk
    plan.price_mmk = new_price_mmk
    plan.updated_at = utc_now()

    if admin_id is not None:
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="pricing.update",
                target_type="pricing_plan",
                target_id=code,
                details_json=f'{{"old_price": {old_price}, "new_price": {new_price_mmk}}}',
            )
        )
    await session.flush()
    return True, plan, "PRICE_UPDATED"

