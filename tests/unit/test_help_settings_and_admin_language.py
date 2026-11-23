from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
# Round 13 Part K retired bot-chat Help & Settings screens; user language toggle
# (menu:change_lang -> user_lang:*) and payment invoice tests remain live below.
from app.handlers.menu import (
    handle_change_language,
    handle_user_language_toggle,
)
from app.models.schema import User
from app.services.ledger import get_or_create_user
from app.ui.keyboards import (
    main_menu_kb,
)


@pytest.mark.asyncio
async def test_language_change_and_toggle_updates_user_and_renders_welcome(db_session):
    """User opens language picker via menu:change_lang and toggles language via user_lang:en."""
    user, _ = await get_or_create_user(db_session, telegram_id=555001)
    user.language_code = "my"
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=555001, is_bot=False, first_name="User")
    cb.message = MagicMock(spec=Message)
    cb.message.answer = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.answer = AsyncMock()

    # 1. Open language picker via menu:change_lang
    cb.data = "menu:change_lang"
    await handle_change_language(cb, session=db_session)

    cb.message.answer.assert_awaited_once()
    kb = cb.message.answer.await_args.kwargs.get("reply_markup")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert callbacks == ["user_lang:my", "user_lang:en"]

    # 2. Switch language to English
    cb.message.answer.reset_mock()
    cb.answer.reset_mock()
    cb.data = "user_lang:en"

    await handle_user_language_toggle(cb, session=db_session)

    cb.message.delete.assert_awaited_once()
    cb.answer.assert_awaited_once()

    # Database updated
    updated_user = (await db_session.execute(select(User).where(User.id == 555001))).scalar_one()
    assert updated_user.language_code == "en"

    # Re-rendered fresh EN welcome with exact 3-button menu
    cb.message.answer.assert_awaited_once()
    en_text = cb.message.answer.await_args.args[0]
    assert "Welcome to Voice Clone Pro" in en_text


@pytest.mark.asyncio
async def test_payment_invoice_has_no_copy_buttons(db_session):
    """Payment invoice displays clean instructions and removes Copy KBZPay/WavePay buttons."""
    from aiogram.fsm.context import FSMContext
    from app.handlers.payment import handle_package_selected
    from app.services.payments import get_active_pricing_plans

    # Seed default v3 plans first
    plans = await get_active_pricing_plans(db_session)
    pkg_code = plans[0].code
    user, _ = await get_or_create_user(db_session, telegram_id=555002)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=555002, is_bot=False, first_name="User")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = f"buy_pkg:{pkg_code}"

    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()
    mock_state.update_data = AsyncMock()

    from contextlib import asynccontextmanager
    from unittest.mock import patch as _patch

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with _patch("app.handlers.payment.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_package_selected(cb, state=mock_state)

    cb.message.edit_text.assert_called_once()
    kb = cb.message.edit_text.call_args[1].get("reply_markup")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]

    # Verify Copy buttons are removed
    assert "pay_copy:kpay" not in callbacks
    assert "pay_copy:wave" not in callbacks
    # Navigation remains
    assert "menu:buy_minutes" in callbacks
    assert "menu:home" in callbacks



