from contextlib import asynccontextmanager
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
# Bot cleanup 2026-11-03: this file's adjust / user-search /
# user-toggle / queue / model-detail tests retired with their chat
# screens. The payment-receipt test stays — that handler is part of
# the kept payment-approval path.
from app.handlers.admin import handle_admin_pay_receipt
from app.models.enums import AccountCategory, JobStatus, PaymentStatus, UserStatus
from app.models.schema import (
    AdjustmentIdempotencyRecord,
    AuditLog,
    Job,
    MinuteLedgerEvent,
    PaymentRequest,
    User,
    VoiceModel,
)
from app.services.ledger import get_minute_balance, get_or_create_user


class FakeFSMContext:
    """In-memory replica of aiogram FSMContext."""

    def __init__(self, initial_state=None, initial_data=None):
        self.current_state = initial_state
        self.data = initial_data or {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}


def create_real_frozen_callback(user_id: int, data: str):
    """Instantiate a real frozen aiogram CallbackQuery with mocked methods."""
    user = TgUser(id=user_id, is_bot=False, first_name="BossAdmin")
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    msg.answer = AsyncMock()
    msg.answer_photo = AsyncMock()

    cb = CallbackQuery(
        id="cb_test_id",
        from_user=user,
        chat_instance="chat_instance_test",
        message=msg,
        data=data,
    )
    cb.as_(AsyncMock())
    object.__setattr__(cb, "answer", AsyncMock())
    return cb


# ---------------------------------------------------------------------------
# Phase 2: Frozen Callback Fix Regression Tests
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Phase 3: Functional Admin Operations Tests
# ---------------------------------------------------------------------------








@pytest.mark.asyncio
async def test_admin_pay_receipt_view(db_session):
    """Admin can view payment receipt slip file."""
    settings.admin_ids_raw = "888111"

    admin_user = User(id=888111, username="admin_boss", language_code="en", status=UserStatus.ACTIVE.value)
    target = User(id=77001, username="payer_alice")
    db_session.add_all([admin_user, target])
    await db_session.flush()

    # Payment with receipt
    p1 = PaymentRequest(
        id="PAY-SLIP-101",
        user_id=77001,
        package_code="MIN_30",
        minutes=30,
        amount_mmk=5000,
        receipt_file_id="AGBAA-TELEGRAM-FILE-ID-SLIP-123",
        status=PaymentStatus.PENDING.value,
    )
    # Payment without receipt
    p2 = PaymentRequest(
        id="PAY-NO-SLIP-102",
        user_id=77001,
        package_code="MIN_10",
        minutes=10,
        amount_mmk=2000,
        receipt_file_id=None,
        status=PaymentStatus.PENDING.value,
    )
    db_session.add_all([p1, p2])
    await db_session.flush()

    # 1. View receipt slip
    cb_with = create_real_frozen_callback(user_id=888111, data=f"admin:pay_receipt:{p1.id}")
    await handle_admin_pay_receipt(cb_with, session=db_session)

    cb_with.message.answer_photo.assert_called_once()
    assert cb_with.message.answer_photo.call_args[1]["photo"] == "AGBAA-TELEGRAM-FILE-ID-SLIP-123"

    # 2. Payment without receipt
    cb_without = create_real_frozen_callback(user_id=888111, data=f"admin:pay_receipt:{p2.id}")
    await handle_admin_pay_receipt(cb_without, session=db_session)

    cb_without.answer.assert_called_once()
    assert "not available" in cb_without.answer.call_args[0][0].lower()




