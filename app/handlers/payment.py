from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.services.payments import create_payment_request, get_active_pricing_plans
from app.ui.keyboards import back_to_menu_kb

logger = logging.getLogger(__name__)
payment_router = Router(name="payment")


class PaymentFSM(StatesGroup):
    waiting_for_receipt = State()


@payment_router.callback_query(F.data == "menu:buy_minutes")
async def handle_buy_minutes_menu(callback: CallbackQuery):
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        plans = await get_active_pricing_plans(session)
        await session.commit()

    buttons = [
        [
            InlineKeyboardButton(
                text=t("btn_buy_package", lang, minutes=p.minutes, price=p.price_mmk),
                callback_data=f"buy_pkg:{p.code}",
            )
        ]
        for p in plans
    ]
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:home")])

    text = f"{t('buy_minutes_title', lang)}\n\n{t('buy_minutes_desc', lang)}\n\n{t('payment_instruction', lang)}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@payment_router.callback_query(F.data.startswith("buy_pkg:"))
async def handle_package_selected(callback: CallbackQuery, state: FSMContext):
    pkg_code = callback.data.split(":")[1]
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code

    await state.set_state(PaymentFSM.waiting_for_receipt)
    await state.update_data(package_code=pkg_code, lang=lang)

    text = t(
        "payment_selected_package",
        lang,
        package_code=pkg_code,
        payment_instruction=t("payment_instruction", lang),
    )
    await callback.message.edit_text(text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
    await callback.answer()


@payment_router.message(PaymentFSM.waiting_for_receipt, F.photo | F.document)
async def handle_receipt_received(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    pkg_code = data.get("package_code", "MIN_10")

    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    await state.clear()

    async with AsyncSessionLocal() as session:
        payment = await create_payment_request(
            session=session,
            user_id=message.from_user.id,
            package_code=pkg_code,
            receipt_file_id=file_id,
        )
        await session.commit()

    # Confirm to user
    await message.answer(
        t("payment_submitted", lang, pay_id=payment.id),
        reply_markup=back_to_menu_kb(lang),
        parse_mode="HTML",
    )

    # Notify admins
    for admin_id in settings.admin_ids:
        try:
            admin_text = t(
                "payment_admin_notification",
                "en",
                pay_id=payment.id,
                user_id=message.from_user.id,
                username=message.from_user.username or "none",
                package_code=payment.package_code,
                minutes=payment.minutes,
                amount_mmk=payment.amount_mmk,
            )
            admin_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=t("btn_admin_pay_approve", "en"), callback_data=f"admin:pay_approve:{payment.id}"),
                        InlineKeyboardButton(text=t("btn_admin_pay_reject", "en"), callback_data=f"admin:pay_reject:{payment.id}"),
                    ]
                ]
            )
            if file_id:
                if message.photo:
                    await message.bot.send_photo(chat_id=admin_id, photo=file_id, caption=admin_text, reply_markup=admin_kb, parse_mode="HTML")
                else:
                    await message.bot.send_document(chat_id=admin_id, document=file_id, caption=admin_text, reply_markup=admin_kb, parse_mode="HTML")
            else:
                await message.bot.send_message(chat_id=admin_id, text=admin_text, reply_markup=admin_kb, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)
