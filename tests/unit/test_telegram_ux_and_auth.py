from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import is_authorized_admin
from app.handlers.clone import CloneFSM, handle_voice_clone_start
from app.handlers.menu import handle_help_command, render_main_menu
from app.handlers.start import handle_language_command, handle_start, handle_set_language, handle_gate_verify
from app.services.gate import verify_user_membership
from app.services.ledger import get_minute_balance, get_or_create_user
from app.ui.keyboards import main_menu_kb


@pytest.mark.asyncio
async def test_four_item_user_menu_and_admin_button():
    # Ordinary user keyboard: exactly 5 items, no admin dashboard
    kb_user_my = main_menu_kb(is_admin=False, lang="my")
    user_buttons = [btn.callback_data for row in kb_user_my.inline_keyboard for btn in row]
    assert user_buttons == [
        "menu:voice_clone",
        "menu:buy_minutes",
        "menu:refer_earn",
        "menu:custom_voice",
        "menu:help_settings",
    ]
    assert "admin:dashboard" not in user_buttons
    assert len(user_buttons) == 5
    assert "အသံ" in kb_user_my.inline_keyboard[0][0].text or "Voice" in kb_user_my.inline_keyboard[0][0].text

    kb_user_en = main_menu_kb(is_admin=False, lang="en")
    assert "Voice" in kb_user_en.inline_keyboard[0][0].text
    assert "Buy Minutes" in kb_user_en.inline_keyboard[1][0].text
    assert "Refer & Earn" in kb_user_en.inline_keyboard[1][1].text
    assert "Custom Voice" in kb_user_en.inline_keyboard[2][0].text
    assert "Help & Settings" in kb_user_en.inline_keyboard[2][1].text

    # Admin keyboard: 5 user items + 1 admin dashboard
    kb_admin = main_menu_kb(is_admin=True, lang="en")
    admin_buttons = [btn.callback_data for row in kb_admin.inline_keyboard for btn in row]
    assert admin_buttons == [
        "menu:voice_clone",
        "menu:buy_minutes",
        "menu:refer_earn",
        "menu:custom_voice",
        "menu:help_settings",
        "admin:dashboard",
    ]
    assert len(admin_buttons) == 6
    assert "Admin Dashboard" in kb_admin.inline_keyboard[3][0].text


@pytest.mark.asyncio
async def test_backend_admin_authorization():
    # Configure an admin ID
    settings.admin_ids_raw = "999001"

    # Non-admin user callback
    non_admin_cb = MagicMock(spec=CallbackQuery)
    non_admin_cb.from_user = TgUser(id=111002, is_bot=False, first_name="RegularUser")
    non_admin_cb.answer = AsyncMock()

    is_allowed = await is_authorized_admin(non_admin_cb)
    assert is_allowed is False
    non_admin_cb.answer.assert_called_once()

    # Admin user callback
    admin_cb = MagicMock(spec=CallbackQuery)
    admin_cb.from_user = TgUser(id=999001, is_bot=False, first_name="RealAdmin")
    admin_cb.answer = AsyncMock()

    is_allowed_admin = await is_authorized_admin(admin_cb)
    assert is_allowed_admin is True


@pytest.mark.asyncio
async def test_membership_gate_and_welcome_bonus_awarding(db_session):
    settings.welcome_bonus_seconds = 300
    user, is_new = await get_or_create_user(db_session, telegram_id=77001)
    assert user.welcome_bonus_awarded is False
    assert user.channel_verified is False

    # Security fix (audit 2026-10-28): the membership gate fails closed.
    # With bot=None (or missing gate IDs) verification must NOT pass and
    # no welcome bonus may be awarded; the old bypass is removed.
    verified, reason = await verify_user_membership(db_session, user, bot=None)
    assert verified is False
    assert user.channel_verified is False
    assert user.group_verified is False
    assert user.welcome_bonus_awarded is False

    bal = await get_minute_balance(db_session, user.id)
    assert bal.welcome_seconds == 0

    # Repeat verification: still not verified, still no bonus.
    verified2, _ = await verify_user_membership(db_session, user, bot=None)
    assert verified2 is False
    bal2 = await get_minute_balance(db_session, user.id)
    assert bal2.welcome_seconds == 0


