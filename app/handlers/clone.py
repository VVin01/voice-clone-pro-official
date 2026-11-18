from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo, FSInputFile, BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import AccountCategory, JobStatus
from app.models.schema import Job, VoiceModel
from app.services.audio import CURATED_TTS_VOICES, SPEED_RATES
from app.services.download_names import branded_download_filename
from app.services.gate import build_gate_reminder_text, is_gate_configured, verify_user_membership
from app.services.i18n import t
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.models import get_displayable_models_for_user
from app.services.myanmar_text import convert_zawgyi_to_unicode
from app.services.queue import create_voice_clone_job, get_queue_position
from app.ui.helpers import safe_edit_message
from app.ui.keyboards import back_to_menu_kb, membership_gate_kb
from app.worker import process_single_job

from pathlib import Path
import uuid

logger = logging.getLogger(__name__)
clone_router = Router(name="clone")


class CloneFSM(StatesGroup):
    waiting_for_text = State()
    waiting_for_voice = State()
    waiting_for_speed = State()
    waiting_for_gender = State()
    waiting_for_model = State()
    waiting_for_voxcpm_reference = State()  # VoxCPM: optional reference audio step


@clone_router.callback_query(F.data.in_({"menu:voice_clone", "menu:start"}))
async def handle_voice_clone_start(callback: CallbackQuery, state: FSMContext):
    # Clean up any lingering temp input audio from aborted session
    old_data = await state.get_data()
    old_audio = old_data.get("source_audio_path")
    if old_audio:
        try:
            p = Path(old_audio)
            if p.is_file() and "temp" in str(p):
                p.unlink(missing_ok=True)
        except Exception:
            pass

    async with AsyncSessionLocal() as session:
        user, _ = await get_or_create_user(session, callback.from_user.id)
        balance = await get_minute_balance(session, user.id)
        lang = user.language_code

    is_admin = settings.is_admin(callback.from_user.id)
    if settings.maintenance_mode and not is_admin:
        await callback.answer(t("maintenance_desc", lang), show_alert=True)
        return

    # Batch AW: clone entry checks the clone-spendable balance —
    # referral seconds are TTS-only now and cannot open the clone flow.
    if not is_admin and balance.clone_spendable_seconds <= 0:
        await callback.answer(t("tts_insufficient_balance", lang), show_alert=True)
        return

    # Membership gate at the generate entry point: a user who taps an old
    # menu message without having joined BOTH chats gets the shared join
    # reminder (per-chat status + both join buttons + live re-check)
    # instead of entering the clone flow. Live check, fail closed —
    # mirrors /start; admins bypass inside verify_user_membership. Only
    # runs when the gate is configured (an unconfigured gate has nothing
    # valid to point the user at — the Mini App reports that case as
    # GATE_NOT_CONFIGURED instead).
    if not is_admin and is_gate_configured():
        async with AsyncSessionLocal() as gate_session:
            gate_user, _ = await get_or_create_user(gate_session, callback.from_user.id)
            gate_ok, _ = await verify_user_membership(gate_session, gate_user, callback.bot)
            gate_ch, gate_gp = gate_user.channel_verified, gate_user.group_verified
            gate_lang = gate_user.language_code
            await gate_session.commit()
        if not gate_ok:
            await safe_edit_message(
                callback,
                build_gate_reminder_text(gate_lang, gate_ch, gate_gp),
                reply_markup=membership_gate_kb(
                    channel_link=settings.channel_invite_link,
                    group_link=settings.group_invite_link,
                    lang=gate_lang,
                ),
                parse_mode="HTML",
            )
            try:
                await callback.answer()
            except Exception:
                pass
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
    await state.update_data(max_chars=max_chars, lang=lang, is_admin=is_admin, source_audio_path=None)

    await safe_edit_message(callback, text, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


@clone_router.message(CloneFSM.waiting_for_text, F.voice | F.audio | F.document)
async def handle_clone_audio_input(message: Message, state: FSMContext, session: AsyncSession | None = None):
    data = await state.get_data()
    lang = data.get("lang", "my")
    is_admin = data.get("is_admin", settings.is_admin(message.from_user.id))

    file_id = None
    duration = 0
    filename = "input_audio.wav"

    if message.voice:
        file_id = message.voice.file_id
        duration = message.voice.duration or 0
        filename = f"voice_{message.from_user.id}.ogg"
    elif message.audio:
        file_id = message.audio.file_id
        duration = message.audio.duration or 0
        filename = message.audio.file_name or f"audio_{message.from_user.id}.mp3"
    elif message.document:
        doc_mime = message.document.mime_type or ""
        doc_name = message.document.file_name or ""
        valid_exts = (".wav", ".mp3", ".ogg", ".oga", ".m4a", ".aac", ".flac")
        if doc_mime.startswith("audio") or any(doc_name.lower().endswith(ext) for ext in valid_exts):
            file_id = message.document.file_id
            filename = doc_name or f"doc_{message.from_user.id}.wav"
        else:
            await message.answer(t("audio_input_invalid_format", lang), reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
            return

    if not file_id:
        return

    # Check balance for non-admins if duration is known
    if not is_admin and duration > 0:
        if session is not None:
            balance = await get_minute_balance(session, message.from_user.id)
        else:
            async with AsyncSessionLocal() as s:
                balance = await get_minute_balance(s, message.from_user.id)

        if balance.clone_spendable_seconds < duration:
            await message.answer(
                t("audio_input_insufficient_balance", lang, duration=duration, balance=balance.clone_spendable_seconds),
                reply_markup=back_to_menu_kb(lang),
                parse_mode="HTML",
            )
            return

    # Download file to storage/temp/inputs/
    inputs_dir = settings.storage_root / "temp" / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    clean_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "audio.wav"
    dest_path = inputs_dir / f"input_{uuid.uuid4().hex[:12]}_{clean_name}"

    try:
        tg_file = await message.bot.get_file(file_id)
        await message.bot.download_file(tg_file.file_path, destination=dest_path)
    except Exception as exc:
        logger.error("Failed to download input audio from user %s: %s", message.from_user.id, exc)
        await message.answer(t("audio_input_download_failed", lang), reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
        return

    caption_or_name = message.caption or f"Audio: {clean_name}"
    await state.update_data(
        input_type="audio",
        source_audio_path=str(dest_path),
        input_text=caption_or_name,
    )
    # Advance state to waiting_for_gender (skip Edge TTS voice & speed selection!)
    await state.set_state(CloneFSM.waiting_for_gender)

    buttons = [
        [InlineKeyboardButton(text=t("preset_m2m", lang), callback_data="clone_gender:m2m")],
        [InlineKeyboardButton(text=t("preset_m2f", lang), callback_data="clone_gender:m2f")],
        [InlineKeyboardButton(text=t("preset_f2f", lang), callback_data="clone_gender:f2f")],
        [InlineKeyboardButton(text=t("preset_f2m", lang), callback_data="clone_gender:f2m")],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone")],
    ]
    await message.answer(
        t("tts_choose_gender_preset_audio", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@clone_router.message(CloneFSM.waiting_for_text)
async def handle_clone_text_input(message: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    raw_text = (message.text or "").strip()
    # Encoding repair before this handler's own length checks
    # (hunt #4 A1): Zawgyi-typed text becomes Unicode here, and again
    # idempotently inside create_voice_clone_job for the API surface.
    raw_text = convert_zawgyi_to_unicode(raw_text)

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

    # Prompt Edge TTS voice selection in balanced 2-column layout
    voice_buttons: list[list[InlineKeyboardButton]] = []
    v_row: list[InlineKeyboardButton] = []
    for code, info in CURATED_TTS_VOICES.items():
        icon = "👨" if info.get("gender") == "male" else "👩"
        v_row.append(InlineKeyboardButton(text=f"{icon} {info['name']}", callback_data=f"clone_voice:{code}"))
        if len(v_row) == 2:
            voice_buttons.append(v_row)
            v_row = []
    if v_row:
        voice_buttons.append(v_row)
    voice_buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone")])

    text = t("tts_choose_base_voice", lang)
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=voice_buttons), parse_mode="HTML")


@clone_router.callback_query(F.data.startswith("clone_voice:"))
async def handle_clone_voice_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    voice_code = callback.data.split(":")[1]

    await state.update_data(edge_voice=voice_code)
    await state.set_state(CloneFSM.waiting_for_speed)

    # Prompt speed selection in sleek single-row trio + back button
    buttons = [
        [
            InlineKeyboardButton(text=t("speed_slow", lang), callback_data="clone_speed:slow"),
            InlineKeyboardButton(text=t("speed_normal", lang), callback_data="clone_speed:normal"),
            InlineKeyboardButton(text=t("speed_fast", lang), callback_data="clone_speed:fast"),
        ],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone")],
    ]
    await safe_edit_message(
        callback,
        t("tts_choose_speed", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@clone_router.callback_query(F.data.startswith("clone_speed:"))
async def handle_clone_speed_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    speed_code = callback.data.split(":")[1]

    await state.update_data(speed_preset=speed_code)
    await state.set_state(CloneFSM.waiting_for_gender)

    # Prompt gender combination selection with back button
    buttons = [
        [InlineKeyboardButton(text=t("preset_m2m", lang), callback_data="clone_gender:m2m")],
        [InlineKeyboardButton(text=t("preset_m2f", lang), callback_data="clone_gender:m2f")],
        [InlineKeyboardButton(text=t("preset_f2f", lang), callback_data="clone_gender:f2f")],
        [InlineKeyboardButton(text=t("preset_f2m", lang), callback_data="clone_gender:f2m")],
        [InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone")],
    ]
    await safe_edit_message(
        callback,
        t("tts_choose_gender_preset", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@clone_router.callback_query(F.data.startswith("clone_gender:"))
async def handle_clone_gender_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    gender_code = callback.data.split(":")[1]

    await state.update_data(gender_preset=gender_code)
    await state.set_state(CloneFSM.waiting_for_model)

    # Prompt voice model selection (VoxCPM2 only for new jobs)
    async with AsyncSessionLocal() as session:
        models = await get_displayable_models_for_user(session, callback.from_user.id)

    if not models:
        await safe_edit_message(
            callback,
            t("tts_no_active_models", lang),
            reply_markup=back_to_menu_kb(lang),
        )
        try:
            await callback.answer()
        except Exception:
            pass
        return

    # Symmetrical 2-column model grid with VIP indicators + back button
    buttons: list[list[InlineKeyboardButton]] = []
    m_row: list[InlineKeyboardButton] = []
    for m in models:
        if m.name.startswith(("🎙️", "👑", "⭐", "⚡", "🎭")):
            label = m.name
        elif getattr(m, "type", None) == "PERSONAL" or getattr(m, "owner_user_id", None) == callback.from_user.id:
            label = f"👑 {m.name}"
        elif getattr(m, "is_free_default", False):
            label = f"⭐ {m.name}"
        else:
            label = f"🎙️ {m.name}"
        m_row.append(InlineKeyboardButton(text=label, callback_data=f"clone_model:{m.id}"))
        if len(m_row) == 2:
            buttons.append(m_row)
            m_row = []
    if m_row:
        buttons.append(m_row)
    buttons.append([
        InlineKeyboardButton(text=t("btn_menu_voice_samples", lang), callback_data="menu:voice_samples"),
        InlineKeyboardButton(text=t("btn_menu_custom_voice", lang), callback_data="menu:custom_voice"),
    ])
    buttons.append([
        InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone"),
    ])
    await safe_edit_message(
        callback,
        t("tts_choose_model", lang),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@clone_router.callback_query(F.data.startswith("clone_model:"))
async def handle_clone_model_selected(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "my")
    model_id = int(callback.data.split(":")[1])

    # ── Detect if this is a VoxCPM model ─────────────────────────────────────
    async with AsyncSessionLocal() as session:
        from app.models.schema import VoiceModel
        model_obj = await session.get(VoiceModel, model_id)

    is_voxcpm_model = False
    if model_obj:
        is_voxcpm_model = (
            getattr(model_obj, "pth_path", "") == "voxcpm"
            or "voxcpm" in getattr(model_obj, "slug", "").lower()
        )

    # ── VoxCPM 2 flow: ask for optional reference audio first ─────────────────
    if is_voxcpm_model:
        await state.update_data(model_id=model_id)
        await state.set_state(CloneFSM.waiting_for_voxcpm_reference)

        ref_prompt = t("clone_ref_prompt", lang)
        skip_btn = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=t("clone_skip_default", lang),
                callback_data="voxcpm_ref:skip"
            )],
            [InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:voice_clone")],
        ])
        await callback.message.answer(ref_prompt, reply_markup=skip_btn, parse_mode="HTML")
        try:
            await callback.answer()
        except Exception:
            pass
        return

    # ── Voice v2: legacy trained-model flow is disabled. ─────────────────────
    await safe_edit_message(
        callback,
        t("legacy_model_disabled", lang),
        reply_markup=back_to_menu_kb(lang),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass
    return
    input_text = data.get("input_text", "")
    source_audio_path = data.get("source_audio_path")
    gender_preset = data.get("gender_preset", "m2m")
    default_voice = "my-MM-NilarNeural" if gender_preset in ("m2f", "f2f") else "my-MM-ThihaNeural"
    edge_voice = data.get("edge_voice") or default_voice
    speed_preset = data.get("speed_preset", "normal")

    await state.clear()

    await _submit_voice_job(
        callback=callback,
        lang=lang,
        model_id=model_id,
        input_text=input_text,
        source_audio_path=source_audio_path,
        edge_voice=edge_voice,
        speed_preset=speed_preset,
        gender_preset=gender_preset,
    )


# ── VoxCPM: Receive Reference Audio ──────────────────────────────────────────

@clone_router.message(
    CloneFSM.waiting_for_voxcpm_reference,
    F.voice | F.audio | F.document
)
async def handle_voxcpm_reference_audio(message: Message, state: FSMContext):
    """Handle user-uploaded reference audio for VoxCPM voice cloning."""
    data = await state.get_data()
    lang = data.get("lang", "my")
    model_id = data.get("model_id")

    file_id = None
    filename = "reference.wav"

    if message.voice:
        file_id = message.voice.file_id
        filename = f"voxcpm_ref_{message.from_user.id}.ogg"
    elif message.audio:
        file_id = message.audio.file_id
        filename = message.audio.file_name or f"voxcpm_ref_{message.from_user.id}.mp3"
    elif message.document:
        doc_mime = message.document.mime_type or ""
        doc_name = message.document.file_name or ""
        valid_exts = (".wav", ".mp3", ".ogg", ".oga", ".m4a", ".aac", ".flac")
        if doc_mime.startswith("audio") or any(doc_name.lower().endswith(ext) for ext in valid_exts):
            file_id = message.document.file_id
            filename = doc_name or f"voxcpm_ref_{message.from_user.id}.wav"
        else:
            await message.answer(
                "❌ ဤဖိုင်ကို လက်မခံနိုင်ပါ။ WAV, MP3 သို့မဟုတ် OGG ဖိုင်ကို ပေးပို့ပေးပါ။",
                reply_markup=back_to_menu_kb(lang),
                parse_mode="HTML",
            )
            return

    if not file_id:
        return

    # Download reference audio to temp location
    ref_dir = settings.storage_root / "temp" / "voxcpm_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    clean_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "reference.wav"
    ref_path = ref_dir / f"ref_{uuid.uuid4().hex[:12]}_{clean_name}"

    try:
        tg_file = await message.bot.get_file(file_id)
        await message.bot.download_file(tg_file.file_path, destination=ref_path)
    except Exception as exc:
        logger.error("Failed to download VoxCPM reference audio from user %s: %s", message.from_user.id, exc)
        await message.answer(
            "❌ Reference Audio ဒေါင်းလုဒ် မအောင်မြင်ပါ။ ထပ်မံ ကြိုးစားပါ။",
            reply_markup=back_to_menu_kb(lang),
            parse_mode="HTML",
        )
        return

    # Retrieve FSM data and submit job
    input_text = data.get("input_text", "")
    gender_preset = data.get("gender_preset", "m2m")
    default_voice = "my-MM-NilarNeural" if gender_preset in ("m2f", "f2f") else "my-MM-ThihaNeural"
    edge_voice = data.get("edge_voice") or default_voice
    speed_preset = data.get("speed_preset", "normal")
    await state.clear()

    confirm_msg = await message.answer(
        f"✅ Reference Audio ရရှိပါပြီ <code>{clean_name}</code>\n"
        "⏳ အသံ Clone ပြုလုပ်ရန် စောင့်ဆိုင်းစာရင်းသို့ ထည့်နေပါသည်…",
        parse_mode="HTML",
    )

    await _submit_voxcpm_job(
        message=message,
        status_message=confirm_msg,
        lang=lang,
        model_id=model_id,
        input_text=input_text,
        source_audio_path=str(ref_path),  # Reference audio = source_audio_path for VoxCPM
        edge_voice=edge_voice,
        speed_preset=speed_preset,
        gender_preset=gender_preset,
    )


@clone_router.callback_query(F.data == "voxcpm_ref:skip")
async def handle_voxcpm_reference_skip(callback: CallbackQuery, state: FSMContext):
    """Skip reference audio — use default Burmese AI voice style."""
    data = await state.get_data()
    lang = data.get("lang", "my")
    model_id = data.get("model_id")
    input_text = data.get("input_text", "")
    gender_preset = data.get("gender_preset", "m2m")
    default_voice = "my-MM-NilarNeural" if gender_preset in ("m2f", "f2f") else "my-MM-ThihaNeural"
    edge_voice = data.get("edge_voice") or default_voice
    speed_preset = data.get("speed_preset", "normal")
    source_audio_path = data.get("source_audio_path")

    await state.clear()

    try:
        await callback.answer(t("clone_toast_default_voice", lang))
    except Exception:
        pass

    await _submit_voice_job(
        callback=callback,
        lang=lang,
        model_id=model_id,
        input_text=input_text,
        source_audio_path=source_audio_path,
        edge_voice=edge_voice,
        speed_preset=speed_preset,
        gender_preset=gender_preset,
    )


# ── Shared Job Submission Helpers ─────────────────────────────────────────────

# Mirrors app/webapp/api.py `_voice_job_error_detail`: every queue-failure
# reason code resolves to a user-safe localized message; unknown reasons
# fall back to a generic message and the raw code is NEVER shown.
_VOICE_QUEUE_FAILURE_KEYS: dict[str, str] = {
    "MODEL_NOT_AUTHORIZED": "queue_failed_model_not_authorized",
    "LEGACY_MODEL_ENGINE_REMOVED": "queue_failed_legacy_model",
    "CONCURRENT_JOB_LIMIT_EXCEEDED": "queue_failed_concurrent",
    "DAILY_JOB_LIMIT_EXCEEDED": "queue_failed_daily",
    "DAILY_MINUTE_LIMIT_EXCEEDED": "queue_failed_daily_minutes",
    "INSUFFICIENT_BALANCE": "queue_failed_insufficient_balance",
    "TEXT_EXCEEDS_CHARACTER_LIMIT": "queue_failed_text_limit",
    "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT": "queue_failed_text_limit",
    "AUDIO_FILE_NOT_FOUND": "queue_failed_audio_not_found",
}


def voice_queue_failure_message(reason: str | None, lang: str, account_category: str | None = None) -> str:
    """Localized, user-safe message for a create_voice_clone_job reason."""
    key = _VOICE_QUEUE_FAILURE_KEYS.get(reason or "", "queue_failed_generic")
    if (reason or "") == "DAILY_MINUTE_LIMIT_EXCEEDED":
        # Batch AN: the message names the caller's OWN tier limit.
        limit = (
            settings.voice_daily_limit_minutes_paid
            if account_category == AccountCategory.PURCHASED.value
            else settings.voice_daily_limit_minutes_free
        )
        return t(key, lang, limit_minutes=int(limit))
    return t(key, lang)


async def _submit_voice_job(
    callback: CallbackQuery,
    lang: str,
    model_id: int,
    input_text: str,
    source_audio_path: str | None,
    edge_voice: str,
    speed_preset: str,
    gender_preset: str,
) -> None:
    """Submit voice clone job from a callback query context."""
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
            source_audio_path=source_audio_path,
        )
        if not job:
            acct_user, _ = await get_or_create_user(session, callback.from_user.id)
            err_msg = voice_queue_failure_message(reason, lang, account_category=acct_user.account_category)
            await callback.answer(err_msg, show_alert=True)
            return

        queue_pos = await get_queue_position(session, job.id)
        status_text = t(
            "queue_status_card",
            lang,
            status_validating=t("status_validating", lang),
            queue_pos=queue_pos,
        )
        msg = await safe_edit_message(callback, status_text, parse_mode="HTML")
        if msg is not None:
            job.status_message_id = msg.message_id
        await session.commit()

    try:
        await callback.answer()
    except Exception:
        pass


async def _submit_voxcpm_job(
    message: Message,
    status_message,
    lang: str,
    model_id: int,
    input_text: str,
    source_audio_path: str | None,
    edge_voice: str,
    speed_preset: str,
    gender_preset: str,
) -> None:
    """Submit VoxCPM voice clone job from a message context (after reference upload)."""
    async with AsyncSessionLocal() as session:
        job, reason = await create_voice_clone_job(
            session=session,
            user_id=message.from_user.id,
            text=input_text,
            edge_voice=edge_voice,
            model_id=model_id,
            gender_preset=gender_preset,
            speed_preset=speed_preset,
            chat_id=message.from_user.id,
            source_audio_path=source_audio_path,
        )
        if not job:
            acct_user, _ = await get_or_create_user(session, message.from_user.id)
            err_msg = voice_queue_failure_message(reason, lang, account_category=acct_user.account_category)
            await message.answer(err_msg, reply_markup=back_to_menu_kb(lang), parse_mode="HTML")
            return

        queue_pos = await get_queue_position(session, job.id)
        status_text = t(
            "queue_status_card",
            lang,
            status_validating=t("status_validating", lang),
            queue_pos=queue_pos,
        )
        try:
            from aiogram.types import Message as TgMessage
            await status_message.edit_text(status_text, parse_mode="HTML")
            job.status_message_id = status_message.message_id
        except Exception:
            sent = await message.answer(status_text, parse_mode="HTML")
            if sent:
                job.status_message_id = sent.message_id
        await session.commit()


# ── On-Demand Delivery Callbacks from Completion Alert Notifications ───────────

@clone_router.callback_query(F.data.startswith("dp:"))
async def handle_voice_choice_callback(callback: CallbackQuery):
    """Batch DP: the parked-job choice buttons (dp:wait:<job_id> /
    dp:edgeall:<job_id> / dp:edgemix:<job_id>). Owner-only — enforced
    in voice_choice.apply_user_choice, never trusted from the tap
    alone. "wait" keeps the job parked with its buttons live; an Edge
    choice re-queues the job immediately."""
    from app.services import voice_choice as _vc

    parts = (callback.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    job_id = parts[2] if len(parts) > 2 else ""
    choice = {
        "wait": _vc.CHOICE_WAIT,
        "edgeall": _vc.CHOICE_EDGE_ALL,
        "edgemix": _vc.CHOICE_EDGE_MIX,
    }.get(action, "")
    try:
        async with AsyncSessionLocal() as _s:
            _u, _ = await get_or_create_user(_s, callback.from_user.id)
            lang = _u.language_code or "my"
    except Exception:
        lang = "my"
    outcome = "not_parked"
    if choice and job_id:
        try:
            async with AsyncSessionLocal() as session:
                job = await session.get(Job, job_id)
                outcome = await _vc.apply_user_choice(
                    session, job, choice, callback.from_user.id
                )
        except Exception:
            logger.warning("DP choice callback failed for job %s", job_id, exc_info=True)
            outcome = "not_parked"
    if outcome == "ok_resume" and getattr(callback, "message", None) is not None:
        # Edge choice: the job resumes NOW — retire the prompt's
        # buttons (a "wait" answer deliberately keeps them live so
        # the owner can still switch to Edge later).
        try:
            await callback.bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
    answer_key = {
        "ok_wait": "dp_choice_wait_answer",
        "ok_resume": "dp_choice_resume_answer",
        "not_owner": "dp_choice_not_owner",
    }.get(outcome, "dp_choice_not_parked")
    try:
        await callback.answer(t(answer_key, lang))
    except Exception:
        pass


@clone_router.callback_query(F.data.startswith("send_audio_"))
async def handle_send_audio_callback(callback: CallbackQuery):
    job_id = callback.data.replace("send_audio_", "").strip()
    try:
        async with AsyncSessionLocal() as _s:
            _u, _ = await get_or_create_user(_s, callback.from_user.id)
            lang = _u.language_code
    except Exception:
        lang = "my"
    try:
        await callback.answer(t("clone_toast_sending_audio", lang))
    except Exception:
        pass

    try:
        target_chat_id = callback.from_user.id
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            if not job or job.status != JobStatus.SUCCESS.value:
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_not_found", lang),
                )
                return

            # Ownership: the completion button is only sent to the job's
            # owner, and the audio must never be delivered to anyone else
            # who obtains or guesses a job id.
            if job.user_id != callback.from_user.id and not settings.is_admin(callback.from_user.id):
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_not_found", lang),
                )
                return

            res_path = Path(job.result_path) if job.result_path else None
            if not res_path or not res_path.is_file():
                # Search by job_id in outputs
                outputs_dir = settings.storage_root / "outputs"
                matches = list(outputs_dir.glob(f"{job.id}*"))
                if matches and matches[0].is_file():
                    res_path = matches[0]

            if res_path and res_path.is_file() and res_path.stat().st_size > 50 * 1024 * 1024:
                # Over the Telegram Bot API send cap (hunt #3 B1): the
                # send would fail inside Telegram. Same honest routing
                # as the missing-file branch below — a Mini App button,
                # never a dead link; the file stays downloadable there.
                app_url = settings.mini_app_url
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_miniapp_listen", lang),
                            web_app=WebAppInfo(url=f"{app_url}?job_id={job_id}")
                        )
                    ]
                ])
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_too_large_text", lang, job_id=job_id),
                    reply_markup=kb,
                    parse_mode="HTML",
                )
            elif res_path and res_path.is_file():
                await callback.bot.send_audio(
                    chat_id=target_chat_id,
                    audio=FSInputFile(str(res_path), filename=branded_download_filename("Voice", res_path.suffix.lstrip(".") or "wav", title=getattr(job, "preset_voice", None), job_id=job_id)),
                    caption=t("clone_caption_voice", lang, job_id=job_id),
                    parse_mode="HTML",
                )
            else:
                # The result file is no longer on this server's disk (the
                # service has no persistent disk, so a redeploy/restart
                # wipes generated outputs). The old fallback fetched
                # /api/audio/download without Telegram auth — that endpoint
                # requires init-data auth, so it could never succeed — and
                # then showed the same URL as a "direct download" link,
                # which only returned 401 in a browser. Be honest instead
                # and route the user into the Mini App.
                app_url = settings.mini_app_url
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_miniapp_listen", lang),
                            web_app=WebAppInfo(url=f"{app_url}?job_id={job_id}")
                        )
                    ]
                ])
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_file_missing_text", lang, job_id=job_id),
                    reply_markup=kb,
                    parse_mode="HTML",
                )
    except Exception as e:
        logger.error(f"Error handling send_audio callback for job {job_id}: {e}", exc_info=True)
        try:
            # Bot cleanup 2026-11-03: the raw exception text (Telegram
            # API errors, file paths) used to be interpolated into the
            # user message. Users get one sanitized line; the detail
            # stays in the server log line above.
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=t("clone_audio_send_failed", lang),
            )
        except Exception:
            pass


