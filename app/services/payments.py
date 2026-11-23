from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, LedgerDirection, PaymentStatus
from app.models.schema import AuditLog, BotSetting, PaymentIntent, PaymentRequest, PricingPlan, User, utc_now
from app.services.ledger import credit_purchased_minutes, get_or_create_user, record_recap_credit_event

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

# Single source of truth for ALL 10 official v3 packages in 3 categories:
#   1. combo  (4 packages: Starter, Creator, Pro, Business — credits BOTH wallets separately)
#   2. voice  (3 packages: 200, 500, 1000 [+200 bonus = 1200 grant minutes])
#   3. recap  (3 packages: 30, 60, 180 — 1 Credit = 1 Min Final Output)
# Database PricingPlan rows win wherever present (admin-editable price/credits/minutes/validity/is_active).
# validity_days=0 means No Expiry (expires_at=None on wallet grant).
PLAN_CATALOGUE = [
    # A. Combo Packages (Credits BOTH wallets separately)
    {"code": "pkg_starter", "name": "Starter", "category": "combo", "sub_type": "combo", "minutes": 100, "recap_credits": 15, "price_mmk": 14900, "validity_days": 30, "is_topup": False, "badge": ""},
    {"code": "pkg_creator", "name": "Creator", "category": "combo", "sub_type": "combo", "minutes": 200, "recap_credits": 30, "price_mmk": 28500, "validity_days": 30, "is_topup": False, "badge": ""},
    {"code": "pkg_pro", "name": "Pro", "category": "combo", "sub_type": "combo", "minutes": 500, "recap_credits": 60, "price_mmk": 54900, "validity_days": 90, "is_topup": False, "badge": ""},
    {"code": "pkg_business", "name": "Business", "category": "combo", "sub_type": "combo", "minutes": 1000, "recap_credits": 180, "price_mmk": 114900, "validity_days": 0, "is_topup": False, "badge": ""},
    # B. Voice Clone Minutes (No Expiry)
    {"code": "voice_200", "name": "200", "category": "voice", "sub_type": "voice", "minutes": 200, "recap_credits": 0, "price_mmk": 19800, "validity_days": 0, "is_topup": False, "badge": ""},
    {"code": "voice_500", "name": "500", "category": "voice", "sub_type": "voice", "minutes": 500, "recap_credits": 0, "price_mmk": 39500, "validity_days": 0, "is_topup": False, "badge": ""},
    {"code": "voice_1000", "name": "1000", "category": "voice", "sub_type": "voice", "minutes": 1200, "display_minutes": 1000, "recap_credits": 0, "price_mmk": 79200, "validity_days": 0, "is_topup": False, "badge": "bonus_200"},
    # C. Recap Credits (1 Recap Credit = 1 Minute Final Recap Output; No Expiry)
    {"code": "recap_30", "name": "30", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 30, "price_mmk": 13500, "validity_days": 0, "is_topup": False, "badge": ""},
    {"code": "recap_60", "name": "60", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 60, "price_mmk": 25500, "validity_days": 0, "is_topup": False, "badge": ""},
    {"code": "recap_180", "name": "180", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 180, "price_mmk": 68400, "validity_days": 0, "is_topup": False, "badge": ""},
]