@pytest.mark.asyncio
async def test_render_main_menu_display(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=88001, language_code="en")
    text, kb = await render_main_menu(user.id, lang="en", session=db_session)
    assert "Main Menu" in text
    assert "Total Balance" in text
    assert "Account:" in text
    assert "FREE" in text
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert callbacks == [
        "menu:voice_clone",
        "menu:buy_minutes",
        "menu:refer_earn",
        "menu:custom_voice",
        "menu:help_settings",
    ]


@pytest.mark.asyncio
async def test_voice_clone_button_routes_to_voice_studio(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=88002)
    user.language_code = "en"
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=88002, is_bot=False, first_name="VoiceUser")
    cb.data = "menu:voice_clone"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.handlers.clone.AsyncSessionLocal", side_effect=mock_session):
        await handle_voice_clone_start(cb, state)

    state.set_state.assert_called_once_with(CloneFSM.waiting_for_text)
    cb.message.edit_text.assert_called_once()
    assert "Enter the text" in cb.message.edit_text.call_args[0][0]
    cb.answer.assert_called_once()


@pytest.mark.asyncio
async def test_language_command_handler(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=88003)
    user.language_code = "en"
    await db_session.flush()

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=88003, is_bot=False, first_name="LangUser")
    msg.answer = AsyncMock()

    await handle_language_command(msg, session=db_session)

    msg.answer.assert_called_once()
    call_args = msg.answer.call_args
    assert "Please choose your preferred language" in call_args[0][0]
    reply_markup = call_args[1]["reply_markup"]
    callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert "set_lang:my" in callbacks
    assert "set_lang:en" in callbacks


@pytest.mark.asyncio
async def test_help_command_handler(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=88004)
    user.language_code = "en"
    await db_session.flush()

    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=88004, is_bot=False, first_name="HelpUser")
    msg.answer = AsyncMock()

    await handle_help_command(msg, session=db_session)

    msg.answer.assert_called_once()
    call_args = msg.answer.call_args
    assert "Guide & Settings" in call_args[0][0] or "Help & Settings" in call_args[0][0]
    reply_markup = call_args[1]["reply_markup"]
    callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert "menu:home" in callbacks
    assert "user_lang:my" in callbacks
    assert "user_lang:en" in callbacks


@pytest.mark.asyncio
async def test_start_command_flow_new_vs_existing(db_session):
    from aiogram.filters import CommandObject
    # 1. New user: receives language selection prompt
    msg_new = MagicMock(spec=Message)
    msg_new.from_user = TgUser(id=77002, is_bot=False, first_name="Newbie")
    msg_new.answer = AsyncMock()
    cmd = CommandObject(prefix="/", command="start", args=None)

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=mock_session):
        await handle_start(msg_new, cmd)

    msg_new.answer.assert_called_once()
    assert "language" in msg_new.answer.call_args[0][0].lower() or "ဘာသာစကား" in msg_new.answer.call_args[0][0]

    # 2. Existing verified user: receives main menu directly with 4-button design
    user, _ = await get_or_create_user(db_session, telegram_id=77003)
    user.language_code = "en"
    user.channel_verified = True
    user.group_verified = True
    await db_session.flush()

    msg_exist = MagicMock(spec=Message)
    msg_exist.bot = None
    msg_exist.from_user = TgUser(id=77003, is_bot=False, first_name="Existing")
    msg_exist.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=mock_session):
        await handle_start(msg_exist, cmd)

    msg_exist.answer.assert_called_once()
    reply_markup = msg_exist.answer.call_args[1]["reply_markup"]
    callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert callbacks == [
        "menu:voice_clone",
        "menu:buy_minutes",
        "menu:refer_earn",
        "menu:custom_voice",
        "menu:help_settings",
    ]
