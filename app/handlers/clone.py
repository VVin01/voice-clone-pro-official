from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import AccountCategory
from app.services.audio import CURATED_TTS_VOICES, SPEED_RATES
from app.services.i18n import t
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.models import get_available_models_for_user
from app.services.queue import create_voice_clone_job, get_queue_position
from app.ui.keyboards import back_to_menu_kb
from app.worker import process_single_job

logger = logging.getLogger(__name__)
clone_router = Router(name="clone")


class CloneFSM(StatesGroup):
    waiting_for_text = State()
    waiting_for_voice = State()
    waiting_for_speed = State()
    waiting_for_gender = State()
    waiting_for_model = State()


@clone_router.callback_query(F.data.in_({"menu:voice_clone", "menu:start"}))
async def handle_voice_clone_start(callback: CallbackQuery, state: FSMContext):
    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        balance = await get_minute_balance(session, user.id)
        lang = user.language_code

    is_admin = settings.is_admin(callback.from_user.id)
    if not is_admin and balance.total_usable_seconds <= 0:
        await callback.answer(t("tts_insufficient_balance", lang), show_alert=True)
        return

    if is_admin:
        # Admins are exempt from user-facing account limits (FREE/PURCHASED)
        max_chars = settings.infrastructure_safety_character_limit
        text = t("tts_admin_enter_text", lang, max_chars=max_chars)
    else:
        max_chars = (
            settings.character_limit_purchased
            if user.account_category == AccountCategory.PURCHASED.value
            else settings.character_limit_free
        )
        text = t("tts_enter_text", lang, max_chars=max_chars)

    await state.set_state(CloneFSM.waiting_for_text)
    await state.update_data(max_chars=max_chars, lang=lang, is_admin=is_admin)

    await callback.message.edit_text(text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
    await callback.answer()


@clone_router.message(CloneFSM.waiting_for_text)
async def handle_clone_text_input(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    raw_text = (message.text or "").strip()

    if not raw_text:
        return

    is_admin = settings.is_admin(message.from_user.id)
    if is_admin:
        infra_limit = settings.infrastructure_safety_character_limit
        if len(raw_text) > infra_limit:
            err = t("tts_admin_text_too_long", lang, infra_limit=infra_limit, curr_chars=len(raw_text))
            await message.answer(err, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
            return
    else:
        max_chars = data.get("max_chars", settings.character_limit_free)
        if len(raw_text) > max_chars:
            err = t("tts_text_too_long", lang, max_chars=max_chars, curr_chars=len(raw_text))
            await message.answer(err, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
            return

    await state.update_data(input_text=raw_text)
    await state.set_state(CloneFSM.waiting_for_voice)

    # Prompt Edge TTS voice selection
    buttons = [
        [InlineKeyboardButton(text=info["name"], callback_data=f"clone_voice:{code}")]
        for code, info in CURATED_TTS_VOICES.items()
    ]
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone")])

    text = t("tts_choose_base_voice", lang)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@clone_router.callback_query(F.data.startswith("clone_voice:"))
async def handle_clone_voice_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    voice_code = callback.data.split(":")[1]

    await state.update_data(edge_voice=voice_code)
    await state.set_state(CloneFSM.waiting_for_speed)

    # Prompt speed selection
    buttons = [
        [InlineKeyboardButton(text=t("speed_slow", lang), callback_data="clone_speed:slow")],
        [InlineKeyboardButton(text=t("speed_normal", lang), callback_data="clone_speed:normal")],
        [InlineKeyboardButton(text=t("speed_fast", lang), callback_data="clone_speed:fast")],
    ]
    await callback.message.edit_text(
        t("tts_choose_speed", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@clone_router.callback_query(F.data.startswith("clone_speed:"))
async def handle_clone_speed_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    speed_code = callback.data.split(":")[1]

    await state.update_data(speed_preset=speed_code)
    await state.set_state(CloneFSM.waiting_for_gender)

    # Prompt gender combination selection
    buttons = [
        [InlineKeyboardButton(text=t("preset_m2m", lang), callback_data="clone_gender:m2m")],
        [InlineKeyboardButton(text=t("preset_m2f", lang), callback_data="clone_gender:m2f")],
        [InlineKeyboardButton(text=t("preset_f2f", lang), callback_data="clone_gender:f2f")],
        [InlineKeyboardButton(text=t("preset_f2m", lang), callback_data="clone_gender:f2m")],
    ]
    await callback.message.edit_text(
        t("tts_choose_gender_preset", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@clone_router.callback_query(F.data.startswith("clone_gender:"))
async def handle_clone_gender_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    gender_code = callback.data.split(":")[1]

    await state.update_data(gender_preset=gender_code)
    await state.set_state(CloneFSM.waiting_for_model)

    # Prompt Applio model selection
    async with AsyncSessionLocal() as session:
        models = await get_available_models_for_user(session, callback.from_user.id)

    if not models:
        await callback.message.edit_text(
            t("tts_no_active_models", lang),
            reply_markup=back_to_menu_kb(lang),
        )
        await callback.answer()
        return

    buttons = [
        [InlineKeyboardButton(text=f"🎙️ {m.name}", callback_data=f"clone_model:{m.id}")]
        for m in models
    ]
    await callback.message.edit_text(
        t("tts_choose_model", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await callback.answer()


@clone_router.callback_query(F.data.startswith("clone_model:"))
async def handle_clone_model_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    model_id = int(callback.data.split(":")[1])

    input_text = data.get("input_text", "")
    edge_voice = data.get("edge_voice", "my-MM-ThihaNeural")
    speed_preset = data.get("speed_preset", "normal")
    gender_preset = data.get("gender_preset", "m2m")

    await state.clear()

    async with AsyncSessionLocal() as session:
        job, reason = await create_voice_clone_job(
            session=session,
            user_id=callback.from_user.id,
            text=input_text,
            edge_voice=edge_voice,
            model_id=model_id,
            gender_preset=gender_preset,
            speed_preset=speed_preset,
            chat_id=callback.from_user.id,
        )
        if not job:
            await callback.answer(t("tts_queue_failed", lang, reason=reason), show_alert=True)
            return

        queue_pos = await get_queue_position(session, job.id)
        status_text = t(
            "queue_status_card",
            lang,
            status_validating=t("status_validating", lang),
            queue_pos=queue_pos,
        )
        msg = await callback.message.edit_text(status_text, parse_mode="HTML")
        job.status_message_id = msg.message_id
        await session.commit()

    await callback.answer()
