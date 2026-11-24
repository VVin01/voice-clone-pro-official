"""Manual admin moderation handlers (Option 3 Hybrid).

Attached as a sub-router of ``admin_router`` so it executes strictly BEFORE
``group_admin_router`` without changing the top-level router tuple in
``app/handlers/__init__.py`` or touching ``prefilter_reason()`` /
``_execute_moderation()``.

Key invariants:
* Filter-level admin and chat gating: non-admin messages never match these
  handlers and fall through identically to nonexistent commands (no oracle).
* Private DM commands carry ``StateFilter(None)`` so active FSM flows
  (``PaymentReviewFSM``, ``GroupAiEditStates``) are never hijacked.
* Kill-switch independent: does NOT consult ``group_ai_admin_enabled`` or
  ``group_ai_moderation_enabled``.
* Zero ``edited_message`` handlers anywhere.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.services import group_ai_admin as gai
from app.services import group_moderation as gmod

logger = logging.getLogger(__name__)

group_moderation_router = Router(name="group_moderation")


# ---------------------------------------------------------------------------
# Filter-level gating (Constraint 2: non-admins see zero new surface / oracle)
# ---------------------------------------------------------------------------


class IsAdminFilter(BaseFilter):
    """Match only non-anonymous, non-bot users listed in ``settings.admin_ids``."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if isinstance(event, Message) and getattr(event, "sender_chat", None) is not None:
            return False
        user = getattr(event, "from_user", None)
        if user is None or getattr(user, "is_bot", False):
            return False
        return bool(settings.is_admin(user.id))


class IsCommunityGroupFilter(BaseFilter):
    """Match only messages inside the configured community group."""

    async def __call__(self, message: Message) -> bool:
        chat = getattr(message, "chat", None)
        if chat is None:
            return False
        chat_type = getattr(chat.type, "value", str(chat.type or ""))
        if chat_type not in ("group", "supergroup"):
            return False
        return gai.is_community_group(chat.id)


# ---------------------------------------------------------------------------
# Session & Telegram delivery helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _session_or_new(session: AsyncSession | None):
    if session is not None:
        yield session
    else:
        async with AsyncSessionLocal() as s:
            yield s


