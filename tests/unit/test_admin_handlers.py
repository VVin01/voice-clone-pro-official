from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import (
    handle_admin_pay_detail,
    handle_admin_pay_reject_confirm,
)
from app.services.ledger import get_or_create_user


# Round 13 Part K moved /admin, admin:dashboard, and admin:tutorial to the Mini App Admin tab.
# Part B3 (2026-11-25) deleted dead render_admin_pulse; payment review push callbacks stay guarded below.


@pytest.mark.asyncio
async def test_admin_handlers_unauthorized():
    settings.admin_ids_raw = "888111"
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=999999, is_bot=False, first_name="Stranger")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    cb.data = "admin:pay_detail:PAY-TEST-1"
    await handle_admin_pay_detail(cb)
    cb.message.edit_text.assert_not_called()
    cb.answer.assert_called_once_with("⛔ Access denied. Admin only.", show_alert=True)


@pytest.mark.asyncio
async def test_admin_pay_reject_html_escaping(db_session):
    from unittest.mock import patch
    from app.models.enums import PaymentStatus
    from app.models.schema import PaymentRequest
    from app.services.ledger import get_or_create_user

    settings.admin_ids_raw = "888111"

    target_user, _ = await get_or_create_user(db_session, telegram_id=777001)
    target_user.language_code = "en"
    await db_session.flush()

    unsafe_reason = "Unverified <fake slip> & duplicate tx #123"
    payment = PaymentRequest(
        id="PAY-TEST-99",
        user_id=target_user.id,
        package_code="MIN_10",
        minutes=10,
        amount_mmk=2000,
        status=PaymentStatus.REJECTED.value,
        rejection_reason=unsafe_reason,
    )

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = f"admin:pay_reject_confirm:{payment.id}"
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    with patch("app.handlers.admin.reject_payment_request", return_value=(True, payment, "REJECTED")):
        await handle_admin_pay_reject_confirm(cb, session=db_session)

    cb.bot.send_message.assert_called_once()
    msg_args = cb.bot.send_message.call_args
    sent_text = msg_args[1]["text"]
    parse_mode = msg_args[1]["parse_mode"]

    assert parse_mode == "HTML"
    # Escaped entities present
    assert "Unverified &lt;fake slip&gt; &amp; duplicate tx #123" in sent_text
    # Raw unsafe characters absent
    assert unsafe_reason not in sent_text
    assert "<fake slip>" not in sent_text
    # Legitimate HTML formatting intact
    assert "<b>Payment Request (#PAY-TEST-99) has been rejected.</b>" in sent_text

