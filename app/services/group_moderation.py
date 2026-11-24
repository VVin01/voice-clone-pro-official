"""Manual admin moderation service for the Telegram Community group.

Shared core used by both the in-group reply commands and private DM commands
(plus 1-tap DM undo callbacks). Designed to stay 100% isolated from the
automatic strike ladder:

* Writes ONLY to ``AuditLog`` (``action="group_mod.<op>"``) — never touches
  ``GroupAiModerationStrike``, ``get_user_strikes_in_window``, or
  ``_execute_moderation``.
* Never stores any group message text in ``AuditLog.details_json``.
* Independent of ``group_ai_admin_enabled`` and
  ``group_ai_moderation_enabled`` kill switches.
* Polite Burmese copy with strictly zero emoji across every user/admin string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions

from app.config import settings
from app.models.schema import AuditLog, utc_now
from app.services import group_ai_admin as gai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MANUAL_MUTE_SECONDS = 3600  # 1 hour
MIN_MANUAL_MUTE_SECONDS = 60  # 1 minute (strictly above Telegram's 30s forever cliff)
MAX_MANUAL_MUTE_SECONDS = 30 * 24 * 3600  # 30 days (safely below Telegram's 366d forever cliff)
STALE_COMMAND_MAX_AGE_SECONDS = 5 * 60  # 5 minutes (guards drop_pending_updates=False replay)

# Telegram synthetic/system user IDs that must never be targeted:
# 777000 = Telegram channel auto-forward, 1087968824 = GroupAnonymousBot, 136817688 = Channel_Bot
SYNTHETIC_SYSTEM_USER_IDS: frozenset[int] = frozenset({777000, 1087968824, 136817688})

AUDIT_ACTION_PREFIX = "group_mod."
AUDIT_ACTION_MUTE = "group_mod.mute"
AUDIT_ACTION_UNMUTE = "group_mod.unmute"
AUDIT_ACTION_BAN = "group_mod.ban"
AUDIT_ACTION_UNBAN = "group_mod.unban"
AUDIT_TARGET_TYPE = "group_member"

UNDO_CALLBACK_PREFIX = "gmod_undo"

_MYANMAR_DIGITS_TABLE = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
_DURATION_RE = re.compile(
    r"^(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?$",
    re.ASCII,
)

# Service-message attributes on a replied-to Telegram Message that indicate
# a system event rather than a regular user-authored content message.
_SERVICE_MESSAGE_ATTRS: tuple[str, ...] = (
    "new_chat_members",
    "left_chat_member",
    "pinned_message",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "forum_topic_closed",
    "forum_topic_reopened",
    "forum_topic_edited",
)

# ---------------------------------------------------------------------------
# Burmese copy (strictly zero emoji)
# ---------------------------------------------------------------------------

# Owner decision 1 (2026-11-24): ONE public notice line in group on successful
# in-group /mute or /ban only. Never on unmute/unban, failures, guard rejections,
# or DM-invoked actions. Numeric ID never appears in group notices.
GROUP_NOTICE_MUTE = (
    "သတိပေးချက်: Group စည်းကမ်း ချိုးဖောက်မှုကြောင့် {target_display} ၏ စာပို့ခွင့်ကို "
    "{duration_my} ပိတ်ထားပါသည်။"
)
GROUP_NOTICE_BAN = (
    "သတိပေးချက်: Group စည်းကမ်း ချိုးဖောက်မှုကြောင့် {target_display} ကို "
    "Community Group မှ ထုတ်ပယ်လိုက်ပါသည်။"
)

# Admin DM confirmations
DM_SUCCESS_MUTE = (
    "Community Group တွင် အသုံးပြုသူ {name} (ID: {uid}) ၏ စာပို့ခွင့်ကို "
    "{duration_my} ပိတ်လိုက်ပါပြီ။"
)
DM_SUCCESS_BAN = (
    "Community Group မှ အသုံးပြုသူ {name} (ID: {uid}) ကို ထုတ်ပယ်လိုက်ပါပြီ။"
)
DM_SUCCESS_UNMUTE = (
    "Community Group တွင် အသုံးပြုသူ {name} (ID: {uid}) ၏ စာပို့ခွင့်ကို "
    "ပြန်ဖွင့်ပေးလိုက်ပါပြီ။"
)
DM_SUCCESS_UNBAN = (
    "Community Group တွင် အသုံးပြုသူ {name} (ID: {uid}) ၏ Ban ကို ဖြုတ်ပေးလိုက်ပါပြီ။ "
    "ဖိတ်ခေါ်လင့်ခ်မှတစ်ဆင့် ပြန်လည် ဝင်ရောက်နိုင်ပါသည်။"
)

# Admin DM undo buttons & callback answers
BTN_UNDO_UNMUTE = "စာပို့ခွင့် ပြန်ဖွင့်မည်"
BTN_UNDO_UNBAN = "Ban ဖြုတ်မည်"
UNDO_STATUS_UNMUTED = "စာပို့ခွင့် ပြန်ဖွင့်ပြီးပါပြီ (Admin ID: {actor_id})။"
UNDO_STATUS_UNBANNED = "Ban ဖြုတ်ပြီးပါပြီ (Admin ID: {actor_id})။"
CALLBACK_UNDO_NOT_ADMIN = "Admin များသာ ဤလုပ်ဆောင်ချက်ကို အသုံးပြုနိုင်ပါသည်။"
CALLBACK_UNDO_INVALID = "ဤခလုတ်သည် အကျုံးမဝင်တော့ပါ။"
CALLBACK_UNDO_ALREADY_UNMUTED = "ဤအသုံးပြုသူ၏ စာပို့ခွင့်ကို ပြန်ဖွင့်ပြီး ဖြစ်ပါသည်။"
CALLBACK_UNDO_ALREADY_UNBANNED = "ဤအသုံးပြုသူ၏ Ban ကို ဖြုတ်ပြီး ဖြစ်ပါသည်။"
CALLBACK_UNDO_UNMUTED = "စာပို့ခွင့် ပြန်ဖွင့်ပေးလိုက်ပါပြီ။"
CALLBACK_UNDO_UNBANNED = "Ban ဖြုတ်ပေးလိုက်ပါပြီ။"

# Admin DM failures & guard rejections
DM_ERR_BOT_NO_RIGHTS = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: Bot တွင် အသုံးပြုသူ ကန့်သတ်ခွင့် (Restrict members) မရှိပါ။ "
    "Group Admin ခွင့်ပြုချက်ကို စစ်ဆေးပေးပါ။"
)
DM_ERR_NOT_ADMIN = "လုပ်ဆောင်မှု မအောင်မြင်ပါ: Admin များသာ ဤလုပ်ဆောင်ချက်ကို အသုံးပြုနိုင်ပါသည်။"
DM_ERR_NOT_COMMUNITY_GROUP = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: ဤအမိန့်ကို Community Group အတွင်း၌သာ အသုံးပြုနိုင်ပါသည်။"
)
DM_ERR_TARGET_GROUP_ADMIN = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: ဤအသုံးပြုသူသည် Group Admin သို့မဟုတ် ပိုင်ရှင် ဖြစ်နေသဖြင့် "
    "Bot မှ ကန့်သတ်၍ မရပါ။"
)
DM_ERR_TARGET_CONFIG_ADMIN = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: Bot Admin အကောင့်အပေါ် ဤလုပ်ဆောင်ချက်ကို အသုံးပြု၍ မရပါ။"
)
DM_ERR_TARGET_SELF_ISSUER = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: မိမိကိုယ်တိုင်အပေါ် ဤလုပ်ဆောင်ချက်ကို အသုံးပြု၍ မရပါ။"
)
DM_ERR_TARGET_BOT_SELF = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: Bot ကိုယ်တိုင်အပေါ် ဤလုပ်ဆောင်ချက်ကို အသုံးပြု၍ မရပါ။"
)
DM_ERR_TARGET_WHITELISTED = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: ဤအသုံးပြုသူသည် Moderation Whitelist ထဲတွင် ပါဝင်နေပါသည်။"
)
DM_ERR_TARGET_NOT_BANNED = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: ဤအသုံးပြုသူ (ID: {uid}) သည် လက်ရှိတွင် "
    "Community Group မှ Ban ခံထားရသူ မဟုတ်ပါ။"
)
DM_ERR_UNMUTE_KICKED = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: ဤအသုံးပြုသူ (ID: {uid}) သည် Community Group မှ "
    "Ban ခံထားရပါသည်။ ပြန်ဝင်ခွင့်ပေးလိုပါက /unban ကို အသုံးပြုပါ။"
)
DM_ERR_MUTE_KICKED = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: ဤအသုံးပြုသူ (ID: {uid}) သည် Community Group မှ "
    "Ban ခံထားရပြီး ဖြစ်ပါသည်။"
)
DM_ERR_TARGET_NOT_FOUND = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: အသုံးပြုသူ (ID: {uid}) ကို Community Group တွင် ရှာမတွေ့ပါ။ "
    "User ID မှန်ကန်မှုကို စစ်ဆေးပေးပါ။"
)
DM_ERR_TELEGRAM_GENERIC = (
    "လုပ်ဆောင်မှု မအောင်မြင်ပါ: Telegram ဘက်မှ ငြင်းပယ်ခံရပါသည် ({detail})။"
)
DM_ERR_MISSING_TARGET_GROUP = (
    "အသုံးပြုသူကို မသတ်မှတ်ထားပါ။ အရေးယူလိုသူ၏ မက်ဆေ့ခ်ျကို Reply ပြန်၍ အမိန့်ပေးပါ "
    "သို့မဟုတ် ဂဏန်း User ID ထည့်သွင်းပေးပါ။ ဥပမာ: /mute 1h သို့မဟုတ် /mute 123456789 1h"
)
DM_ERR_MISSING_TARGET_DM = (
    "အသုံးပြုသူ၏ ဂဏန်း User ID ထည့်သွင်းပေးပါ။ "
    "ဥပမာ: /mute 123456789 1h သို့မဟုတ် /unban 123456789"
)
DM_ERR_USERNAME_UNSUPPORTED = (
    "Username (@username) ဖြင့် တိုက်ရိုက် ရှာဖွေ၍ မရပါ။ "
    "အသုံးပြုသူ၏ မက်ဆေ့ခ်ျကို Reply ပြန်၍ အမိန့်ပေးပါ သို့မဟုတ် ဂဏန်း User ID ထည့်သွင်းပေးပါ။"
)
DM_ERR_INVALID_TARGET_SENDER_CHAT = (
    "အမည်မဖော်သော Admin သို့မဟုတ် Channel မှ ပို့ထားသော မက်ဆေ့ခ်ျအပေါ် "
    "ဤလုပ်ဆောင်ချက်ကို အသုံးပြု၍ မရပါ။"
)
DM_ERR_INVALID_TARGET_SERVICE = (
    "System သို့မဟုတ် Service မက်ဆေ့ခ်ျအပေါ် ဤလုပ်ဆောင်ချက်ကို အသုံးပြု၍ မရပါ။ "
    "အသုံးပြုသူ၏ မက်ဆေ့ခ်ျကို တိုက်ရိုက် Reply ပြန်ပါ သို့မဟုတ် ဂဏန်း User ID ထည့်သွင်းပေးပါ။"
)
DM_ERR_INVALID_TARGET_SYSTEM = (
    "Telegram System အကောင့်အပေါ် ဤလုပ်ဆောင်ချက်ကို အသုံးပြု၍ မရပါ။"
)
DM_ERR_INVALID_TARGET_ARG = (
    "အသုံးပြုသူ ID မမှန်ကန်ပါ။ ဂဏန်း User ID ထည့်သွင်းပေးပါ "
    "သို့မဟုတ် အသုံးပြုသူ၏ မက်ဆေ့ခ်ျကို Reply ပြန်ပေးပါ။"
)
DM_ERR_BAD_DURATION = (
    "ကြာချိန် ပုံစံ မမှန်ကန်ပါ။ ယူနစ် (m, h, d, w) ထည့်သွင်းပေးပါ။ "
    "ဥပမာ: 15m, 1h, 1h30m, 24h, 7d (အနည်းဆုံး 1 မိနစ်၊ အများဆုံး 30 ရက်)။"
)
DM_ERR_DURATION_TOO_SHORT = "Mute ကြာချိန်သည် အနည်းဆုံး 1 မိနစ် (1m) ဖြစ်ရပါမည်။"
DM_ERR_DURATION_TOO_LONG = (
    "Mute ကြာချိန်သည် အများဆုံး 30 ရက် (30d) အထိသာ သတ်မှတ်နိုင်ပါသည်။ "
    "အပြီး ထုတ်ပယ်လိုပါက /ban ကို အသုံးပြုပါ။"
)
DM_ERR_STALE_COMMAND = (
    "ဤအမိန့်သည် 5 မိနစ်ကျော် ကြာမြင့်နေပြီဖြစ်၍ အလိုအလျောက် လုပ်ဆောင်ခြင်း မပြုပါ။ "
    "လိုအပ်ပါက အမိန့်အသစ် ထပ်မံ ပေးပို့ပေးပါ။"
)
DM_FALLBACK_HEADER = "Admin ID {actor_id} ထံ DM ပို့မရသဖြင့် အစားထိုး အသိပေးချက်:"

DM_MODHELP_TEXT = (
    "Community Group Admin Moderation အမိန့်များ\n\n"
    "Group အတွင်း (မက်ဆေ့ခ်ျကို Reply ပြန်၍ သို့မဟုတ် User ID ဖြင့်):\n"
    "/mute [ကြာချိန်] - စာပို့ခွင့် ခေတ္တပိတ်ရန် (မူလ: 1 နာရီ)\n"
    "/unmute [User ID] - စာပို့ခွင့် ပြန်ဖွင့်ရန်\n"
    "/ban [User ID] - Community Group မှ ထုတ်ပယ်ရန်\n"
    "/unban <User ID> - Ban ဖြုတ်ပေးရန်\n\n"
    "Bot DM အတွင်း (Community Group အတွက် တိုက်ရိုက်):\n"
    "/mute <User ID> [ကြာချိန်]\n"
    "/unmute <User ID>\n"
    "/ban <User ID>\n"
    "/unban <User ID>\n\n"
    "ကြာချိန် ပုံစံ: 15m, 1h, 1h30m, 24h, 7d, 2w (အနည်းဆုံး 1m၊ အများဆုံး 30d)\n"
    "မှတ်ချက်: @username ဖြင့် တိုက်ရိုက် မရပါ — Reply သို့မဟုတ် ဂဏန်း User ID ကို အသုံးပြုပါ။"
)

ALL_BURMESE_COPY_TEMPLATES: tuple[str, ...] = (
    GROUP_NOTICE_MUTE,
    GROUP_NOTICE_BAN,
    DM_SUCCESS_MUTE,
    DM_SUCCESS_BAN,
    DM_SUCCESS_UNMUTE,
    DM_SUCCESS_UNBAN,
    BTN_UNDO_UNMUTE,
    BTN_UNDO_UNBAN,
    UNDO_STATUS_UNMUTED,
    UNDO_STATUS_UNBANNED,
    CALLBACK_UNDO_NOT_ADMIN,
    CALLBACK_UNDO_INVALID,
    CALLBACK_UNDO_ALREADY_UNMUTED,
    CALLBACK_UNDO_ALREADY_UNBANNED,
    CALLBACK_UNDO_UNMUTED,
    CALLBACK_UNDO_UNBANNED,
    DM_ERR_BOT_NO_RIGHTS,
    DM_ERR_NOT_ADMIN,
    DM_ERR_NOT_COMMUNITY_GROUP,
    DM_ERR_TARGET_GROUP_ADMIN,
    DM_ERR_TARGET_CONFIG_ADMIN,
    DM_ERR_TARGET_SELF_ISSUER,
    DM_ERR_TARGET_BOT_SELF,
    DM_ERR_TARGET_WHITELISTED,
    DM_ERR_TARGET_NOT_BANNED,
    DM_ERR_UNMUTE_KICKED,
    DM_ERR_MUTE_KICKED,
    DM_ERR_TARGET_NOT_FOUND,
    DM_ERR_TELEGRAM_GENERIC,
    DM_ERR_MISSING_TARGET_GROUP,
    DM_ERR_MISSING_TARGET_DM,
    DM_ERR_USERNAME_UNSUPPORTED,
    DM_ERR_INVALID_TARGET_SENDER_CHAT,
    DM_ERR_INVALID_TARGET_SERVICE,
    DM_ERR_INVALID_TARGET_SYSTEM,
    DM_ERR_INVALID_TARGET_ARG,
    DM_ERR_BAD_DURATION,
    DM_ERR_DURATION_TOO_SHORT,
    DM_ERR_DURATION_TOO_LONG,
    DM_ERR_STALE_COMMAND,
    DM_FALLBACK_HEADER,
    DM_MODHELP_TEXT,
)

# ---------------------------------------------------------------------------
# Typed errors & result objects
# ---------------------------------------------------------------------------


class DurationError(ValueError):
    """Raised when a manual mute duration string is invalid or out of bounds."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TargetResolutionError(ValueError):
    """Raised when a moderation target cannot be safely resolved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TargetResolution:
    user_id: int
    first_name: str | None = None
    username: str | None = None
    reply_message_id: int | None = None
    remaining_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModerationResult:
    ok: bool
    action: str
    chat_id: int
    target_id: int
    actor_id: int
    message: str
    error_code: str | None = None
    already_done: bool = False
    duration_seconds: int | None = None
    until_ts: int | None = None
    target_first_name: str | None = None
    target_username: str | None = None


# ---------------------------------------------------------------------------
# Pure helpers: duration parsing/formatting, target resolution, staleness
# ---------------------------------------------------------------------------


def normalize_digits(text: str) -> str:
    """Convert Myanmar digits (၀-၉) to ASCII digits (0-9)."""
    return (text or "").translate(_MYANMAR_DIGITS_TABLE)


def parse_manual_duration(text: str | None) -> int:
    """Parse a manual mute duration token into bounded seconds.

    - None / empty -> DEFAULT_MANUAL_MUTE_SECONDS (3600).
    - Case-insensitive unit tokens m/h/d/w (1w = 7d), single or compound
      (e.g. '15m', '1h', '1h30m', '1d12h', '7d', '2w'); Myanmar digits
      are normalized to ASCII.
    - Bare unitless integers (e.g. '30') are REJECTED to prevent ambiguity
      with numeric Telegram user IDs.
    - Enforces [MIN_MANUAL_MUTE_SECONDS (60), MAX_MANUAL_MUTE_SECONDS (30d)]
      without clamping so Telegram's <30s and >366d forever-cliffs are
      unreachable.
    """
    if text is None:
        return DEFAULT_MANUAL_MUTE_SECONDS
    raw = normalize_digits(str(text)).strip().lower()
    if not raw:
        return DEFAULT_MANUAL_MUTE_SECONDS
    if raw.isdigit():
        raise DurationError("missing_unit", DM_ERR_BAD_DURATION)

    match = _DURATION_RE.match(raw)
    if not match or not any(match.groups()):
        raise DurationError("invalid_format", DM_ERR_BAD_DURATION)

    weeks = int(match.group("w") or 0)
    days = int(match.group("d") or 0)
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    total_seconds = (weeks * 7 + days) * 86400 + hours * 3600 + minutes * 60

    if total_seconds < MIN_MANUAL_MUTE_SECONDS:
        raise DurationError("too_short", DM_ERR_DURATION_TOO_SHORT)
    if total_seconds > MAX_MANUAL_MUTE_SECONDS:
        raise DurationError("too_long", DM_ERR_DURATION_TOO_LONG)
    return total_seconds


def format_duration_my(seconds: int) -> str:
    """Render a positive duration in seconds into concise polite Burmese."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} ရက်")
    if hours:
        parts.append(f"{hours} နာရီ")
    if minutes:
        parts.append(f"{minutes} မိနစ်")
    if secs and not parts:
        parts.append(f"{secs} စက္ကန့်")
    return " ".join(parts) if parts else "0 မိနစ်"


