from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.schema import PricingPlan
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.services.payments import (
    create_payment_request,
    get_active_pricing_plans,
    get_payment_account_info,
)
from app.ui.helpers import safe_edit_message
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

    badge_map = {10: "💎", 30: "⭐", 60: "🔥"}
    buttons = []
    for p in plans:
        badge = badge_map.get(p.minutes, "💳")
        tag = ""
        if p.minutes == 30:
            tag = " — လူကြိုက်အများဆုံး" if lang == "my" else " — Popular"
        elif p.minutes >= 60:
            tag = " — အတန်ဆုံး" if lang == "my" else " — Best Value"

        if lang == "my":
            btn_text = f"{badge} {p.minutes} မိနစ် — {p.price_mmk:,} ကျပ်{tag}"
        else:
            btn_text = f"{badge} {p.minutes} Mins — {p.price_mmk:,} MMK{tag}"

        buttons.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"buy_pkg:{p.code}",
            )
        ])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:home")])

    if lang == "my":
        text = (
            "💳 <b>မိနစ်များ ဝယ်ယူရန် (Buy Minutes)</b>\n\n"
            "ဝယ်ယူလိုသော မိနစ်ပက်ကေ့ဂျ်ကို အောက်တွင် ရွေးချယ်ပါ:\n\n"
            "✨ <b>VIP & Purchased အကျိုးခံစားခွင့်များ:</b>\n"
            "• ⏱️ ဝယ်ယူထားသော မိနစ်များသည် သက်တမ်းကုန်ဆုံးခြင်း မရှိပါ\n"
            "• 💎 <b>PURCHASED</b> အဆင့် သတ်မှတ်ချက် အလိုအလျောက် ရရှိမည်\n"
            "• 🚀 တစ်ကြိမ်လျှင် စာလုံးရေ အများဆုံး ထုတ်လုပ်နိုင်ခွင့်\n"
            "• ⚡ Voice Studio Priority Queue ဖြင့် လျင်မြန်စွာ ပြောင်းလဲပေးခြင်း\n\n"
            "<i>အောက်ပါ ခလုတ်များမှ မိမိနှစ်သက်ရာ ပက်ကေ့ဂျ်ကို နှိပ်ပါ:</i>"
        )
    else:
        text = (
            "💳 <b>Buy Minutes Packages</b>\n\n"
            "Select a minute package below:\n\n"
            "✨ <b>Purchased Tier Privileges:</b>\n"
            "• ⏱️ Purchased minutes never expire\n"
            "• 💎 Instant <b>PURCHASED</b> account status upgrade\n"
            "• 🚀 Higher character limits per generation job\n"
            "• ⚡ Priority queue processing in Voice Studio\n\n"
            "<i>Choose your preferred package to proceed:</i>"
        )

    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@payment_router.callback_query(F.data.startswith("pay_copy:"))
async def handle_payment_copy_account(callback: CallbackQuery, session: AsyncSession | None = None):
    service = callback.data.split(":")[1]
    if session is not None:
        acc_info = await get_payment_account_info(session)
    else:
        async with AsyncSessionLocal() as s:
            acc_info = await get_payment_account_info(s)

    if service == "kpay":
        acc_num = acc_info["kpay_number"]
        acc_name = acc_info["kpay_name"]
        msg = f"📋 KBZPay: {acc_num} ({acc_name}) Copied!"
    else:
        acc_num = acc_info["wave_number"]
        acc_name = acc_info["wave_name"]
        msg = f"📋 WavePay: {acc_num} ({acc_name}) Copied!"

    # Sleek floating toast banner notification at the top/center of screen
    try:
        await callback.answer(msg, show_alert=False)
    except Exception:
        pass


