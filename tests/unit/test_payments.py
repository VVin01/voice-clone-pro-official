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

    # 1. Fetch active pricing plans (seeds all 10 official v3 defaults)
    plans = await get_active_pricing_plans(db_session)
    assert len(plans) == 10
    codes = {p.code for p in plans}
    for req_code in (
        "pkg_starter", "pkg_creator", "pkg_pro", "pkg_business",
        "voice_200", "voice_500", "voice_1000",
        "recap_30", "recap_60", "recap_180",
    ):
        assert req_code in codes

    # 2. User creates payment request for Voice 200 (200 minutes = 19,800 MMK)
    payment = await create_payment_request(
        session=db_session,
        user_id=user.id,
        package_code="voice_200",
        receipt_file_id="photo_file_123",
    )
    assert payment.status == PaymentStatus.PENDING.value
    assert payment.minutes == 200
    assert payment.amount_mmk == 19800
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
    assert bal.purchased_seconds == 12000  # 200 * 60s
    assert bal.purchased_minutes == 200.0
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
    assert bal.purchased_seconds == 12000  # Unchanged!


@pytest.mark.asyncio
async def test_payment_rejection(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=7002)
    plans = await get_active_pricing_plans(db_session)

    payment = await create_payment_request(
        session=db_session,
        user_id=user.id,
        package_code="pkg_starter",
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