async def _send_recap_extra_missing(callback: CallbackQuery, lang: str, job_id: str) -> None:
    """Honest route for a requested recap companion file that is absent."""
    app_url = settings.mini_app_url
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t("clone_btn_miniapp_watch", lang),
                web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap&recap_job_id={job_id}")
            )
        ]
    ])
    await callback.bot.send_message(
        chat_id=callback.from_user.id,
        text=t("clone_recap_file_missing_text", lang, job_id=job_id),
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _deliver_recap_extra(callback: CallbackQuery, file_type: str) -> None:
    """Send exactly one user-requested recap companion file.

    Batch AP: companion files are never pushed with the video. Each
    completion-message button reaches this helper for its own file only.
    Ownership deliberately mirrors ``handle_send_recap_callback``,
    including AJ M7's fail-closed treatment when no Job row remains.
    """
    prefix = f"send_recap_{file_type}_"
    job_id = callback.data.removeprefix(prefix).strip()
    try:
        async with AsyncSessionLocal() as _s:
            _u, _ = await get_or_create_user(_s, callback.from_user.id)
            lang = _u.language_code
    except Exception:
        lang = "my"
    try:
        await callback.answer(t("clone_toast_sending_file", lang))
    except Exception:
        pass

    try:
        target_chat_id = callback.from_user.id
        from app.webapp.api import (
            RECAP_JOBS_REGISTRY,
            _clean_recap_display_title,
            resolve_recap_deliverable_path,
        )

        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            # Ownership & completion status: never deliver another user's or cancelled/failed recap file.
            if job and job.user_id != callback.from_user.id and not settings.is_admin(callback.from_user.id):
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_not_found", lang),
                )
                return
            if job is not None and job.status != JobStatus.SUCCESS.value:
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_not_found", lang),
                )
                return
            if job is None:
                # AJ M7, mirrored from the video handler: without a Job
                # row, the registry entry must prove ownership (admins
                # exempt); neither source proving it means refuse.
                _reg = RECAP_JOBS_REGISTRY.get(job_id) or {}
                _entry_owner = _reg.get("user_id")
                _reg_status = str(_reg.get("status") or "").upper()
                if (
                    (_entry_owner != callback.from_user.id and not settings.is_admin(callback.from_user.id))
                    or (_reg_status and _reg_status not in ("COMPLETED", "SUCCESS"))
                ):
                    await callback.bot.send_message(
                        chat_id=target_chat_id,
                        text=t("clone_audio_not_found", lang),
                    )
                    return

            resolved = resolve_recap_deliverable_path(job_id, file_type)
            file_path = Path(resolved) if resolved and Path(resolved).is_file() else None
            if file_path is None:
                await _send_recap_extra_missing(callback, lang, job_id)
                return

            _recap_entry = RECAP_JOBS_REGISTRY.get(job_id) or {}
            recap_title = (
                _clean_recap_display_title(_recap_entry.get("title"))
                if _recap_entry.get("title")
                else None
            )
            if recap_title and (
                recap_title.startswith("http") or recap_title == "Movie Recap Video"
            ):
                recap_title = None

            if file_type == "srt":
                await callback.bot.send_document(
                    chat_id=target_chat_id,
                    document=FSInputFile(
                        str(file_path),
                        filename=branded_download_filename(
                            "Subtitles", "srt", title=recap_title, job_id=job_id
                        ),
                    ),
                    caption=t("clone_caption_subtitles", lang),
                    parse_mode="HTML",
                )
            elif file_type == "audio":
                if file_path.stat().st_size > 50 * 1024 * 1024:
                    app_url = settings.mini_app_url
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t("clone_btn_miniapp_watch", lang),
                                web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap&recap_job_id={job_id}")
                            )
                        ]
                    ])
                    await callback.bot.send_message(
                        chat_id=target_chat_id,
                        text=t("clone_recap_large_app_text", lang, job_id=job_id),
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    return
                await callback.bot.send_audio(
                    chat_id=target_chat_id,
                    audio=FSInputFile(
                        str(file_path),
                        filename=branded_download_filename(
                            "RecapAudio", file_path.suffix.lstrip(".") or "mp3",
                            title=recap_title, job_id=job_id,
                        ),
                    ),
                    caption=t("clone_caption_recap_audio", lang),
                    parse_mode="HTML",
                )
            else:  # script
                await callback.bot.send_document(
                    chat_id=target_chat_id,
                    document=FSInputFile(
                        str(file_path),
                        filename=branded_download_filename(
                            "RecapScript", file_path.suffix.lstrip(".") or "txt",
                            title=recap_title, job_id=job_id,
                        ),
                    ),
                    caption=t("clone_caption_recap_script", lang),
                    parse_mode="HTML",
                )
    except Exception as e:
        logger.error(f"Error handling send_recap_{file_type} callback for job {job_id}: {e}", exc_info=True)
        try:
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=t("clone_recap_send_failed", lang),
            )
        except Exception:
            pass


