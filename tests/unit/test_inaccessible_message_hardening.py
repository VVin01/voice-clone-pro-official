from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Chat, ErrorEvent, InaccessibleMessage, Message, Update
from aiogram.types import User as TgUser

from app.handlers import setup_handlers
from app.handlers.menu import handle_menu_home, handle_menu_refer_earn
from app.ui.helpers import safe_answer_callback, safe_edit_message
from app.models.enums import AccountCategory
from app.services.ledger import get_or_create_user


@pytest.mark.asyncio
async def test_safe_edit_message_normal_message():
    """Verify safe_edit_message calls edit_text when message is a regular Message."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.edit_text = AsyncMock(return_value="edited_ok")
    mock_msg.chat = MagicMock(spec=Chat, id=12345)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = mock_msg
    cb.from_user = TgUser(id=12345, is_bot=False, first_name="Tester")
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()

    res = await safe_edit_message(cb, "Hello World", reply_markup=None)
    assert res == "edited_ok"
    mock_msg.edit_text.assert_awaited_once_with(
        "Hello World", reply_markup=None, parse_mode="HTML"
    )
    cb.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_safe_edit_message_with_inaccessible_message():
    """Verify safe_edit_message falls back to send_message when message is an InaccessibleMessage."""
    inaccessible = MagicMock(spec=InaccessibleMessage)
    # InaccessibleMessage has no edit_text attribute
    del inaccessible.edit_text
    inaccessible.chat = MagicMock(spec=Chat, id=98765)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = inaccessible
    cb.from_user = TgUser(id=98765, is_bot=False, first_name="InaccessibleUser")
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock(return_value="sent_ok")

    res = await safe_edit_message(cb, "Fallback Message", reply_markup=None)
    assert res == "sent_ok"
    cb.bot.send_message.assert_awaited_once_with(
        chat_id=98765,
        text="Fallback Message",
        reply_markup=None,
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_safe_edit_message_when_message_is_none():
    """Verify safe_edit_message falls back to send_message when callback.message is None."""
    cb = MagicMock(spec=CallbackQuery)
    cb.message = None
    cb.from_user = TgUser(id=55555, is_bot=False, first_name="NoneUser")
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock(return_value="sent_ok")

    res = await safe_edit_message(cb, "Fallback to User ID", reply_markup=None)
    assert res == "sent_ok"
    cb.bot.send_message.assert_awaited_once_with(
        chat_id=55555,
        text="Fallback to User ID",
        reply_markup=None,
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_safe_edit_message_not_modified_telegram_bad_request():
    """Verify safe_edit_message handles 'message is not modified' gracefully without re-sending."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.edit_text = AsyncMock(
        side_effect=TelegramBadRequest(
            method=MagicMock(), message="Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message"
        )
    )
    mock_msg.chat = MagicMock(spec=Chat, id=12345)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = mock_msg
    cb.from_user = TgUser(id=12345, is_bot=False, first_name="Tester")
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()

    res = await safe_edit_message(cb, "Unchanged Text", reply_markup=None)
    assert res == mock_msg
    cb.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_menu_refer_earn_with_inaccessible_message(db_session):
    """
    Test the exact scenario that caused the live crash:
    A user clicking the 'Refer & Earn' button on an InaccessibleMessage.
    Must gracefully send the referral card via send_message without raising AttributeError.
    """
    user_id = 7393530456
    user, _ = await get_or_create_user(db_session, telegram_id=user_id, username="phyominnaing_660")

    inaccessible = MagicMock(spec=InaccessibleMessage)
    del inaccessible.edit_text
    inaccessible.chat = MagicMock(spec=Chat, id=user_id)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = inaccessible
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Phyo Min Naing")
    cb.bot = MagicMock()
    cb.bot.get_me = AsyncMock(return_value=MagicMock(username="voiceclonepro_bot"))
    cb.bot.send_message = AsyncMock(return_value=MagicMock(spec=Message, message_id=101))
    cb.answer = AsyncMock()

    # Call handle_menu_refer_earn - must not throw AttributeError!
    await handle_menu_refer_earn(cb)

    cb.bot.send_message.assert_awaited_once()
    sent_text = cb.bot.send_message.call_args[1]["text"]
    assert f"start={user.referral_code}" in sent_text
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_menu_home_with_inaccessible_message(db_session):
    """Verify handle_menu_home gracefully falls back when callback.message is InaccessibleMessage."""
    user_id = 8902721285
    await get_or_create_user(db_session, telegram_id=user_id, username="htetlynnzaw")

    inaccessible = MagicMock(spec=InaccessibleMessage)
    del inaccessible.edit_text
    inaccessible.chat = MagicMock(spec=Chat, id=user_id)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = inaccessible
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Htet")
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock(return_value=MagicMock(spec=Message, message_id=102))
    cb.answer = AsyncMock()

    await handle_menu_home(cb)

    cb.bot.send_message.assert_awaited_once()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_error_handler_suppresses_inaccessible_message_error():
    """Verify the global error handler catches and suppresses InaccessibleMessage AttributeError."""
    from aiogram import Dispatcher
    dp = Dispatcher()
    setup_handlers(dp)

    # Trigger error event with InaccessibleMessage AttributeError
    exc = AttributeError("'InaccessibleMessage' object has no attribute 'edit_text'")
    cb = MagicMock(spec=CallbackQuery)
    cb.answer = AsyncMock()
    update = MagicMock(spec=Update)
    update.callback_query = cb
    event = ErrorEvent(update=update, exception=exc)

    error_handler = dp.error.handlers[0].call
    result = await error_handler(event)
    assert result is True
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_error_handler_suppresses_telegram_forbidden_error(db_session):
    """Verify global_error_handler catches TelegramForbiddenError, marks user BLOCKED in DB, and returns True."""
    from aiogram import Dispatcher
    from aiogram.exceptions import TelegramForbiddenError
    from app.models.enums import UserStatus
    from app.models.schema import User

    target_user_id = 99887766
    user = User(id=target_user_id, username="blocked_test", status=UserStatus.ACTIVE.value)
    db_session.add(user)
    await db_session.commit()

    # Create session factory returning our db_session
    class MockSessionFactory:
        def __call__(self):
            return self
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    dp = Dispatcher()
    setup_handlers(dp, session_factory=MockSessionFactory())

    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = TgUser(id=target_user_id, is_bot=False, first_name="BlockedUser")
    update = MagicMock(spec=Update)
    update.message = mock_msg
    update.callback_query = None
    update.my_chat_member = None

    exc = TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
    event = ErrorEvent(update=update, exception=exc)

    error_handler = dp.error.handlers[0].call
    result = await error_handler(event)

    assert result is True
    updated_user = await db_session.get(User, target_user_id)
    assert updated_user.status == UserStatus.BLOCKED.value