def format_target_display(first_name: str | None, username: str | None) -> str:
    """Format target name for public group notice (NEVER includes numeric ID).

    Owner requirement (2026-11-24): first name, plus `` (@username)`` when the
    target has a username. Emoji stripped from user-provided display strings so
    group notices stay strictly emoji-free.
    """
    clean_name = gai.strip_emoji(str(first_name or "")).strip()
    clean_user = str(username or "").strip().lstrip("@")
    if clean_name and clean_user:
        return f"{clean_name} (@{clean_user})"
    if clean_name:
        return clean_name
    if clean_user:
        return f"@{clean_user}"
    return "အသုံးပြုသူ"


def format_target_dm_name(first_name: str | None, username: str | None) -> str:
    """Format target label for admin DM confirmations (zero emoji)."""
    return format_target_display(first_name, username)


def is_command_stale(
    message_date: Any,
    *,
    now: datetime | None = None,
    max_age_seconds: int = STALE_COMMAND_MAX_AGE_SECONDS,
) -> bool:
    """Return True when a Telegram message timestamp is older than max_age_seconds."""
    if message_date is None:
        return False
    if isinstance(message_date, (int, float)):
        msg_dt = datetime.fromtimestamp(float(message_date), tz=timezone.utc)
    elif isinstance(message_date, datetime):
        msg_dt = (
            message_date
            if message_date.tzinfo is not None
            else message_date.replace(tzinfo=timezone.utc)
        )
    else:
        return False
    current = now if now is not None else utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - msg_dt).total_seconds()
    return age > float(max_age_seconds)


