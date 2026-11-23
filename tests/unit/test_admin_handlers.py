from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import (
    handle_admin_pay_detail,
    handle_admin_pay_reject_confirm,
    render_admin_pulse,
)
from app.services.ledger import get_or_create_user


# Round 13 Part K moved /admin, admin:dashboard, and admin:tutorial to the Mini App Admin tab.
# Payment review push callbacks and render_admin_pulse remain live and are guarded below.


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


@pytest.mark.asyncio
async def test_admin_pulse_metrics_and_html_escaping(db_session):
    from app.models.enums import AccountCategory, JobStatus
    from app.models.schema import Job, User
    settings.admin_ids_raw = "888111"

    # Add users
    u1 = User(id=1001, account_category=AccountCategory.FREE.value, language_code="en")
    u2 = User(id=1002, account_category=AccountCategory.PURCHASED.value, language_code="en")
    db_session.add_all([u1, u2])
    await db_session.flush()

    # Add jobs (1 success, 1 failed with unsafe characters in error_message)
    unsafe_err = "VoxCPM2 crashed: <InvalidIndex & BufferOverflow>"
    j1 = Job(id="job-ok-1", user_id=1001, status=JobStatus.SUCCESS.value)
    j2 = Job(id="job-fail-1", user_id=1002, status=JobStatus.FAILED.value, error_message=unsafe_err)
    db_session.add_all([j1, j2])
    await db_session.flush()

    text, kb = await render_admin_pulse(lang="en", session=db_session)

    # Pulse metrics assertions
    assert "Users: <b>2</b> (VIP 1)" in text
    assert "Jobs today: <b>2</b>" in text
    assert "Running: 0" in text
    assert "Queued: 0" in text

    # The failure line carries the escaped first line of the stored
    # error (no machine-class prefix here), never the raw text.
    assert "VoxCPM2 crashed: &lt;InvalidIndex &amp; BufferOverflow&gt;" in text
    assert unsafe_err not in text
    assert "<InvalidIndex" not in text
    assert kb is not None


@pytest.mark.asyncio
async def test_admin_pulse_empty_db_safety(db_session):
    text, kb = await render_admin_pulse(lang="en", session=db_session)

    assert "Users: <b>0</b>" in text
    assert "Jobs today: <b>0</b>" in text
    assert "Last failure: None yet" in text
    assert kb is not None
