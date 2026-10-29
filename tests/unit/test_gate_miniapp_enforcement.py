"""Gate enforcement regression tests (branch gate-env-fix-2026-10-29).

Covers the 2026-10-29 bypass: Mini App endpoints never checked channel/
group membership, and on live main the gate itself failed open. These
tests do not touch the network: membership is exercised with a fake Bot
whose get_chat_member returns a canned status.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import User
from app.services.gate import (
    check_telegram_membership,
    is_gate_configured,
    verify_user_membership,
)


class _Member:
    def __init__(self, status: str):
        self.status = status


class _FakeBot:
    def __init__(self, status: str = "member"):
        self.status = status
        self.calls: list[tuple[object, int]] = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        return _Member(self.status)


def _user(uid: int) -> User:
    return User(
        id=uid,
        first_name="Gate",
        account_category=AccountCategory.FREE.value,
        language_code="my",
    )


def test_is_gate_configured_requires_both_ids(monkeypatch):
    monkeypatch.setattr(settings, "required_channel_id", "-100111")
    monkeypatch.setattr(settings, "required_group_id", "")
    assert is_gate_configured() is False
    monkeypatch.setattr(settings, "required_group_id", "-100222")
    assert is_gate_configured() is True


@pytest.mark.asyncio
async def test_check_membership_fail_closed_without_bot_or_chat():
    assert await check_telegram_membership(None, "-100111", 1) is False
    assert await check_telegram_membership(_FakeBot(), "", 1) is False


@pytest.mark.asyncio
async def test_verify_member_passes_and_sets_flags(db_session):
    settings.required_channel_id = "-100111"
    settings.required_group_id = "-100222"
    settings.admin_ids_raw = "999001"
    user = _user(700001)
    db_session.add(user)
    await db_session.commit()
    verified, reason = await verify_user_membership(db_session, user, _FakeBot("member"))
    assert verified is True
    assert reason == "verified"
    assert user.channel_verified is True and user.group_verified is True


@pytest.mark.asyncio
async def test_verify_non_member_fails_without_bonus(db_session):
    settings.required_channel_id = "-100111"
    settings.required_group_id = "-100222"
    settings.admin_ids_raw = "999001"
    user = _user(700002)
    db_session.add(user)
    await db_session.commit()
    verified, reason = await verify_user_membership(db_session, user, _FakeBot("left"))
    assert verified is False
    assert reason == "channel_or_group_missing"
    assert user.channel_verified is False and user.group_verified is False


@pytest.mark.asyncio
async def test_verify_admin_bypasses_without_joining(db_session):
    # Admins must never be locked out of /start or gated Mini App writes
    # just because they have not joined the channel/group themselves.
    settings.required_channel_id = "-100111"
    settings.required_group_id = "-100222"
    settings.admin_ids_raw = "700003"
    user = _user(700003)
    db_session.add(user)
    await db_session.commit()
    verified, reason = await verify_user_membership(db_session, user, _FakeBot("left"))
    assert verified is True
    assert reason == "admin_bypass"


# The Mini App bypass was "no endpoint checks at all", so lock the wiring
# itself: every write endpoint below must declare require_gate_verified.
GATED_ENDPOINTS = {
    "create_voice_job",
    "create_voice_job_with_audio",
    "extract_media_info",
    "upload_recap_video",
    "create_recap_job",
    "send_recap_to_telegram",
    "send_voice_to_telegram",
    "submit_payment_slip",
}


def test_miniapp_write_endpoints_require_gate():
    api_path = Path(__file__).resolve().parents[2] / "app" / "webapp" / "api.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    by_name = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    missing = [name for name in sorted(GATED_ENDPOINTS) if "require_gate_verified" not in by_name.get(name, "")]
    assert not missing, f"endpoints missing gate dependency: {missing}"
    # Reads that must stay ungated so a blocked user can see why:
    # /api/user/me and /api/user/verify-membership must NOT raise 403.
    assert "verify_membership_endpoint" in by_name