def _is_service_reply(reply_msg: Any) -> bool:
    for attr in _SERVICE_MESSAGE_ATTRS:
        val = getattr(reply_msg, attr, None)
        if val:
            return True
    return False


def _slice_after_utf16_entity(text: str, offset: int, length: int) -> str:
    """Return text following a Telegram UTF-16 entity span."""
    try:
        raw = text.encode("utf-16-le")
        end_byte = (int(offset) + int(length)) * 2
        if end_byte < len(raw):
            return raw[end_byte:].decode("utf-16-le", errors="ignore").strip()
        return ""
    except Exception:
        return text[int(offset) + int(length) :].strip()


def resolve_target_user_id(
    *,
    reply_to_message: Any = None,
    entities: Any = None,
    text: str | None = None,
    args_text: str | None = None,
    is_dm: bool = False,
) -> TargetResolution:
    """Resolve target Telegram user ID from reply, text_mention, or numeric arg.

    Priority order:
    1. ``reply_to_message`` (in group chats only)
    2. ``text_mention`` entity carrying ``entity.user``
    3. Numeric ``user_id`` token in ``args_text``

    ``@username`` targeting is explicitly unsupported in v1 (owner decision 2).
    """
    tokens = [tok for tok in (args_text or "").strip().split() if tok]

    # (a) Reply-to-message (group only)
    if not is_dm and reply_to_message is not None:
        if getattr(reply_to_message, "forum_topic_created", None) is not None:
            raise TargetResolutionError("forum_topic_created", DM_ERR_INVALID_TARGET_SERVICE)
        if getattr(reply_to_message, "sender_chat", None) is not None:
            raise TargetResolutionError("sender_chat_reply", DM_ERR_INVALID_TARGET_SENDER_CHAT)
        if _is_service_reply(reply_to_message):
            raise TargetResolutionError("service_message_reply", DM_ERR_INVALID_TARGET_SERVICE)
        reply_user = getattr(reply_to_message, "from_user", None)
        if reply_user is None or getattr(reply_user, "id", None) is None:
            raise TargetResolutionError("missing_reply_author", DM_ERR_INVALID_TARGET_SERVICE)
        uid = int(reply_user.id)
        if uid in SYNTHETIC_SYSTEM_USER_IDS:
            raise TargetResolutionError("synthetic_system_user", DM_ERR_INVALID_TARGET_SYSTEM)
        reply_mid = getattr(reply_to_message, "message_id", None)
        return TargetResolution(
            user_id=uid,
            first_name=getattr(reply_user, "first_name", None),
            username=getattr(reply_user, "username", None),
            reply_message_id=int(reply_mid) if isinstance(reply_mid, int) else None,
            remaining_args=tuple(tokens),
        )

    # (b) text_mention entity
    for ent in entities or ():
        ent_type = getattr(ent, "type", None)
        ent_type_str = getattr(ent_type, "value", str(ent_type or "")).lower()
        ent_user = getattr(ent, "user", None)
        if ent_type_str == "text_mention" and ent_user is not None and getattr(ent_user, "id", None) is not None:
            uid = int(ent_user.id)
            if uid in SYNTHETIC_SYSTEM_USER_IDS:
                raise TargetResolutionError("synthetic_system_user", DM_ERR_INVALID_TARGET_SYSTEM)
            trailing = ""
            if text and isinstance(getattr(ent, "offset", None), int) and isinstance(getattr(ent, "length", None), int):
                trailing = _slice_after_utf16_entity(text, ent.offset, ent.length)
            rem_tokens = tuple(tok for tok in trailing.split() if tok)
            return TargetResolution(
                user_id=uid,
                first_name=getattr(ent_user, "first_name", None),
                username=getattr(ent_user, "username", None),
                reply_message_id=None,
                remaining_args=rem_tokens,
            )

    # (c) Numeric user_id token
    if not tokens:
        raise TargetResolutionError(
            "missing_target",
            DM_ERR_MISSING_TARGET_DM if is_dm else DM_ERR_MISSING_TARGET_GROUP,
        )

    first = tokens[0]
    if first.startswith("@"):
        raise TargetResolutionError("username_unsupported", DM_ERR_USERNAME_UNSUPPORTED)

    normalized = normalize_digits(first)
    if not re.fullmatch(r"\d+", normalized):
        # If the user forgot to reply and only typed a duration token (e.g. "/mute 1h"),
        # still give a helpful missing/invalid target error.
        raise TargetResolutionError("invalid_target_arg", DM_ERR_INVALID_TARGET_ARG)

    uid = int(normalized)
    if uid <= 0:
        raise TargetResolutionError("invalid_target_arg", DM_ERR_INVALID_TARGET_ARG)
    if uid in SYNTHETIC_SYSTEM_USER_IDS:
        raise TargetResolutionError("synthetic_system_user", DM_ERR_INVALID_TARGET_SYSTEM)

    return TargetResolution(
        user_id=uid,
        first_name=None,
        username=None,
        reply_message_id=None,
        remaining_args=tuple(tokens[1:]),
    )


