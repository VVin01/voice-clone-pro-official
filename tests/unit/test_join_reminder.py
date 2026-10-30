"""Join-reminder UX tests (feat-join-reminder-2026-10-30).

The gate itself is unchanged (fail closed, live check); these tests pin
the REMINDER experience a blocked user gets:

(a) channel-only member -> still blocked, reminder names the GROUP as
    the one still missing (and shows the channel as joined);
(b) group-only member -> still blocked, reminder names the CHANNEL;
(c) both joined -> the re-check (verify_user_membership, the same call
    the bot gate:verify button and POST /api/user/verify-membership use)
    passes and the flow may proceed;
(d) Telegram API error during the check -> still blocked (fail closed).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import User
from app.services.gate import (
    build_gate_reminder_text,
    check_telegram_membership,
    gate_missing_chats,
    gate_still_missing_text,
    verify_user_membership,
)
from app.services.i18n import t

CH = "-100111"
GP = "-100222"
CHANNEL_LINK = "https://t.me/VoiceClonePro"
GROUP_LINK = "https://t.me/+2udU2-fb1zNiZmI1"


class _Member:
    def __init__(self, status: str):
        self.status = status


class _PerChatBot:
    """Fake Bot whose get_chat_member answers per chat_id.

    A value may be an Exception instance to simulate an API failure.
    """

    def __init__(self, statuses: dict):
        self.statuses = statuses
        self.calls: list = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append(chat_id)
        value = self.statuses[chat_id]
        if isinstance(value, Exception):
            raise value
        return _Member(value)


def _user(uid: int) -> User:
    return User(
        id=uid,
        first_name="Join",
        account_category=AccountCategory.FREE.value,
        language_code="my",
    )


@pytest.fixture(autouse=True)
def _gate_ids(monkeypatch):
    monkeypatch.setattr(settings, "required_channel_id", CH)
    monkeypatch.setattr(settings, "required_group_id", GP)
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")


async def _verify(db_session, uid: int, bot):
    user = _user(uid)
    db_session.add(user)
    await db_session.commit()
    verified, _reason = await verify_user_membership(db_session, user, bot)
    return verified, user


@pytest.mark.asyncio
async def test_channel_only_member_blocked_and_group_named_missing(db_session):
    bot = _PerChatBot({CH: "member", GP: "left"})
    verified, user = await _verify(db_session, 710001, bot)
    assert verified is False
    assert user.channel_verified is True and user.group_verified is False
    assert gate_missing_chats(user.channel_verified, user.group_verified) == ["group"]

    text_my = build_gate_reminder_text("my", user.channel_verified, user.group_verified)
    ch_line = t("membership_gate_status_channel", "my", status=t("gate_status_joined", "my"))
    gp_line = t("membership_gate_status_group", "my", status=t("gate_status_missing", "my"))
    assert ch_line in text_my and gp_line in text_my
    # The still-missing alert names the group, not the channel.
    alert = gate_still_missing_text("my", user.channel_verified, user.group_verified)
    assert t("gate_name_group", "my") in alert
    assert t("gate_name_channel", "my") not in alert


@pytest.mark.asyncio
async def test_group_only_member_blocked_and_channel_named_missing(db_session):
    bot = _PerChatBot({CH: "left", GP: "member"})
    verified, user = await _verify(db_session, 710002, bot)
    assert verified is False
    assert user.channel_verified is False and user.group_verified is True
    assert gate_missing_chats(user.channel_verified, user.group_verified) == ["channel"]

    text_en = build_gate_reminder_text("en", user.channel_verified, user.group_verified)
    assert "Not joined yet" in text_en  # channel line
    assert "✓ Joined" in text_en  # group line
    alert = gate_still_missing_text("en", user.channel_verified, user.group_verified)
    assert "Channel" in alert and "Group" not in alert


@pytest.mark.asyncio
async def test_both_joined_recheck_passes(db_session):
    bot = _PerChatBot({CH: "member", GP: "administrator"})
    verified, user = await _verify(db_session, 710003, bot)
    assert verified is True
    assert user.channel_verified is True and user.group_verified is True
    assert gate_missing_chats(True, True) == []


@pytest.mark.asyncio
async def test_api_error_during_recheck_stays_blocked(db_session):
    bot = _PerChatBot({CH: RuntimeError("telegram down"), GP: RuntimeError("telegram down")})
    verified, user = await _verify(db_session, 710004, bot)
    assert verified is False
    assert user.channel_verified is False and user.group_verified is False
    # And the low-level check itself fails closed on errors / missing bot.
    assert await check_telegram_membership(bot, CH, 710004) is False
    assert await check_telegram_membership(None, CH, 710004) is False


@pytest.mark.asyncio
async def test_left_after_joining_is_blocked_again(db_session):
    """Re-check is LIVE: flags from an earlier pass are not trusted."""
    bot = _PerChatBot({CH: "left", GP: "member"})
    user = _user(710005)
    user.channel_verified = True  # stale flags from an earlier pass
    user.group_verified = True
    db_session.add(user)
    await db_session.commit()
    verified, _ = await verify_user_membership(db_session, user, bot)
    assert verified is False
    assert user.channel_verified is False and user.group_verified is True


def test_gate_keyboard_has_both_join_buttons_and_recheck():
    from app.ui.keyboards import membership_gate_kb

    kb = membership_gate_kb(channel_link=CHANNEL_LINK, group_link=GROUP_LINK, lang="en")
    rows = kb.inline_keyboard
    assert rows[0][0].url == CHANNEL_LINK
    assert rows[1][0].url == GROUP_LINK
    assert rows[2][0].callback_data == "gate:verify"
    # The re-check button carries the "I've joined — check again" copy.
    assert rows[2][0].text == t("btn_verify_membership", "en")


def test_miniapp_gate_wiring_is_present():
    """Pin the Mini App side: chips in the banner, 403 -> reminder hook."""
    root = Path(__file__).resolve().parents[2]
    html = (root / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="gate-chip-channel"' in html
    assert 'id="gate-chip-group"' in html

    js = (root / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "MEMBERSHIP_REQUIRED" in js  # 403 interceptor present
    assert "showGateReminder" in js
    assert "renderGateChips" in js
    # Verify button handler repaints chips from the live response.
    assert "renderGateChips(data.channel_verified, data.group_verified)" in js
