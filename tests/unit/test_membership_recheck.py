"""Bot-side periodic membership re-check (join-then-leave hole).

A user who joined the channel + group once got channel_verified /
group_verified and was never re-checked in the bot after /start, so
leaving both chats kept access forever. MembershipRecheckMiddleware
re-probes Telegram for already-verified users at most once per
MEMBERSHIP_RECHECK_TTL_SECONDS. Pinned here:

(a) a verified member who LEFT is blocked: flags reset, handler not
    run, join reminder (both join buttons + gate:verify) shown;
(b) a member still in both chats is unaffected, and within the TTL the
    re-check is cached (no Telegram call on every callback);
(c) a Telegram API error does NOT lock out a previously verified user
    (fail safe on this cache path), while verify_user_membership / the
    API gate stay fail closed for the very same error.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser

from app.config import settings
from app.middlewares import MembershipRecheckMiddleware, setup_middlewares
from app.models.enums import AccountCategory
from app.models.schema import User
from app.services import gate
from app.services.gate import (
    probe_telegram_membership,
    recheck_verified_membership,
    verify_user_membership,
)
from app.services.i18n import t

CH = "-100111"
GP = "-100222"
TTL = 3 * 3600


class _Member:
    def __init__(self, status: str, is_member: bool | None = None):
        self.status = status
        if is_member is not None:
            self.is_member = is_member


class _PerChatBot:
    """Fake Bot: get_chat_member answers per chat_id (Exception = API error)."""

    def __init__(self, statuses: dict):
        self.statuses = statuses
        self.calls: list = []
        self.send_message = AsyncMock()

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(chat_id)
        value = self.statuses[chat_id]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, _Member):
            return value
        return _Member(value)


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch):
    monkeypatch.setattr(settings, "required_channel_id", CH)
    monkeypatch.setattr(settings, "required_group_id", GP)
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")
    monkeypatch.setattr(settings, "membership_recheck_ttl_seconds", TTL)
    monkeypatch.setattr(settings, "membership_recheck_error_retry_seconds", 600)
    clock = _Clock()
    monkeypatch.setattr(gate, "_now", clock)
    gate.reset_membership_cache()
    yield clock
    gate.reset_membership_cache()


def _session_maker(db_session):
    sm = MagicMock()
    sm.return_value.__aenter__ = AsyncMock(return_value=db_session)
    sm.return_value.__aexit__ = AsyncMock(return_value=None)
    return sm


async def _verified_user(db_session, uid: int, lang: str = "en") -> User:
    user = User(
        id=uid,
        first_name="Member",
        account_category=AccountCategory.FREE.value,
        language_code=lang,
        channel_verified=True,
        group_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


def _callback(uid: int, data: str = "menu:history") -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=uid, is_bot=False, first_name="Member")
    cb.data = data
    cb.message = MagicMock()
    cb.message.chat = Chat(id=uid, type="private")
    cb.answer = AsyncMock()
    return cb


def _message(uid: int, text: str = "hello", chat_type: str = "private") -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=uid, is_bot=False, first_name="Member")
    msg.chat = Chat(id=uid, type=chat_type)
    msg.text = text
    msg.answer = AsyncMock()
    return msg


# ── (a) member who left gets blocked + reminder ──────────────────────────


@pytest.mark.asyncio
async def test_member_who_left_is_blocked_with_reminder_on_callback(db_session):
    uid = 720001
    user = await _verified_user(db_session, uid, lang="en")
    bot = _PerChatBot({CH: "left", GP: "left"})
    state = AsyncMock()
    mw = MembershipRecheckMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")
    cb = _callback(uid)

    result = await mw(handler, cb, {"bot": bot, "state": state})

    assert result is None
    handler.assert_not_called()
    await db_session.refresh(user)
    assert user.channel_verified is False and user.group_verified is False
    state.clear.assert_awaited_once()

    # Join reminder: per-chat status text + both join URLs + re-check button.
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == uid
    assert kwargs["text"] == gate.build_gate_reminder_text("en", False, False)
    rows = kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].url == settings.channel_invite_link
    assert rows[1][0].url == settings.group_invite_link
    assert rows[2][0].callback_data == "gate:verify"
    cb.answer.assert_awaited_once()
    assert t("gate_name_channel", "en") in cb.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_member_who_left_one_chat_is_blocked_on_message(db_session):
    uid = 720002
    user = await _verified_user(db_session, uid, lang="my")
    bot = _PerChatBot({CH: "member", GP: "kicked"})
    mw = MembershipRecheckMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")
    msg = _message(uid, "some text in an FSM step")

    result = await mw(handler, msg, {"bot": bot})

    assert result is None
    handler.assert_not_called()
    await db_session.refresh(user)
    assert user.channel_verified is True and user.group_verified is False
    msg.answer.assert_awaited_once()
    assert msg.answer.await_args.args[0] == gate.build_gate_reminder_text("my", True, False)
    assert msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[2][0].callback_data == "gate:verify"

    # Re-joining and pressing the re-check button restores access via the
    # existing live gate (verify_user_membership), which also refreshes
    # the cache so the next tap passes without another probe.
    bot.statuses[GP] = "member"
    ok, _ = await verify_user_membership(db_session, user, bot)
    assert ok is True
    bot.calls.clear()
    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"
    assert bot.calls == []


@pytest.mark.asyncio
async def test_left_while_restricted_counts_as_not_member(db_session):
    bot = _PerChatBot({CH: _Member("restricted", is_member=False), GP: _Member("restricted", is_member=True)})
    assert await probe_telegram_membership(bot, CH, 1) is False
    assert await probe_telegram_membership(bot, GP, 1) is True


@pytest.mark.asyncio
async def test_user_not_found_bad_request_is_definitive_not_member(db_session):
    uid = 720003
    user = await _verified_user(db_session, uid)
    err = TelegramBadRequest(method=GetChatMember(chat_id=CH, user_id=uid), message="Bad Request: user not found")
    bot = _PerChatBot({CH: err, GP: "member"})
    assert await recheck_verified_membership(db_session, user, bot) is False
    assert user.channel_verified is False and user.group_verified is True


# ── (b) member still joined is unaffected; cached within the TTL ─────────


@pytest.mark.asyncio
async def test_member_still_joined_is_unaffected_and_cached(db_session, _gate_env):
    clock = _gate_env
    uid = 720010
    user = await _verified_user(db_session, uid)
    bot = _PerChatBot({CH: "member", GP: "administrator"})
    mw = MembershipRecheckMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")

    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"
    assert bot.calls == [CH, GP]  # first interaction: one live probe

    # Many callbacks within the TTL: no further Telegram calls.
    for i in range(5):
        clock.now += 600
        assert await mw(handler, _callback(uid, f"menu:item{i}"), {"bot": bot}) == "OK"
    assert bot.calls == [CH, GP]
    assert handler.await_count == 6
    bot.send_message.assert_not_awaited()

    # After the TTL expires: exactly one more probe, still allowed.
    clock.now += TTL
    assert await mw(handler, _message(uid), {"bot": bot}) == "OK"
    assert bot.calls == [CH, GP, CH, GP]
    await db_session.refresh(user)
    assert user.channel_verified is True and user.group_verified is True


@pytest.mark.asyncio
async def test_left_user_is_caught_once_ttl_expires(db_session, _gate_env):
    clock = _gate_env
    uid = 720011
    user = await _verified_user(db_session, uid)
    bot = _PerChatBot({CH: "member", GP: "member"})
    mw = MembershipRecheckMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")
    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"

    bot.statuses = {CH: "left", GP: "left"}  # leaves both chats
    clock.now += TTL - 1
    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"  # still cached
    clock.now += 2
    assert await mw(handler, _callback(uid), {"bot": bot}) is None  # re-check -> blocked
    await db_session.refresh(user)
    assert user.channel_verified is False and user.group_verified is False


@pytest.mark.asyncio
async def test_unaffected_paths_admin_unverified_exempt_group_unconfigured(db_session, monkeypatch):
    bot = _PerChatBot({CH: "left", GP: "left"})
    mw = MembershipRecheckMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")

    # Admin: never probed.
    assert await mw(handler, _callback(999001), {"bot": bot}) == "OK"
    # Unknown user (registration via /start) passes.
    assert await mw(handler, _callback(720020), {"bot": bot}) == "OK"
    # User whose flags are not set: left to the existing live gate points.
    db_session.add(User(id=720021, first_name="New", account_category=AccountCategory.FREE.value))
    await db_session.commit()
    assert await mw(handler, _callback(720021), {"bot": bot}) == "OK"
    assert bot.calls == []

    await _verified_user(db_session, 720022)
    # /start, gate:verify and set_lang run the live gate themselves.
    assert await mw(handler, _message(720022, "/start ref123"), {"bot": bot}) == "OK"
    assert await mw(handler, _callback(720022, "gate:verify"), {"bot": bot}) == "OK"
    assert await mw(handler, _callback(720022, "set_lang:en"), {"bot": bot}) == "OK"
    # Messages in a group / supergroup (e.g. the required group) are not gated.
    assert await mw(handler, _message(720022, "hi all", chat_type="supergroup"), {"bot": bot}) == "OK"
    assert bot.calls == []

    # Gate not configured: nothing to re-check against.
    monkeypatch.setattr(settings, "required_group_id", "")
    assert await mw(handler, _callback(720022), {"bot": bot}) == "OK"
    assert bot.calls == []


# ── (c) Telegram API error never locks out a verified user (fail safe) ───


@pytest.mark.asyncio
async def test_api_error_does_not_lock_out_verified_user(db_session, _gate_env):
    clock = _gate_env
    uid = 720030
    user = await _verified_user(db_session, uid)
    bot = _PerChatBot({CH: RuntimeError("telegram down"), GP: RuntimeError("telegram down")})
    mw = MembershipRecheckMiddleware(session_factory=_session_maker(db_session))
    handler = AsyncMock(return_value="OK")

    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"
    await db_session.refresh(user)
    assert user.channel_verified is True and user.group_verified is True  # flags untouched
    bot.send_message.assert_not_awaited()

    # Retried after the short backoff, not on every callback.
    calls_after_first = list(bot.calls)
    clock.now += 60
    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"
    assert bot.calls == calls_after_first
    clock.now += 600
    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"
    assert len(bot.calls) == 2 * len(calls_after_first)

    # Partial outage (one chat errors, the other confirms membership): allowed.
    bot.statuses = {CH: "member", GP: RuntimeError("timeout")}
    clock.now += 600
    assert await mw(handler, _callback(uid), {"bot": bot}) == "OK"
    # Missing bot object is also "unknown" on this path.
    clock.now += 600
    assert await recheck_verified_membership(db_session, user, None) is True


@pytest.mark.asyncio
async def test_api_gate_stays_fail_closed_on_same_error(db_session):
    """The live gate (bot entry points + POST /api/user/verify-membership)
    is unchanged: the same API error that the cache path tolerates still
    fails verify_user_membership closed."""
    uid = 720031
    user = await _verified_user(db_session, uid)
    bot = _PerChatBot({CH: RuntimeError("telegram down"), GP: RuntimeError("telegram down")})
    ok, reason = await verify_user_membership(db_session, user, bot)
    assert ok is False and reason == "channel_or_group_missing"
    assert user.channel_verified is False and user.group_verified is False
    assert await gate.check_telegram_membership(bot, CH, uid) is False
    assert await gate.check_telegram_membership(None, CH, uid) is False


def test_recheck_middleware_registered_after_access_control():
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from app.middlewares import AccessControlMiddleware

    dp = Dispatcher(storage=MemoryStorage())
    setup_middlewares(dp)
    setup_middlewares(dp)  # idempotent
    for observer in (dp.message, dp.callback_query):
        mws = list(observer.outer_middleware)
        kinds = [type(m) for m in mws]
        assert kinds.count(AccessControlMiddleware) == 1
        assert kinds.count(MembershipRecheckMiddleware) == 1
        assert kinds.index(AccessControlMiddleware) < kinds.index(MembershipRecheckMiddleware)


def test_default_ttl_is_a_few_hours():
    from app.config.settings import Settings

    default = Settings.model_fields["membership_recheck_ttl_seconds"].default
    assert 3600 <= default <= 6 * 3600