def resolve_target_from_message(
    message: Any,
    args_text: str | None,
    *,
    is_dm: bool = False,
) -> TargetResolution:
    """Convenience wrapper extracting fields from an aiogram Message."""
    return resolve_target_user_id(
        reply_to_message=getattr(message, "reply_to_message", None),
        entities=getattr(message, "entities", None),
        text=getattr(message, "text", None),
        args_text=args_text,
        is_dm=is_dm,
    )


# ---------------------------------------------------------------------------
# Per-(chat_id, target_id) concurrency lock registry
# ---------------------------------------------------------------------------

_target_locks: dict[tuple[int, int], asyncio.Lock] = {}
_target_lock_waiters: dict[tuple[int, int], int] = {}


def _acquire_lock_entry(key: tuple[int, int]) -> asyncio.Lock:
    lock = _target_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _target_locks[key] = lock
    _target_lock_waiters[key] = _target_lock_waiters.get(key, 0) + 1
    return lock


def _release_lock_entry(key: tuple[int, int]) -> None:
    remaining = _target_lock_waiters.get(key, 1) - 1
    if remaining <= 0:
        _target_lock_waiters.pop(key, None)
        _target_locks.pop(key, None)
    else:
        _target_lock_waiters[key] = remaining


# ---------------------------------------------------------------------------
# Telegram permission / error helpers
# ---------------------------------------------------------------------------


