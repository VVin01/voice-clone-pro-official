"""Comprehensive unit & routing tests for Group AI Manual Admin Moderation (Option 3 Hybrid).

Covers all 5 test pillars:
1. Parser & resolver units (duration grammar, Myanmar digits, forever-cliff fencing,
   reply / text_mention / numeric targeting, @username rejection, service/topic/synthetic rejections).
2. Guards (admin/issuer/bot/whitelist/creator/group-admin, community-group scoping,
   anonymous issuer, kill-switch independence, concurrency serialization).
3. Telegram semantics (mute permissions + bounded until_date, unmute chat.permissions &
   fallback restoration, unban only_if_banned=True + kicked pre-check, honest Burmese
   permission error DMs, delete_message failure swallowing, stale-command guard, DM fallback,
   1-tap DM undo callback & idempotency).
4. Dispatcher routing & invisibility (real setup_handlers(dp) + MemoryStorage: group reply,
   DM commands, @botname mentions, non-admin total silence / zero oracle, active FSM protection,
   exact owner-approved group notice naming + offending-message deletion, zero edited_message).
5. Strike-ladder isolation & audit views (in-memory SQLite: zero strike corruption in either
   direction, AuditLog fields with zero group text, merged get_recent_moderation_actions,
   overview / audit-log / group-ai API endpoints, Admin Console JS badges & filter, zero emoji).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatPermissions,
    MessageEntity,
    Update,
)
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser
from sqlalchemy import select

from app.config import settings
from app.handlers import setup_handlers
from app.handlers.group_admin import GroupAiEditStates, handle_group_message
from app.handlers.group_moderation import (
    group_moderation_router,
    handle_moderation_undo_callback,
)
from app.models.schema import AuditLog, BotSetting, GroupAiModerationStrike, User
from app.services import group_ai_admin as gai
from app.services import group_moderation as gmod
from app.services import private_ai as pai
from app.services.ledger import get_or_create_user
from app.services.payments import save_payment_intent

ROOT = Path(__file__).resolve().parents[2]
GROUP_ID = gai.COMMUNITY_GROUP_ID  # -1003795677289
OTHER_GROUP_ID = -1009998887776
BOT_ID = 999888777
ADMIN_ID = 424242
ADMIN2_ID = 434343
REGULAR_USER_ID = 888111
WHITELISTED_USER_ID = 777222
GROUP_ADMIN_ONLY_ID = 666333
OTHER_BOT_ID = 555444


def _mock_session_maker(session):
    sm = MagicMock()
    sm.return_value.__aenter__ = AsyncMock(return_value=session)
    sm.return_value.__aexit__ = AsyncMock(return_value=None)
    return sm


def _make_member(
    *,
    user_id: int = REGULAR_USER_ID,
    status: str = "member",
    first_name: str = "Mg Mg",
    username: str | None = "mgmg",
    is_bot: bool = False,
):
    return SimpleNamespace(
        status=status,
        user=TgUser(id=user_id, is_bot=is_bot, first_name=first_name, username=username),
    )


def _make_bot(
    *,
    member=None,
    chat_permissions: ChatPermissions | None = None,
    bot_id: int = BOT_ID,
):
    bot = AsyncMock(spec=Bot)
    bot.id = bot_id
    bot.me = AsyncMock(
        return_value=TgUser(
            id=bot_id,
            is_bot=True,
            first_name="VoiceClonePro",
            username=gai.BOT_USERNAME,
        )
    )
    bot.get_me = bot.me
    bot.get_chat_member = AsyncMock(return_value=member or _make_member())
    default_perms = (
        chat_permissions
        if chat_permissions is not None
        else ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=False,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
    )
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(id=GROUP_ID, permissions=default_perms))
    bot.restrict_chat_member = AsyncMock(return_value=True)
    bot.ban_chat_member = AsyncMock(return_value=True)
    bot.unban_chat_member = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9001))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    return bot


def _command_entities(text: str) -> list[MessageEntity]:
    if not text.startswith("/"):
        return []
    cmd_tok = text.split(maxsplit=1)[0]
    return [MessageEntity(type="bot_command", offset=0, length=len(cmd_tok))]


def _make_group_update(
    *,
    text: str,
    from_user_id: int = ADMIN_ID,
    chat_id: int = GROUP_ID,
    message_id: int = 500,
    update_id: int = 1,
    reply_to_message: TgMessage | None = None,
    sender_chat: Chat | None = None,
    from_is_bot: bool = False,
    date: datetime | None = None,
    extra_entities: list[MessageEntity] | None = None,
) -> Update:
    chat = Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Voice Clone Pro Community")
    user = TgUser(id=from_user_id, is_bot=from_is_bot, first_name="Issuer", username="issuer")
    ents = _command_entities(text) + list(extra_entities or [])
    msg = TgMessage(
        message_id=message_id,
        date=date or datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        sender_chat=sender_chat,
        text=text,
        entities=ents or None,
        reply_to_message=reply_to_message,
    )
    return Update(update_id=update_id, message=msg)


def _make_reply_target_message(
    *,
    user_id: int = REGULAR_USER_ID,
    first_name: str = "Mg Mg",
    username: str | None = "mgmg",
    message_id: int = 400,
    chat_id: int = GROUP_ID,
    text: str = "spam text never stored",
    is_bot: bool = False,
    sender_chat: Chat | None = None,
) -> TgMessage:
    chat = Chat(id=chat_id, type=ChatType.SUPERGROUP, title="Voice Clone Pro Community")
    user = TgUser(id=user_id, is_bot=is_bot, first_name=first_name, username=username)
    return TgMessage(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        sender_chat=sender_chat,
        text=text,
    )


def _make_dm_update(
    *,
    text: str,
    from_user_id: int = ADMIN_ID,
    message_id: int = 600,
    update_id: int = 2,
    date: datetime | None = None,
    extra_entities: list[MessageEntity] | None = None,
) -> Update:
    chat = Chat(id=from_user_id, type=ChatType.PRIVATE)
    user = TgUser(id=from_user_id, is_bot=False, first_name="Admin", username="admin")
    ents = _command_entities(text) + list(extra_entities or [])
    msg = TgMessage(
        message_id=message_id,
        date=date or datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=ents or None,
    )
    return Update(update_id=update_id, message=msg)


def _send_calls_for_chat(bot, chat_id: int) -> list:
    out = []
    for call in bot.send_message.await_args_list:
        cid = call.args[0] if call.args else call.kwargs.get("chat_id")
        if int(cid) == int(chat_id):
            out.append(call)
    return out


def _call_text(call) -> str:
    if len(call.args) >= 2:
        return str(call.args[1])
    return str(call.kwargs.get("text", ""))


@pytest.fixture(autouse=True)
def _reset_gai_runtime(monkeypatch):
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    monkeypatch.setattr(gai, "context_buffer", gai.GroupContextBuffer())
    monkeypatch.setattr(pai, "private_rate_limiter", gai.GroupReplyRateLimiter())


# ===========================================================================
# 1. Parser & Target Resolver Units
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 3600),
        ("", 3600),
        ("   ", 3600),
        ("1m", 60),
        ("15m", 900),
        ("1h", 3600),
        ("1h30m", 5400),
        ("1d12h", 129600),
        ("7d", 7 * 86400),
        ("2w", 14 * 86400),
        ("30d", 30 * 86400),
        ("1H30M", 5400),
        ("7D", 7 * 86400),
        ("၁၅m", 900),
        ("၁h၃၀m", 5400),
        ("၇d", 7 * 86400),
        ("၂w", 14 * 86400),
    ],
)
def test_parse_manual_duration_valid(raw, expected):
    parsed = gmod.parse_manual_duration(raw)
    assert parsed == expected
    # Telegram forever-cliffs (<30s and >366d) must be strictly unreachable
    assert gmod.MIN_MANUAL_MUTE_SECONDS <= parsed <= gmod.MAX_MANUAL_MUTE_SECONDS
    assert parsed >= 60
    assert parsed <= 30 * 86400


@pytest.mark.parametrize(
    "raw,expected_code",
    [
        ("30s", "invalid_format"),
        ("0m", "too_short"),
        ("0h0m", "too_short"),
        ("31d", "too_long"),
        ("400d", "too_long"),
        ("5w", "too_long"),
        ("30", "missing_unit"),
        ("3600", "missing_unit"),
        ("၃၀", "missing_unit"),
        ("abc", "invalid_format"),
        ("-1h", "invalid_format"),
        ("1h1h", "invalid_format"),
        ("30m1h", "invalid_format"),
    ],
)
def test_parse_manual_duration_rejections(raw, expected_code):
    with pytest.raises(gmod.DurationError) as exc_info:
        gmod.parse_manual_duration(raw)
    assert exc_info.value.code == expected_code
    assert gai.strip_emoji(exc_info.value.message) == exc_info.value.message


def test_format_duration_my_examples():
    assert gmod.format_duration_my(900) == "15 မိနစ်"
    assert gmod.format_duration_my(3600) == "1 နာရီ"
    assert gmod.format_duration_my(5400) == "1 နာရီ 30 မိနစ်"
    assert gmod.format_duration_my(86400) == "1 ရက်"
    assert gmod.format_duration_my(129600) == "1 ရက် 12 နာရီ"


def test_resolve_target_reply_text_mention_and_numeric():
    # (a) Reply to normal member
    reply_msg = _make_reply_target_message(user_id=REGULAR_USER_ID, first_name="Aung", username="aung", message_id=401)
    res = gmod.resolve_target_user_id(reply_to_message=reply_msg, args_text="2h")
    assert res.user_id == REGULAR_USER_ID
    assert res.first_name == "Aung"
    assert res.username == "aung"
    assert res.reply_message_id == 401
    assert res.remaining_args == ("2h",)

    # (a2) Reply to a third-party bot IS a valid target (only this bot itself is forbidden)
    bot_reply = _make_reply_target_message(user_id=OTHER_BOT_ID, first_name="SpamBot", username="spambot", is_bot=True)
    res_bot = gmod.resolve_target_user_id(reply_to_message=bot_reply, args_text="")
    assert res_bot.user_id == OTHER_BOT_ID

    # (b) text_mention entity with multi-word display name + trailing duration
    text = "/mute John Doe 45m"
    ent = MessageEntity(
        type="text_mention",
        offset=6,
        length=8,
        user=TgUser(id=REGULAR_USER_ID, is_bot=False, first_name="John Doe", username=None),
    )
    res_tm = gmod.resolve_target_user_id(entities=[ent], text=text, args_text="John Doe 45m")
    assert res_tm.user_id == REGULAR_USER_ID
    assert res_tm.first_name == "John Doe"
    assert res_tm.reply_message_id is None
    assert res_tm.remaining_args == ("45m",)

    # (c) Numeric user_id (ASCII and Myanmar digits)
    res_num = gmod.resolve_target_user_id(args_text=f"{REGULAR_USER_ID} 3h", is_dm=True)
    assert res_num.user_id == REGULAR_USER_ID
    assert res_num.remaining_args == ("3h",)

    res_my = gmod.resolve_target_user_id(args_text="၈၈၈၁၁၁ ၁h", is_dm=True)
    assert res_my.user_id == 888111
    assert res_my.remaining_args == ("၁h",)


def test_resolve_target_rejections():
    # @username explicitly unsupported (owner decision 2)
    with pytest.raises(gmod.TargetResolutionError) as e_at:
        gmod.resolve_target_user_id(args_text="@mgmg 1h")
    assert e_at.value.code == "username_unsupported"

    # sender_chat reply (anonymous admin / linked channel)
    anon_reply = _make_reply_target_message(
        user_id=1087968824,
        sender_chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="Community"),
    )
    with pytest.raises(gmod.TargetResolutionError) as e_sc:
        gmod.resolve_target_user_id(reply_to_message=anon_reply)
    assert e_sc.value.code == "sender_chat_reply"

    # forum_topic_created pseudo-reply
    topic_reply = SimpleNamespace(
        forum_topic_created=SimpleNamespace(name="General"),
        sender_chat=None,
        from_user=TgUser(id=REGULAR_USER_ID, is_bot=False, first_name="TopicMaker"),
        message_id=10,
    )
    with pytest.raises(gmod.TargetResolutionError) as e_ft:
        gmod.resolve_target_user_id(reply_to_message=topic_reply)
    assert e_ft.value.code == "forum_topic_created"

    # Service messages (new_chat_members, left_chat_member, pinned_message)
    for attr_name in ("new_chat_members", "left_chat_member", "pinned_message"):
        svc_reply = SimpleNamespace(
            forum_topic_created=None,
            sender_chat=None,
            from_user=TgUser(id=REGULAR_USER_ID, is_bot=False, first_name="Adder"),
            message_id=11,
            **{attr_name: [SimpleNamespace(id=12345)]},
        )
        with pytest.raises(gmod.TargetResolutionError) as e_svc:
            gmod.resolve_target_user_id(reply_to_message=svc_reply)
        assert e_svc.value.code == "service_message_reply"

    # Synthetic Telegram system IDs (reply and numeric)
    for sys_id in (777000, 1087968824, 136817688):
        sys_reply = _make_reply_target_message(user_id=sys_id)
        with pytest.raises(gmod.TargetResolutionError) as e_sys1:
            gmod.resolve_target_user_id(reply_to_message=sys_reply)
        assert e_sys1.value.code == "synthetic_system_user"

        with pytest.raises(gmod.TargetResolutionError) as e_sys2:
            gmod.resolve_target_user_id(args_text=str(sys_id), is_dm=True)
        assert e_sys2.value.code == "synthetic_system_user"

    # Missing target
    with pytest.raises(gmod.TargetResolutionError) as e_miss_grp:
        gmod.resolve_target_user_id(args_text="", is_dm=False)
    assert e_miss_grp.value.code == "missing_target"
    with pytest.raises(gmod.TargetResolutionError) as e_miss_dm:
        gmod.resolve_target_user_id(args_text="", is_dm=True)
    assert e_miss_dm.value.code == "missing_target"


# ===========================================================================
# 2. Guards & Concurrency
# ===========================================================================


@pytest.mark.asyncio
async def test_guards_protect_admins_bot_whitelist_group_admins_and_scope(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    db_session.add(BotSetting(key="group_ai_moderation_whitelist", value=str(WHITELISTED_USER_ID)))
    await db_session.commit()

    bot = _make_bot()

    # Non-admin issuer rejected
    r_non_admin = await gmod.manual_mute(
        bot, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=999111
    )
    assert not r_non_admin.ok and r_non_admin.error_code == "not_admin"

    # Non-community chat rejected
    r_other_chat = await gmod.manual_mute(
        bot, db_session, chat_id=OTHER_GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert not r_other_chat.ok and r_other_chat.error_code == "not_community_group"

    # Dynamic settings.required_group_id overrides COMMUNITY_GROUP_ID
    monkeypatch.setattr(settings, "required_group_id", str(OTHER_GROUP_ID))
    r_dyn_old = await gmod.manual_mute(
        bot, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert not r_dyn_old.ok and r_dyn_old.error_code == "not_community_group"
    monkeypatch.setattr(settings, "required_group_id", "")

    # Self-target rejected
    r_self = await gmod.manual_ban(
        bot, db_session, chat_id=GROUP_ID, target_id=ADMIN_ID, actor_id=ADMIN_ID
    )
    assert not r_self.ok and r_self.error_code == "target_is_issuer"

    # Other configured admin rejected
    r_admin = await gmod.manual_ban(
        bot, db_session, chat_id=GROUP_ID, target_id=ADMIN2_ID, actor_id=ADMIN_ID
    )
    assert not r_admin.ok and r_admin.error_code == "target_is_admin"

    # Bot itself rejected
    r_bot = await gmod.manual_ban(
        bot, db_session, chat_id=GROUP_ID, target_id=BOT_ID, actor_id=ADMIN_ID
    )
    assert not r_bot.ok and r_bot.error_code == "target_is_bot"

    # Whitelisted user (live DB read) rejected
    r_wl = await gmod.manual_mute(
        bot, db_session, chat_id=GROUP_ID, target_id=WHITELISTED_USER_ID, actor_id=ADMIN_ID
    )
    assert not r_wl.ok and r_wl.error_code == "target_is_whitelisted"

    # Telegram-side group creator / administrator (NOT in settings.admin_ids) rejected
    for tg_status in ("creator", "administrator"):
        bot_ga = _make_bot(member=_make_member(user_id=GROUP_ADMIN_ONLY_ID, status=tg_status))
        r_ga = await gmod.manual_mute(
            bot_ga, db_session, chat_id=GROUP_ID, target_id=GROUP_ADMIN_ONLY_ID, actor_id=ADMIN_ID
        )
        assert not r_ga.ok and r_ga.error_code == "target_is_group_admin"
        bot_ga.restrict_chat_member.assert_not_awaited()

    bot.restrict_chat_member.assert_not_awaited()
    bot.ban_chat_member.assert_not_awaited()
    audits = (await db_session.execute(select(AuditLog))).scalars().all()
    assert audits == []


@pytest.mark.asyncio
async def test_manual_moderation_independent_of_kill_switches(db_session, monkeypatch):
    """Works even when BOTH group_ai_admin_enabled and group_ai_moderation_enabled are OFF."""
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="false"))
    db_session.add(BotSetting(key="group_ai_moderation_enabled", value="false"))
    await db_session.commit()

    assert await gai.is_enabled(db_session) is False
    assert await gai.is_moderation_enabled(db_session) is False

    bot = _make_bot()
    res = await gmod.manual_mute(
        bot, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID, duration_seconds=600
    )
    assert res.ok is True
    bot.restrict_chat_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_per_target_lock_serializes_concurrent_actions(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    events: list[str] = []

    bot = _make_bot()
    status_state = {"status": "member"}

    async def slow_get_member(chat_id, user_id):
        events.append(f"get:{status_state['status']}")
        return _make_member(user_id=user_id, status=status_state["status"])

    async def slow_ban(chat_id, user_id):
        events.append("ban_start")
        await asyncio.sleep(0.05)
        status_state["status"] = "kicked"
        events.append("ban_end")
        return True

    bot.get_chat_member = AsyncMock(side_effect=slow_get_member)
    bot.ban_chat_member = AsyncMock(side_effect=slow_ban)

    # Ban runs first and holds the per-(chat_id, target_id) lock; concurrent mute must wait
    # until ban completes and then see status="kicked".
    t_ban = asyncio.create_task(
        gmod.manual_ban(bot, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID)
    )
    await asyncio.sleep(0.01)
    t_mute = asyncio.create_task(
        gmod.manual_mute(bot, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN2_ID)
    )
    res_ban, res_mute = await asyncio.gather(t_ban, t_mute)

    assert res_ban.ok is True
    assert res_mute.ok is False
    assert res_mute.error_code == "target_is_kicked"
    assert events == ["get:member", "ban_start", "ban_end", "get:kicked"]


# ===========================================================================
# 3. Telegram Semantics, Errors, Stale Guard, DM Fallback & Undo Callback
# ===========================================================================


@pytest.mark.asyncio
async def test_telegram_primitives_mute_unmute_ban_unban(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # Mute: can_send_messages=False + bounded until_date
    bot_m = _make_bot()
    res_m = await gmod.manual_mute(
        bot_m,
        db_session,
        chat_id=GROUP_ID,
        target_id=REGULAR_USER_ID,
        actor_id=ADMIN_ID,
        duration_seconds=1800,
        now_fn=lambda: 1_700_000_000.0,
    )
    assert res_m.ok is True
    bot_m.restrict_chat_member.assert_awaited_once()
    m_kw = bot_m.restrict_chat_member.await_args.kwargs
    assert m_kw["chat_id"] == GROUP_ID
    assert m_kw["user_id"] == REGULAR_USER_ID
    assert m_kw["permissions"].can_send_messages is False
    assert m_kw["until_date"] == 1_700_001_800

    # Unmute (with chat.permissions): restores exact chat default permissions
    custom_chat_perms = ChatPermissions(
        can_send_messages=True,
        can_send_photos=True,
        can_send_voice_notes=True,
        can_send_polls=False,
        can_pin_messages=False,
    )
    bot_u = _make_bot(
        member=_make_member(status="restricted"),
        chat_permissions=custom_chat_perms,
    )
    res_u = await gmod.manual_unmute(
        bot_u, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert res_u.ok is True
    bot_u.get_chat.assert_awaited_once_with(chat_id=GROUP_ID)
    u_kw = bot_u.restrict_chat_member.await_args.kwargs
    assert u_kw["permissions"] == custom_chat_perms
    assert u_kw["until_date"] is None

    # Unmute (fallback when chat.permissions is None): restores all can_send_* + previews, never pin/change_info
    bot_ufb = _make_bot(member=_make_member(status="restricted"))
    bot_ufb.get_chat = AsyncMock(return_value=SimpleNamespace(id=GROUP_ID, permissions=None))
    res_ufb = await gmod.manual_unmute(
        bot_ufb, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert res_ufb.ok is True
    fb_perms = bot_ufb.restrict_chat_member.await_args.kwargs["permissions"]
    assert fb_perms.can_send_messages is True
    assert fb_perms.can_send_photos is True
    assert fb_perms.can_send_voice_notes is True
    assert fb_perms.can_send_other_messages is True
    assert fb_perms.can_add_web_page_previews is True
    assert fb_perms.can_pin_messages is not True
    assert fb_perms.can_change_info is not True

    # Unmute refuses kicked user
    bot_uk = _make_bot(member=_make_member(status="kicked"))
    res_uk = await gmod.manual_unmute(
        bot_uk, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert not res_uk.ok and res_uk.error_code == "target_is_kicked"
    bot_uk.restrict_chat_member.assert_not_awaited()

    # Ban: calls ban_chat_member
    bot_b = _make_bot()
    res_b = await gmod.manual_ban(
        bot_b, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert res_b.ok is True
    bot_b.ban_chat_member.assert_awaited_once_with(chat_id=GROUP_ID, user_id=REGULAR_USER_ID)
    assert "Community Group မှ" in res_b.message

    # Unban: refuses non-kicked member (never accidentally kicks!)
    bot_ub_active = _make_bot(member=_make_member(status="member"))
    res_ub_active = await gmod.manual_unban(
        bot_ub_active, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert not res_ub_active.ok and res_ub_active.error_code == "not_banned"
    bot_ub_active.unban_chat_member.assert_not_awaited()

    # Unban: kicked user -> ALWAYS passes only_if_banned=True
    bot_ub = _make_bot(member=_make_member(status="kicked"))
    res_ub = await gmod.manual_unban(
        bot_ub, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID
    )
    assert res_ub.ok is True
    bot_ub.unban_chat_member.assert_awaited_once_with(
        chat_id=GROUP_ID, user_id=REGULAR_USER_ID, only_if_banned=True
    )


@pytest.mark.asyncio
async def test_permission_errors_dm_honestly_and_never_post_in_group(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_moderation.AsyncSessionLocal", sm)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = _make_bot()
    bot.restrict_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(
            method=MagicMock(),
            message="Bad Request: not enough rights to restrict/unrestrict chat member",
        )
    )

    reply_msg = _make_reply_target_message(user_id=REGULAR_USER_ID, message_id=410)
    upd = _make_group_update(text="/mute 1h", from_user_id=ADMIN_ID, message_id=510, reply_to_message=reply_msg)
    await dp.feed_update(bot, upd)

    # Zero group posts, offending message NOT deleted, invoking command WAS best-effort deleted
    assert _send_calls_for_chat(bot, GROUP_ID) == []
    deleted_ids = [c.kwargs["message_id"] for c in bot.delete_message.await_args_list]
    assert deleted_ids == [510]

    # Issuing admin received honest Burmese permission failure by DM
    dm_calls = _send_calls_for_chat(bot, ADMIN_ID)
    assert len(dm_calls) == 1
    assert _call_text(dm_calls[0]) == gmod.DM_ERR_BOT_NO_RIGHTS
    assert dm_calls[0].kwargs.get("reply_markup") is None

    # No AuditLog written on failure
    audits = (await db_session.execute(select(AuditLog))).scalars().all()
    assert audits == []


@pytest.mark.asyncio
async def test_stale_command_rejected_and_dm_unreachable_fallback(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", f"{ADMIN_ID},{ADMIN2_ID}")
    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_moderation.AsyncSessionLocal", sm)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    bot = _make_bot()
    # Issuing admin blocked bot / never started DM -> fallback must go to ADMIN2_ID, never group
    async def send_side_effect(chat_id, text, **kwargs):
        if int(chat_id) == ADMIN_ID:
            raise TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked by the user")
        return SimpleNamespace(message_id=9100)

    bot.send_message = AsyncMock(side_effect=send_side_effect)

    stale_time = datetime.now(timezone.utc) - timedelta(minutes=6)
    reply_msg = _make_reply_target_message(user_id=REGULAR_USER_ID, message_id=411)
    upd = _make_group_update(
        text="/ban",
        from_user_id=ADMIN_ID,
        message_id=511,
        reply_to_message=reply_msg,
        date=stale_time,
    )
    await dp.feed_update(bot, upd)

    # Stale command must NOT execute ban or post in group
    bot.ban_chat_member.assert_not_awaited()
    assert _send_calls_for_chat(bot, GROUP_ID) == []

    # Fallback DM reached ADMIN2_ID with stale notice
    fb_calls = _send_calls_for_chat(bot, ADMIN2_ID)
    assert len(fb_calls) == 1
    assert gmod.DM_ERR_STALE_COMMAND in _call_text(fb_calls[0])
    assert str(ADMIN_ID) in _call_text(fb_calls[0])


@pytest.mark.asyncio
async def test_dm_undo_callback_admin_only_executes_and_is_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))

    # 1. Non-admin callback rejected
    non_admin_cb = MagicMock(spec=CallbackQuery)
    non_admin_cb.from_user = TgUser(id=REGULAR_USER_ID, is_bot=False, first_name="Intruder")
    non_admin_cb.data = f"gmod_undo:unmute:{GROUP_ID}:{REGULAR_USER_ID}"
    non_admin_cb.answer = AsyncMock()
    non_admin_cb.bot = _make_bot()
    await handle_moderation_undo_callback(non_admin_cb, session=db_session)
    non_admin_cb.answer.assert_awaited_once_with(gmod.CALLBACK_UNDO_NOT_ADMIN, show_alert=True)
    non_admin_cb.bot.restrict_chat_member.assert_not_awaited()

    # 2. First admin tap unmutes + clears keyboard + writes AuditLog(source="dm_undo")
    bot1 = _make_bot(member=_make_member(status="restricted"))
    cb1 = MagicMock(spec=CallbackQuery)
    cb1.from_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    cb1.data = f"gmod_undo:unmute:{GROUP_ID}:{REGULAR_USER_ID}"
    cb1.answer = AsyncMock()
    cb1.message = MagicMock()
    cb1.message.text = "Original DM confirmation"
    cb1.message.edit_text = AsyncMock()
    cb1.message.edit_reply_markup = AsyncMock()
    cb1.bot = bot1
    await handle_moderation_undo_callback(cb1, session=db_session)
    bot1.restrict_chat_member.assert_awaited_once()
    cb1.message.edit_text.assert_awaited_once()
    assert cb1.message.edit_text.await_args.kwargs["reply_markup"] is None
    cb1.answer.assert_awaited_once_with(gmod.CALLBACK_UNDO_UNMUTED)

    audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == gmod.AUDIT_ACTION_UNMUTE))
    ).scalar_one()
    details = json.loads(audit.details_json)
    assert details["source"] == "dm_undo"
    assert audit.admin_id == ADMIN_ID
    assert audit.target_id == str(REGULAR_USER_ID)

    # 3. Second tap (member already unmuted) is idempotent — polite notice, no error, clears markup
    bot2 = _make_bot(member=_make_member(status="member"))
    cb2 = MagicMock(spec=CallbackQuery)
    cb2.from_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    cb2.data = f"gmod_undo:unmute:{GROUP_ID}:{REGULAR_USER_ID}"
    cb2.answer = AsyncMock()
    cb2.message = MagicMock()
    cb2.message.edit_reply_markup = AsyncMock()
    cb2.bot = bot2
    await handle_moderation_undo_callback(cb2, session=db_session)
    bot2.restrict_chat_member.assert_not_awaited()
    cb2.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    cb2.answer.assert_awaited_once_with(gmod.CALLBACK_UNDO_ALREADY_UNMUTED)

    # 4. Unban undo second tap (already not kicked) is also idempotent
    bot3 = _make_bot(member=_make_member(status="left"))
    cb3 = MagicMock(spec=CallbackQuery)
    cb3.from_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
    cb3.data = f"gmod_undo:unban:{GROUP_ID}:{REGULAR_USER_ID}"
    cb3.answer = AsyncMock()
    cb3.message = MagicMock()
    cb3.message.edit_reply_markup = AsyncMock()
    cb3.bot = bot3
    await handle_moderation_undo_callback(cb3, session=db_session)
    bot3.unban_chat_member.assert_not_awaited()
    cb3.answer.assert_awaited_once_with(gmod.CALLBACK_UNDO_ALREADY_UNBANNED)


# ===========================================================================
# 4. Dispatcher Routing, Invisibility, Group Notice & Offending Deletion
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatcher_group_reply_mute_and_ban_notice_and_dual_delete(db_session, monkeypatch):
    """In-group reply /mute & /ban:
    - Reach group_moderation_router (never handle_group_message)
    - Best-effort delete BOTH invoking command AND offending replied-to message
    - Post EXACTLY ONE group notice with target_display (first_name + (@username), NEVER numeric ID)
    - Survive delete_message failures cleanly
    """
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_moderation.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.group_admin.AsyncSessionLocal", sm)

    group_ai_spy = AsyncMock()
    monkeypatch.setattr(gai, "ask_group_admin_ai", group_ai_spy)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    # Case A: reply /mute 1h with username present + delete_message FAILS (must not fail action!)
    bot = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, first_name="Kyaw", username="kyaw99"))
    bot.delete_message = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: message can't be deleted")
    )
    reply_msg = _make_reply_target_message(
        user_id=REGULAR_USER_ID, first_name="Kyaw", username="kyaw99", message_id=420
    )
    upd = _make_group_update(
        text="/mute 1h",
        from_user_id=ADMIN_ID,
        message_id=520,
        update_id=10,
        reply_to_message=reply_msg,
    )
    await dp.feed_update(bot, upd)

    group_ai_spy.assert_not_awaited()
    bot.restrict_chat_member.assert_awaited_once()
    # Both offending (420) and invoking (520) deletions were attempted independently
    attempted_del_ids = {c.kwargs["message_id"] for c in bot.delete_message.await_args_list}
    assert attempted_del_ids == {420, 520}

    # Exactly ONE group notice with shipped copy naming Kyaw (@kyaw99) and NEVER numeric ID
    grp_calls = _send_calls_for_chat(bot, GROUP_ID)
    assert len(grp_calls) == 1
    grp_text = _call_text(grp_calls[0])
    assert grp_text == (
        "သတိပေးချက်: Group စည်းကမ်း ချိုးဖောက်မှုကြောင့် Kyaw (@kyaw99) ၏ "
        "စာပို့ခွင့်ကို 1 နာရီ ပိတ်ထားပါသည်။"
    )
    assert str(REGULAR_USER_ID) not in grp_text
    assert gai.strip_emoji(grp_text) == grp_text

    # Admin DM has numeric ID + 1-tap Unmute button
    dm_calls = _send_calls_for_chat(bot, ADMIN_ID)
    assert len(dm_calls) == 1
    assert str(REGULAR_USER_ID) in _call_text(dm_calls[0])
    kb = dm_calls[0].kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == f"gmod_undo:unmute:{GROUP_ID}:{REGULAR_USER_ID}"

    # Case B: reply /ban with NO username -> target_display is first name only, both messages deleted
    bot_b = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, first_name="Su Su", username=None))
    reply_msg_b = _make_reply_target_message(
        user_id=REGULAR_USER_ID, first_name="Su Su", username=None, message_id=421
    )
    upd_b = _make_group_update(
        text=f"/ban@{gai.BOT_USERNAME}",
        from_user_id=ADMIN_ID,
        message_id=521,
        update_id=11,
        reply_to_message=reply_msg_b,
    )
    await dp.feed_update(bot_b, upd_b)

    bot_b.ban_chat_member.assert_awaited_once_with(chat_id=GROUP_ID, user_id=REGULAR_USER_ID)
    assert {c.kwargs["message_id"] for c in bot_b.delete_message.await_args_list} == {421, 521}
    grp_calls_b = _send_calls_for_chat(bot_b, GROUP_ID)
    assert len(grp_calls_b) == 1
    grp_text_b = _call_text(grp_calls_b[0])
    assert grp_text_b == (
        "သတိပေးချက်: Group စည်းကမ်း ချိုးဖောက်မှုကြောင့် Su Su ကို "
        "Community Group မှ ထုတ်ပယ်လိုက်ပါသည်။"
    )
    assert str(REGULAR_USER_ID) not in grp_text_b
    assert "(@" not in grp_text_b
    dm_calls_b = _send_calls_for_chat(bot_b, ADMIN_ID)
    assert len(dm_calls_b) == 1
    assert dm_calls_b[0].kwargs["reply_markup"].inline_keyboard[0][0].callback_data == (
        f"gmod_undo:unban:{GROUP_ID}:{REGULAR_USER_ID}"
    )

    # Case C: group numeric-ID /mute (no reply) -> group notice posted, ONLY command message deleted
    bot_c = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, first_name="Ko Ko", username="koko"))
    upd_c = _make_group_update(
        text=f"/mute {REGULAR_USER_ID} 15m",
        from_user_id=ADMIN_ID,
        message_id=522,
        update_id=12,
    )
    await dp.feed_update(bot_c, upd_c)
    assert [c.kwargs["message_id"] for c in bot_c.delete_message.await_args_list] == [522]
    grp_calls_c = _send_calls_for_chat(bot_c, GROUP_ID)
    assert len(grp_calls_c) == 1
    assert "Ko Ko (@koko)" in _call_text(grp_calls_c[0])
    assert "15 မိနစ်" in _call_text(grp_calls_c[0])
    assert str(REGULAR_USER_ID) not in _call_text(grp_calls_c[0])


@pytest.mark.asyncio
async def test_dispatcher_unmute_unban_and_dm_actions_post_nothing_in_group(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_moderation.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.group_admin.AsyncSessionLocal", sm)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    # Group reply /unmute -> NO group notice, replied-to message NOT deleted (only invoking cmd deleted)
    bot_u = _make_bot(member=_make_member(status="restricted"))
    reply_u = _make_reply_target_message(user_id=REGULAR_USER_ID, message_id=430)
    await dp.feed_update(
        bot_u,
        _make_group_update(text="/unmute", from_user_id=ADMIN_ID, message_id=530, update_id=20, reply_to_message=reply_u),
    )
    bot_u.restrict_chat_member.assert_awaited_once()
    assert _send_calls_for_chat(bot_u, GROUP_ID) == []
    assert [c.kwargs["message_id"] for c in bot_u.delete_message.await_args_list] == [530]
    assert len(_send_calls_for_chat(bot_u, ADMIN_ID)) == 1

    # DM /mute, /ban, /unban -> ZERO group posts, ZERO delete_message calls
    bot_dm_m = _make_bot()
    await dp.feed_update(
        bot_dm_m,
        _make_dm_update(text=f"/mute {REGULAR_USER_ID} 2h", from_user_id=ADMIN_ID, update_id=21),
    )
    bot_dm_m.restrict_chat_member.assert_awaited_once()
    assert _send_calls_for_chat(bot_dm_m, GROUP_ID) == []
    bot_dm_m.delete_message.assert_not_awaited()
    assert len(_send_calls_for_chat(bot_dm_m, ADMIN_ID)) == 1

    bot_dm_b = _make_bot()
    await dp.feed_update(
        bot_dm_b,
        _make_dm_update(text=f"/ban {REGULAR_USER_ID}", from_user_id=ADMIN_ID, update_id=22),
    )
    bot_dm_b.ban_chat_member.assert_awaited_once()
    assert _send_calls_for_chat(bot_dm_b, GROUP_ID) == []
    bot_dm_b.delete_message.assert_not_awaited()

    bot_dm_ub = _make_bot(member=_make_member(status="kicked"))
    await dp.feed_update(
        bot_dm_ub,
        _make_dm_update(text=f"/unban {REGULAR_USER_ID}", from_user_id=ADMIN_ID, update_id=23),
    )
    bot_dm_ub.unban_chat_member.assert_awaited_once_with(
        chat_id=GROUP_ID, user_id=REGULAR_USER_ID, only_if_banned=True
    )
    assert _send_calls_for_chat(bot_dm_ub, GROUP_ID) == []
    bot_dm_ub.delete_message.assert_not_awaited()
    assert len(_send_calls_for_chat(bot_dm_ub, ADMIN_ID)) == 1

    # Admin DM /modhelp returns cheat sheet
    bot_help = _make_bot()
    await dp.feed_update(
        bot_help,
        _make_dm_update(text="/modhelp", from_user_id=ADMIN_ID, update_id=24),
    )
    help_calls = _send_calls_for_chat(bot_help, ADMIN_ID)
    assert len(help_calls) == 1
    assert _call_text(help_calls[0]) == gmod.DM_MODHELP_TEXT


@pytest.mark.asyncio
async def test_dispatcher_non_admin_invisibility_and_fsm_and_anonymous_admin(db_session, monkeypatch):
    """Constraint 2: non-admins get total silence in group and zero oracle in DM;
    anonymous admin ignored; admin plain text still prefiltered; FSM flow protected;
    zero edited_message handlers registered."""
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(settings, "group_ai_admin_enabled", settings.group_ai_admin_enabled)
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="true"))
    await db_session.commit()

    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_moderation.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.group_admin.AsyncSessionLocal", sm)

    group_ai_spy = AsyncMock(return_value=gai.DecisionResult(gai.DECISION_REPLY, "SHOULD NOT RUN"))
    private_ai_spy = AsyncMock(return_value=(gai.DECISION_REPLY, "SHOULD NOT RUN"))
    monkeypatch.setattr(gai, "ask_group_admin_ai", group_ai_spy)
    monkeypatch.setattr(pai, "ask_private_ai", private_ai_spy)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)

    # Zero edited_message handlers registered anywhere on the dispatcher/routers
    assert "edited_message" not in dp.resolve_used_update_types()
    assert len(group_moderation_router.edited_message.handlers) == 0

    # 1. Non-admin tries all 4 commands in group -> total silence
    reply_msg = _make_reply_target_message(user_id=777999, message_id=440)
    for idx, cmd in enumerate(("/mute 1h", "/unmute", "/ban", "/unban 777999"), start=30):
        bot = _make_bot()
        await dp.feed_update(
            bot,
            _make_group_update(text=cmd, from_user_id=REGULAR_USER_ID, message_id=540 + idx, update_id=idx, reply_to_message=reply_msg),
        )
        bot.send_message.assert_not_awaited()
        bot.restrict_chat_member.assert_not_awaited()
        bot.ban_chat_member.assert_not_awaited()
        bot.unban_chat_member.assert_not_awaited()
        bot.delete_message.assert_not_awaited()

    # 2. Anonymous admin in group (sender_chat set) -> ignored completely
    bot_anon = _make_bot()
    await dp.feed_update(
        bot_anon,
        _make_group_update(
            text="/mute 1h",
            from_user_id=1087968824,
            from_is_bot=True,
            sender_chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP, title="Community"),
            message_id=550,
            update_id=40,
            reply_to_message=reply_msg,
        ),
    )
    bot_anon.restrict_chat_member.assert_not_awaited()
    bot_anon.send_message.assert_not_awaited()

    # 3. Admin plain non-command text in group still hits prefilter_reason() == "admin_author"
    assert (
        gai.prefilter_reason(
            chat_id=GROUP_ID,
            chat_type="supergroup",
            text="မင်္ဂလာပါ အားလုံး",
            from_user_id=ADMIN_ID,
            from_user_is_bot=False,
            sender_chat_id=None,
        )
        == "admin_author"
    )
    bot_plain = _make_bot()
    await dp.feed_update(
        bot_plain,
        _make_group_update(text="မင်္ဂလာပါ အားလုံး", from_user_id=ADMIN_ID, message_id=551, update_id=41),
    )
    group_ai_spy.assert_not_awaited()
    bot_plain.send_message.assert_not_awaited()

    # 4. Non-admin DM /mute behaves IDENTICALLY to unknown /foobar (no oracle!)
    bot_dm_na1 = _make_bot()
    bot_dm_na2 = _make_bot()
    await dp.feed_update(bot_dm_na1, _make_dm_update(text=f"/mute {REGULAR_USER_ID} 1h", from_user_id=REGULAR_USER_ID, update_id=42))
    await dp.feed_update(bot_dm_na2, _make_dm_update(text="/foobar 123", from_user_id=REGULAR_USER_ID, update_id=43))
    assert bot_dm_na1.send_message.await_count == bot_dm_na2.send_message.await_count == 0
    bot_dm_na1.restrict_chat_member.assert_not_awaited()

    # Even with an active payment intent (<24h), non-admin /mute matches /foobar identically (no oracle!)
    await get_or_create_user(db_session, telegram_id=REGULAR_USER_ID)
    await save_payment_intent(db_session, REGULAR_USER_ID, "MIN_10")
    await db_session.commit()
    bot_pi1 = _make_bot()
    bot_pi2 = _make_bot()
    await dp.feed_update(bot_pi1, _make_dm_update(text="/mute 12345 1h", from_user_id=REGULAR_USER_ID, update_id=44))
    await dp.feed_update(bot_pi2, _make_dm_update(text="/foobar 12345", from_user_id=REGULAR_USER_ID, update_id=45))
    assert bot_pi1.send_message.await_count == bot_pi2.send_message.await_count
    bot_pi1.restrict_chat_member.assert_not_awaited()

    # 5. Admin inside GroupAiEditStates.waiting_text DM FSM: /mute does NOT fire moderation handler
    bot_fsm = _make_bot()
    key = StorageKey(bot_id=bot_fsm.id, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    await dp.storage.set_state(key, GroupAiEditStates.waiting_text)
    await dp.storage.set_data(key, {"escalation_id": None})
    await dp.feed_update(
        bot_fsm,
        _make_dm_update(text=f"/mute {REGULAR_USER_ID} 1h", from_user_id=ADMIN_ID, update_id=46),
    )
    bot_fsm.restrict_chat_member.assert_not_awaited()


# ===========================================================================
# 5. Strike-Ladder Isolation, Audit Views & Zero-Emoji Invariant
# ===========================================================================


@pytest.mark.asyncio
async def test_ladder_isolation_and_audit_log_and_admin_console_endpoints(db_session, monkeypatch):
    """Manual mute -> unmute -> ban -> unban must NEVER corrupt automatic strike counting,
    must store zero group text, and must surface in overview, audit-log, and group-ai views."""
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(settings, "group_ai_admin_enabled", settings.group_ai_admin_enabled)
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="true"))
    await db_session.commit()

    # Run full manual cycle on REGULAR_USER_ID
    bot_m = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, status="member", username="targetu"))
    r1 = await gmod.manual_mute(
        bot_m,
        db_session,
        chat_id=GROUP_ID,
        target_id=REGULAR_USER_ID,
        actor_id=ADMIN_ID,
        duration_seconds=3600,
        source="group_cmd",
        reply_message_id=701,
    )
    assert r1.ok

    bot_u = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, status="restricted", username="targetu"))
    r2 = await gmod.manual_unmute(
        bot_u, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID, source="dm_cmd"
    )
    assert r2.ok

    bot_b = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, status="member", username="targetu"))
    r3 = await gmod.manual_ban(
        bot_b,
        db_session,
        chat_id=GROUP_ID,
        target_id=REGULAR_USER_ID,
        actor_id=ADMIN_ID,
        source="group_cmd",
        reply_message_id=702,
    )
    assert r3.ok

    bot_ub = _make_bot(member=_make_member(user_id=REGULAR_USER_ID, status="kicked", username="targetu"))
    r4 = await gmod.manual_unban(
        bot_ub, db_session, chat_id=GROUP_ID, target_id=REGULAR_USER_ID, actor_id=ADMIN_ID, source="dm_cmd"
    )
    assert r4.ok

    # Strike count BEFORE any auto violation MUST STILL BE ZERO
    strikes_before = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes_before) == 0

    # Now trigger a real automatic moderation violation -> MUST be Strike 1 (warn), NOT Strike 2/3!
    secret_group_text = "အကောင့်ရောင်းမည် စျေးသက်သာ UNIQUE_GROUP_BODY_SECRET"
    auto_msg = MagicMock(spec=TgMessage)
    auto_msg.message_id = 801
    auto_msg.text = secret_group_text
    auto_msg.voice = None
    auto_msg.chat = MagicMock(id=GROUP_ID, type=ChatType.SUPERGROUP)
    auto_msg.from_user = TgUser(id=REGULAR_USER_ID, is_bot=False, first_name="Target", username="targetu")
    auto_msg.sender_chat = None
    auto_msg.reply_to_message = None
    auto_msg.reply = AsyncMock()
    auto_msg.bot = _make_bot()

    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    await handle_group_message(auto_msg, state=state, session=db_session, bot=auto_msg.bot)

    auto_msg.reply.assert_awaited_once_with(gai.WARNING_TEXT_STRIKE_1)
    auto_msg.bot.restrict_chat_member.assert_not_awaited()

    strikes_after = await gai.get_user_strikes_in_window(db_session, REGULAR_USER_ID)
    assert len(strikes_after) == 1
    assert strikes_after[0].action == "warn"
    assert strikes_after[0].category == "selling"

    # Verify all 4 AuditLog rows & ZERO group message text stored
    audit_rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action.like("group_mod.%")).order_by(AuditLog.id.asc())
        )
    ).scalars().all()
    assert [r.action for r in audit_rows] == [
        "group_mod.mute",
        "group_mod.unmute",
        "group_mod.ban",
        "group_mod.unban",
    ]
    for r in audit_rows:
        assert r.admin_id == ADMIN_ID
        assert r.target_type == "group_member"
        assert r.target_id == str(REGULAR_USER_ID)
        assert r.created_at is not None
        assert secret_group_text not in (r.details_json or "")
        d = json.loads(r.details_json)
        assert d["chat_id"] == GROUP_ID
        assert "text" not in d and "message_text" not in d
    mute_details = json.loads(audit_rows[0].details_json)
    assert mute_details["duration_seconds"] == 3600
    assert isinstance(mute_details["until_ts"], int)
    assert mute_details["source"] == "group_cmd"
    assert mute_details["reply_message_id"] == 701
    assert mute_details["target_username"] == "targetu"

    # Merged get_recent_moderation_actions returns BOTH auto strike + manual rows newest-first
    merged = await gai.get_recent_moderation_actions(db_session, limit=10)
    assert len(merged) == 5
    actions_seen = {item["action"] for item in merged}
    assert actions_seen == {"warn", "manual_mute", "manual_unmute", "manual_ban", "manual_unban"}
    # Backward-compatible keys present on every entry
    for item in merged:
        for key in ("id", "user_id", "category", "action", "created_at", "banned"):
            assert key in item
    # Ordering check: newest-first ISO timestamps
    timestamps = [item["created_at"] for item in merged]
    assert timestamps == sorted(timestamps, reverse=True)

    # All 3 Admin Console API endpoints surface the manual rows
    from app.webapp.api import (
        get_admin_audit_log,
        get_admin_group_ai_console,
        get_admin_overview,
    )

    admin_user = User(id=ADMIN_ID, username="lead_admin")

    overview = await get_admin_overview(admin=admin_user, db=db_session)
    ov_actions = [a["action"] for a in overview["recent_admin_actions"]]
    assert "group_mod.unban" in ov_actions
    assert "group_mod.mute" in ov_actions

    audit_ep = await get_admin_audit_log(action_prefix="group_mod", limit=20, admin=admin_user, db=db_session)
    assert len(audit_ep) == 4
    assert all(row["action"].startswith("group_mod.") for row in audit_ep)

    console_ep = await get_admin_group_ai_console(admin=admin_user, db=db_session)
    console_actions = {a["action"] for a in console_ep["recent_actions"]}
    assert {"warn", "manual_mute", "manual_ban", "manual_unmute", "manual_unban"} <= console_actions


def test_all_manual_moderation_copy_is_zero_emoji():
    """Standing Rule 3: every user/admin-facing Burmese copy string has zero emoji."""
    for template in gmod.ALL_BURMESE_COPY_TEMPLATES:
        assert gai.strip_emoji(template) == template, f"Emoji found in copy: {template!r}"

    # Formatted examples with emoji inside user's Telegram first_name also strip cleanly
    notice = gmod.GROUP_NOTICE_MUTE.format(
        target_display=gmod.format_target_display("Mg🔥Mg", "mgmg"),
        duration_my=gmod.format_duration_my(5400),
    )
    assert gai.strip_emoji(notice) == notice
    assert "🔥" not in notice


def test_admin_console_static_badges_and_audit_filter():
    """Admin Console UI renders manual moderation badges and exposes group_mod audit filter."""
    app_js = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    for token in ("manual_mute", "manual_unmute", "manual_ban", "manual_unban", "Manual Mute", "Manual Ban"):
        assert token in app_js

    admin_pro_js = (ROOT / "app" / "webapp" / "static" / "js" / "admin_pro.js").read_text(encoding="utf-8")
    assert '<option value="group_mod">group_mod.*</option>' in admin_pro_js
