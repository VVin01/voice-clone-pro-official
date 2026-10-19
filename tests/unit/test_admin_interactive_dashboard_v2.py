from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import PricingPlan, User, VoiceModel
from app.handlers.admin import (
    AdminModelRenameFSM,
    AdminPriceEditFSM,
    AdminSettingsFSM,
    handle_admin_model_rename_receive,
    handle_admin_model_rename_start,
    handle_admin_model_toggle,
    handle_admin_pay_accounts_menu,
    handle_admin_pay_field_edit,
    handle_admin_price_custom,
    handle_admin_price_custom_receive,
    handle_admin_price_edit,
    handle_admin_price_set,
    handle_admin_pricing,
    handle_admin_settings_input_receive,
    handle_admin_user_quick_credit,
    handle_admin_user_toggle_status,
    handle_admin_user_toggle_tier,
)
from app.handlers.payment import handle_payment_copy_account, handle_package_selected
from app.services.dynamic_settings import get_dynamic_setting
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.payments import get_active_pricing_plans


class FakeFSMContext:
    def __init__(self, initial_state=None, initial_data=None):
        self.current_state = initial_state
        self.data = initial_data or {}

    async def set_state(self, state):
        self.current_state = state

    async def get_state(self):
        return self.current_state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def clear(self):
        self.current_state = None
        self.data = {}


@pytest.mark.asyncio
async def test_admin_interactive_pricing_preset_and_custom(db_session):
    admin_id = 999888
    settings.admin_ids_raw = str(admin_id)
    await get_active_pricing_plans(db_session)

    # 1. Preset price update
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb.data = "admin:price_set:MIN_10:3500"
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    await handle_admin_price_set(cb, session=db_session)

    # Toast banner verification (show_alert=False)
    cb.answer.assert_any_call("✅ Price updated: MIN_10 = 3,500 MMK", show_alert=False)

    plan = (await db_session.execute(select(PricingPlan).where(PricingPlan.code == "MIN_10"))).scalar_one()
    assert plan.price_mmk == 3500

    # 2. Custom price update via FSM
    state = FakeFSMContext(
        initial_state=AdminPriceEditFSM.waiting_for_price,
        initial_data={"package_code": "MIN_10"},
    )
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg.text = "4,200"
    msg.answer = AsyncMock()

    await handle_admin_price_custom_receive(msg, state, session=db_session)

    assert await state.get_state() is None
    await db_session.refresh(plan)
    assert plan.price_mmk == 4200
    msg.answer.assert_called_once()
    assert "4,200 MMK" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_interactive_payment_accounts(db_session):
    admin_id = 999888
    settings.admin_ids_raw = str(admin_id)

    # 1. Edit payment account via FSM
    state = FakeFSMContext(
        initial_state=AdminSettingsFSM.waiting_for_value,
        initial_data={"setting_key": "payment_kpay_number", "return_to": "admin:pricing"},
    )
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg.text = "09123456789"
    msg.answer = AsyncMock()

    await handle_admin_settings_input_receive(msg, state, session=db_session)

    assert await state.get_state() is None
    val = await get_dynamic_setting(db_session, "payment_kpay_number")
    assert val == "09123456789"

    # User copy button receives updated account in floating toast
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=111, is_bot=False, first_name="NormalUser")
    cb.data = "pay_copy:kpay"
    cb.answer = AsyncMock()

    await handle_payment_copy_account(cb, session=db_session)
    cb.answer.assert_called_once()
    assert "09123456789" in cb.answer.call_args[0][0]
    assert cb.answer.call_args[1].get("show_alert") is False


@pytest.mark.asyncio
async def test_admin_voice_model_rename(db_session):
    admin_id = 999888
    settings.admin_ids_raw = str(admin_id)

    model = VoiceModel(
        name="Old Singer Voice",
        slug="singer-voice",
        model_dir="weights/singer",
        status=VoiceModelStatus.ACTIVE.value,
        type=VoiceModelType.SYSTEM.value,
        pth_path="weights/singer.pth",
    )
    db_session.add(model)
    await db_session.commit()
    await db_session.refresh(model)

    state = FakeFSMContext(
        initial_state=AdminModelRenameFSM.waiting_for_name,
        initial_data={"model_id": model.id},
    )
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    msg.text = "Studio Pro Singer"
    msg.answer = AsyncMock()

    await handle_admin_model_rename_receive(msg, state, session=db_session)

    assert await state.get_state() is None
    await db_session.refresh(model)
    assert model.name == "Studio Pro Singer"
    msg.answer.assert_called_once()
    assert "Studio Pro Singer" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_user_tier_and_quick_credit(db_session):
    admin_id = 999888
    settings.admin_ids_raw = str(admin_id)

    target_user, _ = await get_or_create_user(db_session, telegram_id=888777)
    assert target_user.account_category == AccountCategory.FREE.value

    # 1. Switch Tier: FREE -> PURCHASED
    cb_tier = MagicMock(spec=CallbackQuery)
    cb_tier.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_tier.data = f"admin:user_tier:{target_user.id}:1"
    cb_tier.message = MagicMock(spec=Message)
    cb_tier.message.edit_text = AsyncMock()
    cb_tier.answer = AsyncMock()

    await handle_admin_user_toggle_tier(cb_tier, session=db_session)

    # Toast banner verification
    cb_tier.answer.assert_called_with("⭐ User tier switched to PURCHASED!", show_alert=False)
    await db_session.refresh(target_user)
    assert target_user.account_category == AccountCategory.PURCHASED.value

    # 2. Quick Credit +10m Bonus
    cb_credit = MagicMock(spec=CallbackQuery)
    cb_credit.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_credit.data = f"admin:user_quick_credit:{target_user.id}:10:1"
    cb_credit.message = MagicMock(spec=Message)
    cb_credit.message.edit_text = AsyncMock()
    cb_credit.answer = AsyncMock()

    await handle_admin_user_quick_credit(cb_credit, session=db_session)

    cb_credit.answer.assert_called_with("⚡ Added +10m credit successfully!", show_alert=False)
    bal = await get_minute_balance(db_session, target_user.id)
    assert bal.purchased_seconds == 600
    assert bal.purchased_minutes == 10.0


@pytest.mark.asyncio
async def test_operational_action_toast_notifications(db_session):
    admin_id = 999888
    settings.admin_ids_raw = str(admin_id)

    target_user, _ = await get_or_create_user(db_session, telegram_id=888999)

    # User Block/Unblock status update uses floating toast
    cb_status = MagicMock(spec=CallbackQuery)
    cb_status.from_user = TgUser(id=admin_id, is_bot=False, first_name="Admin")
    cb_status.data = f"admin:user_status:{target_user.id}:BLOCK:1"
    cb_status.message = MagicMock(spec=Message)
    cb_status.message.edit_text = AsyncMock()
    cb_status.answer = AsyncMock()

    await handle_admin_user_toggle_status(cb_status, session=db_session)

    # Asserts that show_alert=False was passed for floating banner
    assert cb_status.answer.call_args[1].get("show_alert") is False