def _full_send_fallback_permissions() -> ChatPermissions:
    """Safe fallback permissions for unmute when chat.permissions is unavailable.

    Grants all send permissions and web page previews without granting
    admin-like flags (can_pin_messages, can_change_info, can_invite_users,
    can_manage_topics).
    """
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


async def _resolve_unmute_permissions(bot: Any, chat_id: int) -> ChatPermissions:
    """Fetch the chat's default permissions so unmute restores media/voice/stickers."""
    try:
        chat = await bot.get_chat(chat_id=chat_id)
        chat_perms = getattr(chat, "permissions", None)
        if isinstance(chat_perms, ChatPermissions):
            return chat_perms
    except Exception as exc:
        logger.debug("group_moderation: get_chat(%s) for unmute permissions failed: %s", chat_id, exc)
    return _full_send_fallback_permissions()


async def _resolve_bot_id(bot: Any) -> int | None:
    raw_id = getattr(bot, "id", None)
    if isinstance(raw_id, int):
        return raw_id
    try:
        me = await bot.get_me()
        me_id = getattr(me, "id", None)
        if isinstance(me_id, int):
            return me_id
    except Exception:
        pass
    return None


def _translate_telegram_error(exc: Exception, *, target_id: int) -> tuple[str, str]:
    """Map Telegram API errors to typed error codes + honest Burmese DM copy."""
    err_low = str(exc).lower()
    if any(
        token in err_low
        for token in (
            "user_admin_invalid",
            "is an administrator",
            "chat owner",
            "chat_owner",
            "can't remove chat owner",
            "can't restrict chat owner",
        )
    ):
        return "target_is_group_admin", DM_ERR_TARGET_GROUP_ADMIN
    if isinstance(exc, TelegramForbiddenError) or any(
        token in err_low
        for token in (
            "not enough rights",
            "chat_admin_required",
            "have no rights",
            "bot was kicked",
            "bot is not a member",
            "can't restrict",
            "not an administrator",
            "forbidden",
        )
    ):
        return "bot_no_rights", DM_ERR_BOT_NO_RIGHTS
    if any(
        token in err_low
        for token in (
            "user not found",
            "participant_id_invalid",
            "member not found",
            "user_id_invalid",
        )
    ):
        return "target_not_found", DM_ERR_TARGET_NOT_FOUND.format(uid=target_id)
    safe_detail = gai.strip_emoji(str(exc)).strip()[:180] or type(exc).__name__
    return "telegram_error", DM_ERR_TELEGRAM_GENERIC.format(detail=safe_detail)


