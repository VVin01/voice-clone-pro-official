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


# 2026-11-20 (H): one tap on "📱 Video ဖိုင် ပို့ပေးပါ" delivered the
# video TWICE. Every tap is a distinct update (the webhook update_id
# dedup cannot catch it), the handler answers the callback instantly
# so the client's spinner clears while a large upload is still running,
# and a second tap / client resend started a second upload. Claim each
# (user, job, file) delivery for the duration of the send plus a short
# cooldown; repeats inside that window are answered with a toast only.
import time as _time

_SEND_CLAIM_COOLDOWN_SECONDS = 90.0
_SEND_CLAIMS: dict[tuple[int, str, str], float] = {}


def _claim_file_send(user_id: int, job_id: str, kind: str) -> bool:
    """Return True when this delivery may proceed (not already claimed)."""
    now = _time.monotonic()
    # Opportunistic purge keeps the map tiny.
    for k, exp in list(_SEND_CLAIMS.items()):
        if exp <= now:
            _SEND_CLAIMS.pop(k, None)
    key = (int(user_id), str(job_id), str(kind))
    if _SEND_CLAIMS.get(key, 0.0) > now:
        return False
    _SEND_CLAIMS[key] = now + _SEND_CLAIM_COOLDOWN_SECONDS
    return True


def _release_file_send(user_id: int, job_id: str, kind: str) -> None:
    """Drop a claim (used when the send failed so a retry is allowed)."""
    _SEND_CLAIMS.pop((int(user_id), str(job_id), str(kind)), None)



# ── On-Demand Delivery Callbacks from Completion Alert Notifications ───────────

@clone_router.callback_query(F.data.startswith("dp:"))
async def handle_voice_choice_callback(callback: CallbackQuery):
    """Parked-job choice buttons (`dp:wait:<job_id>`, `dp:edgeall:<job_id>`,
    `dp:cancel:<job_id>`). Owner-only; `dp:edgemix` is retired (Part 2 #2)."""
    from app.services import voice_choice as _vc

    parts = (callback.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    job_id = parts[2] if len(parts) > 2 else ""
    choice = {
        "wait": _vc.CHOICE_WAIT,
        "edgeall": _vc.CHOICE_EDGE_ALL,
        "cancel": _vc.CHOICE_CANCEL,
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
    elif action and job_id:
        outcome = "bad_choice"
    if outcome in ("ok_resume", "ok_cancel") and getattr(callback, "message", None) is not None:
        # Resume or cancel settles the parked prompt — retire its inline buttons.
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
        "ok_cancel": "dp_choice_cancel_answer",
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
    if not _claim_file_send(callback.from_user.id, job_id, "audio"):
        try:
            await callback.answer(t("clone_toast_send_in_progress", lang), show_alert=False)
        except Exception:
            pass
        return
    try:
        await callback.answer(t("clone_toast_sending_audio", lang))
    except Exception:
        pass

    try:
        target_chat_id = callback.from_user.id
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            if not job or job.status != JobStatus.SUCCESS.value:
                _release_file_send(callback.from_user.id, job_id, "audio")
                await callback.bot.send_message(
                    chat_id=target_chat_id,
                    text=t("clone_audio_not_found", lang),
                )
                return

            # Ownership: the completion button is only sent to the job's
            # owner, and the audio must never be delivered to anyone else
            # who obtains or guesses a job id.
            if job.user_id != callback.from_user.id and not settings.is_admin(callback.from_user.id):
                _release_file_send(callback.from_user.id, job_id, "audio")
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

            from app.services.telegram_bot_factory import (
                get_telegram_max_upload_bytes,
                send_cached_telegram_media,
            )

            if res_path and res_path.is_file() and res_path.stat().st_size > get_telegram_max_upload_bytes():
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
                await send_cached_telegram_media(
                    callback.bot,
                    media_kind="audio",
                    chat_id=target_chat_id,
                    file_path=res_path,
                    filename=branded_download_filename("Voice", res_path.suffix.lstrip(".") or "wav", title=getattr(job, "preset_voice", None), job_id=job_id),
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
        _release_file_send(callback.from_user.id, job_id, "audio")
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
    if not _claim_file_send(callback.from_user.id, job_id, file_type):
        try:
            await callback.answer(t("clone_toast_send_in_progress", lang))
        except Exception:
            pass
        return
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
                from app.services.telegram_bot_factory import (
                    get_telegram_max_upload_bytes,
                    send_cached_telegram_media,
                )

                if file_path.stat().st_size > get_telegram_max_upload_bytes():
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
                await send_cached_telegram_media(
                    callback.bot,
                    media_kind="audio",
                    chat_id=target_chat_id,
                    file_path=file_path,
                    filename=branded_download_filename(
                        "RecapAudio", file_path.suffix.lstrip(".") or "mp3",
                        title=recap_title, job_id=job_id,
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
        _release_file_send(callback.from_user.id, job_id, file_type)
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
    if not _claim_file_send(callback.from_user.id, job_id, "video"):
        try:
            await callback.answer(t("clone_toast_send_in_progress", lang))
        except Exception:
            pass
        return
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
                from app.services.telegram_bot_factory import (
                    get_telegram_max_upload_bytes,
                    send_cached_telegram_media,
                )

                file_size = video_path.stat().st_size
                if file_size <= get_telegram_max_upload_bytes():
                    await send_cached_telegram_media(
                        callback.bot,
                        media_kind="video",
                        chat_id=target_chat_id,
                        file_path=video_path,
                        filename=branded_download_filename("Recap", "mp4", title=recap_title, job_id=job_id),
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
        _release_file_send(callback.from_user.id, job_id, "video")
        try:
            # Bot cleanup 2026-11-03: sanitized user line only — the
            # raw exception stays in the server log above.
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=t("clone_recap_send_failed", lang),
            )
        except Exception:
            pass
