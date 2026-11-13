"""Batch CD (2026-11-07): Admin user detail completed + admin→user
DM + private-AI transcripts (30-day).

Owner orders 2026-11-07 11:56–11:58:
1. The user-detail payload gains the balance bucket breakdown,
   TTS-today (BZ helpers), last-10 payments + approved-only total,
   referrals both directions, last-10 jobs, last-10 ledger,
   verification flags, custom-voice count and the recorded
   private-AI chats (last 50).
2. POST /admin/users/{id}/message — admin→one-user DM via the bot,
   audit-logged per BC-4 conventions, bot-blocked reported as a
   409, empty/oversize rejected.
3. Private-AI exchanges (PRIVATE chain only) are recorded to
   private_ai_messages with 30-day retention; recording is
   failure-isolated (never breaks/delays a reply); group messages
   are NEVER recorded (standing ban untouched).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType, PaymentStatus
from app.models.schema import (
    AuditLog,
    CustomVoiceRequest,
    Job,
    MinuteBalance,
    MinuteLedgerEvent,
    PaymentRequest,
    PrivateAiMessage,
    Referral,
    ReferralReward,
    User,
)
from app.services import group_ai_admin as gai
from app.services import private_ai as pai
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.services.payments import save_payment_intent
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999303
TARGET_ID = 700301
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _user(uid: int, **kw) -> User:
    return User(
        id=uid,
        first_name=kw.pop("first_name", "Test"),
        username=kw.pop("username", None),
        account_category=kw.pop("account_category", AccountCategory.FREE.value),
        language_code="my",
        **kw,
    )


@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    yield


async def _seed(db, *objs):
    for o in objs:
        db.add(o)
    await db.commit()


# ── Token ────────────────────────────────────────────────────────────

def test_cd_cache_token_is_r102():
    html = _read("app/webapp/static/index.html")
    assert html.count("20261113_r143") == 4
    assert ("20261104_r" + "101") not in html


# ── Item 1: completed detail payload ─────────────────────────────────

@pytest.mark.asyncio
async def test_cd_detail_payload_completed(db_session):
    admin = _user(ADMIN_ID)
    target = _user(
        TARGET_ID, first_name="Milo", username="Milo_N4U",
        channel_verified=True, group_verified=False,
    )
    bal = MinuteBalance(
        user_id=TARGET_ID, monthly_free_seconds=600, welcome_seconds=60,
        referral_seconds=120, purchased_seconds=3600, consumed_seconds=900,
    )
    await _seed(db_session, admin, target, bal)

    # 12 payments: newest 10 shown; total = APPROVED only.
    pays = []
    for i in range(12):
        status = PaymentStatus.APPROVED.value
        amount = 1000 * (i + 1)
        if i == 11:
            status, amount = PaymentStatus.PENDING.value, 8000
        if i == 10:
            status, amount = PaymentStatus.REJECTED.value, 5000
        pays.append(PaymentRequest(
            id=f"pay-{i}", user_id=TARGET_ID, package_code="MIN_10",
            minutes=10, amount_mmk=amount, status=status,
            created_at=NOW + timedelta(minutes=i),
        ))
    await _seed(db_session, *pays)

    # Referrals both directions + rewards.
    referred_a = _user(700311, first_name="A")
    referred_b = _user(700312, first_name="B")
    referrer = _user(700313, first_name="Boss", username="boss")
    # Users commit first, then the referral rows (the codebase's
    # seeding pattern — hunt7): same-flush user+referral inserts can
    # hit the FK in the wrong order on SQLite.
    await _seed(db_session, referred_a, referred_b, referrer)
    ref1 = Referral(referrer_id=TARGET_ID, referred_id=700311,
                    referral_code="ref_t", status="REWARDED")
    ref2 = Referral(referrer_id=TARGET_ID, referred_id=700312,
                    referral_code="ref_t", status="PENDING")
    ref_in = Referral(referrer_id=700313, referred_id=TARGET_ID,
                      referral_code="ref_boss", status="REWARDED")
    await _seed(db_session, ref1, ref2, ref_in)
    rw1 = ReferralReward(referrer_id=TARGET_ID, referral_id=ref1.id, reward_index=1, seconds=120)
    rw2 = ReferralReward(referrer_id=TARGET_ID, referral_id=ref2.id, reward_index=2, seconds=120)
    await _seed(db_session, rw1, rw2)

    # 12 jobs → last 10, newest first.
    jobs = [
        Job(id=f"job-{i:02d}", user_id=TARGET_ID, job_type=JobType.VOICE_CLONE.value,
            status=JobStatus.SUCCESS.value, seconds_consumed=30,
            created_at=NOW + timedelta(minutes=i),
            finished_at=NOW + timedelta(minutes=i, seconds=40))
        for i in range(12)
    ]
    await _seed(db_session, *jobs)

    # 12 ledger events → last 10; delta signed by direction.
    led = []
    for i in range(12):
        led.append(MinuteLedgerEvent(
            user_id=TARGET_ID,
            source="CONSUME" if i % 2 else "PURCHASE",
            direction="DEBIT" if i % 2 else "CREDIT",
            seconds=60, note=f"n{i}",
            created_at=NOW + timedelta(minutes=i),
        ))
    await _seed(db_session, *led)

    cv1 = CustomVoiceRequest(user_id=TARGET_ID, model_name="V1", sample_file_id="f1")
    cv2 = CustomVoiceRequest(user_id=TARGET_ID, model_name="V2", sample_file_id="f2")
    chat_rows = [
        PrivateAiMessage(user_id=TARGET_ID, direction="user", kind="user",
                         text="q1", created_at=NOW + timedelta(minutes=1)),
        PrivateAiMessage(user_id=TARGET_ID, direction="ai", kind="ai_reply",
                         text="a1", created_at=NOW + timedelta(minutes=1, seconds=5)),
        PrivateAiMessage(user_id=TARGET_ID, direction="user", kind="user",
                         text="q2", created_at=NOW + timedelta(minutes=2)),
    ]
    await _seed(db_session, cv1, cv2, *chat_rows)

    detail = await api_mod.get_admin_user_detail(TARGET_ID, admin, db_session)

    # Existing fields/shapes untouched.
    assert detail["balance_seconds"] == 600 + 60 + 120 + 3600
    assert detail["jobs_total"] == 12

    bb = detail["balance_breakdown"]
    assert bb == {
        "monthly_free_seconds": 600, "welcome_seconds": 60,
        "referral_seconds": 120, "purchased_seconds": 3600,
        "consumed_seconds": 900,
    }

    # TTS today via the BZ helpers: FREE limit 15 min, nothing consumed,
    # remaining = referral 120 + 900 cap.
    tt = detail["tts_today"]
    assert tt["daily_limit_seconds"] == 900
    assert tt["consumed_seconds"] == 0.0
    assert tt["remaining_seconds"] == 1020.0

    assert len(detail["payments"]) == 10
    assert detail["payments"][0]["amount_mmk"] == 8000  # newest first
    approved_total = sum(1000 * (i + 1) for i in range(10))
    assert detail["payments_total_mmk"] == approved_total

    refs = detail["referrals"]
    assert refs["referred_count"] == 2
    assert refs["referral_earned_seconds"] == 240
    assert refs["referred_by"]["id"] == 700313
    assert refs["referred_by"]["username"] == "boss"

    assert len(detail["recent_jobs"]) == 10
    assert detail["recent_jobs"][0]["id"] == "job-11"
    assert detail["recent_jobs"][0]["seconds_consumed"] == 30
    assert detail["recent_jobs"][0]["finished_at"] is not None

    assert len(detail["ledger_recent"]) == 10
    newest = detail["ledger_recent"][0]
    assert newest["kind"] == "CONSUME" and newest["delta"] == -60
    assert detail["ledger_recent"][1]["delta"] == 60

    assert detail["verification"] == {"channel_verified": True, "group_verified": False}
    assert detail["custom_voices_count"] == 2

    chats = detail["ai_chats"]
    assert [c["text"] for c in chats] == ["q1", "a1", "q2"]  # oldest → newest
    assert chats[1]["direction"] == "ai" and chats[1]["kind"] == "ai_reply"


@pytest.mark.asyncio
async def test_cd_detail_admin_target_tts_nulls_and_zero_buckets(db_session):
    admin = _user(ADMIN_ID)
    other_admin = _user(ADMIN_ID + 1, first_name="Root")
    monkeypatch_ids = f"{ADMIN_ID},{ADMIN_ID + 1}"
    # settings.is_admin consults admin_ids_raw — include both admins.
    import app.webapp.api as _api  # noqa: F401  (module under test)
    from app.config import settings as _settings
    _settings.admin_ids_raw = monkeypatch_ids
    await _seed(db_session, admin, other_admin)
    detail = await api_mod.get_admin_user_detail(ADMIN_ID + 1, admin, db_session)
    assert detail["tts_today"] == {
        "daily_limit_seconds": None, "consumed_seconds": None,
        "remaining_seconds": None,
    }
    assert detail["balance_breakdown"]["purchased_seconds"] == 0
    assert detail["payments"] == [] and detail["payments_total_mmk"] == 0
    assert detail["referrals"]["referred_by"] is None
    assert detail["ai_chats"] == []


# ── Item 2: admin → user message endpoint ────────────────────────────

class _FakeBotSession:
    async def close(self):
        return None


class _FakeBot:
    instances: list = []

    def __init__(self, token=None, **kw):
        self.session = _FakeBotSession()
        self.sent = []
        _FakeBot.instances.append(self)

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)


class _ForbiddenBot(_FakeBot):
    async def send_message(self, chat_id, text):
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods import SendMessage

        raise TelegramForbiddenError(
            method=SendMessage(chat_id=chat_id, text=text),
            message="Forbidden: bot was blocked by the user",
        )


class _BadRequestBot(_FakeBot):
    async def send_message(self, chat_id, text):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendMessage

        raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text),
                                 message="chat not found")


@pytest.fixture
def fake_bot(monkeypatch):
    _FakeBot.instances = []
    monkeypatch.setattr(settings, "bot_token", "123456:fake-token")
    monkeypatch.setattr("aiogram.Bot", _FakeBot)
    return _FakeBot


async def _audit_rows(db, user_id):
    rows = (await db.execute(
        select(AuditLog).where(
            AuditLog.action == "user.message",
            AuditLog.target_id == str(user_id),
        )
    )).scalars().all()
    return rows


@pytest.mark.asyncio
async def test_cd_message_happy_path_and_audit(db_session, fake_bot):
    admin = _user(ADMIN_ID)
    target = _user(TARGET_ID)
    await _seed(db_session, admin, target)

    res = await api_mod.send_admin_user_message(
        TARGET_ID, api_mod.AdminUserMessageRequest(text="  မင်္ဂလာပါ အစ်ကို  "),
        admin, db_session,
    )
    assert res == {"ok": True}
    assert fake_bot.instances[-1].sent == [(TARGET_ID, "မင်္ဂလာပါ အစ်ကို")]

    rows = await _audit_rows(db_session, TARGET_ID)
    assert len(rows) == 1
    assert rows[0].admin_id == ADMIN_ID
    details = json.loads(rows[0].details_json)
    assert details["delivered"] is True and details["error"] is None
    assert details["length"] == len("မင်္ဂလာပါ အစ်ကို")
    assert details["snippet"] == "မင်္ဂလာပါ အစ်ကို"


@pytest.mark.asyncio
async def test_cd_message_bot_blocked_is_409_and_audited(db_session, fake_bot, monkeypatch):
    monkeypatch.setattr("aiogram.Bot", _ForbiddenBot)
    admin = _user(ADMIN_ID)
    target = _user(TARGET_ID, bot_blocked=True)
    await _seed(db_session, admin, target)

    with pytest.raises(HTTPException) as ei:
        await api_mod.send_admin_user_message(
            TARGET_ID, api_mod.AdminUserMessageRequest(text="hello"),
            admin, db_session,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail == "bot_blocked"

    rows = await _audit_rows(db_session, TARGET_ID)
    assert len(rows) == 1
    details = json.loads(rows[0].details_json)
    assert details["delivered"] is False and details["error"] == "bot_blocked"


@pytest.mark.asyncio
async def test_cd_message_chat_not_found_is_409(db_session, fake_bot, monkeypatch):
    monkeypatch.setattr("aiogram.Bot", _BadRequestBot)
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin, _user(TARGET_ID))
    with pytest.raises(HTTPException) as ei:
        await api_mod.send_admin_user_message(
            TARGET_ID, api_mod.AdminUserMessageRequest(text="hello"),
            admin, db_session,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail == "chat_not_found"


@pytest.mark.asyncio
async def test_cd_message_validation(db_session, fake_bot):
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin, _user(TARGET_ID))
    for bad, code in [("", 400), ("   ", 400), ("x" * 4001, 400)]:
        with pytest.raises(HTTPException) as ei:
            await api_mod.send_admin_user_message(
                TARGET_ID, api_mod.AdminUserMessageRequest(text=bad),
                admin, db_session,
            )
        assert ei.value.status_code == code
    with pytest.raises(HTTPException) as ei:
        await api_mod.send_admin_user_message(
            424242, api_mod.AdminUserMessageRequest(text="hello"),
            admin, db_session,
        )
    assert ei.value.status_code == 404
    # Nothing was sent, nothing audited for the rejected inputs.
    assert fake_bot.instances == []
    assert await _audit_rows(db_session, TARGET_ID) == []


@pytest.mark.asyncio
async def test_cd_message_without_bot_token_is_503(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    admin = _user(ADMIN_ID)
    await _seed(db_session, admin, _user(TARGET_ID))
    with pytest.raises(HTTPException) as ei:
        await api_mod.send_admin_user_message(
            TARGET_ID, api_mod.AdminUserMessageRequest(text="hello"),
            admin, db_session,
        )
    assert ei.value.status_code == 503


def test_cd_message_route_is_admin_gated():
    route = next(
        r for r in api_mod.api_router.routes
        if getattr(r, "path", "") == "/api/admin/users/{user_id}/message"
        and "POST" in getattr(r, "methods", set())
    )
    deps = [d.call for d in route.dependant.dependencies]
    assert api_mod.require_admin in deps


# ── Item 3: transcript recording ─────────────────────────────────────

from aiogram.enums import ChatType
from aiogram.types import Message, User as TgUser

from app.handlers.group_admin import handle_private_message


def _fake_private_message(text, *, user_id=555001, message_id=10):
    msg = MagicMock(spec=Message)
    msg.message_id = message_id
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.id = user_id
    msg.chat.type = ChatType.PRIVATE
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Aung", username="aung")
    msg.sender_chat = None
    msg.reply_to_message = None
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


def _fake_state(current=None):
    state = MagicMock()
    state.get_state = AsyncMock(return_value=current)
    return state


@pytest.fixture(autouse=True)
def _fresh_private_limiter(monkeypatch):
    monkeypatch.setattr(pai, "private_rate_limiter", gai.GroupReplyRateLimiter())


async def _chat_rows(db, user_id=555001):
    rows = (await db.execute(
        select(PrivateAiMessage)
        .where(PrivateAiMessage.user_id == user_id)
        .order_by(PrivateAiMessage.id)
    )).scalars().all()
    return [(r.direction, r.kind, r.text) for r in rows]


@pytest.mark.asyncio
async def test_cd_greeting_exchange_is_recorded(db_session):
    msg = _fake_private_message("မင်္ဂလာပါ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_awaited_once()
    assert await _chat_rows(db_session) == [
        ("user", "user", "မင်္ဂလာပါ"),
        ("ai", "fast_path", pai.PRIVATE_WELCOME_TEXT),
    ]


@pytest.mark.asyncio
async def test_cd_ai_reply_exchange_is_recorded(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "Bot ကို ဖွင့်ပြီး စလို့ ရပါတယ်။"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    msg = _fake_private_message("Recap က ဘယ်လို လုပ်ရတာလဲ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_awaited_once()
    rows = await _chat_rows(db_session)
    assert rows[0] == ("user", "user", "Recap က ဘယ်လို လုပ်ရတာလဲ")
    assert rows[1][0:2] == ("ai", "ai_reply")
    assert "စလို့ ရပါတယ်" in rows[1][2]


@pytest.mark.asyncio
async def test_cd_sensitive_exchange_is_recorded_as_escalation(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "777001")
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "မဖြေသင့်တဲ့ အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    monkeypatch.setattr(gai, "draft_escalation_answer", AsyncMock(return_value=None))
    msg = _fake_private_message("ငွေသွင်းပြီးပြီ၊ မိနစ် မဝင်သေး ဘူး ခင်ဗျာ", message_id=77)
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_awaited_once()
    rows = await _chat_rows(db_session)
    assert rows[0][0:2] == ("user", "user")
    assert rows[1] == ("ai", "escalation", t("private_ai_deferral_sensitive", "my"))


@pytest.mark.asyncio
async def test_cd_ai_escalate_exchange_is_recorded(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "777001")
    ai = AsyncMock(return_value=(gai.DECISION_ESCALATE, None))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    monkeypatch.setattr(gai, "draft_escalation_answer", AsyncMock(return_value=None))
    msg = _fake_private_message("ဒီဝန်ဆောင်မှုက ဘယ်နိုင်ငံတွေမှာ သုံးလို့ ရလဲ", message_id=78)
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    rows = await _chat_rows(db_session)
    assert rows[1] == ("ai", "escalation", t("private_ai_deferral_unsure", "my"))


@pytest.mark.asyncio
async def test_cd_other_chains_record_nothing(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)

    # FSM-owned text: another chain's message.
    fsm_msg = _fake_private_message("ဒါက clone စာသား ဖြစ်ပါတယ်", message_id=21)
    await handle_private_message(
        fsm_msg, state=_fake_state("CloneStates:waiting_text"),
        session=db_session, bot=fsm_msg.bot,
    )
    # Command: owned by the command handlers.
    cmd_msg = _fake_private_message("/start", message_id=22)
    await handle_private_message(cmd_msg, state=_fake_state(), session=db_session, bot=cmd_msg.bot)
    # AI failure → silence: nothing was sent, nothing is recorded.
    monkeypatch.setattr(pai, "ask_private_ai",
                        AsyncMock(return_value=(gai.DECISION_AI_FAILED, None)))
    fail_msg = _fake_private_message("Voice Clone ဆိုတာ ဘာလဲ", message_id=23)
    await handle_private_message(fail_msg, state=_fake_state(), session=db_session, bot=fail_msg.bot)

    assert await _chat_rows(db_session) == []


@pytest.mark.asyncio
async def test_cd_payment_intent_hint_records_nothing(db_session, monkeypatch):
    ai = AsyncMock(return_value=(gai.DECISION_REPLY, "အဖြေ"))
    monkeypatch.setattr(pai, "ask_private_ai", ai)
    user, _ = await get_or_create_user(db_session, telegram_id=555001)
    await save_payment_intent(db_session, user.id, "MIN_10")
    await db_session.commit()
    msg = _fake_private_message("paid already", message_id=31)
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.answer.assert_awaited_once()  # the hint fired
    assert await _chat_rows(db_session) == []


@pytest.mark.asyncio
async def test_cd_recording_failure_never_breaks_the_reply(db_session, monkeypatch):
    boom = AsyncMock(side_effect=RuntimeError("db on fire"))
    monkeypatch.setattr(pai, "record_private_exchange", boom)
    msg = _fake_private_message("မင်္ဂလာပါ")
    await handle_private_message(msg, state=_fake_state(), session=db_session, bot=msg.bot)
    msg.reply.assert_awaited_once()  # reply still delivered
    assert msg.reply.await_args.args[0] == pai.PRIVATE_WELCOME_TEXT
    assert await _chat_rows(db_session) == []


@pytest.mark.asyncio
async def test_cd_record_exchange_unit_and_failure_flag(db_session):
    await _seed(db_session, _user(555001))
    ok = await pai.record_private_exchange(
        db_session, user_id=555001, user_text="q", reply_text="a",
        kind=pai.KIND_AI_REPLY,
    )
    assert ok is True
    assert len(await _chat_rows(db_session)) == 2

    broken = MagicMock()
    broken.add = MagicMock(side_effect=RuntimeError("nope"))
    broken.commit = AsyncMock()
    broken.rollback = AsyncMock()
    ok = await pai.record_private_exchange(
        broken, user_id=1, user_text="q", reply_text="a", kind="ai_reply",
    )
    assert ok is False  # swallowed, reported, never raised


@pytest.mark.asyncio
async def test_cd_retention_prune_boundary(db_session):
    await _seed(db_session, _user(555001))
    base = NOW
    rows = [
        PrivateAiMessage(user_id=555001, direction="user", kind="user",
                         text="old", created_at=base - timedelta(days=31)),
        PrivateAiMessage(user_id=555001, direction="user", kind="user",
                         text="edge-out", created_at=base - timedelta(days=30, minutes=1)),
        PrivateAiMessage(user_id=555001, direction="user", kind="user",
                         text="edge-in", created_at=base - timedelta(days=30)),
        PrivateAiMessage(user_id=555001, direction="user", kind="user",
                         text="fresh", created_at=base - timedelta(days=29)),
    ]
    await _seed(db_session, *rows)
    pruned = await pai.prune_private_ai_messages(db_session, now=base)
    assert pruned == 2
    kept = [r.text for r in (await db_session.execute(
        select(PrivateAiMessage).order_by(PrivateAiMessage.id)
    )).scalars().all()]
    assert kept == ["edge-in", "fresh"]  # exactly-30-days is kept


def test_cd_group_path_has_no_recording():
    src = _read("app/handlers/group_admin.py")
    group_body = src.split("async def handle_group_message", 1)[1].split(
        "async def _record_private_exchange", 1)[0]
    assert "record_private_exchange" not in group_body
    escalate_body = src.split("async def _escalate", 1)[1].split(
        "async def handle_group_message", 1)[0]
    assert "PrivateAiMessage" not in escalate_body
    # The group AI service never touches the transcript table.
    assert "PrivateAiMessage" not in _read("app/services/group_ai_admin.py")


def test_cd_schema_and_sweep_wiring():
    from app.models.schema import PrivateAiMessage as M

    assert M.__tablename__ == "private_ai_messages"
    cols = M.__table__.columns
    for name in ("id", "user_id", "direction", "kind", "text", "created_at"):
        assert name in cols
    server_src = _read("app/webapp/server.py")
    # Startup pass + periodic sweep both call the prune entry point.
    assert server_src.count("sweep_private_ai_messages") >= 3
    assert pai.PRIVATE_AI_RETENTION_DAYS == 30


# ── Client pins ──────────────────────────────────────────────────────

CD_KEYS = [
    "admin_balance_breakdown", "admin_bal_monthly", "admin_bal_welcome",
    "admin_bal_referral", "admin_bal_purchased", "admin_bal_consumed",
    "admin_tts_today", "admin_tts_used", "admin_tts_left",
    "admin_verified_channel", "admin_verified_group", "admin_verified_yes",
    "admin_verified_no", "admin_referrals_made", "admin_referral_earned",
    "admin_referred_by", "admin_custom_voices", "admin_msg_title",
    "admin_msg_ph", "admin_msg_send", "admin_msg_confirm", "admin_msg_sent",
    "admin_msg_empty", "admin_msg_blocked", "admin_msg_unreachable",
    "admin_msg_send_failed", "admin_msg_banned_note", "admin_msg_blocked_note",
    "admin_payments_title", "admin_payments_total", "admin_payments_empty",
    "admin_ledger_title", "admin_ledger_empty", "admin_recent_jobs_title",
    "admin_ai_chats_title", "admin_ai_chats_empty", "admin_ai_chat_user",
    "admin_ai_chat_ai",
]


def _locale_keys(lang: str) -> set:
    src = _read("app/webapp/static/js/i18n.js")
    marker = "    " + lang + ": {"
    start = src.index(marker) + len(marker)
    end = src.index("\n    }", start)
    return set(re.findall(r"(?m)^        (\w+):", src[start:end]))


def test_cd_i18n_keys_complete_both_languages():
    for lang in ("my", "en"):
        keys = _locale_keys(lang)
        for key in CD_KEYS:
            assert key in keys, f"{key} missing from i18n.js {lang}"


def test_cd_client_sections_and_send_wiring():
    html = _read("app/webapp/static/index.html")
    for el_id in (
        "adm-detail-breakdown", "adm-detail-tts", "adm-detail-verify",
        "adm-detail-referrals", "adm-msg-text", "adm-msg-note",
        "adm-msg-send-btn", "adm-detail-recent-jobs", "adm-detail-payments",
        "adm-detail-payments-total", "adm-detail-ledger", "adm-detail-ai-chats",
    ):
        assert f'id="{el_id}"' in html
    for key in ("admin_balance_breakdown", "admin_msg_title",
                "admin_recent_jobs_title", "admin_payments_title",
                "admin_ledger_title", "admin_ai_chats_title"):
        assert f'data-i18n="{key}"' in html

    js = _read("app/webapp/static/js/app.js")
    assert "async function adminSendUserMessage()" in js
    assert "`/api/admin/users/${uid}/message`" in js
    assert 'showStudioConfirm(t("admin_msg_confirm")' in js
    assert "function renderAdminDetailExtras(u)" in js
    assert "renderAdminDetailExtras(u);" in js
    for key in ("admin_msg_blocked", "admin_msg_unreachable", "admin_msg_sent",
                "admin_ai_chats_empty", "admin_payments_empty", "admin_ledger_empty"):
        assert f't("{key}")' in js

    css = _read("app/webapp/static/css/style.css")
    assert ".adm-chat-row" in css and ".adm-chat-user" in css and ".adm-chat-ai" in css