# Batch AP: these narrower prefixes MUST be registered before the
# broader send_recap_ handler below. All start with send_recap_, and
# aiogram dispatches the first matching callback handler.
@clone_router.callback_query(F.data.startswith("send_recap_srt_"))
async def handle_send_recap_srt_callback(callback: CallbackQuery):
    await _deliver_recap_extra(callback, "srt")


@clone_router.callback_query(F.data.startswith("send_recap_audio_"))
async def handle_send_recap_audio_callback(callback: CallbackQuery):
    await _deliver_recap_extra(callback, "audio")


@clone_router.callback_query(F.data.startswith("send_recap_script_"))
async def handle_send_recap_script_callback(callback: CallbackQuery):
    await _deliver_recap_extra(callback, "script")


@clone_router.callback_query(F.data.startswith("send_recap_"))
async def handle_send_recap_callback(callback: CallbackQuery):
    job_id = callback.data.replace("send_recap_", "").strip()
    try:
        async with AsyncSessionLocal() as _s:
            _u, _ = await get_or_create_user(_s, callback.from_user.id)
            lang = _u.language_code
    except Exception:
        lang = "my"
    try:
        await callback.answer(t("clone_toast_sending_recap", lang))
    except Exception:
        pass

    try:
        target_chat_id = callback.from_user.id
        from app.webapp.api import resolve_recap_deliverable_path, resolve_recap_part_paths
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            # Ownership: never deliver another user's recap video.
            if job and job.user_id != callback.from_user.id and not settings.is_admin(callback.from_user.id):
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_not_found", lang),
                )
                return
            if job is None:
                # Batch AJ M7: with no Job row, the paths below resolve
                # from the in-memory registry / disk alone — the old
                # code skipped ownership entirely in that case, so any
                # user who learned a job id could pull its files after
                # the row was swept. Fail closed like the voice
                # sibling: the registry entry must prove ownership
                # (admins exempt); neither source proving it = refuse.
                from app.webapp.api import RECAP_JOBS_REGISTRY as _REG

                _entry_owner = (_REG.get(job_id) or {}).get("user_id")
                if _entry_owner != callback.from_user.id and not settings.is_admin(callback.from_user.id):
                    await callback.bot.send_message(
                        chat_id=target_chat_id,
                        text=t("clone_audio_not_found", lang),
                    )
                    return
            db_res_path = job.result_path if job else None
            resolved_vid = resolve_recap_deliverable_path(job_id, "video", result_path=db_res_path)
            video_path = Path(resolved_vid) if resolved_vid and Path(resolved_vid).is_file() else None

            has_parts = bool(resolve_recap_part_paths(job_id))
            from app.webapp.api import RECAP_JOBS_REGISTRY, _clean_recap_display_title

            _recap_entry = RECAP_JOBS_REGISTRY.get(job_id) or {}
            recap_title = (
                _clean_recap_display_title(_recap_entry.get("title"))
                if _recap_entry.get("title")
                else None
            )
            if recap_title and (
                recap_title.startswith("http") or recap_title == "Movie Recap Video"
            ):
                recap_title = None

            is_segmented = bool(
                (RECAP_JOBS_REGISTRY.get(job_id) or {}).get("segmented")
            )
            if has_parts and is_segmented:
                # Split/segmented job: the parts ARE the delivery (the
                # registered "video" is just part 1 — don't send it twice).
                try:
                    from app.webapp.api import _deliver_recap_parts

                    await _deliver_recap_parts(
                        callback.bot, target_chat_id, job_id, lang, title=recap_title
                    )
                except Exception:
                    pass
            elif video_path and video_path.is_file():
                file_size = video_path.stat().st_size
                if file_size <= 50 * 1024 * 1024:
                    await callback.bot.send_video(
                        chat_id=target_chat_id,
                        video=FSInputFile(str(video_path), filename=branded_download_filename("Recap", "mp4", title=recap_title, job_id=job_id)),
                        caption=t("clone_caption_recap", lang, job_id=job_id),
                        parse_mode="HTML",
                    )
                elif has_parts:
                    # >50MB full render with generated parts: parts ARE the Telegram delivery.
                    try:
                        from app.webapp.api import _deliver_recap_parts

                        await _deliver_recap_parts(
                            callback.bot, target_chat_id, job_id, lang, title=recap_title
                        )
                    except Exception:
                        pass
                else:
                    # >50MB cannot be sent through the Bot API and no split parts exist.
                    app_url = settings.mini_app_url
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t("clone_btn_miniapp_watch", lang),
                                web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap&recap_job_id={job_id}")
                            )
                        ]
                    ])
                    await callback.bot.send_message(
                        chat_id=target_chat_id,
                        text=t("clone_recap_large_app_text", lang, job_id=job_id),
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
            else:
                # File no longer on this server's disk (no persistent
                # disk: redeploys/restarts wipe generated outputs). The
                # old message embedded the raw /api/recap/download URL,
                # which requires Telegram init-data auth and only
                # returned 401 in a browser — say so honestly instead.
                app_url = settings.mini_app_url
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("clone_btn_miniapp_watch", lang),
                            web_app=WebAppInfo(url=f"{app_url}?tab=tab-recap&recap_job_id={job_id}")
                        )
                    ]
                ])
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_recap_file_missing_text", lang, job_id=job_id),
                    reply_markup=kb,
                    parse_mode="HTML",
                )
    except Exception as e:
        logger.error(f"Error handling send_recap callback for job {job_id}: {e}", exc_info=True)
        try:
            # Bot cleanup 2026-11-03: sanitized user line only — the
            # raw exception stays in the server log above.
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=t("clone_recap_send_failed", lang),
            )
        except Exception:
            pass