# Legacy retired package codes kept read-only for in-flight historical requests / compatibility.
LEGACY_PLAN_CATALOGUE = {
    # Pre-v2 legacy codes
    "MIN_10": {"code": "MIN_10", "name": "Legacy 10m", "category": "voice", "sub_type": "voice", "minutes": 10, "recap_credits": 0, "price_mmk": 8000, "validity_days": 30, "is_topup": False, "badge": ""},
    "MIN_30": {"code": "MIN_30", "name": "Legacy 30m", "category": "voice", "sub_type": "voice", "minutes": 30, "recap_credits": 0, "price_mmk": 20000, "validity_days": 30, "is_topup": False, "badge": ""},
    "MIN_60": {"code": "MIN_60", "name": "Legacy 60m", "category": "voice", "sub_type": "voice", "minutes": 60, "recap_credits": 0, "price_mmk": 38000, "validity_days": 30, "is_topup": False, "badge": ""},
    "voice_studio_pro": {"code": "voice_studio_pro", "name": "Legacy Voice Pro", "category": "voice", "sub_type": "voice", "minutes": 250, "recap_credits": 0, "price_mmk": 75000, "validity_days": 30, "is_topup": False, "badge": ""},
    "recap_studio_pro": {"code": "recap_studio_pro", "name": "Legacy Recap Pro", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 25, "price_mmk": 90000, "validity_days": 30, "is_topup": False, "badge": ""},
    "combined_starter": {"code": "combined_starter", "name": "Legacy Combined Starter", "category": "combo", "sub_type": "combo", "minutes": 30, "recap_credits": 5, "price_mmk": 30000, "validity_days": 30, "is_topup": False, "badge": ""},
    "all_in_one_master": {"code": "all_in_one_master", "name": "Legacy Master", "category": "combo", "sub_type": "combo", "minutes": 500, "recap_credits": 60, "price_mmk": 180000, "validity_days": 30, "is_topup": False, "badge": ""},
    # Retired v2 22-package catalogue (read-only for historical orders/grants)
    "voice_mini": {"code": "voice_mini", "name": "Mini", "category": "voice", "sub_type": "voice", "minutes": 10, "recap_credits": 0, "price_mmk": 3000, "validity_days": 30, "is_topup": False, "badge": ""},
    "voice_starter": {"code": "voice_starter", "name": "Starter", "category": "voice", "sub_type": "voice", "minutes": 20, "recap_credits": 0, "price_mmk": 5000, "validity_days": 30, "is_topup": False, "badge": ""},
    "voice_creator": {"code": "voice_creator", "name": "Creator", "category": "voice", "sub_type": "voice", "minutes": 45, "recap_credits": 0, "price_mmk": 10000, "validity_days": 30, "is_topup": False, "badge": "popular"},
    "voice_pro": {"code": "voice_pro", "name": "Pro", "category": "voice", "sub_type": "voice", "minutes": 100, "recap_credits": 0, "price_mmk": 20000, "validity_days": 30, "is_topup": False, "badge": ""},
    "voice_business": {"code": "voice_business", "name": "Business", "category": "voice", "sub_type": "voice", "minutes": 220, "recap_credits": 0, "price_mmk": 40000, "validity_days": 30, "is_topup": False, "badge": ""},
    "recap_mini": {"code": "recap_mini", "name": "Mini", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 15, "price_mmk": 6000, "validity_days": 30, "is_topup": False, "badge": ""},
    "recap_starter": {"code": "recap_starter", "name": "Starter", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 30, "price_mmk": 10000, "validity_days": 30, "is_topup": False, "badge": ""},
    "recap_creator": {"code": "recap_creator", "name": "Creator", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 70, "price_mmk": 20000, "validity_days": 30, "is_topup": False, "badge": "popular"},
    "recap_pro": {"code": "recap_pro", "name": "Pro", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 160, "price_mmk": 40000, "validity_days": 30, "is_topup": False, "badge": ""},
    "recap_business": {"code": "recap_business", "name": "Business", "category": "recap", "sub_type": "recap", "minutes": 0, "recap_credits": 350, "price_mmk": 80000, "validity_days": 30, "is_topup": False, "badge": ""},
    "combo_mini": {"code": "combo_mini", "name": "Mini Combo", "category": "combo", "sub_type": "combo", "minutes": 10, "recap_credits": 15, "price_mmk": 8100, "validity_days": 30, "is_topup": False, "badge": ""},
    "combo_starter": {"code": "combo_starter", "name": "Starter Combo", "category": "combo", "sub_type": "combo", "minutes": 20, "recap_credits": 30, "price_mmk": 13500, "validity_days": 30, "is_topup": False, "badge": ""},
    "combo_creator": {"code": "combo_creator", "name": "Creator Combo", "category": "combo", "sub_type": "combo", "minutes": 45, "recap_credits": 70, "price_mmk": 27000, "validity_days": 30, "is_topup": False, "badge": "best_value"},
    "combo_pro": {"code": "combo_pro", "name": "Pro Combo", "category": "combo", "sub_type": "combo", "minutes": 100, "recap_credits": 160, "price_mmk": 54000, "validity_days": 30, "is_topup": False, "badge": ""},
    "combo_business": {"code": "combo_business", "name": "Business Combo", "category": "combo", "sub_type": "combo", "minutes": 220, "recap_credits": 350, "price_mmk": 108000, "validity_days": 30, "is_topup": False, "badge": ""},
    "topup_voice_10": {"code": "topup_voice_10", "name": "Voice Top-up 10m", "category": "topup", "sub_type": "voice", "minutes": 10, "recap_credits": 0, "price_mmk": 2500, "validity_days": 30, "is_topup": True, "badge": ""},
    "topup_voice_30": {"code": "topup_voice_30", "name": "Voice Top-up 30m", "category": "topup", "sub_type": "voice", "minutes": 30, "recap_credits": 0, "price_mmk": 6000, "validity_days": 30, "is_topup": True, "badge": ""},
    "topup_voice_60": {"code": "topup_voice_60", "name": "Voice Top-up 60m", "category": "topup", "sub_type": "voice", "minutes": 60, "recap_credits": 0, "price_mmk": 11000, "validity_days": 30, "is_topup": True, "badge": ""},
    "topup_recap_5": {"code": "topup_recap_5", "name": "Recap Top-up 5c", "category": "topup", "sub_type": "recap", "minutes": 0, "recap_credits": 5, "price_mmk": 3000, "validity_days": 30, "is_topup": True, "badge": ""},
    "topup_recap_15": {"code": "topup_recap_15", "name": "Recap Top-up 15c", "category": "topup", "sub_type": "recap", "minutes": 0, "recap_credits": 15, "price_mmk": 8000, "validity_days": 30, "is_topup": True, "badge": ""},
    "topup_recap_30": {"code": "topup_recap_30", "name": "Recap Top-up 30c", "category": "topup", "sub_type": "recap", "minutes": 0, "recap_credits": 30, "price_mmk": 14000, "validity_days": 30, "is_topup": True, "badge": ""},
    "topup_recap_60": {"code": "topup_recap_60", "name": "Recap Top-up 60c", "category": "topup", "sub_type": "recap", "minutes": 0, "recap_credits": 60, "price_mmk": 26000, "validity_days": 30, "is_topup": True, "badge": ""},
}