@pytest.mark.asyncio
async def test_handle_start_catches_forbidden_error_gracefully(db_session, monkeypatch):
    """Verify handle_start catches TelegramForbiddenError when blocked user sends /start."""
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.filters import CommandObject
    from app.handlers.start import handle_start
    from app.models.enums import UserStatus
    from app.models.schema import User

    user_id = 8923364289
    user = User(id=user_id, username="spammer", status=UserStatus.ACTIVE.value)
    db_session.add(user)
    await db_session.commit()

    # Mock AsyncSessionLocal to use db_session
    class MockSessionCtx:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr("app.handlers.start.AsyncSessionLocal", lambda: MockSessionCtx())

    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = TgUser(id=user_id, is_bot=False, first_name="BlockedSpammer")
    mock_msg.bot = MagicMock()
    mock_msg.answer = AsyncMock(side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user"))

    cmd = CommandObject(prefix="/", command="start", args=None)

    # Should not raise any unhandled exception
    await handle_start(mock_msg, cmd)

    updated_user = await db_session.get(User, user_id)
    assert updated_user.status == UserStatus.BLOCKED.value


@pytest.mark.asyncio
async def test_safe_edit_message_catches_forbidden_error():
    """Verify safe_edit_message handles TelegramForbiddenError during fallback send gracefully."""
    from aiogram.exceptions import TelegramForbiddenError
    inaccessible = MagicMock(spec=InaccessibleMessage)
    del inaccessible.edit_text
    inaccessible.chat = MagicMock(spec=Chat, id=112233)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = inaccessible
    cb.from_user = TgUser(id=112233, is_bot=False, first_name="Blocked")
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock(
        side_effect=TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
    )

    res = await safe_edit_message(cb, "Hello")
    assert res is None
