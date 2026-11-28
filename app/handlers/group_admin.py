"""Group AI admin handler — see app/services/group_ai_admin.py.

Registered LAST among the routers so command/FSM handlers always win.
The handler itself is deliberately thin: all decisions (pre-filter,
kill switch, sensitive-topic hard rule, rate limits, the AI call) live
in the service module where they are unit-tested. It never raises —
any failure degrades to silence, never to a broken update pipeline.

Batch H additions wired here: per-message BotSetting limits applied
to the shared limiter, the in-memory conversation-context buffer,
the FAQ cache check (admin-approved answers only, no AI call), the
escalation draft on the admin DM with [Post] / [Edit] / [Dismiss]
buttons, and the callback + FSM machinery behind them:

* First tap wins — approve/dismiss close the record, edit claims it
  (status EDITING) and prompts the claiming admin in DM for the
  corrected text; saving or cancelling the edit releases the claim
  and refreshes every admin's DM with a fresh keyboard.
* Every DM copy's coordinates live on the escalation record, so any
  state change rewrites ALL admin copies at once: a short status
  line is appended and the keyboard is cleared (terminal states) —
  a failed edit on one copy never fails the action itself.
"""

from __future__ import annotations

import html
import inspect
import json
import logging
import time
from contextlib import asynccontextmanager

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services import group_ai_admin as gai
from app.services import private_ai as pai
from app.services.i18n import t as _bot_t
from app.services.payments import get_recent_payment_intent

logger = logging.getLogger(__name__)

group_admin_router = Router(name="group_admin")


def _plain_send_kw(obj: object) -> dict:
    """Pass parse_mode=None only when the real Bot defaults to HTML/Markdown."""
    bot = getattr(obj, "bot", obj)
    default = getattr(bot, "default", None)
    if isinstance(getattr(default, "parse_mode", None), str):
        return {"parse_mode": None}
    return {}


class GroupAiEditStates(StatesGroup):
    """Admin is composing a corrected draft for a claimed escalation."""

    waiting_text = State()


@asynccontextmanager
async def _session_or_new(session: AsyncSession | None):
    """Use the middleware-supplied session, else open a short-lived one."""
    if session is not None:
        yield session
    else:
        async with AsyncSessionLocal() as s:
            yield s


async def _kill_switch_on(session: AsyncSession | None) -> bool:
    async with _session_or_new(session) as s:
        return await gai.is_enabled(s)


async def _refresh_limits(session: AsyncSession | None) -> None:
    """Apply the owner-tuned BotSetting limits to the shared limiter."""
    async with _session_or_new(session) as s:
        gai.rate_limiter.update_limits(await gai.get_limits(s))
        # Batch AJ (M12): one-shot restore of the persisted limiter
        # snapshot (budgets / cooldowns / answered-set) after a restart.
        await gai.ensure_limiter_restored(s)


async def _flush_limiter(session: AsyncSession | None) -> None:
    """Batch AJ (M12): debounced write-through of limiter state."""
    try:
        async with _session_or_new(session) as s:
            await gai.flush_limiter_state_if_dirty(s)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cross-admin DM sync (batch H, requirement B)
# ---------------------------------------------------------------------------


