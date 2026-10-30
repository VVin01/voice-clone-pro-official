from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.schema import User
from app.services.i18n import t
from app.services.ledger import award_welcome_bonus, confirm_referral_and_reward

logger = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}

# TelegramBadRequest texts that mean "this user has no membership in that
# chat" (a definitive answer), as opposed to config / transport errors.
_NOT_A_MEMBER_ERRORS = ("user not found", "participant_id_invalid", "member not found")


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


async def probe_telegram_membership(bot: Bot | None, chat_id: str | int, user_id: int) -> bool | None:
    """Tri-state live membership probe (the single get_chat_member call site).

    Returns True (member), False (definitively NOT a member: left/kicked,
    or Telegram reports the user is not a participant) or None when the
    answer is unknown (missing bot / chat_id, network or API error).

    Callers decide how to treat None: the gate paths (verify_user_membership,
    the Mini App API gate) go through check_telegram_membership(), which
    maps None -> False (fail closed). Only the bot-side periodic re-check
    of an already-verified user treats None as "keep current access"
    (fail safe), so a Telegram outage cannot lock out existing members.
    """
    if bot is None or not chat_id:
        logger.warning(
            "Membership check failed closed: bot or gate chat_id is missing "
            "for user %s",
            user_id,
        )
        return None
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status == "restricted" and getattr(member, "is_member", True) is False:
            # A restricted user who LEFT keeps status "restricted" with
            # is_member=False; that is not membership.
            return False
        return member.status in MEMBER_STATUSES
    except TelegramBadRequest as e:
        # "user not found" / PARTICIPANT_ID_INVALID mean Telegram has no
        # membership record for this user in that chat: a definitive no.
        # Anything else (chat not found, bot not admin, ...) is unknown.
        err = str(e).lower()
        if any(term in err for term in _NOT_A_MEMBER_ERRORS):
            return False
        logger.warning("Error checking membership for user %s in %s: %s", user_id, chat_id, e)
        return None
    except Exception as e:
        logger.warning("Error checking membership for user %s in %s: %s", user_id, chat_id, e)
        return None


async def check_telegram_membership(bot: Bot | None, chat_id: str | int, user_id: int) -> bool:
    """Query Telegram API to check if user is in channel/group.

    Fail closed: if bot is None or chat_id is empty, returns False.
    A missing gate configuration must never silently pass a user.
    Unknown results (API / network errors) are also False.
    """
    return (await probe_telegram_membership(bot, chat_id, user_id)) is True


# ── Bot-side periodic membership re-check (join-then-leave hole) ─────────
#
# verify_user_membership() runs LIVE at /start, the language pick, the
# gate:verify button and the generate / custom-voice entry points. Every
# other bot interaction used to trust the legacy channel_verified /
# group_verified flags forever, so a user who joined once and then left
# both chats kept access. MembershipRecheckMiddleware closes that: for a
# user whose flags say "verified" it re-runs the live probe at most once
# per MEMBERSHIP_RECHECK_TTL_SECONDS (never on every callback). The cache
# below is in-process; after a restart the next interaction simply
# re-checks once.

_next_check_at: dict[int, float] = {}


def _now() -> float:
    return time.monotonic()


def _ttl_seconds() -> int:
    return max(0, int(settings.membership_recheck_ttl_seconds))


def mark_membership_fresh(user_id: int) -> None:
    """Record a successful live check; the next re-check is due after the TTL."""
    _next_check_at[user_id] = _now() + _ttl_seconds()


def forget_membership(user_id: int) -> None:
    """Drop cached state so the next interaction re-checks live."""
    _next_check_at.pop(user_id, None)


def membership_recheck_due(user_id: int) -> bool:
    due_at = _next_check_at.get(user_id)
    return due_at is None or _now() >= due_at


def reset_membership_cache() -> None:
    """Test helper: clear all cached re-check state."""
    _next_check_at.clear()


async def recheck_verified_membership(
    session: AsyncSession,
    user: User,
    bot: Bot | None,
) -> bool:
    """TTL-cached live re-check for a user whose legacy flags say verified.

    Returns True when the user may proceed, False when they must be shown
    the join reminder instead:

    - cache fresh (checked within the TTL) -> True, no Telegram call;
    - live probe says member of BOTH chats -> True, cache refreshed;
    - live probe says NOT a member of either chat (definitive) -> the
      legacy flags are reset to the live per-chat result (an unknown
      side keeps its previous value), flushed, cache dropped, False;
    - probe result unknown (Telegram/API error) and nothing definitively
      negative -> True (fail SAFE, for this bot-side cache path only),
      and the next attempt is scheduled after a short retry backoff
      rather than the full TTL. The gate paths themselves
      (verify_user_membership, the Mini App API gate) stay fail closed.
    """
    if settings.is_admin(user.id) or not is_gate_configured():
        return True
    if not membership_recheck_due(user.id):
        return True

    ch = await probe_telegram_membership(bot, settings.required_channel_id, user.id)
    gp = await probe_telegram_membership(bot, settings.required_group_id, user.id)

    if ch is False or gp is False:
        if ch is not None:
            user.channel_verified = ch
        if gp is not None:
            user.group_verified = gp
        await session.flush()
        forget_membership(user.id)
        logger.info(
            "Membership re-check failed for user %s (channel=%s group=%s); "
            "legacy flags reset, join reminder shown.",
            user.id,
            ch,
            gp,
        )
        return False

    if ch is True and gp is True:
        mark_membership_fresh(user.id)
        return True

    # Inconclusive (API error) for a previously verified user: keep access.
    retry = min(max(0, int(settings.membership_recheck_error_retry_seconds)), _ttl_seconds())
    _next_check_at[user.id] = _now() + retry
    logger.warning(
        "Membership re-check inconclusive for verified user %s (channel=%s group=%s); "
        "keeping access, retrying in %ss.",
        user.id,
        ch,
        gp,
        retry,
    )
    return True


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
        forget_membership(user.id)
        return False, "channel_or_group_missing"

    user.channel_verified = True
    user.group_verified = True
    # A live pass also refreshes the bot-side re-check cache, so the
    # middleware does not re-probe right after /start or gate:verify.
    mark_membership_fresh(user.id)

    # Confirm pending referral reward for referrer
    await confirm_referral_and_reward(session, user.id)

    # In V2, free user allowance is governed by the monthly 180s allowance (grant_monthly_free_allowance).
    # Legacy welcome bonus is only awarded if explicit welcome_bonus_seconds is configured (> 0).
    if settings.welcome_bonus_seconds > 0 and not settings.is_admin(user.id):
        await award_welcome_bonus(session, user.id, seconds=settings.welcome_bonus_seconds)

    await session.flush()
    return True, "verified"
