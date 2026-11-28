"""Unit & handler tests for Group Command Cleanup (v2, consult-corrected, 2026-11-28).

Covers:
1. Pure `foreign_command_kind` classifier table:
   - Targets: `/start`, `/start <payload>`, `/start@voiceclonepro_bot` (case-insensitive) -> `"start"`;
     `/start@VoiceCloneProMM_Bot`, `/help@OtherBot`, multiline foreign commands -> `"foreign_mention"`.
   - Never-targets: `/mute`, `/unmute`, `/ban`, `/unban` (bare or `@voiceclonepro_bot`),
     paths `/storage/emulated/0/x.mp3`, fractions `/10`, comments `// note`, plain text, empty/None.
2. Notice copy invariants: anonymous, female Senior-Admin persona, zero emoji.
3. Dynamic setting plumbing: `group_command_cleanup_enabled` in `SETTING_TYPES` / `CONFIG_SETTING_KEYS`,
   `AdminSettingsPayload` + `get_admin_settings` + `update_admin_settings` round-trip.
4. `handle_group_message` behavior:
   - Foreign command -> deleted + standalone redirect notice + 60s TTL scheduled; zero strikes,
     zero mutes, zero AuditLog rows, zero admin DMs; privacy-safe decision log (no message text).
   - Second command 5 min later -> deleted, NO notice.
   - Command 31 min later -> deleted + notice sent again.
   - Concurrent single-loop race -> optimistic reservation sends only ONE notice while deleting both.
   - `send_message` failure -> throttle stamp rolled back so next command retries the notice.
   - `delete_message` failure -> swallowed cleanly; notice still sent when unthrottled.
   - Kill switch (`group_ai_admin_enabled`) OFF -> nothing deleted, nothing sent.
   - `group_command_cleanup_enabled` OFF -> nothing deleted, nothing sent.
   - `/start@voiceclonepro_bot` -> deleted + notice (`reason=start`).
   - Non-admin `/mute 1h` and `/mute@voiceclonepro_bot` -> nothing deleted, nothing sent.
   - Voice message whose ASR transcript begins `"/start please"` (Site-2 guard) -> NOT deleted.
"""

from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType
from sqlalchemy import select

from app.config import settings
from app.handlers import group_admin as ga_mod
from app.handlers import group_moderation as gm_mod
from app.models.enums import AccountCategory
from app.models.schema import AuditLog, BotSetting, GroupAiModerationStrike, User
from app.services import dynamic_settings as ds_mod
from app.services import group_ai_admin as gai
from app.webapp import api as api_mod

GROUP_ID = gai.COMMUNITY_GROUP_ID
MEMBER_ID = 880777
ADMIN_ID = 424242


@pytest.fixture(autouse=True)
def _reset_cleanup_runtime(monkeypatch):
    ga_mod.reset_command_cleanup_throttle()
    monkeypatch.setattr(gai, "rate_limiter", gai.GroupReplyRateLimiter())
    ds_mod.invalidate_dynamic_settings_cache()
    yield
    ga_mod.reset_command_cleanup_throttle()
    ds_mod.invalidate_dynamic_settings_cache()


def _fake_state():
    st = MagicMock()
    st.get_state = AsyncMock(return_value=None)
    return st


def _fake_bot():
    bot = MagicMock()
    bot.default = SimpleNamespace(parse_mode="HTML")
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9501))
    bot.restrict_chat_member = AsyncMock(return_value=True)
    bot.ban_chat_member = AsyncMock(return_value=True)
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="voice/note.ogg"))
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"OggS-fake-voice-bytes"))
    return bot


def _fake_group_message(
    text: str | None = None,
    *,
    user_id: int = MEMBER_ID,
    chat_id: int = GROUP_ID,
    message_id: int = 701,
    bot=None,
    voice=None,
):
    b = bot or _fake_bot()
    msg = MagicMock()
    msg.message_id = message_id
    msg.text = text
    msg.voice = voice
    msg.chat = SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP)
    msg.from_user = SimpleNamespace(
        id=user_id,
        is_bot=False,
        first_name="Mg Kyaw",
        username="mgkyaw",
    )
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.bot = b
    return msg


async def _enable_group_ai(db_session, *, cleanup_enabled: bool = True) -> None:
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="true"))
    db_session.add(
        BotSetting(
            key=gai.COMMAND_CLEANUP_KEY,
            value="true" if cleanup_enabled else "false",
        )
    )
    await db_session.commit()
    ds_mod.invalidate_dynamic_settings_cache()


