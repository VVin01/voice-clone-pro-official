from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import (
    handle_admin_command,
    handle_admin_dashboard,
    handle_admin_pay_reject,
    handle_admin_tutorial,
    render_admin_pulse,
)
from app.services.ledger import get_or_create_user




@pytest.mark.asyncio
async def test_admin_tutorial_handler_authorized(db_session):
    settings.admin_ids_raw = "888111"
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_tutorial(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    call_args = cb.message.edit_text.call_args[0][0]
    assert "Tutorial Video Manager" in call_args
    assert "Link:" in call_args
    cb.answer.assert_called_once()




@pytest.mark.asyncio
async def test_admin_handlers_unauthorized():
    settings.admin_ids_raw = "888111"
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=999999, is_bot=False, first_name="Stranger")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    cb.data = "admin:dashboard"
    await handle_admin_dashboard(cb)
    cb.message.edit_text.assert_not_called()
    cb.answer.assert_called_once_with("⛔ Access denied. Admin only.", show_alert=True)




@pytest.mark.asyncio
async def test_admin_pay_reject_html_escaping(db_session):
    from contextlib import asynccontextmanager
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
    cb.data = f"admin:pay_reject:{payment.id}"
    cb.bot = AsyncMock()
    cb.bot.send_message = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()), \
         patch("app.handlers.admin.reject_payment_request", return_value=(True, payment, "REJECTED")):
        await handle_admin_pay_reject(cb)

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
async def test_admin_command_handler_authorized(db_session):
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin_user.language_code = "en"
    await db_session.flush()

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    msg.answer = AsyncMock()

    await handle_admin_command(msg, session=db_session)

    msg.answer.assert_called_once()
    call_args = msg.answer.call_args[0][0]
    call_kwargs = msg.answer.call_args[1]
    assert call_kwargs.get("parse_mode") == "HTML"
    # Bot cleanup 2026-11-03: /admin renders the compact pulse, not
    # the retired 11-button dashboard.
    assert "Admin Panel" in call_args
    assert "Users: <b>1</b> (VIP 0)" in call_args
    assert "Last failure: None yet" in call_args

    reply_markup = call_kwargs.get("reply_markup")
    assert reply_markup is not None
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for btn in buttons]
    assert "admin:broadcast" in callbacks
    assert "menu:home" in callbacks
    # The Admin Suite entry is a WebApp deep-link to the Mini App tab.
    assert any(btn.web_app is not None and "tab=tab-admin" in (btn.web_app.url or "") for btn in buttons)
    # Retired chat screens are gone from the landing.
    for retired in ("admin:voices", "admin:payments", "admin:users", "admin:queue", "admin:settings", "admin:cvr", "admin:audit"):
        assert retired not in callbacks


@pytest.mark.asyncio
async def test_admin_command_handler_unauthorized(db_session):
    # Bot cleanup 2026-11-03: a non-admin /admin no longer gets the
    # "Admin only" denial — it silently falls through to the normal
    # user welcome (here: the membership gate card, since the probe
    # cannot verify this user).
    settings.admin_ids_raw = "888111"
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=999999, is_bot=False, first_name="Intruder")
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.get_chat_member = AsyncMock(side_effect=Exception("no membership"))

    await handle_admin_command(msg, session=db_session)

    msg.answer.assert_called_once()
    sent_text = msg.answer.call_args[0][0]
    assert "Access denied" not in sent_text
    assert "Admin only" not in sent_text


@pytest.mark.asyncio
async def test_admin_dashboard_callback_refresh(db_session):
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin_user.language_code = "en"
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = "admin:refresh"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_dashboard(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    assert cb.message.edit_text.call_args[1].get("parse_mode") == "HTML"
    cb.answer.assert_called_once()
    assert "Dashboard refreshed" in cb.answer.call_args[0][0]


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