def get_plan_by_code(code: str, *, include_legacy: bool = True) -> dict | None:
    """Return the catalogue entry for a plan code, or None if unknown."""
    for entry in PLAN_CATALOGUE:
        if entry["code"] == code:
            return entry
    if include_legacy:
        return LEGACY_PLAN_CATALOGUE.get(code)
    return None


def package_recap_credits(package_code: str | None) -> int:
    """Recap credits granted by a package code (0 for voice-only packs)."""
    cat = get_plan_by_code(package_code or "")
    return int(cat["recap_credits"]) if cat else 0


DEFAULT_PRICING_PLANS = [
    {
        "code": p["code"],
        "name": p["name"],
        "category": p["category"],
        "sub_type": p["sub_type"],
        "minutes": p["minutes"],
        "display_minutes": int(p["display_minutes"]) if p.get("display_minutes") is not None else int(p["minutes"]),
        "recap_credits": p["recap_credits"],
        "price_mmk": p["price_mmk"],
        "validity_days": int(p["validity_days"]) if p.get("validity_days") is not None else 30,
        "is_topup": p["is_topup"],
        "badge": p["badge"],
        "sort_order": i + 1,
    }
    for i, p in enumerate(PLAN_CATALOGUE)
]
_DEFAULT_BY_CODE = {p["code"]: p for p in DEFAULT_PRICING_PLANS}


async def _ensure_pricing_plans_seeded(session: AsyncSession) -> None:
    """Idempotently seed all 10 official v3 plans and deactivate legacy DB rows safely."""
    existing_rows = list((await session.execute(select(PricingPlan))).scalars().all())
    by_code = {row.code: row for row in existing_rows}
    changed = False

    # 1. Idempotently deactivate retired legacy rows (never delete them; keep historical orders resolvable)
    #    and upgrade any un-named rows safely.
    for row in existing_rows:
        if row.code in LEGACY_PLAN_CATALOGUE:
            leg = LEGACY_PLAN_CATALOGUE[row.code]
            if not getattr(row, "name", ""):
                row.name = leg["name"]
                row.category = leg["category"]
                row.sub_type = leg["sub_type"]
                row.recap_credits = leg["recap_credits"]
                row.sort_order = 900
                changed = True
            if bool(row.is_active):
                row.is_active = False
                changed = True
        elif row.code in _DEFAULT_BY_CODE and not getattr(row, "name", ""):
            spec = _DEFAULT_BY_CODE[row.code]
            row.name = spec["name"]
            row.category = spec["category"]
            row.sub_type = spec["sub_type"]
            row.recap_credits = spec["recap_credits"]
            row.is_topup = spec["is_topup"]
            row.badge = spec["badge"]
            row.sort_order = spec["sort_order"]
            changed = True

    # 2. Fill-only seed for any missing official v3 catalogue plan (never overwrite existing v3 rows)
    for p_data in DEFAULT_PRICING_PLANS:
        if p_data["code"] not in by_code:
            vdays = int(p_data["validity_days"]) if p_data.get("validity_days") is not None else 30
            plan = PricingPlan(
                code=p_data["code"],
                name=p_data["name"],
                category=p_data["category"],
                sub_type=p_data["sub_type"],
                minutes=p_data["minutes"],
                recap_credits=p_data["recap_credits"],
                price_mmk=p_data["price_mmk"],
                validity_days=vdays,
                is_topup=p_data["is_topup"],
                badge=p_data["badge"],
                sort_order=p_data["sort_order"],
                is_active=True,
            )
            session.add(plan)
            changed = True

    if changed:
        await session.flush()


