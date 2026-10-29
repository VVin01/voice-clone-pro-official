from __future__ import annotations

import logging
import uuid
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PaymentStatus
from app.models.schema import AuditLog, BotSetting, PaymentRequest, PricingPlan, User, utc_now
from app.services.ledger import credit_purchased_minutes, get_or_create_user

logger = logging.getLogger(__name__)

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
) -> PaymentRequest:
    """Create a new pending payment request."""
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

    pay_id = f"PAY-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    payment = PaymentRequest(
        id=pay_id,
        user_id=user_id,
        package_code=plan.code,
        minutes=plan.minutes,
        amount_mmk=plan.price_mmk,
        receipt_file_id=receipt_file_id,
        status=PaymentStatus.PENDING.value,
    )
    session.add(payment)
    await session.flush()
    logger.info("Created payment request %s for user %s (%s min)", pay_id, user_id, plan.minutes)
    return payment


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

    # Mark approved transactionally
    payment.status = PaymentStatus.APPROVED.value
    payment.reviewed_by = admin_id
    payment.reviewed_at = utc_now()

    # Credit purchased minutes
    await credit_purchased_minutes(
        session=session,
        user_id=payment.user_id,
        minutes=payment.minutes,
        payment_id=payment.id,
        note=f"Approved by admin {admin_id}: {payment.minutes} minutes ({payment.package_code})",
    )

    # Credit recap credits for recap/combined plans (bot approval path).
    # The Mini App approval path in app/webapp/api.py uses the same
    # catalogue, so both paths credit the same amount for the same code.
    cat = get_plan_by_code(payment.package_code)
    if cat and cat["recap_credits"]:
        target = await session.get(User, payment.user_id)
        if target is not None:
            target.recap_credits = (getattr(target, "recap_credits", 0) or 0) + cat["recap_credits"]

    # Record audit log
    session.add(AuditLog(
        admin_id=admin_id,
        action="payment.approve",
        target_type="payment_request",
        target_id=str(payment.id),
        details_json=f'{{"user_id": {payment.user_id}, "minutes": {payment.minutes}, "amount_mmk": {payment.amount_mmk}}}',
    ))

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

    payment.status = PaymentStatus.REJECTED.value
    payment.reviewed_by = admin_id
    payment.reviewed_at = utc_now()
    payment.rejection_reason = reason or "Payment evidence unverified"

    session.add(AuditLog(
        admin_id=admin_id,
        action="payment.reject",
        target_type="payment_request",
        target_id=str(payment.id),
        details_json=f'{{"reason": "{payment.rejection_reason}"}}',
    ))

    await session.flush()
    logger.info("Payment %s rejected by admin %s", payment.id, admin_id)
    return True, payment, "REJECTED"


async def get_payment_account_info(session: AsyncSession) -> dict[str, str]:
    """Retrieve active payment account numbers and names."""
    rows = (await session.execute(select(BotSetting).where(BotSetting.key.like("payment_%")))).scalars().all()
    d = {r.key: r.value for r in rows}
    return {
        "kpay_number": d.get("payment_kpay_number", ""),
        "kpay_name": d.get("payment_kpay_name", ""),
        "wave_number": d.get("payment_wave_number", ""),
        "wave_name": d.get("payment_wave_name", ""),
    }


async def update_pricing_plan_price(
    session: AsyncSession,
    code: str,
    new_price_mmk: int,
    admin_id: int | None = None,
) -> tuple[bool, PricingPlan | None, str]:
    """Update price of an existing pricing plan."""
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