async def _sync_admin_copies(bot: Bot, record: dict, *, status_line: str | None) -> None:
    """Rewrite every stored admin DM copy after a state change.

    Appends the status line to the stored DM body and clears the
    keyboard. A copy that cannot be edited (too old, deleted, …)
    falls back to a markup-only clear, and a copy that fails even
    that is logged and skipped — syncing must never fail the action
    that triggered it. The tap-time status check in the callback
    handler remains the backstop for any copy left stale.
    """
    dm_text = record.get("dm_text")
    for chat_id, message_id in record.get("dm_copies") or []:
        try:
            if dm_text:
                new_text = dm_text + (f"\n\n{status_line}" if status_line else "")
                await bot.edit_message_text(
                    new_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
        except Exception as exc:
            logger.debug(
                "group_ai_admin: DM copy sync failed for %s/%s: %s", chat_id, message_id, exc
            )
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            except Exception:
                pass


async def _resend_escalation_dms(bot: Bot, session: AsyncSession, record: dict) -> None:
    """Send FRESH escalation DMs (current draft + live keyboard) to all
    admins and store the new copies on the record.

    Used after an edit is saved or cancelled: the old copies have
    just been status-lined and de-keyboarded by _sync_admin_copies,
    so the actionable copy going forward is this new one.
    """
    dm_text = gai.build_escalation_dm(
        author_name=record.get("author_label") or "",
        author_username=record.get("author_username"),
        author_id=record["author_id"],
        text=record.get("question_text") or "",
        chat_id=record["chat_id"],
        message_id=record["message_id"],
        reason=record.get("reason") or "",
        draft_text=record.get("draft_text"),
    )
    keyboard = (
        gai.build_escalation_keyboard(record["id"]) if record.get("draft_text") else None
    )
    copies: list[tuple[int, int]] = []
    for admin_id in settings.admin_ids:
        try:
            sent = await bot.send_message(
                admin_id, dm_text, parse_mode="HTML", reply_markup=keyboard
            )
            mid = getattr(sent, "message_id", None)
            if isinstance(mid, int):
                copies.append((int(admin_id), mid))
        except Exception as exc:
            logger.warning("group_ai_admin: escalation re-DM to %s failed: %s", admin_id, exc)
    if copies:
        await gai.set_escalation_dm(session, record["id"], dm_text=dm_text, copies=copies)


async def _release_edit_and_refresh(
    *,
    bot: Bot,
    session: AsyncSession,
    escalation_id: int,
    admin_id: int,
    admin_name: str,
    new_draft: str | None,
) -> dict | None:
    """Release an edit claim and refresh every admin's DM.

    Returns the released record snapshot, or None when the claim was
    not this admin's to release. Old copies get the edit-saved /
    edit-cancelled status line; fresh copies carry the live draft.
    """
    released, snapshot = await gai.release_escalation_edit(
        session, escalation_id, admin_id=admin_id, new_draft=new_draft
    )
    if not released or snapshot is None:
        return None
    line = gai.STATUS_LINE_EDIT_SAVED if new_draft is not None else gai.STATUS_LINE_EDIT_CANCELLED
    await _sync_admin_copies(bot, snapshot, status_line=line.format(name=admin_name))
    await _resend_escalation_dms(bot, session, snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Escalation delivery
# ---------------------------------------------------------------------------


async def _escalate(
    message: Message,
    bot: Bot | None,
    *,
    reason: str,
    deferral_text: str,
    session: AsyncSession | None = None,
    limiter=None,
    dm_link: str | None = ...,
    text_override: str | None = None,
    is_group: bool = True,
) -> None:
    """Post the polite deferral line + DM every admin (cooldown-gated).

    Batch H: when the AI-call budget allows, one extra Gemini call
    drafts a suggested answer; the escalation is persisted so the
    DM's [Post] / [Edit] / [Dismiss] buttons can act on it later
    (first tap wins), and every DM copy's coordinates are stored on
    the record for cross-admin keyboard sync. Without budget — or if
    drafting/persisting fails — the DM goes out exactly as before:
    no draft, no buttons.

    Batch BX: ``limiter`` and ``dm_link`` parameterise the two
    group-specific pieces for the private-chat path — the deferral
    pacing/budget can run on the private limiter, and the admin DM
    can drop the (invalid-for-private) group message link. Both
    default to the historical group behaviour, byte-identical.
    """
    limiter = limiter if limiter is not None else gai.rate_limiter
    user = message.from_user
    if user is None or not limiter.can_defer(user.id):
        return
    effective_text = text_override if text_override is not None else (message.text or "")
    limiter.record_deferral(user.id)
    try:
        await message.reply(deferral_text)
    except Exception as exc:
        logger.debug("group_ai_admin: deferral post failed: %s", exc)

    draft_text = None
    if limiter.ai_budget_available():
        limiter.record_ai_call()
        draft_text = await gai.draft_escalation_answer(
            text=effective_text,
            author_name=user.first_name or "",
            reason=reason,
            is_group=is_group,
        )

    escalation_id = None
    if draft_text:
        async with _session_or_new(session) as s:
            escalation_id = await gai.create_escalation(
                s,
                chat_id=message.chat.id,
                message_id=message.message_id,
                author_id=user.id,
                author_label=user.first_name or "",
                author_username=user.username,
                question_text=effective_text,
                reason=reason,
                draft_text=draft_text,
            )

    dm_text = gai.build_escalation_dm(
        author_name=user.first_name or "",
        author_username=user.username,
        author_id=user.id,
        text=effective_text,
        chat_id=message.chat.id,
        message_id=message.message_id,
        reason=reason,
        draft_text=draft_text,
        dm_link=dm_link,
    )
    keyboard = gai.build_escalation_keyboard(escalation_id) if escalation_id else None
    sender = bot or message.bot
    copies: list[tuple[int, int]] = []
    for admin_id in settings.admin_ids:
        try:
            sent = await sender.send_message(
                admin_id, dm_text, parse_mode="HTML", reply_markup=keyboard
            )
            mid = getattr(sent, "message_id", None)
            if escalation_id and isinstance(mid, int):
                copies.append((int(admin_id), mid))
        except Exception as exc:
            logger.warning("group_ai_admin: escalation DM to %s failed: %s", admin_id, exc)
    if escalation_id and copies:
        async with _session_or_new(session) as s:
            await gai.set_escalation_dm(s, escalation_id, dm_text=dm_text, copies=copies)


def _log_decision(message: Message, user_id: int | None, decision: str, reason: str = "") -> None:
    """Batch AQ: one INFO line per terminal Group-AI decision.

    Before this, decisions were logged NOWHERE — a member question
    could vanish into silence (AI timeout, NO_REPLY, budget) with no
    trace at all, and two unanswered questions on 2026-11-05 were
    undiagnosable for exactly that reason. Ids + the reason class
    only — NEVER the message text or the AI's raw output (group
    messages are not stored; the logs follow the same rule).
    """
    logger.info(
        "group_ai_admin: decision=%s reason=%s chat_id=%s message_id=%s user_id=%s",
        decision,
        reason or "-",
        message.chat.id,
        message.message_id,
        user_id,
    )


async def _deliver_ai_failed_holding(
    message: Message,
    bot: Bot | None,
    *,
    limiter: gai.GroupReplyRateLimiter,
    text: str,
    is_group: bool,
) -> bool:
    """Post ONE holding reply + ONE lightweight no-draft Admin DM when cooldowns allow."""
    user = message.from_user
    if user is None or not limiter.can_post_holding_reply(user.id, is_group=is_group):
        return False
    limiter.record_holding_reply(user.id, is_group=is_group)
    try:
        await message.reply(gai.HOLDING_REPLY_AI_FAILED, **_plain_send_kw(message))
    except Exception as exc:
        logger.debug("group_ai_admin: holding reply post failed: %s", exc)

    dm_text = gai.build_ai_failed_dm(
        author_name=user.first_name or "",
        author_username=user.username,
        author_id=user.id,
        text=text,
        chat_id=message.chat.id,
        message_id=message.message_id,
        failure_stage=gai.get_last_failure_stage(),
        is_group=is_group,
    )
    sender = bot or message.bot
    for admin_id in settings.admin_ids:
        try:
            await sender.send_message(admin_id, dm_text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("group_ai_admin: ai_failed DM to %s failed: %s", admin_id, exc)
    return True


async def _post_moderator_notice_and_delete(
    message: Message,
    sender: Bot | Any,
    *,
    user: Any,
    category: str,
    strike_num: int,
) -> None:
    """Best-effort delete offending message + post ONE named 60s-TTL public correction notice."""
    from app.handlers import group_moderation as gm_handlers

    try:
        await sender.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception as exc:
        logger.warning("group_ai_admin: moderation delete_message failed: %s", exc)

    notice_text = gai.build_moderator_notice(
        category=category,
        strike_num=strike_num,
        first_name=getattr(user, "first_name", None),
        username=getattr(user, "username", None),
    )
    try:
        sent_notice = await sender.send_message(chat_id=message.chat.id, text=notice_text)
        notice_mid = getattr(sent_notice, "message_id", None)
        if isinstance(notice_mid, int):
            gm_handlers._schedule_message_deletion(
                sender,
                message.chat.id,
                notice_mid,
                delay_seconds=gm_handlers.GROUP_NOTICE_TTL_SECONDS,
            )
    except Exception as exc:
        logger.warning("group_ai_admin: moderator notice failed: %s", exc)


_cmd_cleanup_last_notice_mono: dict[int, float] = {}
_monotonic = time.monotonic


def reset_command_cleanup_throttle() -> None:
    """Reset in-memory per-chat command-cleanup notice throttle timestamps."""
    _cmd_cleanup_last_notice_mono.clear()


async def _command_cleanup_on(session: AsyncSession | None) -> bool:
    async with _session_or_new(session) as s:
        return await gai.is_enabled(s) and await gai.is_command_cleanup_enabled(s)


async def _cleanup_foreign_group_command(
    message: Message,
    sender: Bot | Any,
    *,
    cmd_kind: str,
    author_id: int | None = None,
) -> None:
    """Best-effort delete foreign/handler-less group command + 1/30m anonymous 60s-TTL notice."""
    from app.handlers import group_moderation as gm_handlers

    chat_id = int(message.chat.id)
    try:
        await sender.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as exc:
        logger.warning("group_ai_admin: command cleanup delete_message failed: %s", exc)

    now = _monotonic()
    prev = _cmd_cleanup_last_notice_mono.get(chat_id)
    if prev is None or (now - prev) >= gai.COMMAND_CLEANUP_NOTICE_COOLDOWN_SECONDS:
        _cmd_cleanup_last_notice_mono[chat_id] = now
        try:
            sent_notice = await sender.send_message(
                chat_id,
                gai.strip_emoji(gai.COMMAND_CLEANUP_NOTICE_TEXT),
                **_plain_send_kw(message),
            )
            notice_mid = getattr(sent_notice, "message_id", None)
            if isinstance(notice_mid, int):
                gm_handlers._schedule_message_deletion(
                    sender,
                    chat_id,
                    notice_mid,
                    delay_seconds=gm_handlers.GROUP_NOTICE_TTL_SECONDS,
                )
        except Exception as exc:
            if _cmd_cleanup_last_notice_mono.get(chat_id) == now:
                if prev is None:
                    _cmd_cleanup_last_notice_mono.pop(chat_id, None)
                else:
                    _cmd_cleanup_last_notice_mono[chat_id] = prev
            logger.warning("group_ai_admin: command cleanup notice send failed: %s", exc)

    _log_decision(message, author_id, "command_cleanup", cmd_kind)


async def _execute_moderation(
    message: Message,
    bot: Bot | None,
    *,
    session: AsyncSession | None,
    user: Any,
    category: str,
    text: str,
) -> None:
    """Execute the moderation action ladder (strikes in rolling 7 days).

    Legacy selling/bashing/harm: reply in-place with WARNING_TEXT_STRIKE_*.
    Disparagement/competitor: best-effort delete offending message, post ONE
    named 60s auto-delete notice (female persona, zero emoji), and apply
    Strike 1 warn / Strike 2 1h mute / Strike 3 24h mute + Admin Ban DM.
    """
    import time

    sender = bot or message.bot
    is_delete_moderator = category in gai.MODERATOR_DELETE_NOTICE_CATEGORIES

    async with _session_or_new(session) as s:
        previous_strikes = await gai.get_user_strikes_in_window(s, user.id)
        strike_num = len(previous_strikes) + 1
        s2_duration, s3_duration = await gai.get_moderation_mute_durations(s)

        if strike_num == 1:
            action = "warn"
            if is_delete_moderator:
                await _post_moderator_notice_and_delete(
                    message, sender, user=user, category=category, strike_num=1
                )
            else:
                try:
                    await message.reply(gai.WARNING_TEXT_STRIKE_1)
                except Exception as exc:
                    logger.warning("group_ai_admin: strike 1 warning failed: %s", exc)

            strike = await gai.record_moderation_strike(
                s,
                user_id=user.id,
                category=category,
                message_id=message.message_id,
                chat_id=message.chat.id,
                action=action,
            )
            _log_decision(message, user.id, "moderation", f"strike_1:{category}:warn")

        elif strike_num == 2:
            action = "mute_1h"
            until_date = int(time.time() + s2_duration)
            try:
                from aiogram.types import ChatPermissions

                await sender.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date,
                )
            except Exception as exc:
                logger.warning("group_ai_admin: strike 2 restrict failed: %s", exc)

            if is_delete_moderator:
                await _post_moderator_notice_and_delete(
                    message, sender, user=user, category=category, strike_num=2
                )
            else:
                try:
                    await message.reply(gai.WARNING_TEXT_STRIKE_2)
                except Exception as exc:
                    logger.warning("group_ai_admin: strike 2 warning failed: %s", exc)

            strike = await gai.record_moderation_strike(
                s,
                user_id=user.id,
                category=category,
                message_id=message.message_id,
                chat_id=message.chat.id,
                action=action,
            )
            _log_decision(message, user.id, "moderation", f"strike_2:{category}:mute_1h")

        else:
            action = "mute_24h"
            until_date = int(time.time() + s3_duration)
            try:
                from aiogram.types import ChatPermissions

                await sender.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date,
                )
            except Exception as exc:
                logger.warning("group_ai_admin: strike 3 restrict failed: %s", exc)

            if is_delete_moderator:
                await _post_moderator_notice_and_delete(
                    message, sender, user=user, category=category, strike_num=3
                )
            else:
                try:
                    await message.reply(gai.WARNING_TEXT_STRIKE_3)
                except Exception as exc:
                    logger.warning("group_ai_admin: strike 3 warning failed: %s", exc)

            strike = await gai.record_moderation_strike(
                s,
                user_id=user.id,
                category=category,
                message_id=message.message_id,
                chat_id=message.chat.id,
                action=action,
            )
            _log_decision(message, user.id, "moderation", f"strike_3:{category}:mute_24h_escalated")

            # Escalation DM to all admins carrying live copy of offending message
            dm_text = gai.build_moderation_escalation_dm(
                author_name=user.first_name or "",
                author_username=user.username,
                author_id=user.id,
                text=text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                category=category,
                strike_count=strike_num,
                previous_strikes=previous_strikes,
            )
            keyboard = gai.build_moderation_keyboard(strike.id, message.chat.id, user.id)
            for admin_id in settings.admin_ids:
                try:
                    await sender.send_message(
                        admin_id, dm_text, parse_mode="HTML", reply_markup=keyboard
                    )
                except Exception as exc:
                    logger.warning("group_ai_admin: moderation escalation DM to %s failed: %s", admin_id, exc)


@group_admin_router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text | F.voice,
)
async def handle_group_message(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    try:
        # A user mid-flow in an FSM conversation owns their messages.
        if state is not None:
            try:
                if await state.get_state():
                    return
            except Exception:
                pass

        user = message.from_user
        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        sender = bot or message.bot
        msg_voice = getattr(message, "voice", None)

        # Prefilter chat, sender, admin, and bot status BEFORE any voice download / ASR.
        if msg_voice is not None:
            pre_reason = gai.prefilter_reason(
                chat_id=message.chat.id,
                chat_type=chat_type,
                text="__voice__",
                from_user_id=user.id if user else None,
                from_user_is_bot=bool(user.is_bot) if user else True,
                sender_chat_id=message.sender_chat.id if message.sender_chat else None,
            )
            if pre_reason is not None:
                _log_decision(message, user.id if user else None, "prefiltered", pre_reason)
                return
        else:
            message_text = getattr(message, "text", "") or ""
            reason = gai.prefilter_reason(
                chat_id=message.chat.id,
                chat_type=chat_type,
                text=message_text,
                from_user_id=user.id if user else None,
                from_user_is_bot=bool(user.is_bot) if user else True,
                sender_chat_id=message.sender_chat.id if message.sender_chat else None,
            )
            if reason is not None:
                if reason == "command":
                    cmd_kind = gai.foreign_command_kind(message.text)
                    if cmd_kind is not None and await _command_cleanup_on(session):
                        await _cleanup_foreign_group_command(
                            message,
                            sender,
                            cmd_kind=cmd_kind,
                            author_id=user.id if user else None,
                        )
                        return
                _log_decision(message, user.id if user else None, "prefiltered", reason)
                return

        if not await _kill_switch_on(session):
            _log_decision(message, user.id if user else None, "disabled", "kill_switch_off")
            return

        # Owner-tuned anti-spam values (BotSettings, kill-switch style).
        await _refresh_limits(session)

        # Voice understanding (Part 2): runs only after prefilter + kill switch + limits restore.
        if msg_voice is not None:
            if not await gai.is_voice_enabled(session):
                _log_decision(message, user.id if user else None, "voice_ignored", "voice_disabled")
                return
            duration = getattr(msg_voice, "duration", 0) or 0
            file_size = getattr(msg_voice, "file_size", 0) or 0
            if duration > gai.VOICE_MAX_DURATION_SECONDS or file_size > gai.VOICE_MAX_FILE_SIZE_BYTES:
                _log_decision(message, user.id if user else None, "voice_ignored", "duration_or_size_cap")
                return
            daily_voice_cap = await gai.get_voice_daily_cap(session)
            if not gai.rate_limiter.can_transcribe_voice(daily_voice_cap):
                _log_decision(message, user.id if user else None, "voice_ignored", "daily_voice_cap_exhausted")
                return
            try:
                file_info = await sender.get_file(msg_voice.file_id)
                file_bytes_io = await sender.download_file(file_info.file_path)
                voice_bytes = file_bytes_io.read()
            except Exception as exc:
                logger.warning("group_ai_admin: failed to download voice file: %s", exc)
                _log_decision(message, user.id if user else None, "voice_failed", "download_error")
                return
            transcript = await gai.transcribe_voice_gemini(
                voice_bytes,
                mime_type=getattr(msg_voice, "mime_type", "audio/ogg") or "audio/ogg",
            )
            if not transcript or not transcript.strip():
                _log_decision(message, user.id if user else None, "voice_failed", "empty_transcript")
                return
            gai.rate_limiter.record_voice_transcription()
            message_text = transcript.strip()
            reason = gai.prefilter_reason(
                chat_id=message.chat.id,
                chat_type=chat_type,
                text=message_text,
                from_user_id=user.id if user else None,
                from_user_is_bot=bool(user.is_bot) if user else True,
                sender_chat_id=message.sender_chat.id if message.sender_chat else None,
            )
            if reason is not None:
                _log_decision(message, user.id if user else None, "prefiltered", reason)
                return

        is_admin = settings.is_admin(user.id) if user else True
        is_bot = bool(user.is_bot) if user else True
        whitelist = await gai.get_moderation_whitelist(session)
        user_exempt = is_admin or is_bot or gai.is_user_whitelisted(user.id, whitelist)
        mod_enabled = await gai.is_moderation_enabled(session)

        # Disparagement / competitor-promotion deterministic hits run before sensitive-topic deferral
        # (so public scam accusations / competitor redirects trigger delete+notice+strike instead of helpdesk escalation).
        det_category, _det_conf = (
            gai.detect_moderation_violation(message_text)
            if (mod_enabled and not user_exempt)
            else (None, 0.0)
        )
        if det_category in gai.MODERATOR_DELETE_NOTICE_CATEGORIES:
            await _execute_moderation(
                message,
                bot,
                session=session,
                user=user,
                category=det_category,
                text=message_text or "",
            )
            return

        # Hard rule: payment/refund/account topics never reach the AI and NEVER enter selling moderation.
        if gai.detect_sensitive_topic(message_text):
            _log_decision(message, user.id if user else None, "escalate", "sensitive")
            await _escalate(
                message,
                bot,
                reason="sensitive",
                deferral_text=gai.DEFERRAL_TEXT_SENSITIVE,
                session=session,
                text_override=message_text,
                is_group=True,
            )
            return

        # Moderation Layer (a): Deterministic rules check (runs even during reply cooldown!)
        if det_category is not None:
            await _execute_moderation(
                message,
                bot,
                session=session,
                user=user,
                category=det_category,
                text=message_text or "",
            )
            return

        if not gai.rate_limiter.can_reply(user.id, message.message_id):
            return

        # Conversation context for the AI (in-memory only): snapshot BEFORE
        # pushing this message, and only push clean messages (after moderation checks).
        context_lines = gai.context_buffer.snapshot(message.chat.id)
        reply_to = message.reply_to_message
        replied_to_text = (getattr(reply_to, "text", None) or None) if reply_to else None

        fast_path_text = None
        fast_path_kind = None
        if gai.is_greeting_only(message_text):
            fast_path_text = gai.WELCOME_TEXT
            fast_path_kind = "greeting"
        elif gai.is_starter_fragment(message_text):
            fast_path_text = gai.STARTER_POINTER_TEXT
            fast_path_kind = "starter"
        elif gai.maintenance_mode_on() and gai.is_service_availability_question(
            message_text,
            context_lines=context_lines,
            replied_to_text=replied_to_text,
        ):
            fast_path_text = gai.build_maintenance_reply()
            fast_path_kind = "maintenance"
        if fast_path_text is not None:
            gai.rate_limiter.reset_ai_failures(user.id)
            gai.context_buffer.push(message.chat.id, user.first_name or "", message_text or "")
            await message.reply(fast_path_text, **_plain_send_kw(message))
            gai.rate_limiter.record_reply(user.id, message.message_id)
            _log_decision(message, user.id, f"fast_path:{fast_path_kind}")
            return

        is_reply_to_bot = bool(
            reply_to
            and reply_to.from_user
            and (reply_to.from_user.username or "").lower() == gai.BOT_USERNAME
        )
        bot_mentioned = f"@{gai.BOT_USERNAME}" in (message_text or "").lower()

        async with _session_or_new(session) as s:
            faq_hit = await gai.find_faq_answer(s, message_text or "")
            if faq_hit is not None:
                faq_id, faq_answer = faq_hit
                answer_text = gai.strip_emoji(faq_answer)
                if answer_text:
                    gai.rate_limiter.reset_ai_failures(user.id)
                    gai.context_buffer.push(message.chat.id, user.first_name or "", message_text or "")
                    await message.reply(answer_text, **_plain_send_kw(message))
                    gai.rate_limiter.record_reply(user.id, message.message_id)
                    await gai.record_faq_hit(s, faq_id)
                    _log_decision(message, user.id, "faq_hit")
                    return

        if not gai.rate_limiter.ai_budget_available():
            _log_decision(message, user.id, "no_reply", "ai_budget_exhausted")
            return
        gai.rate_limiter.record_ai_call()

        decision_res = await gai.ask_group_admin_ai(
            text=message_text or "",
            author_name=user.first_name or "",
            bot_mentioned=bot_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            context_lines=context_lines,
            replied_to_text=replied_to_text,
        )

        decision = decision_res[0]
        reply_text = decision_res[1]
        mod_verdict = getattr(decision_res, "moderation", None) or {"category": "none", "confidence": 0.0}

        # Moderation Layer (b): Gemini verdict check (never push spam into context buffer!)
        if mod_enabled and not user_exempt:
            ai_category = mod_verdict.get("category", "none")
            ai_conf = float(mod_verdict.get("confidence", 0.0))
            if ai_category in gai.MODERATION_CATEGORIES:
                threshold = await gai.get_moderation_threshold(session)
                if ai_conf >= threshold:
                    await _execute_moderation(
                        message,
                        bot,
                        session=session,
                        user=user,
                        category=ai_category,
                        text=message_text or "",
                    )
                    return
                else:
                    _log_decision(message, user.id, "moderation_ignored", f"{ai_category}:{ai_conf:.2f}_below_threshold")
                    if decision != gai.DECISION_REPLY or not reply_text:
                        return

        if decision == gai.DECISION_AI_FAILED:
            consec_fails = gai.rate_limiter.record_ai_failure(user.id)
            if consec_fails >= 2:
                gai.rate_limiter.undo_last_ai_call()
            gai.context_buffer.push_dedup(message.chat.id, user.first_name or "", message_text or "")
            if gai.is_high_intent_group_message(
                message_text,
                bot_mentioned=bot_mentioned,
                is_reply_to_bot=is_reply_to_bot,
                consecutive_ai_failures=consec_fails,
            ):
                await _deliver_ai_failed_holding(
                    message,
                    bot,
                    limiter=gai.rate_limiter,
                    text=message_text or "",
                    is_group=True,
                )
            _log_decision(message, user.id, "ai_failed", "ai_call")
            return

        gai.rate_limiter.reset_ai_failures(user.id)
        gai.context_buffer.push(message.chat.id, user.first_name or "", message_text or "")
        if decision == gai.DECISION_ESCALATE:
            _log_decision(message, user.id, "escalate", "ai_escalate")
            await _escalate(
                message,
                bot,
                reason="ai_escalate",
                deferral_text=gai.DEFERRAL_TEXT_UNSURE,
                session=session,
                text_override=message_text,
                is_group=True,
            )
            return
        if decision != gai.DECISION_REPLY or not reply_text:
            _log_decision(message, user.id, "no_reply", "ai_decision")
            return

        # Owner rule (2026-11-02): zero emoji in group replies. The
        # service sanitises parsed AI output already; strip again here
        # so the send path itself is guaranteed clean. A reply that is
        # only emoji becomes empty and is treated as silence.
        reply_text = gai.strip_emoji(reply_text)
        if not reply_text:
            _log_decision(message, user.id, "no_reply", "empty_reply")
            return

        # Re-check the cap at send time (another reply may have landed
        # while the AI was thinking), then answer as a threaded reply.
        if not gai.rate_limiter.can_reply(user.id, message.message_id):
            _log_decision(message, user.id, "no_reply", "cooldown_recheck")
            return
        await message.reply(reply_text, **_plain_send_kw(message))
        gai.rate_limiter.record_reply(user.id, message.message_id)
        _log_decision(message, user.id, "reply", "ai")
    except Exception as exc:
        logger.warning("group_ai_admin: handler failed (silence): %s", exc)
    finally:
        # Batch AJ (M12): persist any limiter changes from this
        # message (debounced + dirty-checked; never raises).
        await _flush_limiter(session)


@group_admin_router.callback_query(F.data.startswith("gai_ban:"))
async def handle_moderation_ban_callback(
    callback_query: CallbackQuery,
    bot: Bot | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Execute permanent ban on admin button tap from Strike 3 escalation DM."""
    admin_user = callback_query.from_user
    if not settings.is_admin(admin_user.id):
        await callback_query.answer("Admin များသာ ban ပြုလုပ်နိုင်ပါသည်။", show_alert=True)
        return

    parts = (callback_query.data or "").split(":")
    if len(parts) != 4:
        await callback_query.answer("Invalid callback data", show_alert=True)
        return

    try:
        strike_id = int(parts[1])
        chat_id = int(parts[2])
        target_user_id = int(parts[3])
    except (ValueError, IndexError):
        await callback_query.answer("Invalid ID parameters", show_alert=True)
        return

    sender = bot or callback_query.bot
    try:
        await sender.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
    except Exception as exc:
        logger.warning("group_ai_admin: ban_chat_member failed: %s", exc)
        await callback_query.answer(f"Ban မအောင်မြင်ပါ: {exc}", show_alert=True)
        return

    # Update DB strike row + write AuditLog
    async with _session_or_new(session) as s:
        from sqlalchemy import select
        from app.models.schema import AuditLog, GroupAiModerationStrike, utc_now

        strike = (
            await s.execute(
                select(GroupAiModerationStrike).where(GroupAiModerationStrike.id == strike_id)
            )
        ).scalar_one_or_none()
        if strike is not None:
            strike.banned_by = admin_user.id
            strike.banned_at = utc_now()
        s.add(
            AuditLog(
                admin_id=admin_user.id,
                action="group_mod.gai_ban",
                target_type="group_member",
                target_id=str(target_user_id),
                details_json=json.dumps(
                    {
                        "chat_id": chat_id,
                        "strike_id": strike_id,
                        "source": "gai_ban",
                    }
                ),
            )
        )
        await s.commit()

    # Update the escalation DM with 1-tap undo button reusing gmod_undo:unban:
    if callback_query.message:
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            from app.services import group_moderation as gmod

            admin_label = admin_user.username or str(admin_user.id)
            current_text = getattr(callback_query.message, "html_text", None) or callback_query.message.text or ""
            updated_text = f"{current_text}\n\n🔨 <b>Banned by @{admin_label}</b>"
            undo_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="↩️ ပြန်ဖြည်မည်",
                            callback_data=f"{gmod.UNDO_CALLBACK_PREFIX}:unban:{int(chat_id)}:{int(target_user_id)}",
                        )
                    ]
                ]
            )
            await callback_query.message.edit_text(
                updated_text,
                parse_mode="HTML",
                reply_markup=undo_kb,
            )
        except Exception as exc:
            logger.debug("group_ai_admin: failed to edit escalation DM after ban: %s", exc)

    await callback_query.answer("အသုံးပြုသူကို Group မှ အပြီး ပိတ်သိမ်း (Ban) လိုက်ပါပြီ")


# ---------------------------------------------------------------------------
# Bot private-chat AI replies (Batch BX) — same router, same "last
# word" position: commands and FSM flows have already had their
# chance by the time a plain-text private message lands here.
# ---------------------------------------------------------------------------


def _log_private_decision(message: Message, user_id: int | None, decision: str, reason: str = "") -> None:
    """One INFO line per terminal private-AI decision — ids + reason
    class only, NEVER the message text (the no-storage rule)."""
    logger.info(
        "private_ai: decision=%s reason=%s chat_id=%s message_id=%s user_id=%s",
        decision,
        reason or "-",
        message.chat.id,
        message.message_id,
        user_id,
    )


async def _record_private_exchange(
    session: AsyncSession | None,
    message: Message,
    *,
    reply_text: str,
    kind: str,
) -> None:
    """Batch CD: persist the exchange the private-AI chain just
    handled (user text + the reply actually sent) for the admin
    user-detail chat log. Failure-isolated twice over — the service
    swallows its own errors and this wrapper swallows the rest — so
    recording can never break or delay the reply. PRIVATE chain
    only: the group handler never calls this (group messages are
    never stored — the standing ban)."""
    try:
        user = message.from_user
        if user is None:
            return
        async with _session_or_new(session) as s:
            await pai.record_private_exchange(
                s,
                user_id=user.id,
                user_text=message.text or "",
                reply_text=reply_text,
                kind=kind,
            )
    except Exception as exc:
        logger.warning("private_ai: transcript record failed (dropped): %s", exc)


async def _user_language(session: AsyncSession | None, user_id: int) -> str:
    """The user's stored language for the localised deferral lines."""
    try:
        async with _session_or_new(session) as s:
            from app.services.ledger import get_or_create_user

            user, _ = await get_or_create_user(s, user_id)
            return user.language_code or "my"
    except Exception:
        return "my"


# Batch CU: the FSM edit-text handler is registered BEFORE the
# private-AI catch-all below on purpose — aiogram matches handlers
# in registration order and a matched handler consumes the update
# even when it returns early, so while this state-filtered handler
# sat after the catch-all, an admin's edited text was swallowed
# by the catch-all's FSM guard and the edit could never complete.
@group_admin_router.message(GroupAiEditStates.waiting_text, F.chat.type == ChatType.PRIVATE)
async def handle_group_ai_edit_text(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    """The claiming admin's corrected draft arrives as a private text.

    The text is sanitised through the same emoji-strip path as any
    group post, saved as the record's draft_text (the AI original
    stays in original_draft_text), the claim is released back to
    PENDING, and every admin gets a fresh DM with the updated draft
    and a live keyboard — anyone may then approve or dismiss it.
    """
    try:
        admin = message.from_user
        if admin is None or not settings.is_admin(admin.id):
            return
        if state is None:
            return
        data = await state.get_data()
        escalation_id = data.get("escalation_id")
        if not escalation_id:
            await state.clear()
            return
        sender = bot or message.bot

        raw = (message.text or "").strip()
        if raw == "/cancel":
            async with _session_or_new(session) as s:
                await _release_edit_and_refresh(
                    bot=sender,
                    session=s,
                    escalation_id=int(escalation_id),
                    admin_id=admin.id,
                    admin_name=admin.first_name or "Admin",
                    new_draft=None,
                )
            await state.clear()
            await message.answer(gai.ADMIN_CONFIRM_EDIT_CANCELLED)
            return

        cleaned = gai.strip_emoji(raw)
        if not cleaned:
            await message.answer(gai.ADMIN_EDIT_EMPTY)
            return

        async with _session_or_new(session) as s:
            snapshot = await _release_edit_and_refresh(
                bot=sender,
                session=s,
                escalation_id=int(escalation_id),
                admin_id=admin.id,
                admin_name=admin.first_name or "Admin",
                new_draft=cleaned,
            )
        await state.clear()
        if snapshot is None:
            await message.answer(gai.ADMIN_ALERT_EDIT_GONE)
            return
        await message.answer(gai.ADMIN_CONFIRM_EDIT_SAVED)
    except Exception as exc:
        logger.warning("group_ai_admin: edit text handling failed: %s", exc)



async def _load_private_history(
    session: AsyncSession | None, user_id: int, *, limit: int = 6
) -> list[tuple[str, str]]:
    """Fetch the user's most recent private AI exchanges in chronological order."""
    try:
        from sqlalchemy import select
        from app.models.schema import PrivateAiMessage

        async with _session_or_new(session) as s:
            rows = (
                await s.execute(
                    select(PrivateAiMessage)
                    .where(PrivateAiMessage.user_id == int(user_id))
                    .order_by(PrivateAiMessage.id.desc())
                    .limit(limit)
                )
            ).scalars().all()
        return [(str(r.direction or "user"), str(r.text or "")) for r in reversed(rows)]
    except Exception:
        return []


@group_admin_router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_private_message(
    message: Message,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    """Plain-text private chat: greetings + service questions get an
    AI answer in Burmese; payment/refund/account topics are deferred
    to the admins and NEVER answered by the AI; any failure degrades
    to silence. Mirrors handle_group_message step for step, on the
    private limiter/kill-switch (app/services/private_ai.py)."""
    try:
        # A user mid-flow in an FSM conversation owns their messages
        # (clone text input, payment slips, admin edit prompts…).
        if state is not None:
            try:
                if await state.get_state():
                    return
            except Exception:
                pass

        user = message.from_user

        # Batch BY: payment-intent hint, folded in from the old
        # payment.py catch-all (deleted there). That handler sat on
        # payment_router — included BEFORE this router — with a
        # bare (no-state, private, text) filter, so it consumed EVERY
        # plain private text even when it returned silently, and this
        # handler never fired in production (BX eyes-on FAIL 1: zero
        # private_ai log lines while the toggle was ON). This handler
        # is now the single terminal owner of plain private text, so
        # the hint check lives here, right after the FSM-state return
        # and BEFORE the AI prefilter: a user mid-purchase (recent
        # <24h persisted intent, no live FSM state) gets exactly the
        # polite screenshot request the old handler sent, in their
        # language, and the AI never also answers. The check runs
        # before the private-AI kill switch on purpose — the hint
        # predates the AI feature and must not depend on its toggle.
        if user is not None:
            intent = None
            hint_lang = "my"
            async with _session_or_new(session) as s:
                intent = await get_recent_payment_intent(s, user.id)
                if intent is not None:
                    from app.services.ledger import get_or_create_user

                    hint_user, _ = await get_or_create_user(s, user.id)
                    hint_lang = hint_user.language_code or "my"
            if intent is not None:
                await message.answer(
                    _bot_t("pay_receipt_wrong_content", hint_lang),
                    parse_mode="HTML",
                )
                _log_private_decision(message, user.id, "hint", "payment_intent")
                return

        chat_type = getattr(message.chat.type, "value", str(message.chat.type))
        reason = pai.prefilter_private_reason(
            chat_type=chat_type,
            text=message.text,
            from_user_id=user.id if user else None,
            from_user_is_bot=bool(user.is_bot) if user else True,
            sender_chat_id=message.sender_chat.id if message.sender_chat else None,
        )
        if reason is not None:
            return

        async with _session_or_new(session) as s:
            if not await pai.is_private_ai_enabled(s):
                return
            limits = await pai.get_private_limits(s)
        pai.private_rate_limiter.update_limits(
            gai.GroupAiLimits(
                user_cooldown_seconds=limits.user_cooldown_seconds,
                hourly_budget=limits.hourly_budget,
                daily_budget=limits.daily_budget,
            )
        )
        lang = await _user_language(session, user.id)

        # Hard rule first: payment/refund/account topics never reach
        # the AI — defer to the user and hand the question to a human.
        if gai.detect_sensitive_topic(message.text):
            _log_private_decision(message, user.id, "escalate", "sensitive")
            deferral_text = _bot_t("private_ai_deferral_sensitive", lang)
            # _escalate posts the deferral only when the limiter's
            # deferral cooldown allows it — record the exchange only
            # when the deferral will actually be posted.
            will_defer = pai.private_rate_limiter.can_defer(user.id)
            await _escalate(
                message,
                bot,
                reason="sensitive",
                deferral_text=deferral_text,
                session=session,
                limiter=pai.private_rate_limiter,
                dm_link=None,
                is_group=False,
            )
            if will_defer:
                await _record_private_exchange(
                    session, message,
                    reply_text=deferral_text, kind=pai.KIND_ESCALATION,
                )
            return

        if not pai.private_rate_limiter.can_reply(user.id, message.message_id):
            return

        # Deterministic fast paths (no AI budget): greeting, starter
        # fragment, maintenance availability — same placement as the
        # group handler (after the sensitive check + cooldown).
        fast_path_text = None
        fast_path_kind = None
        if gai.is_greeting_only(message.text):
            fast_path_text = pai.PRIVATE_WELCOME_TEXT
            fast_path_kind = "greeting"
        elif gai.is_starter_fragment(message.text):
            fast_path_text = gai.STARTER_POINTER_TEXT
            fast_path_kind = "starter"
        elif gai.maintenance_mode_on() and gai.is_service_availability_question(message.text):
            fast_path_text = gai.build_maintenance_reply()
            fast_path_kind = "maintenance"
        if fast_path_text is not None:
            await message.reply(fast_path_text, **_plain_send_kw(message))
            pai.private_rate_limiter.record_reply(user.id, message.message_id)
            _log_private_decision(message, user.id, f"fast_path:{fast_path_kind}")
            await _record_private_exchange(
                session, message,
                reply_text=fast_path_text, kind=pai.KIND_FAST_PATH,
            )
            return

        # AI answers are budget-gated; fast paths and deferrals above
        # are unaffected — they need no AI call.
        if not pai.private_rate_limiter.ai_budget_available():
            _log_private_decision(message, user.id, "no_reply", "ai_budget_exhausted")
            return
        pai.private_rate_limiter.record_ai_call()

        history = await _load_private_history(session, user.id)
        replied_to_text = getattr(getattr(message, "reply_to_message", None), "text", None)
        ask_fn = pai.ask_private_ai
        ask_kwargs: dict[str, object] = {
            "text": message.text or "",
            "author_name": user.first_name or "",
        }
        try:
            sig = inspect.signature(ask_fn)
            if "history" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                ask_kwargs["history"] = history
            if "reply_to_text" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                ask_kwargs["reply_to_text"] = replied_to_text
        except Exception:
            pass
        decision, reply_text = await ask_fn(**ask_kwargs)

        if decision == gai.DECISION_AI_FAILED:
            consec_fails = pai.private_rate_limiter.record_ai_failure(user.id)
            if consec_fails >= 2:
                pai.private_rate_limiter.undo_last_ai_call()
            await _deliver_ai_failed_holding(
                message,
                bot,
                limiter=pai.private_rate_limiter,
                text=message.text or "",
                is_group=False,
            )
            _log_private_decision(message, user.id, "ai_failed", "ai_call")
            return

        pai.private_rate_limiter.reset_ai_failures(user.id)
        if decision == gai.DECISION_ESCALATE:
            _log_private_decision(message, user.id, "escalate", "ai_escalate")
            deferral_text = _bot_t("private_ai_deferral_unsure", lang)
            will_defer = pai.private_rate_limiter.can_defer(user.id)
            await _escalate(
                message,
                bot,
                reason="ai_escalate",
                deferral_text=deferral_text,
                session=session,
                limiter=pai.private_rate_limiter,
                dm_link=None,
                is_group=False,
            )
            if will_defer:
                await _record_private_exchange(
                    session, message,
                    reply_text=deferral_text, kind=pai.KIND_ESCALATION,
                )
            return
        if decision != gai.DECISION_REPLY or not reply_text:
            _log_private_decision(message, user.id, "no_reply", "ai_decision")
            return

        reply_text = gai.strip_emoji(reply_text)
        if not reply_text:
            _log_private_decision(message, user.id, "no_reply", "empty_reply")
            return

        # Re-check the cap at send time, then answer.
        if not pai.private_rate_limiter.can_reply(user.id, message.message_id):
            _log_private_decision(message, user.id, "no_reply", "cooldown_recheck")
            return
        await message.reply(reply_text, **_plain_send_kw(message))
        pai.private_rate_limiter.record_reply(user.id, message.message_id)
        _log_private_decision(message, user.id, "reply", "ai")
        await _record_private_exchange(
            session, message,
            reply_text=reply_text, kind=pai.KIND_AI_REPLY,
        )
    except Exception as exc:
        logger.warning("private_ai: handler failed (silence): %s", exc)


# ---------------------------------------------------------------------------
# Escalation buttons: approve / dismiss / edit / cancel-edit
# ---------------------------------------------------------------------------


@group_admin_router.callback_query(F.data.startswith(f"{gai.ESCALATION_CALLBACK_PREFIX}:"))
async def handle_escalation_callback(
    callback: CallbackQuery,
    state: FSMContext = None,
    session: AsyncSession | None = None,
    bot: Bot = None,
) -> None:
    """Button taps on escalation DMs.

    Bot admins only. First tap wins (the service claims the pending
    record atomically); a later tap is told the item is handled, and
    every state change also rewrites all stored DM copies (status
    line + keyboard cleared) so stale buttons disappear everywhere.
    Approving posts the (possibly admin-edited) draft as a reply to
    the original group message and saves the pair into the FAQ cache.
    """
    try:
        parsed = gai.parse_escalation_callback(callback.data)
        if parsed is None:
            await callback.answer(gai.ADMIN_ALERT_BAD_BUTTON, show_alert=True)
            return
        action, escalation_id = parsed

        admin = callback.from_user
        if admin is None or not settings.is_admin(admin.id):
            await callback.answer(gai.ADMIN_ALERT_NOT_ADMIN, show_alert=True)
            return
        sender = bot or callback.bot
        admin_name = admin.first_name or "Admin"

        if action == "cancel":
            await _handle_edit_cancel(
                callback,
                sender,
                state=state,
                session=session,
                escalation_id=escalation_id,
                admin_id=admin.id,
                admin_name=admin_name,
            )
            return

        async with _session_or_new(session) as s:
            if action == "edit":
                await _handle_edit_claim(
                    callback,
                    sender,
                    state=state,
                    session=s,
                    escalation_id=escalation_id,
                    admin_id=admin.id,
                    admin_name=admin_name,
                )
                return

            outcome, record = await gai.claim_escalation(
                s, escalation_id, admin_id=admin.id, approve=(action == "approve")
            )
            if outcome == "not_found":
                await callback.answer(gai.ADMIN_ALERT_NOT_FOUND, show_alert=True)
                return
            if outcome == "expired":
                await _sync_admin_copies(sender, record, status_line=gai.STATUS_LINE_EXPIRED)
                await callback.answer(gai.ADMIN_ALERT_EXPIRED, show_alert=True)
                return
            if outcome != "claimed" or record is None:
                # Backstop for a copy whose keyboard could not be
                # cleared when the state changed: clear THIS copy now.
                await _clear_keyboard(callback)
                await callback.answer(gai.ADMIN_ALERT_ALREADY_HANDLED, show_alert=True)
                return

            if action == "dismiss":
                await _sync_admin_copies(
                    sender,
                    record,
                    status_line=gai.STATUS_LINE_DISMISSED.format(name=admin_name),
                )
                await callback.answer(gai.ADMIN_CONFIRM_DISMISSED)
                return

            draft_text = gai.strip_emoji(record.get("draft_text") or "")
            if not draft_text:
                await callback.answer(gai.ADMIN_ALERT_NO_DRAFT, show_alert=True)
                return

            try:
                await sender.send_message(
                    record["chat_id"],
                    draft_text,
                    reply_to_message_id=record["message_id"],
                    **_plain_send_kw(sender),
                )
            except Exception as exc:
                # The record stays APPROVED (this admin claimed it);
                # the draft is still in the DM for a manual repost.
                logger.warning("group_ai_admin: approved draft post failed: %s", exc)
                await callback.answer(gai.ADMIN_ALERT_POST_FAILED, show_alert=True)
                return

            await _sync_admin_copies(
                sender,
                record,
                status_line=gai.STATUS_LINE_POSTED.format(name=admin_name),
            )

            # Only a successfully posted, admin-approved GROUP answer
            # may enter the shared FAQ cache (private 1:1 escalations
            # must never pollute group FAQs; log them to the private thread).
            esc_chat_id = int(record.get("chat_id") or 0)
            if esc_chat_id < 0:
                await gai.save_faq_pair(
                    s,
                    question_text=record.get("question_text") or "",
                    answer_text=draft_text,
                )
            elif esc_chat_id > 0:
                try:
                    await pai.record_private_exchange(
                        s,
                        user_id=int(record.get("author_id") or esc_chat_id),
                        user_text=record.get("question_text") or "",
                        reply_text=draft_text,
                        kind=pai.KIND_ESCALATION,
                    )
                except Exception:
                    pass

        await callback.answer(gai.ADMIN_CONFIRM_POSTED)
    except Exception as exc:
        logger.warning("group_ai_admin: escalation callback failed: %s", exc)
        try:
            await callback.answer(gai.ADMIN_ALERT_BAD_BUTTON, show_alert=True)
        except Exception:
            pass


async def _handle_edit_claim(
    callback: CallbackQuery,
    bot: Bot,
    *,
    state: FSMContext | None,
    session: AsyncSession,
    escalation_id: int,
    admin_id: int,
    admin_name: str,
) -> None:
    """✏️ tap: claim the record, prompt the claimer in DM via FSM."""
    # Abandoning a previous edit claim (same admin tapped edit on a
    # second escalation) releases it back to pending first — with the
    # usual copy sync + fresh DMs, so it never strands without a
    # live keyboard.
    if state is not None:
        try:
            data = await state.get_data()
            previous_id = data.get("escalation_id")
            if previous_id and int(previous_id) != int(escalation_id):
                released_prev, prev_snapshot = await gai.release_escalation_edit(
                    session, int(previous_id), admin_id=admin_id
                )
                await state.clear()
                if released_prev and prev_snapshot is not None:
                    await _sync_admin_copies(
                        bot,
                        prev_snapshot,
                        status_line=gai.STATUS_LINE_EDIT_CANCELLED.format(name=admin_name),
                    )
                    await _resend_escalation_dms(bot, session, prev_snapshot)
        except Exception:
            pass

    outcome, record = await gai.claim_escalation_for_edit(
        session, escalation_id, admin_id=admin_id
    )
    if outcome == "not_found":
        await callback.answer(gai.ADMIN_ALERT_NOT_FOUND, show_alert=True)
        return
    if outcome == "expired":
        await _sync_admin_copies(bot, record, status_line=gai.STATUS_LINE_EXPIRED)
        await callback.answer(gai.ADMIN_ALERT_EXPIRED, show_alert=True)
        return
    if outcome not in ("claimed", "reclaimed") or record is None:
        await _clear_keyboard(callback)
        await callback.answer(gai.ADMIN_ALERT_ALREADY_HANDLED, show_alert=True)
        return

    # Every admin's copy loses its keyboard while the edit runs.
    await _sync_admin_copies(
        bot, record, status_line=gai.STATUS_LINE_EDITING.format(name=admin_name)
    )

    if state is not None:
        await state.set_state(GroupAiEditStates.waiting_text)
        await state.update_data(escalation_id=int(escalation_id))
    try:
        await bot.send_message(
            admin_id,
            gai.EDIT_PROMPT_TEXT.format(draft=html.escape(record.get("draft_text") or "")),
            parse_mode="HTML",
            reply_markup=gai.build_edit_cancel_keyboard(escalation_id),
        )
    except Exception as exc:
        logger.warning("group_ai_admin: edit prompt DM failed: %s", exc)
    await callback.answer(gai.ADMIN_CONFIRM_EDIT_CLAIMED)


async def _handle_edit_cancel(
    callback: CallbackQuery,
    bot: Bot,
    *,
    state: FSMContext | None,
    session: AsyncSession | None,
    escalation_id: int,
    admin_id: int,
    admin_name: str,
) -> None:
    """❌ tap on the edit prompt: release the claim (owner-only)."""
    async with _session_or_new(session) as s:
        snapshot = await _release_edit_and_refresh(
            bot=bot,
            session=s,
            escalation_id=escalation_id,
            admin_id=admin_id,
            admin_name=admin_name,
            new_draft=None,
        )
        if snapshot is None:
            # Not this admin's claim (or no claim at all). Distinguish
            # "someone else is editing" from "nothing to cancel".
            from app.models.schema import GroupAiEscalation

            row = await s.get(GroupAiEscalation, int(escalation_id))
            if row is not None and row.status == "EDITING":
                await callback.answer(gai.ADMIN_ALERT_NOT_EDITOR, show_alert=True)
            else:
                await callback.answer(gai.ADMIN_ALERT_EDIT_GONE, show_alert=True)
            return
    if state is not None:
        try:
            await state.clear()
        except Exception:
            pass
    await callback.answer(gai.ADMIN_CONFIRM_EDIT_CANCELLED)


async def _clear_keyboard(callback: CallbackQuery) -> None:
    """Remove the buttons from a handled escalation DM (best-effort)."""
    try:
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