def _member_status_str(member: Any) -> str:
    raw = getattr(member, "status", "")
    return str(getattr(raw, "value", raw) or "").lower()


# ---------------------------------------------------------------------------
# AuditLog writer (isolated from GroupAiModerationStrike)
# ---------------------------------------------------------------------------


async def _record_manual_audit(
    session: Any,
    *,
    actor_id: int,
    audit_action: str,
    chat_id: int,
    target_id: int,
    source: str,
    duration_seconds: int | None = None,
    until_ts: int | None = None,
    target_username: str | None = None,
    reply_message_id: int | None = None,
) -> None:
    """Persist a manual moderation AuditLog row. Never stores group message text."""
    details: dict[str, Any] = {
        "chat_id": int(chat_id),
        "source": str(source),
    }
    if duration_seconds is not None:
        details["duration_seconds"] = int(duration_seconds)
    if until_ts is not None:
        details["until_ts"] = int(until_ts)
    if target_username:
        details["target_username"] = str(target_username).lstrip("@")
    if reply_message_id is not None:
        details["reply_message_id"] = int(reply_message_id)

    row = AuditLog(
        admin_id=int(actor_id),
        action=audit_action,
        target_type=AUDIT_TARGET_TYPE,
        target_id=str(int(target_id)),
        details_json=json.dumps(details, ensure_ascii=False),
        created_at=utc_now(),
    )
    session.add(row)
    await session.commit()


# ---------------------------------------------------------------------------
# Core execution engine
# ---------------------------------------------------------------------------


