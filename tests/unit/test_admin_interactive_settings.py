from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers.admin import (
    AdminSettingsFSM,
    handle_admin_settings,
    handle_admin_settings_allowance,
    handle_admin_settings_daily_limit,
    handle_admin_settings_free_chars,
    handle_admin_settings_gates,
    handle_admin_settings_input_receive,
    handle_admin_settings_input_start,
    handle_admin_settings_preset,
    handle_admin_settings_purchased_chars,
    handle_admin_settings_toggle_maint,
)
from app.handlers.start import handle_start
from app.models.schema import AuditLog, BotSetting, User
from app.services.dynamic_settings import load_dynamic_settings, set_dynamic_setting
from app.services.ledger import get_or_create_user


@pytest.mark.asyncio
async def test_admin_interactive_settings_panel_rendering(db_session):
    """Admin settings panel renders with full interactive management buttons."""
    settings.admin_ids_raw = "888111"
    admin_user, _ = await get_or_create_user(db_session, telegram_id=888111)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    cb.data = "admin:settings"
    await handle_admin_settings(cb, session=db_session)

    cb.message.edit_text.assert_called_once()
    rendered_text = cb.message.edit_text.call_args[0][0]
    kb = cb.message.edit_text.call_args[1].get("reply_markup")

    assert "Bot Runtime Settings & System Policies" in rendered_text
    assert "Operational Status:" in rendered_text
    assert "Free Tier Policy:" in rendered_text
    assert "Purchased Tier Policy:" in rendered_text

    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "admin:settings:allowance" in callbacks
    assert "admin:settings:free_chars" in callbacks
    assert "admin:settings:purchased_chars" in callbacks
    assert "admin:settings:daily_limit" in callbacks
    assert "admin:settings:gates" in callbacks
    assert "admin:tutorial" not in callbacks  # Verified removed from Bot Settings to eliminate duplication
    assert "admin:settings:toggle_maintenance" in callbacks
    assert "admin:settings" in callbacks
    assert "admin:dashboard" in callbacks


@pytest.mark.asyncio
async def test_admin_settings_sub_menus(db_session):
    """Admin settings sub-menus display correct presets and custom input options."""
    settings.admin_ids_raw = "888111"

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    # 1. Allowance sub-menu
    await handle_admin_settings_allowance(cb, session=db_session)
    allowance_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    allowance_cbs = [btn.callback_data for row in allowance_kb.inline_keyboard for btn in row]
    assert "admin:set:free_monthly_allowance_seconds:60" in allowance_cbs
    assert "admin:set:free_monthly_allowance_seconds:180" in allowance_cbs
    assert "admin:set:free_monthly_allowance_seconds:300" in allowance_cbs
    assert "admin:settings_input:free_monthly_allowance_seconds" in allowance_cbs

    # 2. Free chars sub-menu
    await handle_admin_settings_free_chars(cb, session=db_session)
    fc_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    fc_cbs = [btn.callback_data for row in fc_kb.inline_keyboard for btn in row]
    assert "admin:set:character_limit_free:250" in fc_cbs
    assert "admin:set:character_limit_free:500" in fc_cbs
    assert "admin:set:character_limit_free:1000" in fc_cbs

    # 3. Purchased chars sub-menu
    await handle_admin_settings_purchased_chars(cb, session=db_session)
    pc_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    pc_cbs = [btn.callback_data for row in pc_kb.inline_keyboard for btn in row]
    assert "admin:set:character_limit_purchased:2000" in pc_cbs
    assert "admin:set:character_limit_purchased:5000" in pc_cbs

    # 4. Daily limit sub-menu
    await handle_admin_settings_daily_limit(cb, session=db_session)
    dl_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    dl_cbs = [btn.callback_data for row in dl_kb.inline_keyboard for btn in row]
    assert "admin:set:free_daily_job_limit:3" in dl_cbs
    assert "admin:set:free_daily_job_limit:5" in dl_cbs
    assert "admin:set:free_daily_job_limit:0" in dl_cbs

    # 5. Gates sub-menu
    await handle_admin_settings_gates(cb, session=db_session)
    gates_kb = cb.message.edit_text.call_args[1].get("reply_markup")
    gates_cbs = [btn.callback_data for row in gates_kb.inline_keyboard for btn in row]
    assert "admin:settings_input:required_channel_id" in gates_cbs
    assert "admin:settings_input:required_group_id" in gates_cbs