# ---------------------------------------------------------------------------
# 1 & 2. Pure Classifier Table & Notice Copy Invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Targets -> "start"
        ("/start", "start"),
        ("  /start  ", "start"),
        ("/start ref_abc123", "start"),
        ("/START payload", "start"),
        ("/start@voiceclonepro_bot", "start"),
        ("/START@VoiceClonePro_Bot deep_link", "start"),
        # Targets -> "foreign_mention"
        ("/start@VoiceCloneProMM_Bot", "foreign_mention"),
        ("/start@VoiceCloneProMM_Bot payload", "foreign_mention"),
        ("/help@OtherBot", "foreign_mention"),
        ("/mute@OtherAdminBot 1h", "foreign_mention"),
        ("/settings@some_bot\nextra line", "foreign_mention"),
        # Never-targets -> None (moderation commands, paths, numbers, notes, plain text)
        ("/mute", None),
        ("/mute 1h", None),
        ("/unmute", None),
        ("/ban", None),
        ("/unban 777999", None),
        ("/mute@voiceclonepro_bot", None),
        ("/mute@VOICECLONEPRO_BOT 15m", None),
        ("/ban@voiceclonepro_bot", None),
        ("/unmute@voiceclonepro_bot", None),
        ("/unban@voiceclonepro_bot 12345", None),
        ("/help", None),
        ("/help@voiceclonepro_bot", None),
        ("/storage/emulated/0/x.mp3", None),
        ("/etc/hosts", None),
        ("/10", None),
        ("/2026/11/28", None),
        ("// note", None),
        ("/ 5", None),
        ("/", None),
        ("", None),
        ("   ", None),
        (None, None),
        ("မင်္ဂလာပါ @OtherBot /start မေးချင်လို့ပါ", None),
    ],
)
def test_foreign_command_kind_table(text, expected):
    assert gai.foreign_command_kind(text) == expected


def test_foreign_command_kind_custom_bot_username_with_leading_at():
    assert gai.foreign_command_kind("/start@MyCustom_Bot", bot_username="@mycustom_bot") == "start"
    assert gai.foreign_command_kind("/ping@MyCustom_Bot", bot_username="@mycustom_bot") is None
    assert (
        gai.foreign_command_kind("/ping@Foreign_Bot", bot_username="@mycustom_bot")
        == "foreign_mention"
    )


def test_cleanup_notice_copy_is_anonymous_zero_emoji_and_female_persona():
    txt = gai.COMMAND_CLEANUP_NOTICE_TEXT
    assert gai.strip_emoji(txt) == txt
    assert "@voiceclonepro_bot" in txt
    assert "Open (Mini App)" in txt
    assert "ရှင့်" in txt
    assert "ခင်ဗျာ" not in txt
    assert "{" not in txt and "}" not in txt  # strictly anonymous (no format placeholders)


# ---------------------------------------------------------------------------
# 3. Dynamic Setting & Admin API Plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_plumbing_allowlist_and_admin_api_roundtrip(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    orig_attr = getattr(settings, "group_command_cleanup_enabled", True)
    try:
        assert gai.COMMAND_CLEANUP_KEY in ds_mod.SETTING_TYPES
        assert ds_mod.SETTING_TYPES[gai.COMMAND_CLEANUP_KEY] is bool
        assert gai.COMMAND_CLEANUP_KEY in ds_mod.DYNAMIC_SETTING_KEYS
        assert gai.COMMAND_CLEANUP_KEY in ds_mod.CONFIG_SETTING_KEYS

        admin = User(
            id=ADMIN_ID,
            first_name="LeadAdmin",
            account_category=AccountCategory.PURCHASED.value,
        )
        db_session.add(admin)
        await db_session.commit()

        # Default True
        assert await gai.is_command_cleanup_enabled(db_session) is True
        initial_cfg = await api_mod.get_admin_settings(admin=admin, db=db_session)
        assert initial_cfg["group_command_cleanup_enabled"] is True

        # Toggle False via AdminSettingsPayload -> persisted in BotSetting & reloaded
        await api_mod.update_admin_settings(
            api_mod.AdminSettingsPayload(group_command_cleanup_enabled=False),
            admin=admin,
            db=db_session,
        )
        ds_mod.invalidate_dynamic_settings_cache()
        assert await gai.is_command_cleanup_enabled(db_session) is False
        after_cfg = await api_mod.get_admin_settings(admin=admin, db=db_session)
        assert after_cfg["group_command_cleanup_enabled"] is False
    finally:
        settings.group_command_cleanup_enabled = orig_attr
        ds_mod.invalidate_dynamic_settings_cache()


