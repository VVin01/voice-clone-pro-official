from __future__ import annotations

import logging
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.schema import User
from app.services.i18n import t
from app.services.ledger import award_welcome_bonus, confirm_referral_and_reward

logger = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


def warn_if_gate_not_configured() -> None:
    """Log a startup warning when gate IDs are missing.

    Call this at application startup. In production mode a missing gate ID
    is a misconfiguration, not a silent bypass: check_telegram_membership()
    now fails closed, so an unconfigured gate blocks everyone.
    """
    missing = [
        name
        for name, value in (
            ("REQUIRED_CHANNEL_ID", settings.required_channel_id),
            ("REQUIRED_GROUP_ID", settings.required_group_id),
        )
        if not value
    ]
    if not missing:
        return
    if str(settings.app_env).lower() not in ("development", "dev", "test", ""):
        logger.warning(
            "PRODUCTION gate misconfiguration: %s not set; membership checks "
            "will fail closed until the gate IDs are configured.",
            ", ".join(missing),
        )
    else:
        logger.warning(
            "Membership gate IDs not configured (%s); membership checks "
            "will fail closed.",
            ", ".join(missing),
        )


def is_gate_configured() -> bool:
    """True only when BOTH gate IDs are set.

    Used by the Mini App so it can return a distinct GATE_NOT_CONFIGURED
    error instead of a misleading "you have not joined" message when the
    operator has not set REQUIRED_CHANNEL_ID / REQUIRED_GROUP_ID yet.
    """
    return bool(settings.required_channel_id) and bool(settings.required_group_id)


def gate_missing_chats(channel_ok: bool, group_ok: bool) -> list[str]:
    """Which gate chats are still missing: [] / ["channel"] / ["group"] / both."""
    missing: list[str] = []
    if not channel_ok:
        missing.append("channel")
    if not group_ok:
        missing.append("group")
    return missing


def build_gate_reminder_text(lang: str, channel_ok: bool, group_ok: bool) -> str:
    """The single shared join-reminder message (bot surface).

    Friendly premium copy plus a per-chat status line so the user can
    see exactly which of the two chats they still miss, e.g.
    "📢 Channel — ✓ Joined" / "👥 Group — Not joined yet". Paired with
    membership_gate_kb() (both join URL buttons + the gate:verify
    re-check button) at every bot block point: /start, language pick,
    voice-clone entry and custom-voice entry.
    """

    def _mark(ok: bool) -> str:
        return t("gate_status_joined", lang) if ok else t("gate_status_missing", lang)

    return "\n".join(
        [
            t("membership_gate_title", lang),
            "",
            t("membership_gate_desc", lang),
            "",
            t("membership_gate_status_channel", lang, status=_mark(channel_ok)),
            t("membership_gate_status_group", lang, status=_mark(group_ok)),
        ]
    )


def gate_still_missing_text(lang: str, channel_ok: bool, group_ok: bool) -> str:
    """Short alert naming the chat(s) still missing after a failed re-check."""
    names = [
        t("gate_name_channel", lang) if chat == "channel" else t("gate_name_group", lang)
        for chat in gate_missing_chats(channel_ok, group_ok)
    ]
    return t("membership_still_missing", lang, missing=" + ".join(names))


async def check_telegram_membership(bot: Bot | None, chat_id: str | int, user_id: int) -> bool:
    """Query Telegram API to check if user is in channel/group.

    Fail closed: if bot is None or chat_id is empty, returns False.
    A missing gate configuration must never silently pass a user.
    """
    if bot is None or not chat_id:
        logger.warning(
            "Membership check failed closed: bot or gate chat_id is missing "
            "for user %s",
            user_id,
        )
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in MEMBER_STATUSES
    except Exception as e:
        logger.warning("Error checking membership for user %s in %s: %s", user_id, chat_id, e)
        return False


async def verify_user_membership(
    session: AsyncSession,
    user: User,
    bot: Bot | None = None,
) -> tuple[bool, str]:
    """Verify channel and group membership.

    If both verified:
    - Sets user.channel_verified = True and user.group_verified = True.
    - Idempotently awards welcome bonus (5 minutes) if not awarded yet.
    - Confirms pending referral and rewards referrer (30s) if applicable.

    Returns (is_verified, reason).
    """
    # Admin bypass (mirrors AccessControlMiddleware, which already lets
    # admins through with zero DB lookup). Without this, an admin who has
    # not joined the channel/group would be locked out of /start and of
    # every gated Mini App write. No welcome bonus or referral reward is
    # granted here: admins are already excluded from the welcome bonus
    # below, and referral confirmation stays a bot-side flow.
    if settings.is_admin(user.id):
        user.channel_verified = True
        user.group_verified = True
        await session.flush()
        return True, "admin_bypass"

    ch_ok = await check_telegram_membership(bot, settings.required_channel_id, user.id)
    gp_ok = await check_telegram_membership(bot, settings.required_group_id, user.id)

    if not ch_ok or not gp_ok:
        user.channel_verified = ch_ok
        user.group_verified = gp_ok
        await session.flush()
        return False, "channel_or_group_missing"

    user.channel_verified = True
    user.group_verified = True

    # Confirm pending referral reward for referrer
    await confirm_referral_and_reward(session, user.id)

    # In V2, free user allowance is governed by the monthly 180s allowance (grant_monthly_free_allowance).
    # Legacy welcome bonus is only awarded if explicit welcome_bonus_seconds is configured (> 0).
    if settings.welcome_bonus_seconds > 0 and not settings.is_admin(user.id):
        await award_welcome_bonus(session, user.id, seconds=settings.welcome_bonus_seconds)

    await session.flush()
    return True, "verified"