@payment_router.callback_query(F.data.startswith("buy_pkg:"))
async def handle_package_selected(callback: CallbackQuery, state: FSMContext):
    pkg_code = callback.data.split(":")[1]
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        acc_info = await get_payment_account_info(session)
        plan_stmt = select(PricingPlan).where(PricingPlan.code == pkg_code)
        plan = (await session.execute(plan_stmt)).scalar_one_or_none()

    pkg_minutes = plan.minutes if plan else 10
    pkg_price = plan.price_mmk if plan else 2000

    await state.set_state(PaymentFSM.waiting_for_receipt)
    await state.update_data(package_code=pkg_code, lang=lang, minutes=pkg_minutes, price=pkg_price)

    if lang == "my":
        text = (
            "🧾 <b>ငွေပေးချေမှု အချက်အလက် (Invoice)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>ရွေးချယ်ထားသော ပက်ကေ့ဂျ်:</b> <code>{pkg_code}</code>\n"
            f"⏱️ <b>ရရှိမည့် မိနစ်:</b> <b>{pkg_minutes} မိနစ်</b>\n"
            f"💰 <b>ကျသင့်ငွေ ပမာဏ:</b> <b>{pkg_price:,} ကျပ်</b>\n\n"
            "🏦 <b>လွှဲပေးချေနိုင်သော အကောင့်များ:</b>\n"
            f"• <b>KBZPay:</b> <code>{acc_info['kpay_number']}</code> ({acc_info['kpay_name']})\n"
            f"• <b>WavePay:</b> <code>{acc_info['wave_number']}</code> ({acc_info['wave_name']})\n\n"
            "ℹ️ <i>အကောင့်နံပါတ်ကို နှိပ်၍ အလွယ်တကူ Copy ကူးယူနိုင်ပါသည်။</i>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📸 <b>ငွေလွှဲပြီးပါက ငွေလွှဲပြေစာ (Screenshot) ကို ဤနေရာသို့ ပေးပို့ပေးပါ။</b>"
        )
    else:
        text = (
            "🧾 <b>Payment Invoice & Instructions</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Selected Package:</b> <code>{pkg_code}</code>\n"
            f"⏱️ <b>Credited Minutes:</b> <b>{pkg_minutes} minutes</b>\n"
            f"💰 <b>Total Payable:</b> <b>{pkg_price:,} MMK</b>\n\n"
            "🏦 <b>Official Payment Accounts:</b>\n"
            f"• <b>KBZPay:</b> <code>{acc_info['kpay_number']}</code> ({acc_info['kpay_name']})\n"
            f"• <b>WavePay:</b> <code>{acc_info['wave_number']}</code> ({acc_info['wave_name']})\n\n"
            "ℹ️ <i>Tap on the account numbers above to copy.</i>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📸 <b>Please send your transfer screenshot or receipt file below.</b>"
        )

    buttons = [
        [
            InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:buy_minutes"),
            InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
        ],
    ]

    await safe_edit_message(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


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

    # Confirm to user with modern status card
    if lang == "my":
        confirm_text = (
            "✅ <b>ငွေပေးချေမှု တောင်းဆိုချက် ပေးပို့ပြီးပါပြီ!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 <b>တောင်းဆိုမှုနံပါတ်:</b> <code>#{payment.id}</code>\n"
            f"📦 <b>ပက်ကေ့ဂျ်:</b> <b>{payment.package_code}</b> ({payment.minutes} မိနစ်)\n"
            f"💰 <b>ပမာဏ:</b> <b>{payment.amount_mmk:,} ကျပ်</b>\n"
            "📊 <b>အခြေအနေ:</b> 🟡 <b>စစ်ဆေးနေဆဲ (Under Review)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏳ Admin မှ စစ်ဆေးအတည်ပြုပြီးပါက သင့်အကောင့်ထဲသို့ မိနစ်များ အလိုအလျောက် ချက်ချင်း ထည့်သွင်းပေးပါမည်။"
        )
    else:
        confirm_text = (
            "✅ <b>Payment Request Submitted!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 <b>Request ID:</b> <code>#{payment.id}</code>\n"
            f"📦 <b>Package:</b> <b>{payment.package_code}</b> ({payment.minutes} mins)\n"
            f"💰 <b>Amount:</b> <b>{payment.amount_mmk:,} MMK</b>\n"
            "📊 <b>Status:</b> 🟡 <b>Under Review</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏳ Admin will verify and approve shortly. Your purchased minutes will be credited automatically!"
        )

    await message.answer(
        confirm_text,
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