# ---------------------------------------------------------------------------
# 4. Handler Behavior, Throttle, Rollback, Gates & Site-2 Voice Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_deletes_always_throttles_notice_30m_and_logs_without_text(
    db_session, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    await _enable_group_ai(db_session, cleanup_enabled=True)

    clock = {"t": 10_000.0}
    monkeypatch.setattr(ga_mod, "_monotonic", lambda: clock["t"])

    scheduled_deletes: list[tuple] = []

    def _spy_schedule(bot, chat_id, message_id, delay_seconds=60):
        scheduled_deletes.append((chat_id, message_id, delay_seconds))
        return None

    monkeypatch.setattr(gm_mod, "_schedule_message_deletion", _spy_schedule)

    bot = _fake_bot()
    secret_cmd = "/start@VoiceCloneProMM_Bot SECRET_PAYLOAD_999"

    # 1st foreign command at t=10_000 -> deleted + standalone notice + 60s TTL scheduled
    m1 = _fake_group_message(secret_cmd, message_id=801, bot=bot)
    with caplog.at_level(logging.INFO, logger="app.handlers.group_admin"):
        await ga_mod.handle_group_message(m1, state=_fake_state(), session=db_session, bot=bot)

    bot.delete_message.assert_awaited_once_with(chat_id=GROUP_ID, message_id=801)
    bot.send_message.assert_awaited_once_with(
        GROUP_ID,
        gai.COMMAND_CLEANUP_NOTICE_TEXT,
        parse_mode=None,
    )
    m1.reply.assert_not_awaited()  # NEVER reply to deleted message
    assert scheduled_deletes == [(GROUP_ID, 9501, gm_mod.GROUP_NOTICE_TTL_SECONDS)]
    assert "decision=command_cleanup" in caplog.text
    assert "reason=foreign_mention" in caplog.text
    assert "SECRET_PAYLOAD_999" not in caplog.text
    assert secret_cmd not in caplog.text

    # Not an offense: zero strikes, zero mutes, zero AuditLog rows, zero admin DMs
    bot.restrict_chat_member.assert_not_awaited()
    bot.ban_chat_member.assert_not_awaited()
    strikes = (await db_session.execute(select(GroupAiModerationStrike))).scalars().all()
    audits = (await db_session.execute(select(AuditLog))).scalars().all()
    assert strikes == []
    assert audits == []

    # 2nd command 5 min later (t + 300s) -> deleted, NO notice
    clock["t"] += 300.0
    m2 = _fake_group_message("/start@voiceclonepro_bot", user_id=880778, message_id=802, bot=bot)
    await ga_mod.handle_group_message(m2, state=_fake_state(), session=db_session, bot=bot)
    assert bot.delete_message.await_count == 2
    bot.delete_message.assert_awaited_with(chat_id=GROUP_ID, message_id=802)
    assert bot.send_message.await_count == 1  # throttled!
    assert len(scheduled_deletes) == 1

    # 3rd command 31 min after 1st notice (t + 1860s total) -> deleted + notice sent again
    clock["t"] = 10_000.0 + 1860.0
    m3 = _fake_group_message("/start", user_id=880779, message_id=803, bot=bot)
    await ga_mod.handle_group_message(m3, state=_fake_state(), session=db_session, bot=bot)
    assert bot.delete_message.await_count == 3
    assert bot.send_message.await_count == 2
    assert len(scheduled_deletes) == 2


@pytest.mark.asyncio
async def test_handler_send_failure_rolls_back_throttle_and_concurrent_burst_sends_once(
    db_session, monkeypatch
):
    await _enable_group_ai(db_session, cleanup_enabled=True)
    monkeypatch.setattr(gm_mod, "_schedule_message_deletion", lambda *a, **kw: None)

    clock = {"t": 50_000.0}
    monkeypatch.setattr(ga_mod, "_monotonic", lambda: clock["t"])

    # Part A: send_message raises on 1st command -> stamp rolled back -> 2nd command 5s later retries
    bot_fail = _fake_bot()
    bot_fail.send_message = AsyncMock(
        side_effect=[RuntimeError("Telegram transient 502"), SimpleNamespace(message_id=9602)]
    )

    mf1 = _fake_group_message("/start@OtherBot", message_id=810, bot=bot_fail)
    await ga_mod.handle_group_message(mf1, state=_fake_state(), session=db_session, bot=bot_fail)
    assert bot_fail.delete_message.await_count == 1
    assert bot_fail.send_message.await_count == 1

    clock["t"] += 5.0  # Only 5s later (< 1800s), must still retry because previous send failed
    mf2 = _fake_group_message("/start", message_id=811, bot=bot_fail)
    await ga_mod.handle_group_message(mf2, state=_fake_state(), session=db_session, bot=bot_fail)
    assert bot_fail.delete_message.await_count == 2
    assert bot_fail.send_message.await_count == 2

    # Part B: Concurrent burst in single event loop -> optimistic stamp prevents duplicate notice
    ga_mod.reset_command_cleanup_throttle()
    bot_race = _fake_bot()

    async def _slow_send(*args, **kwargs):
        await asyncio.sleep(0.02)
        return SimpleNamespace(message_id=9700)

    bot_race.send_message = AsyncMock(side_effect=_slow_send)
    mr1 = _fake_group_message("/start", user_id=901, message_id=820, bot=bot_race)
    mr2 = _fake_group_message("/start@ForeignBot", user_id=902, message_id=821, bot=bot_race)
    await asyncio.gather(
        ga_mod.handle_group_message(mr1, state=_fake_state(), session=db_session, bot=bot_race),
        ga_mod.handle_group_message(mr2, state=_fake_state(), session=db_session, bot=bot_race),
    )
    assert bot_race.delete_message.await_count == 2
    assert bot_race.send_message.await_count == 1


