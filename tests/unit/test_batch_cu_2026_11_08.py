"""Batch CU — escalation edit routing + claim recovery + recap ledger.

Production evidence (2026-11-08): escalation id 2 was claimed for
editing twice and both times the admin's edited text vanished — the
record wedged in EDITING and nothing further could be done. Root
cause: inside group_admin_router the private-AI catch-all
(handle_private_message, registered first, no state filter) matched
the admin's plain private text before the state-filtered edit
handler; its FSM guard returned early, but a matched aiogram handler
consumes the update, so the edit handler never fired.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Update
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser

from app.config import settings
from app.handlers import setup_handlers
from app.models.schema import GroupAiEscalation, RecapCreditLedgerEvent, User
from app.services import group_ai_admin as gai
from app.services import private_ai as pai
from app.services.i18n import load_locales

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999000

load_locales()


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


async def _make_escalation(db_session, *, status="EDITING", handled_by=ADMIN_ID,
                           handled_minutes_ago=5, draft=" မူကြမ်း"):
    row = GroupAiEscalation(
        chat_id=555001,
        message_id=77,
        author_id=555001,
        author_label="Milo",
        question_text="Admin အကောင့်သိချင်တယ်",
        draft_text=draft,
        original_draft_text=draft,
        status=status,
        reason="ai_escalate",
        handled_by=handled_by,
        handled_at=(datetime.now(timezone.utc) - timedelta(minutes=handled_minutes_ago))
        if handled_by else None,
        dm_copies=json.dumps([]),
    )
    db_session.add(row)
    await db_session.commit()
    return row.id


# ---------------------------------------------------------------------------
# CU-1 — the edit handler must be registered before the private catch-all
# ---------------------------------------------------------------------------

def test_cu_edit_handler_registered_before_private_catchall():
    src = _read("app/handlers/group_admin.py")
    edit_idx = src.index("GroupAiEditStates.waiting_text")
    catchall_idx = src.index("async def handle_private_message")
    assert edit_idx < catchall_idx


def _mock_session_maker(session):
    sm = MagicMock()
    sm.return_value.__aenter__ = AsyncMock(return_value=session)
    sm.return_value.__aexit__ = AsyncMock(return_value=None)
    return sm


def _make_message_update(user_id: int, text: str, update_id: int) -> Update:
    chat = Chat(id=user_id, type="private")
    user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    msg = TgMessage(
        message_id=update_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=msg)


@pytest.mark.asyncio
async def test_cu_admin_edit_text_reaches_edit_handler(db_session, monkeypatch):
    """Dispatcher-level: an admin in waiting_text state sends the edited
    text — the record must release to PENDING with the new draft, and
    the private-AI path must NOT be consulted."""
    from app.handlers.group_admin import GroupAiEditStates

    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    sm = _mock_session_maker(db_session)
    monkeypatch.setattr("app.handlers.group_admin.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.start.AsyncSessionLocal", sm)
    monkeypatch.setattr("app.handlers.payment.AsyncSessionLocal", sm)
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "AI_REPLY"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)

    esc_id = await _make_escalation(db_session)

    dp = Dispatcher(storage=MemoryStorage())
    setup_handlers(dp, session_factory=sm)
    bot = AsyncMock(spec=Bot)
    bot.id = 999888
    sent = AsyncMock()
    sent.message_id = 4242
    bot.send_message = AsyncMock(return_value=sent)

    key = StorageKey(bot_id=999888, chat_id=ADMIN_ID, user_id=ADMIN_ID)
    await dp.storage.set_state(key, GroupAiEditStates.waiting_text)
    await dp.storage.set_data(key, {"escalation_id": esc_id})

    edited = "Admin အကောင့်က @VoiceCloneProAdmin ပါ"
    await dp.feed_update(bot, _make_message_update(ADMIN_ID, edited, 9001))

    row = await db_session.get(GroupAiEscalation, esc_id)
    await db_session.refresh(row)
    assert row.status == "PENDING"          # released by the edit handler
    assert row.draft_text == edited          # the admin's edit was saved
    ai.assert_not_awaited()                  # private AI never consulted


# ---------------------------------------------------------------------------
# CU-1b — stale EDITING claims release; the claimer can re-enter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cu_stale_editing_claim_released_fresh_untouched(db_session):
    stale_id = await _make_escalation(db_session, handled_minutes_ago=31)
    fresh_id = await _make_escalation(db_session, handled_minutes_ago=5)
    released = await gai.release_stale_editing_claims(db_session)
    assert [r["id"] for r in released] == [stale_id]
    stale = await db_session.get(GroupAiEscalation, stale_id)
    fresh = await db_session.get(GroupAiEscalation, fresh_id)
    await db_session.refresh(stale)
    await db_session.refresh(fresh)
    assert stale.status == "PENDING" and stale.handled_by is None
    assert fresh.status == "EDITING"


@pytest.mark.asyncio
async def test_cu_claimer_reclaims_own_editing_row(db_session):
    esc_id = await _make_escalation(db_session)
    outcome, _ = await gai.claim_escalation_for_edit(
        db_session, esc_id, admin_id=ADMIN_ID
    )
    assert outcome == "reclaimed"
    outcome2, _ = await gai.claim_escalation_for_edit(
        db_session, esc_id, admin_id=ADMIN_ID + 7
    )
    assert outcome2 == "already_handled"


# ---------------------------------------------------------------------------
# CU-2 — admin user detail carries recap-credit movements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cu_admin_detail_includes_recap_ledger(db_session, monkeypatch):
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    admin = User(id=ADMIN_ID, first_name="Boss")
    target = User(id=700401, first_name="RecapFan", recap_credits=6)
    db_session.add_all([admin, target])
    await db_session.commit()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        RecapCreditLedgerEvent(
            user_id=700401, source="RECAP", direction="DEBIT",
            credits=2, reference_id="rc_job_1", note="submit charge",
            created_at=now - timedelta(minutes=3),
        ),
        RecapCreditLedgerEvent(
            user_id=700401, source="RECAP", direction="CREDIT",
            credits=3, reference_id=None, note="welcome",
            created_at=now - timedelta(days=2),
        ),
    ])
    await db_session.commit()

    detail = await api_mod.get_admin_user_detail(700401, admin, db_session)
    rows = detail["recap_ledger_recent"]
    assert len(rows) == 2
    assert rows[0]["delta"] == -2 and rows[0]["kind"] == "RECAP"
    assert rows[1]["delta"] == 3


def test_cu_recap_ledger_ui_wired():
    js = _read("app/webapp/static/js/admin.js")
    html = _read("app/webapp/static/index.html")
    i18n = _read("app/webapp/static/js/i18n.js")
    assert "recap_ledger_recent" in js
    assert 'id="adm-detail-ledger"' in html
    assert "admin_recap_ledger_title" in i18n and "admin_recap_ledger_empty" in i18n
    assert i18n.count("admin_recap_ledger_title") >= 2  # my + en
