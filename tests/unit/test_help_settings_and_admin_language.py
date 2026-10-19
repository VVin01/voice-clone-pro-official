from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    handle_admin_settings,
    handle_admin_settings_allowance,
    handle_admin_settings_free_chars,
)
from app.handlers.menu import (
    handle_help_command,
    handle_menu_help_settings,
    handle_user_language_toggle,
)
from app.models.schema import User
from app.services.ledger import get_or_create_user
from app.ui.keyboards import (
    admin_dashboard_kb,
    admin_settings_interactive_kb,
    help_settings_kb,
    main_menu_kb,
)


@pytest.mark.asyncio
async def test_admin_settings_language_honors_user_profile(db_session):
    """Bot settings panel displays buttons localized to the admin's active language."""
    settings.admin_ids_raw = "888001"

    # 1. Admin with English language
    admin_en, _ = await get_or_create_user(db_session, telegram_id=888001)
    admin_en.language_code = "en"
    await db_session.flush()

    cb_en = MagicMock(spec=CallbackQuery)
    cb_en.from_user = TgUser(id=888001, is_bot=False, first_name="AdminEn")
    cb_en.message = MagicMock(spec=Message)
    cb_en.message.edit_text = AsyncMock()
    cb_en.answer = AsyncMock()
    cb_en.data = "admin:settings"

    await handle_admin_settings(cb_en, session=db_session)
    cb_en.message.edit_text.assert_called_once()
    kb_en = cb_en.message.edit_text.call_args[1].get("reply_markup")
    button_texts_en = [btn.text for row in kb_en.inline_keyboard for btn in row]

    # Verify English labels are displayed
    assert any("Edit Free Allowance" in t for t in button_texts_en)
    assert any("Edit Free Char Limit" in t for t in button_texts_en)
    assert any("Edit Purchased Char Limit" in t for t in button_texts_en)
    assert any("Edit Daily Limit" in t for t in button_texts_en)
    assert any("Edit Channels/Gates" in t for t in button_texts_en)
    assert any("Back" in t for t in button_texts_en)

    # 2. Admin with Burmese language
    admin_en.language_code = "my"
    await db_session.flush()

    cb_my = MagicMock(spec=CallbackQuery)
    cb_my.from_user = TgUser(id=888001, is_bot=False, first_name="AdminMy")
    cb_my.message = MagicMock(spec=Message)
    cb_my.message.edit_text = AsyncMock()
    cb_my.answer = AsyncMock()
    cb_my.data = "admin:settings"

    await handle_admin_settings(cb_my, session=db_session)
    cb_my.message.edit_text.assert_called_once()
    kb_my = cb_my.message.edit_text.call_args[1].get("reply_markup")
    button_texts_my = [btn.text for row in kb_my.inline_keyboard for btn in row]

    # Verify Myanmar labels are displayed
    assert any("အခမဲ့မိနစ် ပြင်ရန်" in t for t in button_texts_my)
    assert any("အခမဲ့စာလုံးရေ ပြင်ရန်" in t for t in button_texts_my)
    assert any("ဝယ်ယူသူစာလုံးရေ ပြင်ရန်" in t for t in button_texts_my)
    assert any("နေ့စဉ်အကြိမ်ရေ ပြင်ရန်" in t for t in button_texts_my)
    assert any("ချန်နယ်/ဂရု ပြင်ရန်" in t for t in button_texts_my)


@pytest.mark.asyncio
async def test_tutorial_manager_no_longer_duplicated_in_bot_settings():
    """Tutorial Manager is on Admin Dashboard but NOT in Bot Settings."""
    # Main Admin Dashboard has tutorial manager
    dash_kb = admin_dashboard_kb(lang="en")
    dash_cbs = [btn.callback_data for row in dash_kb.inline_keyboard for btn in row]
    assert "admin:tutorial" in dash_cbs

    # Bot Settings interactive panel does NOT have tutorial manager
    settings_kb = admin_settings_interactive_kb(lang="en")
    settings_cbs = [btn.callback_data for row in settings_kb.inline_keyboard for btn in row]
    assert "admin:tutorial" not in settings_cbs

    # Grid is 4 rows of 2 buttons each
    assert len(settings_kb.inline_keyboard) == 4
    for row in settings_kb.inline_keyboard:
        assert len(row) == 2