async def _execute_manual_action(
    bot: Any,
    session: Any,
    *,
    op: str,
    chat_id: int,
    target_id: int,
    actor_id: int,
    duration_seconds: int | None = None,
    source: str = "group_cmd",
    reply_message_id: int | None = None,
    target_first_name: str | None = None,
    target_username: str | None = None,
    now_fn: Any = None,
) -> ModerationResult:
    chat_id = int(chat_id)
    target_id = int(target_id)
    actor_id = int(actor_id)

    def _fail(
        code: str,
        msg: str,
        *,
        already_done: bool = False,
        first_name: str | None = target_first_name,
        username: str | None = target_username,
    ) -> ModerationResult:
        return ModerationResult(
            ok=False,
            action=op,
            chat_id=chat_id,
            target_id=target_id,
            actor_id=actor_id,
            message=msg,
            error_code=code,
            already_done=already_done,
            target_first_name=first_name,
            target_username=username,
        )

    # 1. Issuer & chat guards
    if not settings.is_admin(actor_id):
        return _fail("not_admin", DM_ERR_NOT_ADMIN)
    if not gai.is_community_group(chat_id):
        return _fail("not_community_group", DM_ERR_NOT_COMMUNITY_GROUP)

    # 2. Target identity & exemption guards
    if target_id in SYNTHETIC_SYSTEM_USER_IDS:
        return _fail("synthetic_system_user", DM_ERR_INVALID_TARGET_SYSTEM)
    if target_id == actor_id:
        return _fail("target_is_issuer", DM_ERR_TARGET_SELF_ISSUER)
    if settings.is_admin(target_id):
        return _fail("target_is_admin", DM_ERR_TARGET_CONFIG_ADMIN)

    bot_id = await _resolve_bot_id(bot)
    if bot_id is not None and target_id == bot_id:
        return _fail("target_is_bot", DM_ERR_TARGET_BOT_SELF)

    whitelist = await gai.get_moderation_whitelist(session)
    if gai.is_user_whitelisted(target_id, whitelist):
        return _fail("target_is_whitelisted", DM_ERR_TARGET_WHITELISTED)

    # 3. Duration guard for mute
    effective_duration: int | None = None
    if op == "mute":
        if duration_seconds is None:
            effective_duration = DEFAULT_MANUAL_MUTE_SECONDS
        else:
            effective_duration = int(duration_seconds)
            if effective_duration < MIN_MANUAL_MUTE_SECONDS:
                return _fail("duration_too_short", DM_ERR_DURATION_TOO_SHORT)
            if effective_duration > MAX_MANUAL_MUTE_SECONDS:
                return _fail("duration_too_long", DM_ERR_DURATION_TOO_LONG)

    # 4. Serialize concurrent manual actions on the same (chat_id, target_id)
    lock_key = (chat_id, target_id)
    lock = _acquire_lock_entry(lock_key)
    try:
        async with lock:
            # 5. Live Telegram pre-flight member check
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=target_id)
            except (TelegramBadRequest, TelegramForbiddenError, Exception) as exc:
                code, err_msg = _translate_telegram_error(exc, target_id=target_id)
                logger.warning(
                    "group_moderation: preflight get_chat_member failed op=%s chat=%s target=%s: %s",
                    op,
                    chat_id,
                    target_id,
                    exc,
                )
                return _fail(code, err_msg)

            member_user = getattr(member, "user", None)
            live_first = getattr(member_user, "first_name", None) if member_user is not None else None
            live_username = getattr(member_user, "username", None) if member_user is not None else None
            eff_first = live_first or target_first_name
            eff_username = live_username or target_username

            status = _member_status_str(member)
            if status in ("creator", "administrator"):
                return _fail(
                    "target_is_group_admin",
                    DM_ERR_TARGET_GROUP_ADMIN,
                    first_name=eff_first,
                    username=eff_username,
                )

            if op == "unban" and status != "kicked":
                is_undo = source == "dm_undo"
                return _fail(
                    "not_banned",
                    CALLBACK_UNDO_ALREADY_UNBANNED if is_undo else DM_ERR_TARGET_NOT_BANNED.format(uid=target_id),
                    already_done=is_undo,
                    first_name=eff_first,
                    username=eff_username,
                )

            if op == "unmute":
                if status == "kicked":
                    return _fail(
                        "target_is_kicked",
                        DM_ERR_UNMUTE_KICKED.format(uid=target_id),
                        first_name=eff_first,
                        username=eff_username,
                    )
                if source == "dm_undo" and status != "restricted":
                    return _fail(
                        "already_unmuted",
                        CALLBACK_UNDO_ALREADY_UNMUTED,
                        already_done=True,
                        first_name=eff_first,
                        username=eff_username,
                    )

            if op == "mute" and status == "kicked":
                return _fail(
                    "target_is_kicked",
                    DM_ERR_MUTE_KICKED.format(uid=target_id),
                    first_name=eff_first,
                    username=eff_username,
                )

            # 6. Execute Telegram moderation primitive
            until_ts: int | None = None
            audit_action = {
                "mute": AUDIT_ACTION_MUTE,
                "unmute": AUDIT_ACTION_UNMUTE,
                "ban": AUDIT_ACTION_BAN,
                "unban": AUDIT_ACTION_UNBAN,
            }[op]

            try:
                if op == "mute":
                    now_val = float(now_fn()) if callable(now_fn) else time.time()
                    until_ts = int(now_val + int(effective_duration))
                    await bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=target_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=until_ts,
                    )
                elif op == "unmute":
                    restore_perms = await _resolve_unmute_permissions(bot, chat_id)
                    await bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=target_id,
                        permissions=restore_perms,
                        until_date=None,
                    )
                elif op == "ban":
                    await bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
                elif op == "unban":
                    await bot.unban_chat_member(
                        chat_id=chat_id,
                        user_id=target_id,
                        only_if_banned=True,
                    )
            except (TelegramBadRequest, TelegramForbiddenError, Exception) as exc:
                code, err_msg = _translate_telegram_error(exc, target_id=target_id)
                logger.warning(
                    "group_moderation: telegram action failed op=%s chat=%s target=%s actor=%s: %s",
                    op,
                    chat_id,
                    target_id,
                    actor_id,
                    exc,
                )
                return _fail(code, err_msg, first_name=eff_first, username=eff_username)

            # 7. Persist AuditLog row (never touches GroupAiModerationStrike)
            try:
                await _record_manual_audit(
                    session,
                    actor_id=actor_id,
                    audit_action=audit_action,
                    chat_id=chat_id,
                    target_id=target_id,
                    source=source,
                    duration_seconds=effective_duration,
                    until_ts=until_ts,
                    target_username=eff_username,
                    reply_message_id=reply_message_id,
                )
            except Exception as audit_exc:
                logger.warning(
                    "group_moderation: audit log write failed op=%s target=%s: %s",
                    op,
                    target_id,
                    audit_exc,
                )

            dm_name = format_target_dm_name(eff_first, eff_username)
            if op == "mute":
                success_msg = DM_SUCCESS_MUTE.format(
                    name=dm_name,
                    uid=target_id,
                    duration_my=format_duration_my(int(effective_duration)),
                )
            elif op == "ban":
                success_msg = DM_SUCCESS_BAN.format(name=dm_name, uid=target_id)
            elif op == "unmute":
                success_msg = DM_SUCCESS_UNMUTE.format(name=dm_name, uid=target_id)
            else:
                success_msg = DM_SUCCESS_UNBAN.format(name=dm_name, uid=target_id)

            logger.info(
                "group_moderation: action=%s source=%s chat_id=%s target_id=%s actor_id=%s duration=%s",
                audit_action,
                source,
                chat_id,
                target_id,
                actor_id,
                effective_duration,
            )
            return ModerationResult(
                ok=True,
                action=op,
                chat_id=chat_id,
                target_id=target_id,
                actor_id=actor_id,
                message=success_msg,
                duration_seconds=effective_duration,
                until_ts=until_ts,
                target_first_name=eff_first,
                target_username=eff_username,
            )
    finally:
        _release_lock_entry(lock_key)