async def _best_effort_delete(bot: Any, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug(
            "group_moderation: best-effort delete_message(%s, %s) skipped: %s",
            chat_id,
            message_id,
            exc,
        )


async def _deliver_admin_dm(
    bot: Any,
    actor_id: int,
    text: str,
    *,
    reply_markup: Any = None,
) -> bool:
    """Deliver honest Burmese feedback by DM; fall back to other admins, never the group."""
    try:
        await bot.send_message(
            actor_id,
            text,
            parse_mode=None,
            reply_markup=reply_markup,
        )
        return True
    except Exception as exc:
        logger.warning(
            "group_moderation: failed to DM issuing admin %s: %s",
            actor_id,
            exc,
        )

    fallback_text = f"{gmod.DM_FALLBACK_HEADER.format(actor_id=actor_id)}\n{text}"
    delivered_fallback = False
    for other_admin_id in sorted(settings.admin_ids):
        if int(other_admin_id) == int(actor_id):
            continue
        try:
            await bot.send_message(
                other_admin_id,
                fallback_text,
                parse_mode=None,
                reply_markup=reply_markup,
            )
            delivered_fallback = True
        except Exception as fb_exc:
            logger.warning(
                "group_moderation: fallback DM to admin %s failed: %s",
                other_admin_id,
                fb_exc,
            )
    return delivered_fallback


def _extract_command_and_args(
    message: Message,
    command: CommandObject | None,
) -> tuple[str, str]:
    if command is not None and command.command:
        return command.command.lower(), (command.args or "")
    raw = (getattr(message, "text", None) or "").strip()
    parts = raw.split(maxsplit=1)
    head = parts[0] if parts else ""
    cmd_name = head.lstrip("/").split("@", 1)[0].lower()
    args_text = parts[1] if len(parts) > 1 else ""
    return cmd_name, args_text


# ---------------------------------------------------------------------------
# Shared command orchestration (group & private DM)
# ---------------------------------------------------------------------------


async def _handle_moderation_command(
    message: Message,
    command: CommandObject | None,
    *,
    is_dm: bool,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
) -> None:
    sender = bot or message.bot
    actor = message.from_user
    if actor is None or sender is None:
        return
    actor_id = int(actor.id)
    op, args_text = _extract_command_and_args(message, command)
    if op not in ("mute", "unmute", "ban", "unban"):
        return

    invoking_chat_id = int(message.chat.id)
    invoking_message_id = getattr(message, "message_id", None)
    target_chat_id = gai.resolve_group_chat_id() if is_dm else invoking_chat_id
    source = "dm_cmd" if is_dm else "group_cmd"

    try:
        # 1. Stale-command protection (drop_pending_updates=False safety)
        if gmod.is_command_stale(getattr(message, "date", None)):
            await _deliver_admin_dm(sender, actor_id, gmod.DM_ERR_STALE_COMMAND)
            return

        # 2. Target resolution (reply -> text_mention -> numeric user_id; NO @username)
        try:
            target = gmod.resolve_target_from_message(message, args_text, is_dm=is_dm)
        except gmod.TargetResolutionError as err:
            await _deliver_admin_dm(sender, actor_id, err.message)
            return

        # 3. Duration parsing for /mute
        duration_seconds: int | None = None
        if op == "mute":
            duration_token = target.remaining_args[0] if target.remaining_args else None
            try:
                duration_seconds = gmod.parse_manual_duration(duration_token)
            except gmod.DurationError as err:
                await _deliver_admin_dm(sender, actor_id, err.message)
                return

        # 4. Execute through shared moderation service
        action_fn = {
            "mute": gmod.manual_mute,
            "unmute": gmod.manual_unmute,
            "ban": gmod.manual_ban,
            "unban": gmod.manual_unban,
        }[op]

        async with _session_or_new(session) as s:
            result = await action_fn(
                sender,
                s,
                chat_id=target_chat_id,
                target_id=target.user_id,
                actor_id=actor_id,
                duration_seconds=duration_seconds,
                source=source,
                reply_message_id=target.reply_message_id,
                target_first_name=target.first_name,
                target_username=target.username,
            )

        # 5. Offending-message deletion (owner requirement 2026-11-24 ~13:45):
        #    ONLY on successful reply-invoked in-group /mute or /ban.
        if result.ok and not is_dm and op in ("mute", "ban") and target.reply_message_id is not None:
            await _best_effort_delete(sender, target_chat_id, target.reply_message_id)

        # 6. Public group notice (owner decision 1):
        #    EXACTLY ONE polite Burmese line on successful in-group /mute or /ban only.
        if result.ok and not is_dm and op in ("mute", "ban"):
            notice = gmod.build_group_notice(result)
            if notice:
                try:
                    await sender.send_message(target_chat_id, notice, parse_mode=None)
                except Exception as notice_exc:
                    logger.warning(
                        "group_moderation: failed to post group notice in %s: %s",
                        target_chat_id,
                        notice_exc,
                    )

        # 7. Admin DM confirmation or honest failure report (with 1-tap undo on mute/ban)
        kb = (
            gmod.build_undo_keyboard(result.action, result.chat_id, result.target_id)
            if result.ok and result.action in ("mute", "ban")
            else None
        )
        await _deliver_admin_dm(sender, actor_id, result.message, reply_markup=kb)
    finally:
        # Best-effort delete the admin's invoking command message in group (success OR failure)
        if not is_dm:
            await _best_effort_delete(sender, invoking_chat_id, invoking_message_id)


# ---------------------------------------------------------------------------
# Registered command & callback handlers
# ---------------------------------------------------------------------------


@group_moderation_router.message(
    IsCommunityGroupFilter(),
    IsAdminFilter(),
    Command("mute", "unmute", "ban", "unban"),
)
async def handle_group_moderation_command(
    message: Message,
    command: CommandObject | None = None,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
) -> None:
    """In-group admin moderation commands (/mute, /unmute, /ban, /unban)."""
    await _handle_moderation_command(
        message,
        command,
        is_dm=False,
        session=session,
        bot=bot,
    )


@group_moderation_router.message(
    F.chat.type == ChatType.PRIVATE,
    StateFilter(None),
    IsAdminFilter(),
    Command("mute", "unmute", "ban", "unban"),
)
async def handle_dm_moderation_command(
    message: Message,
    command: CommandObject | None = None,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
) -> None:
    """Private DM admin moderation commands scoped to the community group."""
    await _handle_moderation_command(
        message,
        command,
        is_dm=True,
        session=session,
        bot=bot,
    )


@group_moderation_router.message(
    F.chat.type == ChatType.PRIVATE,
    StateFilter(None),
    IsAdminFilter(),
    Command("modhelp"),
)
async def handle_dm_modhelp_command(
    message: Message,
    bot: Bot | None = None,
) -> None:
    """Private DM admin-only cheat sheet for manual moderation commands."""
    sender = bot or message.bot
    actor = message.from_user
    if sender is None or actor is None:
        return
    await _deliver_admin_dm(sender, int(actor.id), gmod.DM_MODHELP_TEXT)


@group_moderation_router.callback_query(F.data.startswith(f"{gmod.UNDO_CALLBACK_PREFIX}:"))
async def handle_moderation_undo_callback(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
) -> None:
    """1-tap DM undo button handler (gmod_undo:{unmute|unban}:{chat_id}:{target_id})."""
    admin = callback.from_user
    if admin is None or not settings.is_admin(admin.id):
        await callback.answer(gmod.CALLBACK_UNDO_NOT_ADMIN, show_alert=True)
        return

    parsed = gmod.parse_undo_callback(callback.data)
    if parsed is None:
        await callback.answer(gmod.CALLBACK_UNDO_INVALID, show_alert=True)
        return

    undo_op, chat_id, target_id = parsed
    sender = bot or callback.bot
    action_fn = gmod.manual_unmute if undo_op == "unmute" else gmod.manual_unban

    async with _session_or_new(session) as s:
        result = await action_fn(
            sender,
            s,
            chat_id=chat_id,
            target_id=target_id,
            actor_id=admin.id,
            source="dm_undo",
        )

    if result.ok:
        status_line = (
            gmod.UNDO_STATUS_UNMUTED.format(actor_id=admin.id)
            if undo_op == "unmute"
            else gmod.UNDO_STATUS_UNBANNED.format(actor_id=admin.id)
        )
        if callback.message is not None:
            current_text = getattr(callback.message, "text", None) or ""
            updated_text = f"{current_text}\n\n{status_line}" if current_text else status_line
            try:
                await callback.message.edit_text(
                    updated_text,
                    parse_mode=None,
                    reply_markup=None,
                )
            except Exception as edit_exc:
                logger.debug("group_moderation: failed to edit DM after undo: %s", edit_exc)
                try:
                    await callback.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        await callback.answer(
            gmod.CALLBACK_UNDO_UNMUTED if undo_op == "unmute" else gmod.CALLBACK_UNDO_UNBANNED
        )
        return

    if result.already_done:
        if callback.message is not None:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await callback.answer(result.message)
        return

    await callback.answer(result.message, show_alert=True)