@pytest.mark.asyncio
async def test_help_and_settings_navigation_and_language_toggle(db_session):
    """User can view Help & Settings and toggle language instantly with floating toast."""
    user, _ = await get_or_create_user(db_session, telegram_id=555001)
    user.language_code = "my"
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=555001, is_bot=False, first_name="User")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    # 1. Open Help & Settings via menu:help_settings
    cb.data = "menu:help_settings"
    await handle_menu_help_settings(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1].get("reply_markup")

    assert "အသုံးပြုနည်းလမ်းညွှန်နှင့် ဆက်တင်များ" in rendered_text
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "user_lang:my" in callbacks
    assert "user_lang:en" in callbacks
    assert "menu:home" in callbacks

    # 2. Switch language to English
    cb.message.edit_text.reset_mock()
    cb.answer.reset_mock()
    cb.data = "user_lang:en"

    await handle_user_language_toggle(cb, session=db_session)

    # Toast feedback must be floating (show_alert=False)
    cb.answer.assert_called_once()
    assert cb.answer.call_args[1].get("show_alert") is False
    assert "English" in cb.answer.call_args[0][0]

    # Database updated
    updated_user = (await db_session.execute(select(User).where(User.id == 555001))).scalar_one()
    assert updated_user.language_code == "en"

    # Re-rendered in English
    cb.message.edit_text.assert_called_once()
    en_text = cb.message.edit_text.call_args[0][0]
    assert "Guide & Settings" in en_text

    # 3. Legacy menu:tutorial callback also works
    cb.message.edit_text.reset_mock()
    cb.data = "menu:tutorial"
    await handle_menu_help_settings(cb, session=db_session)
    cb.message.edit_text.assert_called_once()


@pytest.mark.asyncio
async def test_admin_settings_dynamically_reflects_updated_pricing_plans(db_session):
    """Bot Settings panel automatically updates Minute Packages when pricing changes."""
    from app.services.payments import get_active_pricing_plans, update_pricing_plan_price

    settings.admin_ids_raw = "888001"
    admin, _ = await get_or_create_user(db_session, telegram_id=888001)

    # Seed default plans first
    await get_active_pricing_plans(db_session)

    # 1. Update MIN_10 to 3,850 MMK
    ok, plan, status = await update_pricing_plan_price(db_session, "MIN_10", 3850, admin_id=888001)
    assert ok is True
    await db_session.flush()

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888001, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = "admin:settings"

    await handle_admin_settings(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]

    assert "Minute Packages:" in rendered_text
    assert "3,850 MMK" in rendered_text


@pytest.mark.asyncio
async def test_payment_invoice_has_no_copy_buttons(db_session):
    """Payment invoice displays clean instructions and removes Copy KBZPay/WavePay buttons."""
    from aiogram.fsm.context import FSMContext
    from app.handlers.payment import handle_package_selected
    from app.services.payments import get_active_pricing_plans

    # Seed default plans first
    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=555002)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=555002, is_bot=False, first_name="User")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = "buy_pkg:MIN_10"

    mock_state = MagicMock(spec=FSMContext)
    mock_state.set_state = AsyncMock()
    mock_state.update_data = AsyncMock()

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


@pytest.mark.asyncio
async def test_help_settings_dynamically_reflects_admin_changes(db_session):
    """Help & Settings dynamically updates allowance, char limits, and daily limits when Admin changes them."""
    from app.handlers.menu import render_help_settings_content
    from app.services.dynamic_settings import set_dynamic_setting

    orig_allowance = settings.free_monthly_allowance_seconds
    orig_free_chars = settings.character_limit_free
    orig_purchased_chars = settings.character_limit_purchased
    orig_daily_limit = settings.free_daily_job_limit

    try:
        user, _ = await get_or_create_user(db_session, telegram_id=555003)
        user.language_code = "my"
        await db_session.flush()

        # 1. Baseline rendering in Burmese (defaults: 180s = 3m, 500 chars, 2000 chars, 5 daily)
        text_my, _ = await render_help_settings_content(db_session, user.id)
        assert "၃ မိနစ် (၁၈၀ စက္ကန့်)" in text_my or "3 မိနစ် (180 စက္ကန့်)" in text_my
        assert "၅၀၀" in text_my or "500" in text_my
        assert "၂,၀၀၀" in text_my or "2,000" in text_my
        assert "၅" in text_my or "5" in text_my

        # 2. Admin modifies settings dynamically
        await set_dynamic_setting(db_session, "free_monthly_allowance_seconds", 300, admin_id=999)
        await set_dynamic_setting(db_session, "character_limit_free", 800, admin_id=999)
        await set_dynamic_setting(db_session, "character_limit_purchased", 5000, admin_id=999)
        await set_dynamic_setting(db_session, "free_daily_job_limit", 10, admin_id=999)
        await db_session.commit()

        # 3. Help & Settings immediately renders modified values in Burmese
        text_updated_my, _ = await render_help_settings_content(db_session, user.id)
        assert "5 မိနစ် (300 စက္ကန့်)" in text_updated_my
        assert "800" in text_updated_my
        assert "5,000" in text_updated_my
        assert "10" in text_updated_my

        # 4. English user also immediately sees updated values
        user.language_code = "en"
        await db_session.commit()
        text_updated_en, _ = await render_help_settings_content(db_session, user.id)
        assert "5 free minutes (300s)" in text_updated_en
        assert "800" in text_updated_en
        assert "5,000" in text_updated_en
        assert "10" in text_updated_en
    finally:
        settings.free_monthly_allowance_seconds = orig_allowance
        settings.character_limit_free = orig_free_chars
        settings.character_limit_purchased = orig_purchased_chars
        settings.free_daily_job_limit = orig_daily_limit