async def manual_mute(
    bot: Any,
    session: Any,
    *,
    chat_id: int,
    target_id: int,
    actor_id: int,
    duration_seconds: int | None = None,
    source: str = "group_cmd",
    reply_message_id: int | None = None,
    target_first_name: str | None = None,
    target_username: str | None = None,
    now_fn: Any = None,
) -> ModerationResult:
    return await _execute_manual_action(
        bot,
        session,
        op="mute",
        chat_id=chat_id,
        target_id=target_id,
        actor_id=actor_id,
        duration_seconds=duration_seconds,
        source=source,
        reply_message_id=reply_message_id,
        target_first_name=target_first_name,
        target_username=target_username,
        now_fn=now_fn,
    )


async def manual_unmute(
    bot: Any,
    session: Any,
    *,
    chat_id: int,
    target_id: int,
    actor_id: int,
    duration_seconds: int | None = None,
    source: str = "group_cmd",
    reply_message_id: int | None = None,
    target_first_name: str | None = None,
    target_username: str | None = None,
) -> ModerationResult:
    return await _execute_manual_action(
        bot,
        session,
        op="unmute",
        chat_id=chat_id,
        target_id=target_id,
        actor_id=actor_id,
        duration_seconds=None,
        source=source,
        reply_message_id=reply_message_id,
        target_first_name=target_first_name,
        target_username=target_username,
    )


async def manual_ban(
    bot: Any,
    session: Any,
    *,
    chat_id: int,
    target_id: int,
    actor_id: int,
    duration_seconds: int | None = None,
    source: str = "group_cmd",
    reply_message_id: int | None = None,
    target_first_name: str | None = None,
    target_username: str | None = None,
) -> ModerationResult:
    return await _execute_manual_action(
        bot,
        session,
        op="ban",
        chat_id=chat_id,
        target_id=target_id,
        actor_id=actor_id,
        duration_seconds=None,
        source=source,
        reply_message_id=reply_message_id,
        target_first_name=target_first_name,
        target_username=target_username,
    )


async def manual_unban(
    bot: Any,
    session: Any,
    *,
    chat_id: int,
    target_id: int,
    actor_id: int,
    duration_seconds: int | None = None,
    source: str = "group_cmd",
    reply_message_id: int | None = None,
    target_first_name: str | None = None,
    target_username: str | None = None,
) -> ModerationResult:
    return await _execute_manual_action(
        bot,
        session,
        op="unban",
        chat_id=chat_id,
        target_id=target_id,
        actor_id=actor_id,
        duration_seconds=None,
        source=source,
        reply_message_id=reply_message_id,
        target_first_name=target_first_name,
        target_username=target_username,
    )


def build_group_notice(result: ModerationResult) -> str | None:
    """Build the single polite Burmese group notice line for successful in-group mute/ban."""
    if not result.ok or result.action not in ("mute", "ban"):
        return None
    target_display = format_target_display(result.target_first_name, result.target_username)
    if result.action == "mute":
        duration_my = format_duration_my(int(result.duration_seconds or DEFAULT_MANUAL_MUTE_SECONDS))
        return GROUP_NOTICE_MUTE.format(target_display=target_display, duration_my=duration_my)
    return GROUP_NOTICE_BAN.format(target_display=target_display)


def build_undo_keyboard(action: str, chat_id: int, target_id: int):
    """Build the 1-tap inline undo keyboard attached to admin mute/ban DM confirmations."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if action == "mute":
        label = BTN_UNDO_UNMUTE
        undo_op = "unmute"
    elif action == "ban":
        label = BTN_UNDO_UNBAN
        undo_op = "unban"
    else:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{UNDO_CALLBACK_PREFIX}:{undo_op}:{int(chat_id)}:{int(target_id)}",
                )
            ]
        ]
    )


def parse_undo_callback(data: str | None) -> tuple[str, int, int] | None:
    """Parse ``gmod_undo:{unmute|unban}:{chat_id}:{target_id}``."""
    parts = str(data or "").split(":")
    if len(parts) != 4 or parts[0] != UNDO_CALLBACK_PREFIX:
        return None
    undo_op = parts[1].strip().lower()
    if undo_op not in ("unmute", "unban"):
        return None
    try:
        return undo_op, int(parts[2]), int(parts[3])
    except (TypeError, ValueError):
        return None