@pytest.mark.asyncio
async def test_gates_own_start_non_admin_mute_and_voice_site2_guard(db_session, monkeypatch):
    monkeypatch.setattr(gm_mod, "_schedule_message_deletion", lambda *a, **kw: None)

    # 1. Kill switch OFF -> nothing deleted, nothing sent
    db_session.add(BotSetting(key=gai.KILL_SWITCH_KEY, value="false"))
    db_session.add(BotSetting(key=gai.COMMAND_CLEANUP_KEY, value="true"))
    await db_session.commit()
    ds_mod.invalidate_dynamic_settings_cache()

    bot_ks_off = _fake_bot()
    await ga_mod.handle_group_message(
        _fake_group_message("/start@OtherBot", message_id=830, bot=bot_ks_off),
        state=_fake_state(),
        session=db_session,
        bot=bot_ks_off,
    )
    bot_ks_off.delete_message.assert_not_awaited()
    bot_ks_off.send_message.assert_not_awaited()

    # 2. Kill switch ON, group_command_cleanup_enabled OFF -> nothing deleted, nothing sent
    await ds_mod.set_dynamic_setting(db_session, gai.KILL_SWITCH_KEY, True, admin_id=ADMIN_ID)
    await ds_mod.set_dynamic_setting(db_session, gai.COMMAND_CLEANUP_KEY, False, admin_id=ADMIN_ID)
    await db_session.commit()

    bot_cc_off = _fake_bot()
    await ga_mod.handle_group_message(
        _fake_group_message("/start@voiceclonepro_bot", message_id=831, bot=bot_cc_off),
        state=_fake_state(),
        session=db_session,
        bot=bot_cc_off,
    )
    bot_cc_off.delete_message.assert_not_awaited()
    bot_cc_off.send_message.assert_not_awaited()

    # 3. Enable both -> `/start@voiceclonepro_bot` IS deleted + noticed
    await ds_mod.set_dynamic_setting(db_session, gai.COMMAND_CLEANUP_KEY, True, admin_id=ADMIN_ID)
    await db_session.commit()

    bot_own_start = _fake_bot()
    await ga_mod.handle_group_message(
        _fake_group_message("/start@voiceclonepro_bot", message_id=832, bot=bot_own_start),
        state=_fake_state(),
        session=db_session,
        bot=bot_own_start,
    )
    bot_own_start.delete_message.assert_awaited_once_with(chat_id=GROUP_ID, message_id=832)
    bot_own_start.send_message.assert_awaited_once()

    # 4. Non-admin `/mute 1h` and `/mute@voiceclonepro_bot` -> total silence (nothing deleted/sent)
    ga_mod.reset_command_cleanup_throttle()
    for mid, cmd in ((833, "/mute 1h"), (834, "/mute@voiceclonepro_bot")):
        bot_mute = _fake_bot()
        await ga_mod.handle_group_message(
            _fake_group_message(cmd, message_id=mid, bot=bot_mute),
            state=_fake_state(),
            session=db_session,
            bot=bot_mute,
        )
        bot_mute.delete_message.assert_not_awaited()
        bot_mute.send_message.assert_not_awaited()

    # 5. Voice note whose ASR transcript is "/start please" (Site 2) -> NEVER deleted!
    ga_mod.reset_command_cleanup_throttle()
    monkeypatch.setattr(gai, "transcribe_voice_gemini", AsyncMock(return_value="/start please"))
    bot_voice = _fake_bot()
    voice_obj = SimpleNamespace(
        file_id="voice_file_1",
        duration=4,
        file_size=8000,
        mime_type="audio/ogg",
    )
    msg_voice = _fake_group_message(
        text=None,
        message_id=835,
        bot=bot_voice,
        voice=voice_obj,
    )
    await ga_mod.handle_group_message(
        msg_voice,
        state=_fake_state(),
        session=db_session,
        bot=bot_voice,
    )
    bot_voice.get_file.assert_awaited_once_with("voice_file_1")
    bot_voice.delete_message.assert_not_awaited()
    bot_voice.send_message.assert_not_awaited()

    # Source pin: _cleanup_foreign_group_command is invoked at Site 1 ONLY (never Site 2 ASR)
    import inspect

    body = inspect.getsource(ga_mod.handle_group_message)
    assert body.count("_cleanup_foreign_group_command(") == 1
