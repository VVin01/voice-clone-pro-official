"""Refer & Earn must be reachable from the bot main menu.

The handler (menu:refer_earn), referral ledger and i18n strings all
existed, but no keyboard ever emitted the callback, so users could not
open the page. These tests pin the button in main_menu_kb and verify
the button's callback renders the referral page end-to-end.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser

from app.handlers.menu import handle_menu_refer_earn
from app.services.ledger import get_or_create_user
from app.ui.keyboards import main_menu_kb


def _flat(kb):
    return [btn for row in kb.inline_keyboard for btn in row]


def test_main_menu_has_refer_earn_button_both_languages():
    for lang, label in (("my", "မိတ်ဆက်ဆု"), ("en", "Refer & Earn")):
        kb = main_menu_kb(is_admin=False, lang=lang)
        matches = [b for b in _flat(kb) if b.callback_data == "menu:refer_earn"]
        assert len(matches) == 1
        assert label in matches[0].text


def test_admin_main_menu_also_has_refer_earn_button():
    kb = main_menu_kb(is_admin=True, lang="my")
    datas = [b.callback_data for b in _flat(kb)]
    assert datas.count("menu:refer_earn") == 1


@pytest.mark.asyncio
async def test_refer_earn_button_callback_renders_referral_page(db_session):
    """Pressing the menu button must render stats + the user's own link."""
    user_id = 700001001
    user, _ = await get_or_create_user(db_session, telegram_id=user_id)

    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat, id=user_id)
    msg.edit_text = AsyncMock(return_value=msg)

    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="ReferTester")
    cb.bot = MagicMock()
    cb.bot.get_me = AsyncMock(return_value=MagicMock(username="voiceclonepro_bot"))
    cb.answer = AsyncMock()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with patch("app.handlers.menu.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_menu_refer_earn(cb)

    msg.edit_text.assert_awaited_once()
    sent_text = msg.edit_text.call_args[0][0]
    assert f"https://t.me/voiceclonepro_bot?start={user.referral_code}" in sent_text
    markup = msg.edit_text.call_args[1]["reply_markup"]
    back_datas = [b.callback_data for b in _flat(markup)]
    assert "menu:home" in back_datas
    cb.answer.assert_awaited_once()
