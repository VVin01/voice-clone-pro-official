from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.filters import StateFilter
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
    clear_payment_intent,
    create_payment_request,
    get_active_pricing_plans,
    get_payment_account_info,
    get_recent_payment_intent,
    save_payment_intent,
)
from app.ui.helpers import safe_edit_message
from app.ui.keyboards import back_to_menu_kb

logger = logging.getLogger(__name__)
payment_router = Router(name="payment")


class PaymentFSM(StatesGroup):
    waiting_for_receipt = State()


@payment_router.callback_query(F.data == "menu:buy_minutes")
async def handle_buy_minutes_menu(callback: CallbackQuery, state: FSMContext):
    # Entering the buy menu abandons any half-finished flow (e.g. a
    # pending waiting_for_receipt from an earlier invoice, reached via
    # that invoice's Back button). Without this the stale FSM state
    # survived and the next photo was treated as that old slip (F12).
    await state.clear()
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        lang = user.language_code
        plans = await get_active_pricing_plans(session)
        # Re-entering the buy menu abandons the flow — the persisted
        # selection goes with the FSM state (hunt B2).
        await clear_payment_intent(session, callback.from_user.id)
        await session.commit()

    # Maintenance: the buy flow is a write surface /start never sees
    # once the menu is open (hunt C1) — same alert as the clone entry.
    if settings.maintenance_mode and not settings.is_admin(callback.from_user.id):
        try:
            await callback.answer(t("maintenance_desc", lang), show_alert=True)
        except Exception:
            pass
        return

    # Three separate paid categories, in order: Voice-only (minute packs
    # from the DB seed), Recap-only, Combined Voice+Recap (from the plan
    # catalogue). Free tier stays separate and is not listed here.
    from app.services.payments import PLAN_CATALOGUE

    badge_map = {10: "💎", 30: "⭐", 60: "🔥"}
    buttons = []
    for p in plans:
        badge = badge_map.get(p.minutes, "💳")
        tag = ""
        if p.minutes == 30:
            tag = t("pay_tag_popular", lang)
        elif p.minutes >= 60:
            tag = t("pay_tag_best_value", lang)
        btn_text = t("pay_package_btn", lang, badge=badge, minutes=p.minutes, price=f"{p.price_mmk:,}", tag=tag)

        buttons.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"buy_pkg:{p.code}",
            )
        ])
    for entry in PLAN_CATALOGUE:
        if entry["category"] == "recap":
            buttons.append([
                InlineKeyboardButton(
                    text=t("pay_package_recap_btn", lang, credits=entry["recap_credits"], price=f"{entry['price_mmk']:,}"),
                    callback_data=f"buy_pkg:{entry['code']}",
                )
            ])
    for entry in PLAN_CATALOGUE:
        if entry["category"] == "combined":
            buttons.append([
                InlineKeyboardButton(
                    text=t("pay_package_combined_btn", lang, minutes=entry["minutes"], credits=entry["recap_credits"], price=f"{entry['price_mmk']:,}"),
                    callback_data=f"buy_pkg:{entry['code']}",
                )
            ])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:home")])

    text = t("pay_store_text", lang)

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
        service_name = "KBZPay"
    else:
        acc_num = acc_info["wave_number"]
        acc_name = acc_info["wave_name"]
        service_name = "WavePay"
    # Language for a toast: use the requester's stored language when available.
    try:
        async with AsyncSessionLocal() as _s:
            _u, _ = await get_or_create_user(_s, callback.from_user.id)
            _lang = _u.language_code
    except Exception:
        _lang = "my"
    msg = t("pay_copy_copied", _lang, service=service_name, acc_num=acc_num, acc_name=acc_name)

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

    # DB minute packs first; bundle/recap/combined codes come from the
    # plan catalogue (they are not DB PricingPlan rows). An unknown code
    # (stale keyboard, tampered callback) is rejected outright — the old
    # code defaulted to a phantom 10 min / 2,000 MMK invoice that matches
    # no real plan (hunt F12).
    pkg_recap = 0
    if plan:
        pkg_minutes, pkg_price = plan.minutes, plan.price_mmk
    else:
        from app.services.payments import get_plan_by_code

        cat = get_plan_by_code(pkg_code)
        if cat:
            pkg_minutes, pkg_price, pkg_recap = cat["minutes"], cat["price_mmk"], cat["recap_credits"]
        else:
            try:
                await callback.answer(t("pay_package_invalid", lang), show_alert=True)
            except Exception:
                pass
            return

    # Persist the selection in the DB alongside the FSM state: the
    # FSM is in-memory, so without this a restart between the tap and
    # the slip photo lost the flow entirely (hunt B2).
    async with AsyncSessionLocal() as session:
        await save_payment_intent(session, callback.from_user.id, pkg_code)
        await session.commit()

    await state.set_state(PaymentFSM.waiting_for_receipt)
    await state.update_data(package_code=pkg_code, lang=lang, minutes=pkg_minutes, price=pkg_price)

    text = t(
        "pay_invoice_text",
        lang,
        pkg_code=pkg_code,
        pkg_minutes=pkg_minutes,
        pkg_recap_credits=pkg_recap,
        pkg_price=f"{pkg_price:,}",
        kpay_number=acc_info['kpay_number'],
        kpay_name=acc_info['kpay_name'],
        wave_number=acc_info['wave_number'],
        wave_name=acc_info['wave_name'],
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


async def _finalize_receipt_submission(message: Message, payment, file_id, lang: str) -> None:
    """Confirm to the user and notify admins — shared by the live-FSM
    slip handler and the post-restart catch-all (hunt B2), so a
    resumed flow is indistinguishable from an uninterrupted one."""
    # Confirm to user with modern status card
    confirm_text = t(
        "pay_receipt_submitted",
        lang,
        payment_id=payment.id,
        package_code=payment.package_code,
        minutes=payment.minutes,
        amount=f"{payment.amount_mmk:,}",
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
        # The slip exists now; the persisted selection has done its job.
        await clear_payment_intent(session, message.from_user.id)
        await session.commit()

    await _finalize_receipt_submission(message, payment, file_id, lang)


@payment_router.message(StateFilter(None), F.photo | F.document)
async def handle_receipt_after_restart(message: Message):
    """Slip photo/document with NO live FSM state (hunt B2).

    The FSM is in-memory: if the bot restarted (deploy) after the
    user picked a package but before they sent the slip, the photo
    used to meet silence. When a recent (<24h) persisted intent
    exists, resume the slip flow from it — create the payment
    request from the persisted plan, confirm to the user and notify
    admins, exactly like the uninterrupted flow. With no intent,
    stay silent (this catch-all must not swallow unrelated media).

    StateFilter(None) also guarantees this never steals media from
    users mid-flow in OTHER routers (clone/custom-voice states).
    Registered after handle_receipt_received, which owns the
    in-state path.
    """
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    payment = None
    resume_failed = False
    lang = "my"
    async with AsyncSessionLocal() as session:
        intent = await get_recent_payment_intent(session, message.from_user.id)
        if intent is None:
            return
        user, _ = await get_or_create_user(session, message.from_user.id)
        lang = user.language_code or "my"
        intent_code = intent.package_code  # read now: a rollback expires the row object
        try:
            payment = await create_payment_request(
                session=session,
                user_id=message.from_user.id,
                package_code=intent_code,
                receipt_file_id=file_id,
            )
        except Exception as exc:
            # Poisoned intent (hunt #5): the persisted plan no longer
            # exists (removed from the DB/catalogue after the user
            # picked it), so the resume can NEVER succeed -- and left
            # in place, the intent would re-poison every later photo
            # the same way while the user hears only silence. Clear
            # the intent, keep the failure visible in the log, and
            # tell the user how to restart the purchase.
            await session.rollback()
            logger.warning(
                "Persisted payment intent for user %s is unusable (plan %s): %s",
                message.from_user.id, intent_code, exc,
            )
            await clear_payment_intent(session, message.from_user.id)
            await session.commit()
            resume_failed = True
        if not resume_failed:
            await clear_payment_intent(session, message.from_user.id)
            await session.commit()

    if resume_failed:
        await message.answer(t("payment_resume_failed", lang), parse_mode="HTML")
        return

    logger.info(
        "Resumed slip flow after restart for user %s (intent plan %s)",
        message.from_user.id, payment.package_code,
    )
    await _finalize_receipt_submission(message, payment, file_id, lang)


@payment_router.message(PaymentFSM.waiting_for_receipt)
async def handle_receipt_wrong_content(message: Message, state: FSMContext):
    """Anything that is not a photo/document while waiting for a slip.

    Registered AFTER handle_receipt_received, so photos/documents match
    that handler first. Previously text/stickers/etc. in this state met
    silence and users could not tell whether the bot was waiting (F12).
    The state is left untouched so the slip can still be sent.
    """
    data = await state.get_data()
    lang = data.get("lang", "my")
    await message.answer(t("pay_receipt_wrong_content", lang), parse_mode="HTML")
