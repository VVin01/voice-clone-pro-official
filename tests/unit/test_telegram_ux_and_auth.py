from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.admin import is_authorized_admin
from app.handlers.menu import render_main_menu
from app.handlers.start import handle_start, handle_set_language, handle_gate_verify
from app.services.gate import verify_user_membership
from app.services.ledger import get_minute_balance, get_or_create_user
from app.ui.keyboards import main_menu_kb


@pytest.mark.asyncio
async def test_three_item_user_menu_contract():
    # Round 13 Part K: main_menu_kb has exactly 3 buttons (Mini App, Refer, Language)
    # for both regular users and admins.
    kb_user_my = main_menu_kb(is_admin=False, lang="my")
    user_buttons = [btn.callback_data for row in kb_user_my.inline_keyboard for btn in row]
    assert user_buttons == [None, "menu:refer_earn", "menu:change_lang"]
    assert "admin:broadcast_prompt" not in user_buttons
    assert len(user_buttons) == 3
    flat_my = [btn for row in kb_user_my.inline_keyboard for btn in row]
    assert flat_my[0].web_app is not None

    kb_user_en = main_menu_kb(is_admin=False, lang="en")
    flat_en = [btn for row in kb_user_en.inline_keyboard for btn in row]
    assert "Open Mini App" in flat_en[0].text
    assert "Refer Friends" in flat_en[1].text
    assert "Change Language" in flat_en[2].text

    kb_admin = main_menu_kb(is_admin=True, lang="en")
    admin_buttons = [btn.callback_data for row in kb_admin.inline_keyboard for btn in row]
    assert admin_buttons == [None, "menu:refer_earn", "menu:change_lang"]


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
    # Round 13 Part K: render_main_menu shows the compact Welcome + 3-button keyboard.
    assert "Welcome to Voice Clone Pro" in text
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert callbacks == [None, "menu:refer_earn", "menu:change_lang"]


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

    # 2. Existing user with cached verified flags: /start does NOT trust
    # the cached flags — it re-verifies membership live (gate fix). With
    # bot=None the check fails closed, so the user receives the
    # membership gate screen (channel/group URL buttons + verify button),
    # not the main menu.
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
    gate_text = msg_exist.answer.call_args[0][0]
    from app.services.i18n import t
    assert t("membership_gate_title", "en") in gate_text
    reply_markup = msg_exist.answer.call_args[1]["reply_markup"]
    callbacks = [btn.callback_data for row in reply_markup.inline_keyboard for btn in row]
    assert callbacks == [None, None, "gate:verify"]
