from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import (
    handle_admin_command,
    handle_admin_dashboard,
    handle_admin_model_detail,
    handle_admin_pay_reject,
    handle_admin_pricing,
    handle_admin_settings,
    handle_admin_tutorial,
    render_admin_dashboard,
)
from app.services.ledger import get_or_create_user


@pytest.mark.asyncio
async def test_admin_settings_handler_authorized(db_session):
    settings.admin_ids_raw = "888111"
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_settings(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    call_args = cb.message.edit_text.call_args[0][0]
    assert "Bot Runtime Settings" in call_args
    assert "App Env:" in call_args
    assert "Free Char Limit:" in call_args
    cb.answer.assert_called_once()


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
async def test_admin_pricing_handler_authorized(db_session):
    settings.admin_ids_raw = "888111"
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_pricing(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    call_args = cb.message.edit_text.call_args[0][0]
    assert ("Active Minute Packages" in call_args or "လက်ရှိ မိနစ်ပက်ကေ့ဂျ်များ" in call_args)
    assert "MMK" in call_args
    cb.answer.assert_called_once()


@pytest.mark.asyncio
async def test_admin_handlers_unauthorized():
    settings.admin_ids_raw = "888111"
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=999999, is_bot=False, first_name="Stranger")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_settings(cb)
    cb.message.edit_text.assert_not_called()
    cb.answer.assert_called_once_with("⛔ Access denied. Admin only.", show_alert=True)


@pytest.mark.asyncio
async def test_admin_model_detail_html_escaping(db_session):
    from contextlib import asynccontextmanager
    from unittest.mock import patch
    from app.models.schema import VoiceModel

    settings.admin_ids_raw = "888111"

    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)
    admin_user.language_code = "en"
    await db_session.flush()

    unsafe_path = "C:\\Models\\Rock & Roll <v1>\\model.pth"
    model = VoiceModel(
        name="TestModel",
        slug="test-model",
        type="SYSTEM",
        model_dir="C:\\Models",
        pth_path=unsafe_path,
        status="ACTIVE",
        is_default_free=False,
    )
    db_session.add(model)
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
    cb.data = f"admin:model:{model.id}"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_model_detail(cb)

    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    parse_mode = cb.message.edit_text.call_args[1].get("parse_mode")

    assert parse_mode == "HTML"
    # Escaped entities present
    assert "Rock &amp; Roll &lt;v1&gt;" in rendered_text
    # Raw unsafe characters absent
    assert unsafe_path not in rendered_text
    assert "<v1>" not in rendered_text
    # Legitimate HTML formatting intact
    assert "<b>Model: TestModel</b>" in rendered_text
    assert "<code>" in rendered_text


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
    assert "Server Status: 🟢 Online" in call_args
    assert "<code>888111</code>" in call_args
    assert "Users Overview:" in call_args
    assert "Voice Studio Jobs:" in call_args
    assert "Minutes & Ledger Pool:" in call_args

    reply_markup = call_kwargs.get("reply_markup")
    assert reply_markup is not None
    callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert "admin:voices" in callbacks
    assert "admin:payments" in callbacks
    assert "admin:users" in callbacks
    assert "admin:queue" in callbacks
    assert "admin:settings" in callbacks
    assert "admin:tutorial" in callbacks
    assert "admin:refresh" in callbacks
    assert "menu:home" in callbacks


@pytest.mark.asyncio
async def test_admin_command_handler_unauthorized():
    settings.admin_ids_raw = "888111"
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=999999, is_bot=False, first_name="Intruder")
    msg.answer = AsyncMock()

    await handle_admin_command(msg)

    msg.answer.assert_called_once_with("⛔ Access denied. Admin only.")


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
async def test_admin_dashboard_metrics_and_html_escaping(db_session):
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

    text, kb = await render_admin_dashboard(admin_id=888111, lang="en", session=db_session)

    # Metrics assertions
    assert "Total: <b>2</b> (Free: 1 · Purchased: 1)" in text
    assert "Total Jobs: <b>2</b>" in text
    assert "✅ Success: <b>1</b>" in text
    assert "❌ Failed: <b>1</b>" in text

    # HTML escaping assertions
    assert "&lt;InvalidIndex &amp; BufferOverflow&gt;" in text
    assert unsafe_err not in text
    assert "<InvalidIndex" not in text
    assert "<b>Recent Issues:</b>" in text


@pytest.mark.asyncio
async def test_admin_dashboard_empty_db_safety(db_session):
    text, kb = await render_admin_dashboard(admin_id=777, lang="en", session=db_session)

    assert "Total: <b>0</b>" in text
    assert "Total Jobs: <b>0</b>" in text
    assert "Clean (0 failed jobs)" in text
    assert "Purchased: <b>0.0m</b>" in text
    assert kb is not None