async def get_all_pricing_plans(session: AsyncSession) -> list[PricingPlan]:
    """Retrieve all non-legacy pricing plans (active + disabled) for admin management."""
    await _ensure_pricing_plans_seeded(session)
    stmt = (
        select(PricingPlan)
        .where(~PricingPlan.code.in_(list(LEGACY_PLAN_CATALOGUE.keys())))
        .order_by(PricingPlan.sort_order, PricingPlan.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_active_pricing_plans(session: AsyncSession) -> list[PricingPlan]:
    """Retrieve active official pricing plans, seeding any missing catalogue rows."""
    await _ensure_pricing_plans_seeded(session)
    stmt = (
        select(PricingPlan)
        .where(
            PricingPlan.is_active.is_(True),
            ~PricingPlan.code.in_(list(LEGACY_PLAN_CATALOGUE.keys())),
        )
        .order_by(PricingPlan.sort_order, PricingPlan.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def create_payment_request(
    session: AsyncSession,
    user_id: int,
    package_code: str,
    receipt_file_id: str | None = None,
    slip_fingerprint: str | None = None,
    payment_reference: str | None = None,
) -> PaymentRequest:
    """Create a new pending payment request."""
    if payment_reference:
        dup_ref = (
            await session.execute(
                select(PaymentRequest).where(PaymentRequest.payment_reference == payment_reference)
            )
        ).scalar_one_or_none()
        if dup_ref is not None:
            logger.info("Duplicate payment_reference %s ignored; returning %s", payment_reference, dup_ref.id)
            return dup_ref

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

    # Resolve package details: DB PricingPlan row wins (including edited credits/minutes/price).
    # Inactive catalogue rows cannot be ordered; legacy retired codes fallback cleanly for tests/history.
    stmt = select(PricingPlan).where(PricingPlan.code == package_code)
    res = await session.execute(stmt)
    plan = res.scalar_one_or_none()
    if plan is not None:
        if not plan.is_active and package_code not in LEGACY_PLAN_CATALOGUE:
            raise ValueError(f"Pricing plan code '{package_code}' is currently disabled")
        plan_recap = int(getattr(plan, "recap_credits", 0) or 0)
        if plan_recap == 0:
            cat_meta = get_plan_by_code(plan.code)
            if cat_meta and plan.category == cat_meta["category"] and not getattr(plan, "name", ""):
                plan_recap = int(cat_meta.get("recap_credits", 0) or 0)
    else:
        cat = get_plan_by_code(package_code)
        if not cat:
            raise ValueError(f"Pricing plan code '{package_code}' not found")
        plan_recap = int(cat.get("recap_credits", 0) or 0)
        plan = PricingPlan(
            code=cat["code"],
            name=cat["name"],
            category=cat["category"],
            sub_type=cat.get("sub_type", cat["category"]),
            minutes=cat["minutes"],
            recap_credits=plan_recap,
            price_mmk=cat["price_mmk"],
            validity_days=int(cat["validity_days"]) if cat.get("validity_days") is not None else 30,
            is_topup=bool(cat.get("is_topup", False)),
            badge=cat.get("badge", ""),
            sort_order=0,
            is_active=True,
        )

    pay_id = f"PAY-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:12].upper()}"
    _raw_plan_vdays = getattr(plan, "validity_days", None)
    _plan_vdays = int(_raw_plan_vdays) if _raw_plan_vdays is not None else 30
    payment = PaymentRequest(
        id=pay_id,
        user_id=user_id,
        package_code=plan.code,
        minutes=int(plan.minutes),
        recap_credits=int(plan_recap),
        amount_mmk=int(plan.price_mmk),
        validity_days=_plan_vdays,
        payment_reference=payment_reference,
        receipt_file_id=receipt_file_id,
        slip_fingerprint=fingerprint,
        status=PaymentStatus.PENDING.value,
    )
    session.add(payment)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if payment_reference:
            dup_winner = (await session.execute(
                select(PaymentRequest).where(PaymentRequest.payment_reference == payment_reference)
            )).scalar_one_or_none()
            if dup_winner is not None:
                return dup_winner
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
    logger.info("Created payment request %s for user %s (%s min, %s recap)", pay_id, user_id, plan.minutes, plan_recap)
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

    # Credit purchased voice minutes and/or recap credits separately into their respective wallets.
    if payment.minutes > 0:
        await credit_purchased_minutes(
            session=session,
            user_id=payment.user_id,
            minutes=payment.minutes,
            payment_id=payment.id,
            note=f"Approved by admin {admin_id}: {payment.minutes} minutes ({payment.package_code})",
            record_wallet_tx=False,
        )

    cat = get_plan_by_code(payment.package_code)
    _edited_recap = getattr(payment, "edited_recap_credits", None)
    _snap_recap = getattr(payment, "recap_credits", None)
    if _edited_recap is not None:
        _recap_to_credit = int(_edited_recap)
    elif _snap_recap is not None and int(_snap_recap) > 0:
        _recap_to_credit = int(_snap_recap)
    else:
        _recap_to_credit = int(cat["recap_credits"]) if cat else 0

    from app.services.billing import (
        DEFAULT_PLAN_VALIDITY_DAYS,
        append_wallet_transaction,
        create_wallet_grant,
        get_recap_available_seconds,
        sync_user_recap_balance,
    )
    from app.services.ledger import get_minute_balance

    target_user = (
        await session.execute(
            select(User).where(User.id == payment.user_id).with_for_update()
        )
    ).scalar_one_or_none()
    bal = await get_minute_balance(session, payment.user_id)

    if _recap_to_credit > 0 and target_user is not None:
        new_avail_sec = get_recap_available_seconds(target_user) + (_recap_to_credit * 60)
        sync_user_recap_balance(target_user, new_avail_sec)
        target_user.account_category = AccountCategory.PURCHASED.value
        await record_recap_credit_event(
            session,
            user_id=payment.user_id,
            direction=LedgerDirection.CREDIT.value,
            credits=_recap_to_credit,
            seconds=_recap_to_credit * 60,
            reference_id=str(payment.id),
            note=(
                f"Approved by admin {admin_id}: {_recap_to_credit} "
                f"recap credits ({payment.package_code})"
            ),
        )

    _grant_vdays_raw = getattr(payment, "validity_days", None)
    _grant_vdays = int(_grant_vdays_raw) if _grant_vdays_raw is not None else DEFAULT_PLAN_VALIDITY_DAYS
    _is_topup = bool(cat.get("is_topup", False)) if cat else False
    _grant_source = (
        "TOPUP"
        if _is_topup
        else ("COMBO" if (payment.minutes > 0 and _recap_to_credit > 0) else "PACKAGE")
    )
    _pkg_name = (cat.get("name") if cat else None) or payment.package_code

    if target_user is not None and (payment.minutes > 0 or _recap_to_credit > 0):
        grant = await create_wallet_grant(
            session,
            payment.user_id,
            source_type=_grant_source,
            voice_seconds=int(payment.minutes or 0) * 60,
            recap_seconds=_recap_to_credit * 60,
            validity_days=_grant_vdays if _grant_vdays > 0 else None,
            payment_request_id=str(payment.id),
            package_code=payment.package_code,
            package_name=_pkg_name,
            purchased_at=payment.reviewed_at or utc_now(),
            note=f"Approved by admin {admin_id}: {payment.package_code}",
        )
        _exp_note = f" (valid {_grant_vdays}d until {grant.expires_at.strftime('%Y-%m-%d')})" if grant.expires_at else ""
        _w_kind = (
            "COMBO"
            if (payment.minutes > 0 and _recap_to_credit > 0)
            else ("RECAP" if _recap_to_credit > 0 else "VOICE")
        )
        await append_wallet_transaction(
            session,
            user=target_user,
            balance=bal,
            tx_type="PURCHASE",
            wallet_kind=_w_kind,
            recap_seconds_delta=_recap_to_credit * 60,
            voice_seconds_delta=int(payment.minutes or 0) * 60,
            reference_id=str(payment.id),
            note=f"Purchased {payment.package_code} (+{payment.minutes}m, +{_recap_to_credit} cr){_exp_note}",
        )

    session.add(AuditLog(
        admin_id=admin_id,
        action="payment.approve",
        target_type="payment_request",
        target_id=str(payment.id),
        details_json=json.dumps({
            "user_id": payment.user_id,
            "minutes": payment.minutes,
            "recap_credits": _recap_to_credit,
            "amount_mmk": payment.amount_mmk,
            "package_code": payment.package_code,
        }),
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

    # Batch BC-4: the reject entry previously recorded ONLY the reason —
    # no target user, no amount — so a rejection could not be tied back
    # to the income history. It now carries the same identity fields as
    # the approve entry (admin id is the column; the decision is the
    # action), serialised with json.dumps (a quoted reason used to break
    # the hand-built JSON string outright).
    session.add(AuditLog(
        admin_id=admin_id,
        action="payment.reject",
        target_type="payment_request",
        target_id=str(payment.id),
        details_json=json.dumps({
            "user_id": payment.user_id,
            "minutes": payment.minutes,
            "amount_mmk": payment.amount_mmk,
            "reason": payment.rejection_reason,
        }),
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


PAYMENT_ACCOUNT_PRESETS: list[dict[str, str]] = [
    {"provider": "KBZPay", "account_type": "mobile_wallet", "color": "#0047BA"},
    {"provider": "WavePay", "account_type": "mobile_wallet", "color": "#EAB308"},
    {"provider": "AYA Pay", "account_type": "mobile_wallet", "color": "#D92D20"},
    {"provider": "CB Pay", "account_type": "mobile_wallet", "color": "#0284C7"},
    {"provider": "UAB Pay", "account_type": "mobile_wallet", "color": "#0D9488"},
    {"provider": "KBZ Bank", "account_type": "bank", "color": "#1D4ED8"},
    {"provider": "AYA Bank", "account_type": "bank", "color": "#B91C1C"},
    {"provider": "CB Bank", "account_type": "bank", "color": "#0369A1"},
    {"provider": "Yoma Bank", "account_type": "bank", "color": "#EA580C"},
    {"provider": "UAB Bank", "account_type": "bank", "color": "#0F766E"},
    {"provider": "USDT (TRC20/BEP20)", "account_type": "crypto", "color": "#16A34A"},
]

_SAFE_ACC_ID_RE = None


def _sanitize_account_id(raw_id: str | None, provider: str = "acc") -> str:
    import re as _re
    import uuid as _uuid

    src = str(raw_id or "").strip().lower()
    cleaned = _re.sub(r"[^a-z0-9_-]+", "_", src).strip("_")[:40]
    if cleaned:
        return cleaned
    base = _re.sub(r"[^a-z0-9]+", "_", str(provider or "bank").lower()).strip("_")[:18] or "bank"
    return f"{base}_{_uuid.uuid4().hex[:6]}"


def _normalize_payment_account(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    provider = str(item.get("provider") or "").strip()[:60]
    number = str(item.get("number") if item.get("number") is not None else item.get("account_number") or "").strip()[:80]
    name = str(item.get("name") if item.get("name") is not None else item.get("account_name") or "").strip()[:100]
    if not provider or not number:
        return None
    acc_type = str(item.get("account_type") or "mobile_wallet").strip().lower()
    if acc_type not in ("mobile_wallet", "bank", "crypto", "other"):
        acc_type = "bank"
    color = str(item.get("color") or "#0047BA").strip()
    if not (color.startswith("#") and len(color) in (4, 7)):
        color = "#0047BA"
    acc_id = _sanitize_account_id(item.get("id"), provider)
    try:
        sort_order = int(item.get("sort_order", (idx + 1) * 10))
    except (TypeError, ValueError):
        sort_order = (idx + 1) * 10
    return {
        "id": acc_id,
        "provider": provider,
        "account_type": acc_type,
        "number": number,
        "name": name,
        "bank_branch": str(item.get("bank_branch") or "").strip()[:80],
        "note": str(item.get("note") or "").strip()[:200],
        "color": color,
        "qr_url": str(item.get("qr_url") or "").strip()[:200],
        "is_active": bool(item.get("is_active", True)),
        "sort_order": sort_order,
    }


def _default_payment_accounts_from_legacy(legacy: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    if legacy.get("kpay_number") or legacy.get("kpay_name"):
        out.append({
            "id": "kpay_primary",
            "provider": "KBZPay",
            "account_type": "mobile_wallet",
            "number": legacy.get("kpay_number", ""),
            "name": legacy.get("kpay_name", ""),
            "bank_branch": "",
            "note": "",
            "color": "#0047BA",
            "qr_url": "",
            "is_active": True,
            "sort_order": 10,
        })
    if legacy.get("wave_number") or legacy.get("wave_name"):
        out.append({
            "id": "wave_primary",
            "provider": "WavePay",
            "account_type": "mobile_wallet",
            "number": legacy.get("wave_number", ""),
            "name": legacy.get("wave_name", ""),
            "bank_branch": "",
            "note": "",
            "color": "#FEE600",
            "qr_url": "",
            "is_active": True,
            "sort_order": 20,
        })
    return out


async def list_payment_accounts(
    session: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[dict]:
    """Return ordered list of payment/bank accounts.

    Reads ``payment_accounts_json`` from BotSetting; if absent or empty,
    synthesizes the primary KBZPay & WavePay entries from legacy settings
    so existing deployments work seamlessly without migration.
    """
    legacy = await get_payment_account_info(session)
    row = (
        await session.execute(
            select(BotSetting).where(BotSetting.key == "payment_accounts_json")
        )
    ).scalar_one_or_none()
    accounts: list[dict] = []
    if row and row.value:
        try:
            parsed = json.loads(row.value)
            if isinstance(parsed, list):
                seen: set[str] = set()
                for idx, raw_item in enumerate(parsed):
                    norm = _normalize_payment_account(raw_item, idx)
                    if norm and norm["id"] not in seen:
                        seen.add(norm["id"])
                        accounts.append(norm)
        except Exception as exc:
            logger.warning("Invalid payment_accounts_json; falling back to legacy keys: %s", exc)
    if not accounts:
        accounts = _default_payment_accounts_from_legacy(legacy)
    accounts.sort(key=lambda a: (int(a.get("sort_order", 100)), a.get("id", "")))
    if not include_inactive:
        accounts = [a for a in accounts if a.get("is_active", True)]
    return accounts


async def save_payment_accounts(
    session: AsyncSession,
    accounts: list[dict],
    *,
    admin_id: int = 0,
) -> list[dict]:
    """Persist validated payment/bank accounts & mirror primary KPay/Wave into legacy keys."""
    from app.services.dynamic_settings import set_dynamic_setting

    normalized: list[dict] = []
    seen: set[str] = set()
    for idx, raw_item in enumerate(accounts):
        norm = _normalize_payment_account(raw_item, idx)
        if not norm:
            raise ValueError("Provider name နှင့် Account Number မဖြစ်မနေ ထည့်ရပါမည်။")
        if norm["id"] in seen:
            norm["id"] = _sanitize_account_id(None, norm["provider"])
        seen.add(norm["id"])
        normalized.append(norm)

    if not any(a.get("is_active", True) for a in normalized):
        raise ValueError("အနည်းဆုံး Active ဖြစ်နေသော Payment / Bank Account ၁ ခု ရှိရပါမည်။")

    normalized.sort(key=lambda a: (int(a.get("sort_order", 100)), a.get("id", "")))
    await set_dynamic_setting(
        session,
        "payment_accounts_json",
        json.dumps(normalized, ensure_ascii=False),
        admin_id=admin_id,
    )

    # Mirror primary KBZPay & WavePay into legacy keys for backward compatibility.
    primary_kpay = next(
        (a for a in normalized if a.get("is_active") and ("kbzpay" in a["provider"].lower() or "kpay" in a["provider"].lower() or a["id"] == "kpay_primary")),
        None,
    )
    primary_wave = next(
        (a for a in normalized if a.get("is_active") and ("wave" in a["provider"].lower() or a["id"] == "wave_primary")),
        None,
    )
    if primary_kpay:
        await set_dynamic_setting(session, "payment_kpay_number", primary_kpay["number"], admin_id=admin_id)
        await set_dynamic_setting(session, "payment_kpay_name", primary_kpay["name"], admin_id=admin_id)
    if primary_wave:
        await set_dynamic_setting(session, "payment_wave_number", primary_wave["number"], admin_id=admin_id)
        await set_dynamic_setting(session, "payment_wave_name", primary_wave["name"], admin_id=admin_id)
    return normalized


async def sync_legacy_payment_keys_to_accounts(
    session: AsyncSession,
    *,
    admin_id: int = 0,
) -> None:
    """When Admin updates the legacy 4 kpay/wave inputs in Settings, mirror into payment_accounts_json if present."""
    row = (
        await session.execute(
            select(BotSetting).where(BotSetting.key == "payment_accounts_json")
        )
    ).scalar_one_or_none()
    if not row or not row.value:
        return
    accounts = await list_payment_accounts(session, include_inactive=True)
    legacy = await get_payment_account_info(session)
    changed = False
    for acc in accounts:
        low = acc["provider"].lower()
        if acc["id"] == "kpay_primary" or "kbzpay" in low or "kpay" in low:
            if legacy.get("kpay_number"):
                acc["number"] = legacy["kpay_number"]
                acc["name"] = legacy.get("kpay_name", acc["name"])
                changed = True
                break
    for acc in accounts:
        low = acc["provider"].lower()
        if acc["id"] == "wave_primary" or "wave" in low:
            if legacy.get("wave_number"):
                acc["number"] = legacy["wave_number"]
                acc["name"] = legacy.get("wave_name", acc["name"])
                changed = True
                break
    if changed:
        from app.services.dynamic_settings import set_dynamic_setting

        await set_dynamic_setting(
            session,
            "payment_accounts_json",
            json.dumps(accounts, ensure_ascii=False),
            admin_id=admin_id,
        )


def format_extra_payment_accounts_html(accounts: list[dict], lang: str = "my") -> str:
    """Format additional active Bank/Wallet accounts beyond primary KPay/Wave for Telegram Bot invoice."""
    import html as _html

    extras = []
    seen_primary_kpay = False
    seen_primary_wave = False
    for acc in accounts or []:
        if not acc.get("is_active", True):
            continue
        low = str(acc.get("provider") or "").lower()
        if not seen_primary_kpay and (acc.get("id") == "kpay_primary" or "kbzpay" in low or "kpay" in low):
            seen_primary_kpay = True
            continue
        if not seen_primary_wave and (acc.get("id") == "wave_primary" or "wave" in low):
            seen_primary_wave = True
            continue
        extras.append(acc)
    if not extras:
        return ""
    header = "🏦 <b>အခြား ငွေလွှဲ Bank / Wallet အကောင့်များ</b>" if lang == "my" else "🏦 <b>Other Bank / Wallet Accounts</b>"
    lines = ["", header]
    for acc in extras:
        icon = "🏦" if acc.get("account_type") == "bank" else ("🪙" if acc.get("account_type") == "crypto" else "💳")
        prov = _html.escape(str(acc.get("provider") or ""))
        num = _html.escape(str(acc.get("number") or ""))
        name = _html.escape(str(acc.get("name") or ""))
        branch = _html.escape(str(acc.get("bank_branch") or ""))
        note = _html.escape(str(acc.get("note") or ""))
        line = f"• {icon} <b>{prov}</b>: <code>{num}</code>"
        if name:
            line += f" ({name})"
        if branch:
            line += f" — {branch}"
        if note:
            line += f" <i>[{note}]</i>"
        lines.append(line)
    return "\n".join(lines)


# The one price range every surface must respect (hunt #4 B3): the
# webapp endpoint and the bot typed-input path already enforced
# 100 – 10,000,000 MMK at their edges, but the bot PRESET path called
# the service directly and the service itself accepted anything —
# a crafted preset callback could store a 0 or absurd price.
PRICE_MMK_MIN = 100
PRICE_MMK_MAX = 10_000_000


async def update_pricing_plan_config(
    session: AsyncSession,
    code: str,
    *,
    price_mmk: int | None = None,
    minutes: int | None = None,
    recap_credits: int | None = None,
    validity_days: int | None = None,
    is_active: bool | None = None,
    name: str | None = None,
    badge: str | None = None,
    admin_id: int | None = None,
) -> tuple[bool, PricingPlan | None, str]:
    """Update price, minutes, credits, status, or metadata of a pricing plan."""
    await _ensure_pricing_plans_seeded(session)
    stmt = select(PricingPlan).where(PricingPlan.code == code)
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if not plan:
        return False, None, "PLAN_NOT_FOUND"

    old_price = plan.price_mmk
    changes: dict[str, dict[str, Any]] = {}

    if price_mmk is not None:
        val_p = int(price_mmk)
        if not (PRICE_MMK_MIN <= val_p <= PRICE_MMK_MAX):
            return False, None, "PRICE_OUT_OF_RANGE"
        if plan.price_mmk != val_p:
            changes["price_mmk"] = {"old": plan.price_mmk, "new": val_p}
            plan.price_mmk = val_p

    if minutes is not None:
        val_m = int(minutes)
        if not (0 <= val_m <= 100_000):
            return False, None, "MINUTES_OUT_OF_RANGE"
        if plan.minutes != val_m:
            changes["minutes"] = {"old": plan.minutes, "new": val_m}
            plan.minutes = val_m

    if recap_credits is not None:
        val_c = int(recap_credits)
        if not (0 <= val_c <= 100_000):
            return False, None, "CREDITS_OUT_OF_RANGE"
        old_c = int(getattr(plan, "recap_credits", 0) or 0)
        if old_c != val_c:
            changes["recap_credits"] = {"old": old_c, "new": val_c}
            plan.recap_credits = val_c

    if validity_days is not None:
        val_v = int(validity_days)
        if not (0 <= val_v <= 3650):
            return False, None, "VALIDITY_OUT_OF_RANGE"
        _old_v_raw = getattr(plan, "validity_days", None)
        old_v = int(_old_v_raw) if _old_v_raw is not None else 30
        if old_v != val_v:
            changes["validity_days"] = {"old": old_v, "new": val_v}
            plan.validity_days = val_v

    if is_active is not None:
        val_a = bool(is_active)
        if bool(plan.is_active) != val_a:
            changes["is_active"] = {"old": bool(plan.is_active), "new": val_a}
            plan.is_active = val_a

    if name is not None and name.strip():
        clean_n = name.strip()[:80]
        if getattr(plan, "name", "") != clean_n:
            changes["name"] = {"old": getattr(plan, "name", ""), "new": clean_n}
            plan.name = clean_n

    if badge is not None:
        clean_b = str(badge).strip()[:30]
        if getattr(plan, "badge", "") != clean_b:
            changes["badge"] = {"old": getattr(plan, "badge", ""), "new": clean_b}
            plan.badge = clean_b

    plan.updated_at = utc_now()

    if admin_id is not None:
        details: dict[str, Any] = {"changes": changes}
        if price_mmk is not None:
            details["old_price"] = old_price
            details["new_price"] = int(price_mmk)
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="pricing.update",
                target_type="pricing_plan",
                target_id=code,
                details_json=json.dumps(details),
            )
        )
    await session.flush()
    return True, plan, "PLAN_UPDATED"


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
    plan.price_mmk = int(new_price_mmk)
    plan.updated_at = utc_now()

    if admin_id is not None:
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="pricing.update",
                target_type="pricing_plan",
                target_id=code,
                details_json=json.dumps({"old_price": old_price, "new_price": int(new_price_mmk)}),
            )
        )
    await session.flush()
    return True, plan, "PRICE_UPDATED"

