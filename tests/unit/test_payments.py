import pytest
from sqlalchemy import select

from app.models.enums import AccountCategory, PaymentStatus
from app.models.schema import AuditLog, PaymentRequest
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.payments import (
    approve_payment_request,
    create_payment_request,
    get_active_pricing_plans,
    reject_payment_request,
)


@pytest.mark.asyncio
async def test_pricing_plans_and_payment_lifecycle(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7001)
    assert user.account_category == AccountCategory.FREE.value

    # 1. Fetch active pricing plans (seeds all 22 official defaults)
    plans = await get_active_pricing_plans(db_session)
    assert len(plans) == 22
    codes = {p.code for p in plans}
    for req_code in (
        "voice_mini", "voice_starter", "voice_creator", "voice_pro", "voice_business",
        "recap_mini", "recap_starter", "recap_creator", "recap_pro", "recap_business",
        "combo_mini", "combo_starter", "combo_creator", "combo_pro", "combo_business",
        "topup_voice_10", "topup_voice_30", "topup_voice_60",
        "topup_recap_5", "topup_recap_15", "topup_recap_30", "topup_recap_60",
    ):
        assert req_code in codes

    # 2. User creates payment request for 10 minutes (Voice Mini = 3,000 MMK)
    payment = await create_payment_request(
        session=db_session,
        user_id=user.id,
        package_code="voice_mini",
        receipt_file_id="photo_file_123",
    )
    assert payment.status == PaymentStatus.PENDING.value
    assert payment.minutes == 10
    assert payment.amount_mmk == 3000
    assert payment.id.startswith("PAY-")

    # 3. Admin approves payment request
    ok, approved_pay, msg = await approve_payment_request(
        session=db_session,
        payment_id=payment.id,
        admin_id=999,
    )
    assert ok is True
    assert msg == "APPROVED"
    assert approved_pay.status == PaymentStatus.APPROVED.value
    assert approved_pay.reviewed_by == 999

    # Verify purchased minutes credited
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 600  # 10 * 60s
    assert bal.purchased_minutes == 10.0
    assert user.account_category == AccountCategory.PURCHASED.value

    # Verify audit log recorded
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == payment.id)
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "payment.approve"

    # 4. Duplicate approval attempt fails and never double-credits!
    dup_ok, _, dup_msg = await approve_payment_request(
        session=db_session,
        payment_id=payment.id,
        admin_id=999,
    )
    assert dup_ok is False
    assert dup_msg == "ALREADY_APPROVED"
    assert bal.purchased_seconds == 600  # Unchanged!


@pytest.mark.asyncio
async def test_payment_rejection(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7002)
    plans = await get_active_pricing_plans(db_session)

    payment = await create_payment_request(
        session=db_session,
        user_id=user.id,
        package_code="voice_starter",
        receipt_file_id="invalid_slip",
    )

    # Admin rejects payment
    ok, rejected_pay, msg = await reject_payment_request(
        session=db_session,
        payment_id=payment.id,
        admin_id=999,
        reason="Invalid transfer screenshot",
    )
    assert ok is True
    assert msg == "REJECTED"
    assert rejected_pay.status == PaymentStatus.REJECTED.value
    assert rejected_pay.rejection_reason == "Invalid transfer screenshot"

    # User remains FREE and balance unchanged
    bal = await get_minute_balance(db_session, user.id)
    assert bal.purchased_seconds == 0
    assert user.account_category == AccountCategory.FREE.value

    # Cannot approve rejected payment
    app_ok, _, app_msg = await approve_payment_request(
        session=db_session,
        payment_id=payment.id,
        admin_id=999,
    )
    assert app_ok is False
    assert app_msg == "ALREADY_REJECTED"
