from __future__ import annotations

import logging
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.schema import User
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
