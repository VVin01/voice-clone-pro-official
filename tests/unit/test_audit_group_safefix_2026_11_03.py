"""Audit fix G (2026-11-03): group/bot safe-fixes.

Covers four findings from the read-only group/key audit at 5cc57bf:

* /start, /language, /help, /history and /samples ran in ANY chat —
  /history posted the caller's balance, jobs and ledger overview into
  the group. The five command handlers are now private-only via
  F.chat.type filters (the payment.py pattern).
* AccessControlMiddleware answered EVERY message from a BLOCKED user
  with a public suspension notice, in any chat, with no cooldown — in
  the community group a banned user could weaponise that into a flood
  and the ban was publicly disclosed. Group messages are now dropped
  silently; private chats keep the notice.
* Groq (transcriber.py, whisper_service.py) and HF (voxcpm_engine.py)
  logs printed first-7/last-4 key fragments. They now log only the
  non-reversible key_rotation.key_id digest.
* Groq cooldowns triggered on HTTP 429 only, so a revoked/invalid key
  (401/403) stayed in rotation forever. Both Groq implementations now
  mark_failure on 401/403 too, mirroring the HF triggers of audit fix E.

All tests run offline with synthetic keys and faked HTTP.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests
from aiogram.types import Chat, Message, User as TgUser

from app.services import key_rotation

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_groq_health():
    key_rotation.health_for("groq").reset()
    yield
    key_rotation.health_for("groq").reset()


# ---------------------------------------------------------------------------
# 1. Private-only command handlers (filter level)
# ---------------------------------------------------------------------------

_COMMAND_HANDLERS = {
    "handle_start": "/start",
    "handle_language_command": "/language",
    "handle_help_command": "/help",
    "handle_history_command": "/history",
    "handle_samples_command": "/samples",
}


def _command_handler_objects():
    from app.handlers.menu import menu_router
    from app.handlers.start import start_router

    found = {}
    for router in (start_router, menu_router):
        for handler in router.message.handlers:
            name = handler.callback.__name__
            if name in _COMMAND_HANDLERS:
                found[name] = handler
    return found


def _make_message(text: str, chat_type: str) -> Message:
    chat_id = 424242 if chat_type == "private" else -1001234567890
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=TgUser(id=424242, is_bot=False, first_name="Test"),
        text=text,
    )


def test_all_five_command_handlers_are_registered():
    assert set(_command_handler_objects()) == set(_COMMAND_HANDLERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", sorted(_COMMAND_HANDLERS))
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_command_handlers_do_not_resolve_in_groups(handler_name, chat_type):
    """The private flow must not even resolve for a group message."""
    handler = _command_handler_objects()[handler_name]
    bot = AsyncMock()
    bot.username = "test_bot"
    message = _make_message(_COMMAND_HANDLERS[handler_name], chat_type)
    resolved, _ = await handler.check(message, bot=bot)
    assert resolved is False


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", sorted(_COMMAND_HANDLERS))
async def test_command_handlers_still_resolve_in_private(handler_name):
    """Positive control: the same commands keep working in private chats."""
    handler = _command_handler_objects()[handler_name]
    bot = AsyncMock()
    bot.username = "test_bot"
    message = _make_message(_COMMAND_HANDLERS[handler_name], "private")
    resolved, _ = await handler.check(message, bot=bot)
    assert resolved is True


# ---------------------------------------------------------------------------
# 2. Banned user: silent drop in groups, notice kept in private
# ---------------------------------------------------------------------------


async def _blocked_user_middleware(db_session, blocked_uid: int):
    from app.config import settings
    from app.middlewares import AccessControlMiddleware
    from app.models.enums import AccountCategory, UserStatus
    from app.models.schema import User

    settings.admin_ids_raw = "999001"
    db_session.add(
        User(
            id=blocked_uid,
            username="blocked_g",
            first_name="Blocked G",
            language_code="en",
            account_category=AccountCategory.FREE.value,
            status=UserStatus.BLOCKED.value,
        )
    )
    await db_session.commit()

    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_maker.return_value.__aexit__ = AsyncMock(return_value=None)
    return AccessControlMiddleware(session_factory=session_maker)


def _blocked_message(blocked_uid: int, chat_type: str) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=blocked_uid, is_bot=False, first_name="Blocked G")
    chat_id = blocked_uid if chat_type == "private" else -1001234567890
    msg.chat = Chat(id=chat_id, type=chat_type)
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_blocked_user_group_message_dropped_silently(db_session, chat_type):
    blocked_uid = 555101
    middleware = await _blocked_user_middleware(db_session, blocked_uid)
    handler = AsyncMock(return_value="OK")
    msg = _blocked_message(blocked_uid, chat_type)

    result = await middleware(handler, msg, {})

    assert result is None
    handler.assert_not_called()
    msg.answer.assert_not_called()  # no public suspension notice in the group


@pytest.mark.asyncio
async def test_blocked_user_private_message_still_gets_notice(db_session):
    blocked_uid = 555102
    middleware = await _blocked_user_middleware(db_session, blocked_uid)
    handler = AsyncMock(return_value="OK")
    msg = _blocked_message(blocked_uid, "private")

    result = await middleware(handler, msg, {})

    assert result is None
    handler.assert_not_called()
    msg.answer.assert_called_once()
    assert "suspended" in msg.answer.call_args.args[0]


# ---------------------------------------------------------------------------
# 3+4. Groq: 401/403 cooldown in both implementations + digest-only logging
# ---------------------------------------------------------------------------

_GROQ_KEYS = ["gsk-aaaaaaaaaaaa1", "gsk-bbbbbbbbbbbb2", "gsk-cccccccccccc3"]


class _FakeGroqResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "fake groq response"

    def json(self):
        return self._payload


def _patch_groq(monkeypatch, behaviour: dict, attempts: list):
    """behaviour: key -> HTTP status. Records attempted keys in order."""

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        key = str((headers or {}).get("Authorization", "")).removeprefix("Bearer ")
        attempts.append(key)
        status = behaviour.get(key, 500)
        payload = {}
        if status == 200:
            payload = {
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
                "language": "en",
            }
        return _FakeGroqResponse(status, payload)

    monkeypatch.setattr(requests, "post", fake_post)


def _groq_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", ",".join(_GROQ_KEYS))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")


@pytest.mark.parametrize("status", [401, 403])
def test_transcriber_groq_auth_failure_cools_key(tmp_path, monkeypatch, status):
    from app.services import transcriber

    _groq_env(monkeypatch)
    attempts: list = []
    behaviour = {_GROQ_KEYS[0]: status, _GROQ_KEYS[1]: 200, _GROQ_KEYS[2]: 200}
    _patch_groq(monkeypatch, behaviour, attempts)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 64)

    result = transcriber._transcribe_with_groq_srt(str(audio), str(tmp_path / "o1.srt"))
    assert result is not None
    assert attempts == [_GROQ_KEYS[0], _GROQ_KEYS[1]]
    # The revoked/invalid key is sidelined, exactly like a 429 key.
    assert key_rotation.health_for("groq").in_cooldown(_GROQ_KEYS[0])

    # Later call: the cooled key is skipped entirely.
    attempts.clear()
    behaviour.update({_GROQ_KEYS[1]: 429, _GROQ_KEYS[2]: 429, _GROQ_KEYS[0]: 200})
    result = transcriber._transcribe_with_groq_srt(str(audio), str(tmp_path / "o2.srt"))
    assert result is None
    assert _GROQ_KEYS[0] not in attempts


@pytest.mark.parametrize("status", [401, 403])
def test_whisper_service_groq_auth_failure_cools_key(tmp_path, monkeypatch, status):
    from app.services.recap_engine.services.whisper_service import (
        WhisperTimestampService,
    )

    _groq_env(monkeypatch)
    attempts: list = []
    behaviour = {_GROQ_KEYS[0]: status, _GROQ_KEYS[1]: 200, _GROQ_KEYS[2]: 200}
    _patch_groq(monkeypatch, behaviour, attempts)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 64)

    svc = WhisperTimestampService(log=lambda msg: None)
    result = svc._transcribe_groq(str(audio))
    assert result is not None and result["backend"] == "groq-whisper"
    assert attempts == [_GROQ_KEYS[0], _GROQ_KEYS[1]]
    # Same shared registry the transcriber uses.
    assert key_rotation.health_for("groq").in_cooldown(_GROQ_KEYS[0])

    attempts.clear()
    behaviour.update({_GROQ_KEYS[1]: 429, _GROQ_KEYS[2]: 429, _GROQ_KEYS[0]: 200})
    svc2 = WhisperTimestampService(log=lambda msg: None)
    assert svc2._transcribe_groq(str(audio)) is None
    assert _GROQ_KEYS[0] not in attempts


def test_transcriber_logs_digest_not_key_fragments(tmp_path, monkeypatch, caplog):
    import logging

    from app.services import transcriber

    _groq_env(monkeypatch)
    attempts: list = []
    _patch_groq(monkeypatch, {k: 200 for k in _GROQ_KEYS}, attempts)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 64)

    with caplog.at_level(logging.INFO, logger="app.services.transcriber"):
        result = transcriber._transcribe_with_groq_srt(str(audio), str(tmp_path / "o.srt"))
    assert result is not None
    used_key = attempts[0]
    assert f"key #{key_rotation.key_id(used_key)}" in caplog.text
    assert used_key[:7] not in caplog.text
    assert used_key[-4:] not in caplog.text


# ---------------------------------------------------------------------------
# 3 (cont.). Source scan: no key-fragment logging patterns remain
# ---------------------------------------------------------------------------

_TOUCHED_SERVICE_FILES = [
    "app/services/transcriber.py",
    "app/services/recap_engine/services/whisper_service.py",
    "app/services/voxcpm_engine.py",
]


@pytest.mark.parametrize("rel_path", _TOUCHED_SERVICE_FILES)
def test_no_key_fragment_patterns_in_touched_files(rel_path):
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert "[:7]" not in source
    assert "[-4:]" not in source
    # The non-reversible digest is what gets logged instead.
    assert "key_id" in source


def test_mask_hf_token_returns_digest_not_fragments():
    from app.services.voxcpm_engine import mask_hf_token

    token = "<redacted>"
    label = mask_hf_token(token)
    assert label == f"hf#{key_rotation.key_id(token)}"
    assert token[:7] not in label
    assert token[-4:] not in label
    assert mask_hf_token(None) == "anonymous"
    assert mask_hf_token("") == "anonymous"
    assert mask_hf_token("   ") == "anonymous"