@pytest.mark.asyncio
async def test_admin_preset_and_dynamic_persistence(db_session):
    """Presets immediately update settings in memory, save to BotSetting table, and log audit."""
    settings.admin_ids_raw = "888111"

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    # Apply 5-minute allowance preset (300s)
    cb.data = "admin:set:free_monthly_allowance_seconds:300"
    await handle_admin_settings_preset(cb, session=db_session)

    assert settings.free_monthly_allowance_seconds == 300

    row = (await db_session.execute(
        select(BotSetting).where(BotSetting.key == "free_monthly_allowance_seconds")
    )).scalar_one_or_none()
    assert row is not None
    assert row.value == "300"

    # Check AuditLog entry
    audit = (await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "settings.update",
            AuditLog.target_id == "free_monthly_allowance_seconds",
        )
    )).scalar_one_or_none()
    assert audit is not None
    assert audit.admin_id == 888111

    # Revert back to 180s
    cb.data = "admin:set:free_monthly_allowance_seconds:180"
    await handle_admin_settings_preset(cb, session=db_session)
    assert settings.free_monthly_allowance_seconds == 180


@pytest.mark.asyncio
async def test_maintenance_mode_toggle_and_gate_enforcement(db_session):
    """Maintenance mode pauses non-admin users while granting unrestricted access to admins."""
    settings.admin_ids_raw = "888111"
    settings.maintenance_mode = False

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    # 1. Admin toggles maintenance ON
    await handle_admin_settings_toggle_maint(cb, session=db_session)
    assert settings.maintenance_mode is True

    # 2. Non-admin user tries /start -> receives maintenance notice
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    cmd_mock = MagicMock()
    cmd_mock.args = None

    user_msg = MagicMock(spec=Message)
    user_msg.from_user = TgUser(id=999888, is_bot=False, first_name="NormalUser")
    user_msg.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_start(user_msg, cmd_mock)

    user_msg.answer.assert_called_once()
    assert "ဆာဗာပြုပြင်ထိန်းသိမ်းမှု ဆောင်ရွက်နေပါသည်" in user_msg.answer.call_args[0][0]

    # 3. Admin user sends /start -> bypasses maintenance and registers/proceeds
    admin_msg = MagicMock(spec=Message)
    admin_msg.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    admin_msg.answer = AsyncMock()

    with patch("app.handlers.start.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_start(admin_msg, cmd_mock)

    admin_msg.answer.assert_called_once()
    assert "ဆာဗာပြုပြင်ထိန်းသိမ်းမှု" not in admin_msg.answer.call_args[0][0]

    # 4. Admin toggles maintenance OFF
    await handle_admin_settings_toggle_maint(cb, session=db_session)
    assert settings.maintenance_mode is False


@pytest.mark.asyncio
async def test_admin_settings_custom_input_fsm(db_session):
    """Admin can enter custom validated values via FSM."""
    settings.admin_ids_raw = "888111"

    # Start FSM input
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = "admin:settings_input:character_limit_free"

    fsm_state = MagicMock(spec=FSMContext)
    fsm_state.set_state = AsyncMock()
    fsm_state.update_data = AsyncMock()
    fsm_state.get_data = AsyncMock(return_value={"setting_key": "character_limit_free"})
    fsm_state.clear = AsyncMock()

    orig_free_chars = settings.character_limit_free
    await handle_admin_settings_input_start(cb, state=fsm_state, session=db_session)
    fsm_state.set_state.assert_called_once_with(AdminSettingsFSM.waiting_for_value)
    fsm_state.update_data.assert_called_once_with(setting_key="character_limit_free")

    # Send message with custom value "650"
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=888111, is_bot=False, first_name="Admin")
    msg.text = "650"
    msg.answer = AsyncMock()

    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    with patch("app.handlers.admin.AsyncSessionLocal", side_effect=lambda: mock_session_ctx()):
        await handle_admin_settings_input_receive(msg, state=fsm_state)

    assert settings.character_limit_free == 650
    fsm_state.clear.assert_called_once()

    # Restore the original value (current default is 1000, not 500)
    settings.character_limit_free = orig_free_chars
