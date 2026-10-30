from __future__ import annotations

import html
import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services.custom_voice import (
    create_custom_voice_request,
    get_user_custom_voice_quota,
)
from app.services.gate import build_gate_reminder_text, is_gate_configured, verify_user_membership
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.ui.helpers import safe_edit_message
from app.ui.keyboards import (
    back_to_menu_kb,
    custom_voice_cancel_kb,
    custom_voice_hub_kb,
    custom_voice_vip_promo_kb,
    membership_gate_kb,
)

logger = logging.getLogger(__name__)
custom_voice_router = Router(name="custom_voice")


class CustomVoiceFSM(StatesGroup):
    waiting_for_name = State()
    waiting_for_sample = State()


@custom_voice_router.callback_query(F.data == "menu:custom_voice")
async def handle_custom_voice_menu(
    callback: CallbackQuery,
    state: FSMContext | None = None,
    session: AsyncSession | None = None,
):
    if state is not None:
        await state.clear()

    user_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        lang = user.language_code
        gate_ok, _ = await verify_user_membership(session, user, callback.bot)
        gate_ch, gate_gp = user.channel_verified, user.group_verified
        quota = await get_user_custom_voice_quota(session, user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            gate_ok, _ = await verify_user_membership(s, user, callback.bot)
            gate_ch, gate_gp = user.channel_verified, user.group_verified
            await s.commit()
            quota = await get_user_custom_voice_quota(s, user_id)

    # Membership gate at this entry point too: non-members get the shared
    # join reminder (per-chat status + join buttons + live re-check),
    # never the custom-voice flow. Live check, fail closed — mirrors
    # /start. Only when the gate is configured (see clone.py note).
    if not gate_ok and is_gate_configured():
        await safe_edit_message(
            callback,
            build_gate_reminder_text(lang, gate_ch, gate_gp),
            reply_markup=membership_gate_kb(
                channel_link=settings.channel_invite_link,
                group_link=settings.group_invite_link,
                lang=lang,
            ),
            parse_mode="HTML",
        )
        try:
            await callback.answer()
        except Exception:
            pass
        return

    # Free users see VIP promotion card
    if not (quota["is_purchased"] or quota["is_admin"]):
        text = t("custom_voice_vip_promo", lang)
        await safe_edit_message(
            callback,
            text,
            reply_markup=custom_voice_vip_promo_kb(lang),
            parse_mode="HTML",
        )
        try:
            await callback.answer()
        except Exception:
            pass
        return

    # Purchased users and Admin see Custom Voice Hub
    text = t(
        "custom_voice_hub_title",
        lang,
        max_allowed=quota["max_allowed"],
        slots_used=quota["slots_used"],
        remaining_slots=quota["remaining_slots"],
    )

    if quota["active_models"]:
        models_str = "\n".join([f"• 🎙️ <b>{html.escape(m.name)}</b>" for m in quota["active_models"]])
        text += t("custom_voice_active_models_list", lang, models_list=models_str)

    if quota["pending_requests"]:
        pending_str = "\n".join(
            [f"• ⏳ <code>{r.id}</code> - <b>{html.escape(r.model_name)}</b>" for r in quota["pending_requests"]]
        )
        text += t("custom_voice_pending_requests_list", lang, pending_list=pending_str)

    await safe_edit_message(
        callback,
        text,
        reply_markup=custom_voice_hub_kb(can_request=quota["can_request"], lang=lang),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@custom_voice_router.callback_query(F.data == "cvr:request_start")
async def handle_cvr_request_start(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    user_id = callback.from_user.id
    if session is not None:
        user, _ = await get_or_create_user(session, user_id)
        lang = user.language_code
        quota = await get_user_custom_voice_quota(session, user_id)
    else:
        async with AsyncSessionLocal() as s:
            user, _ = await get_or_create_user(s, user_id)
            lang = user.language_code
            quota = await get_user_custom_voice_quota(s, user_id)

    if not quota["can_request"]:
        try:
            await callback.answer(
                t("cvr_limit_reached", lang),
                show_alert=True,
            )
        except Exception:
            pass
        return

    await state.set_state(CustomVoiceFSM.waiting_for_name)
    await state.update_data(lang=lang)

    text = t("custom_voice_prompt_name", lang)
    await safe_edit_message(
        callback,
        text,
        reply_markup=custom_voice_cancel_kb(lang),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@custom_voice_router.message(CustomVoiceFSM.waiting_for_name, F.text)
async def handle_cvr_name_input(message: Message, state: FSMContext):
    model_name = message.text.strip()
    if not model_name or len(model_name) > 100:
        await message.answer(t("cvr_name_length", lang))
        return

    data = await state.get_data()
    lang = data.get("lang", "my")

    await state.update_data(model_name=model_name)
    await state.set_state(CustomVoiceFSM.waiting_for_sample)

    text = t("custom_voice_prompt_sample", lang, model_name=html.escape(model_name))
    await message.answer(
        text,
        reply_markup=custom_voice_cancel_kb(lang),
        parse_mode="HTML",
    )


@custom_voice_router.message(CustomVoiceFSM.waiting_for_sample, F.voice | F.audio | F.document)
async def handle_cvr_sample_input(message: Message, state: FSMContext):
    data = await state.get_data()
    model_name = data.get("model_name", "Custom Voice")
    lang = data.get("lang", "my")

    sample_file_id: str
    sample_file_name: str | None = None
    sample_duration: int | None = None

    if message.voice:
        sample_file_id = message.voice.file_id
        sample_file_name = "voice_sample.ogg"
        sample_duration = message.voice.duration
    elif message.audio:
        sample_file_id = message.audio.file_id
        sample_file_name = message.audio.file_name or "audio_sample.mp3"
        sample_duration = message.audio.duration
    elif message.document:
        doc = message.document
        mime = (doc.mime_type or "").lower()
        fname = (doc.file_name or "").lower()
        is_audio = mime.startswith("audio/") or any(fname.endswith(ext) for ext in [".wav", ".mp3", ".ogg", ".m4a", ".flac"])
        if not is_audio:
            await message.answer(t("cvr_audio_required", lang))
            return
        sample_file_id = doc.file_id
        sample_file_name = doc.file_name or "audio_file"
        sample_duration = None
    else:
        return

    # Create request in DB
    async with AsyncSessionLocal() as session:
        success, req, err = await create_custom_voice_request(
            session=session,
            user_id=message.from_user.id,
            model_name=model_name,
            sample_file_id=sample_file_id,
            sample_file_name=sample_file_name,
            sample_duration_seconds=sample_duration,
        )
        await session.commit()

    if not success or not req:
        await state.clear()
        err_msg = t("cvr_create_failed", lang)
        await message.answer(err_msg, reply_markup=back_to_menu_kb(lang))
        return

    await state.clear()

    # Success confirmation to user
    confirm_text = t(
        "custom_voice_request_success",
        lang,
        request_id=req.id,
        model_name=html.escape(model_name),
        file_name=html.escape(sample_file_name or "audio"),
        duration=str(sample_duration) if sample_duration else "N/A",
    )
    await message.answer(confirm_text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")

    # Alert admins
    admin_text = t(
        "custom_voice_request_admin_alert",
        "en",
        request_id=req.id,
        user_id=message.from_user.id,
        user_name=html.escape(message.from_user.full_name or "N/A"),
        model_name=html.escape(model_name),
        file_name=html.escape(sample_file_name or "audio"),
        duration=str(sample_duration) if sample_duration else "N/A",
    )
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("custom_voice_admin_btn_review", "en"),
                    callback_data=f"admin:cvr:detail:{req.id}",
                )
            ]
        ]
    )

    for admin_id in settings.admin_ids:
        try:
            await message.bot.send_message(admin_id, admin_text, reply_markup=admin_kb, parse_mode="HTML")
            if message.voice:
                await message.bot.send_voice(admin_id, sample_file_id, caption=t("cvr_admin_sample_caption", "en", request_id=req.id))
            elif message.audio:
                await message.bot.send_audio(admin_id, sample_file_id, caption=t("cvr_admin_sample_caption", "en", request_id=req.id))
            elif message.document:
                await message.bot.send_document(admin_id, sample_file_id, caption=t("cvr_admin_sample_caption", "en", request_id=req.id))
        except Exception as e:
            logger.warning("Failed to alert admin %s of custom voice request %s: %s", admin_id, req.id, e)


@custom_voice_router.callback_query(F.data == "cvr:cancel")
async def handle_cvr_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
):
    await state.clear()
    await handle_custom_voice_menu(callback, state=None, session=session)
