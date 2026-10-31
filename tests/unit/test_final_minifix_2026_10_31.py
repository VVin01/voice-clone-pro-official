"""Final mini-fix round from fresh-eyes hunt #5 (2026-10-31).

2.  A recap job cancelled while it waited for an execution-semaphore
    slot was resurrected by the executor: flipped back to RUNNING,
    rendered and delivered although the cancel path had already
    refunded it. The executor now re-checks the cancel state after
    acquiring the slot (and again at the delivery boundary).
3.  The bot's tutorial-URL receivers accepted any http(s) string,
    while the Mini App settings save validates t.me links (hunt #4
    B1). Both receivers now use the same shared validator.
4.  The Zawgyi detector's asat-position signal fired on genuine
    Unicode text that merely ended in a stray U+1039 fragment, as
    long as the text also contained a real stack (the stack proves
    the writer produces Unicode). The signal is now distrusted when
    valid-stack evidence exists.
5.  Refund state lived only in the in-memory registry entry: the
    persisted recap blob now carries credit_refunded/credits_refunded,
    the refund path writes them, re-dispatch restores them, and the
    admin-cancel registry-missing fallback refunds the blob's proven
    charge (not an assumed 1) without double-refunding.
6.  The recap queue worker claimed a job (QUEUED->RUNNING) BEFORE
    building its re-dispatch plan; a raising plan stranded the job in
    RUNNING forever. The plan is now built first, and a raising plan
    is logged while the job stays QUEUED.
7.  A persisted payment intent pointing at a since-removed plan made
    handle_receipt_after_restart raise on every slip photo -- user
    silence, intent re-poisoning forever. The failure now clears the
    intent, is logged, and the user gets a restart-the-purchase reply.

(Item 1, the asset-version bump, is pinned in
test_hardening_family5_2026_10_31.py.)
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_mod
import app.handlers.admin as admin_mod
import app.handlers.payment as payment_mod
import app.services.downloader as downloader_mod
from app.config import settings
from app.db.base import Base
from app.handlers.admin import (
    handle_admin_tutorial_recap_url_input,
    handle_admin_tutorial_voice_url_input,
)
from app.handlers.payment import handle_receipt_after_restart
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, PaymentIntent, PaymentRequest, User
from app.services.ledger import get_or_create_user
from app.services.myanmar_text import convert_zawgyi_to_unicode, is_probably_zawgyi
from app.services.payments import (
    get_active_pricing_plans,
    get_recent_payment_intent,
    save_payment_intent,
)
from app.services.url_safety import is_valid_telegram_link
from app.webapp import api as api_mod
from app.webapp.server import _dispatch_oldest_queued_recap


def U(*cps: int) -> str:
    return "".join(chr(c) for c in cps)


async def _tmp_maker(tmp_path, monkeypatch, name="minifix.db"):
    """File-backed test DB bound into app.db.session.AsyncSessionLocal,
    the pattern the F9 sweep test established."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


# ── 4. Zawgyi detector guard ─────────────────────────────────────────

def test_zawgyi_guard_probe_stays_byte_identical():
    # The hunt #5 probe, built from codepoints: "သဒ္ဒါစာအုပ်" (with a
    # genuine geminate stack U+1012 U+1039 U+1012) plus a trailing
    # fragment "က\u1039". The stray asat-position U+1039 must NOT flip
    # the document to Zawgyi now that valid-stack evidence exists.
    probe = U(
        0x101E, 0x1012, 0x1039, 0x1012, 0x102B,
        0x1005, 0x102C, 0x1021, 0x102F, 0x1015, 0x103A,
        0x20, 0x1000, 0x1039,
    )
    assert is_probably_zawgyi(probe) is False
    assert convert_zawgyi_to_unicode(probe) == probe


def test_zawgyi_asat_signal_still_fires_without_stack_evidence():
    # Control: a bare coda written with U+1039 (Zawgyi's asat) and no
    # stack anywhere still detects and converts to U+103A.
    assert is_probably_zawgyi(U(0x1014, 0x1039)) is True
    assert convert_zawgyi_to_unicode(U(0x1014, 0x1039)) == U(0x1014, 0x103A)


# ── 3. Bot tutorial-link validation parity ───────────────────────────

@pytest.mark.parametrize("good", [
    "https://t.me/VoiceClonePro/29",
    "https://t.me/VoiceClonePro/30",
    "https://t.me/VoiceClonePro",
    "https://t.me/+2udU2-fb1zNiZmI1",
])
def test_telegram_link_helper_accepts_stored_shapes(good):
    assert is_valid_telegram_link(good) is True


@pytest.mark.parametrize("bad", [
    "https://t.me/Voice Clone/29",          # space in the name
    "junk https://t.me/VoiceClonePro/29",   # junk around a real link
    "https://example.com/VoiceClonePro/29",
    "http://t.me/VoiceClonePro/29",         # webapp parity: https only
    "https://youtube.com/watch?v=1",
    "javascript:alert(1)",
    "not a url",
    "",
])
def test_telegram_link_helper_rejects_junk(bad):
    assert is_valid_telegram_link(bad) is False


def _patch_admin_sessions(monkeypatch, db_session):
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(admin_mod, "AsyncSessionLocal", lambda: mock_session_ctx())


def _fake_url_message(text, uid=424242):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=uid, is_bot=False, first_name="Admin")
    msg.text = text
    msg.answer = AsyncMock()
    return msg


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [
    handle_admin_tutorial_voice_url_input,
    handle_admin_tutorial_recap_url_input,
])
async def test_bot_tutorial_receivers_reject_junk_url(db_session, monkeypatch, handler):
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    _patch_admin_sessions(monkeypatch, db_session)
    state = SimpleNamespace(clear=AsyncMock())
    msg = _fake_url_message("https://t.me/Voice Clone/29 and other junk")
    await handler(msg, state)
    assert msg.answer.await_count == 1
    assert "Invalid URL format" in msg.answer.await_args.args[0]
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,attr", [
    (handle_admin_tutorial_voice_url_input, "tutorial_voice_link"),
    (handle_admin_tutorial_recap_url_input, "tutorial_recap_link"),
])
async def test_bot_tutorial_receivers_accept_real_tme_link(db_session, monkeypatch, handler, attr):
    monkeypatch.setattr(settings, "admin_ids_raw", "424242")
    # set_dynamic_setting also applies the value to the in-memory
    # settings object; pin the attribute so teardown restores it.
    monkeypatch.setattr(settings, attr, getattr(settings, attr))
    _patch_admin_sessions(monkeypatch, db_session)
    state = SimpleNamespace(clear=AsyncMock())
    url = "https://t.me/VoiceClonePro/29"
    msg = _fake_url_message(url)
    await handler(msg, state)
    state.clear.assert_awaited()
    assert msg.answer.await_count == 1
    assert "Updated" in msg.answer.await_args.args[0]
    assert getattr(settings, attr) == url


# ── 2. Post-semaphore CANCELLED re-check ─────────────────────────────

class _CountingDownloader:
    calls = 0
    fail = False

    def __init__(self):
        type(self).calls += 1
        if type(self).fail:
            raise RuntimeError("sentinel-downloader")


async def _seed_recap_job(maker, jid, uid):
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=uid)
        s.add(Job(
            id=jid, user_id=user.id, chat_id=user.id,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="T",
            source_audio_path="https://example.com/v",
        ))
        await s.commit()
        return user.id


def _queued_entry(jid, uid):
    return {
        "id": jid, "user_id": uid, "title": "T", "status": "QUEUED",
        "progress_percent": 5, "progress_step": "0/4",
        "progress_label": "queued", "error_message": None,
        "credit_deducted": True, "credits_charged": 1,
        "credits_estimated": None, "parts": [],
    }


@pytest.mark.asyncio
async def test_recap_cancelled_while_waiting_for_semaphore_never_executes(
    tmp_path, monkeypatch
):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "sem_cancel.db")
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    _CountingDownloader.calls = 0
    _CountingDownloader.fail = False
    monkeypatch.setattr(downloader_mod, "MediaDownloader", _CountingDownloader)
    # A contended acquire binds a semaphore to this test's event loop;
    # use a private instance so the module-level one stays loop-clean
    # for other tests (production has exactly one loop).
    sem = asyncio.Semaphore(2)
    monkeypatch.setattr(api_mod, "RECAP_EXECUTION_SEMAPHORE", sem)

    jid = "rc_sem_cancel"
    uid = await _seed_recap_job(maker, jid, 991001)
    entry = _queued_entry(jid, uid)
    api_mod.RECAP_JOBS_REGISTRY[jid] = entry
    req = api_mod.RecapConfigRequest(
        url="https://example.com/v", title="T", voice_slug="komin"
    )
    try:
        # Occupy every execution slot so the job truly waits.
        await sem.acquire()
        await sem.acquire()
        task = asyncio.create_task(
            api_mod._run_recap_background_job(jid, req, uid, uid, credit_deducted=True)
        )
        await asyncio.sleep(0.1)
        assert not task.done()

        # Admin cancels while the job waits (exactly the endpoint's
        # writes: DB CANCELLED + registry FAILED/cancel marker; the
        # cancel path also refunds -- not this test's concern).
        async with maker() as s:
            job = await s.get(Job, jid)
            job.status = JobStatus.CANCELLED.value
            await s.commit()
        entry["status"] = "FAILED"
        entry["error_message"] = "Cancelled by Administrator"

        sem.release()
        sem.release()
        await asyncio.wait_for(task, timeout=5)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
        await engine.dispose()

    # Nothing executed: the pipeline was never even constructed, the
    # entry was never flipped to RUNNING/SUCCESS, and the DB row keeps
    # the cancel path's terminal state.
    assert _CountingDownloader.calls == 0
    assert entry["status"] == "FAILED"
    assert entry["error_message"] == "Cancelled by Administrator"
    async with maker() as s:
        job = await s.get(Job, jid)
        assert job.status == JobStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_recap_not_cancelled_still_executes(tmp_path, monkeypatch):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "sem_run.db")
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    _CountingDownloader.calls = 0
    _CountingDownloader.fail = True  # fail fast inside the try: proves we got in
    monkeypatch.setattr(downloader_mod, "MediaDownloader", _CountingDownloader)
    monkeypatch.setattr(api_mod, "RECAP_EXECUTION_SEMAPHORE", asyncio.Semaphore(2))

    jid = "rc_sem_run"
    uid = await _seed_recap_job(maker, jid, 991002)
    entry = _queued_entry(jid, uid)
    api_mod.RECAP_JOBS_REGISTRY[jid] = entry
    req = api_mod.RecapConfigRequest(
        url="https://example.com/v", title="T", voice_slug="komin"
    )
    try:
        await asyncio.wait_for(
            api_mod._run_recap_background_job(jid, req, uid, uid, credit_deducted=True),
            timeout=10,
        )
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
        await engine.dispose()

    # The guard let it through: the pipeline was constructed and the
    # job travelled the normal failure path (entry + DB FAILED with
    # the sentinel error), instead of aborting quietly.
    assert _CountingDownloader.calls == 1
    assert entry["status"] == "FAILED"
    assert "sentinel-downloader" in (entry["error_message"] or "")
    async with maker() as s:
        job = await s.get(Job, jid)
        assert job.status == JobStatus.FAILED.value


# ── 5. Refund-state persistence ──────────────────────────────────────

def test_persisted_state_carries_refund_flag():
    req = api_mod.RecapConfigRequest(
        url="https://example.com/m", title="M", voice_slug="komin"
    )
    blob = api_mod._persisted_recap_state(
        req, credit_deducted=True, credits_charged=3,
        is_vip=False, phase="full", credit_refunded=True, credits_refunded=3,
    )
    job = SimpleNamespace(recap_config=blob)
    state = api_mod._load_persisted_recap_state(job)
    assert state["credit_refunded"] is True
    assert state["credits_refunded"] == 3
    plan = api_mod._recap_redispatch_plan(
        SimpleNamespace(
            id="rc_blob_flag", user_id=1, chat_id=1,
            source_audio_path="https://example.com/m",
            input_text="M", preset_voice="komin", recap_config=blob,
        ),
        SimpleNamespace(account_category="FREE"),
    )
    assert plan["entry"]["credit_refunded"] is True
    assert plan["entry"]["credits_refunded"] == 3


@pytest.mark.asyncio
async def test_refund_is_persisted_and_never_doubled(tmp_path, monkeypatch):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "refund.db")
    jid = "rc_refund_persist"
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=991101)
        uid = user.id
        user.recap_credits = 5
        req = api_mod.RecapConfigRequest(
            url="https://example.com/m", title="M", voice_slug="komin"
        )
        blob = api_mod._persisted_recap_state(
            req, credit_deducted=True, credits_charged=3,
            is_vip=False, phase="full",
        )
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.RUNNING.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="M",
            source_audio_path="https://example.com/m",
            recap_config=blob,
        ))
        await s.commit()

    entry = {
        "id": jid, "user_id": uid,
        "credit_deducted": True, "credits_charged": 3,
    }
    assert await api_mod._refund_recap_job_charge(uid, entry) is True

    async with maker() as s:
        user = await s.get(User, uid)
        assert user.recap_credits == 8  # 5 + the proven 3
        job = await s.get(Job, jid)
        state = api_mod._load_persisted_recap_state(job)
        assert state["credit_refunded"] is True
        assert state["credits_refunded"] == 3

        # A re-dispatch after a restart restores the refund flag...
        plan = api_mod._recap_redispatch_plan(job, user)
    assert plan["entry"]["credit_refunded"] is True
    # ...so a second failure of the restored job refunds nothing more.
    assert await api_mod._refund_recap_job_charge(uid, plan["entry"]) is False
    async with maker() as s:
        user = await s.get(User, uid)
        assert user.recap_credits == 8
    await engine.dispose()


async def _cancel_via_endpoint(maker, jid, admin):
    async with maker() as s:
        return await api_mod.cancel_queued_job(jid, admin, s)


@pytest.mark.asyncio
async def test_admin_cancel_fallback_refunds_blob_amount(tmp_path, monkeypatch):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "cancel_blob.db")
    jid = "rc_cancel_blob"
    async with maker() as s:
        admin, _ = await get_or_create_user(s, telegram_id=991201)
        user, _ = await get_or_create_user(s, telegram_id=991202)
        uid = user.id
        user.recap_credits = 2
        req = api_mod.RecapConfigRequest(
            url="https://example.com/m", title="M", voice_slug="komin"
        )
        blob = api_mod._persisted_recap_state(
            req, credit_deducted=True, credits_charged=3,
            is_vip=False, phase="full",
        )
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="M",
            source_audio_path="https://example.com/m",
            recap_config=blob,
        ))
        await s.commit()
    api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)  # registry lost (restart)

    res = await _cancel_via_endpoint(maker, jid, admin)
    assert res["refund"]["refunded"] is True
    assert res["refund"]["recap_credits"] == 3  # the proven N, not 1
    async with maker() as s:
        user = await s.get(User, uid)
        assert user.recap_credits == 5  # 2 + 3
        job = await s.get(Job, jid)
        assert job.status == JobStatus.CANCELLED.value
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_cancel_fallback_respects_persisted_refund(tmp_path, monkeypatch):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "cancel_refunded.db")
    jid = "rc_cancel_refunded"
    async with maker() as s:
        admin, _ = await get_or_create_user(s, telegram_id=991211)
        user, _ = await get_or_create_user(s, telegram_id=991212)
        uid = user.id
        user.recap_credits = 5  # refund of 3 already landed pre-restart
        req = api_mod.RecapConfigRequest(
            url="https://example.com/m", title="M", voice_slug="komin"
        )
        blob = api_mod._persisted_recap_state(
            req, credit_deducted=True, credits_charged=3,
            is_vip=False, phase="full",
            credit_refunded=True, credits_refunded=3,
        )
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="M",
            source_audio_path="https://example.com/m",
            recap_config=blob,
        ))
        await s.commit()
    api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)

    res = await _cancel_via_endpoint(maker, jid, admin)
    assert res["refund"]["refunded"] is True  # reported, but not paid twice
    async with maker() as s:
        user = await s.get(User, uid)
        assert user.recap_credits == 5  # unchanged
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_cancel_fallback_legacy_row_refunds_one(tmp_path, monkeypatch):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "cancel_legacy.db")
    jid = "rc_cancel_legacy"
    async with maker() as s:
        admin, _ = await get_or_create_user(s, telegram_id=991221)
        user, _ = await get_or_create_user(s, telegram_id=991222)
        uid = user.id
        user.recap_credits = 0
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="M",
            source_audio_path="https://example.com/m",
            recap_config=None,  # legacy row: no blob at all
        ))
        await s.commit()
    api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)

    res = await _cancel_via_endpoint(maker, jid, admin)
    assert res["refund"]["refunded"] is True
    assert res["refund"]["recap_credits"] == 1  # historical behaviour kept
    async with maker() as s:
        user = await s.get(User, uid)
        assert user.recap_credits == 1
    await engine.dispose()


# ── 6. Queue worker: plan before claim ───────────────────────────────

@pytest.mark.asyncio
async def test_raising_plan_leaves_job_queued_and_next_poll_dispatches(
    tmp_path, monkeypatch
):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch, "worker.db")
    jid = "rc_worker_plan"
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=991301)
        uid = user.id
        req = api_mod.RecapConfigRequest(
            url="https://example.com/m", title="M", voice_slug="komin"
        )
        blob = api_mod._persisted_recap_state(
            req, credit_deducted=True, credits_charged=1,
            is_vip=False, phase="full",
        )
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="M",
            source_audio_path="https://example.com/m",
            recap_config=blob,
        ))
        await s.commit()

    real_plan = api_mod._recap_redispatch_plan
    gate = {"fail": True}

    def flaky_plan(job, user, **kwargs):
        if gate["fail"]:
            raise RuntimeError("corrupt persisted blob")
        return real_plan(job, user, **kwargs)

    dispatched = []

    async def fake_runner(job_id, *args, **kwargs):
        dispatched.append(job_id)

    monkeypatch.setattr(api_mod, "_recap_redispatch_plan", flaky_plan)
    monkeypatch.setattr(api_mod, "_run_recap_background_job", fake_runner)
    api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
    try:
        # Poll 1: the plan raises -> no claim, job stays QUEUED.
        assert await _dispatch_oldest_queued_recap() is False
        async with maker() as s:
            job = await s.get(Job, jid)
            assert job.status == JobStatus.QUEUED.value
        assert dispatched == []

        # Poll 2: the plan builds -> claimed (RUNNING) and dispatched.
        gate["fail"] = False
        assert await _dispatch_oldest_queued_recap() is True
        await asyncio.sleep(0.1)
        assert dispatched == [jid]
        async with maker() as s:
            job = await s.get(Job, jid)
            assert job.status == JobStatus.RUNNING.value
        assert api_mod.RECAP_JOBS_REGISTRY[jid]["status"] == "QUEUED"
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
        await engine.dispose()


# ── 7. Poisoned payment intent ───────────────────────────────────────

def _patch_payment_sessions(monkeypatch, db_session):
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(payment_mod, "AsyncSessionLocal", lambda: mock_session_ctx())


def _fake_photo_message(uid=992001, file_id="fid-slip-x"):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=uid, is_bot=False, first_name="Buyer", username="buyer")
    msg.photo = [SimpleNamespace(file_id=file_id)]
    msg.document = None
    msg.answer = AsyncMock()
    msg.bot = SimpleNamespace(
        send_photo=AsyncMock(), send_document=AsyncMock(), send_message=AsyncMock()
    )
    return msg


@pytest.mark.asyncio
async def test_poisoned_intent_is_cleared_and_user_is_told(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=992001)
    uid = user.id
    user_lang = user.language_code or "my"  # capture before any rollback expires it
    # A plan code that exists in neither the DB plans nor the catalogue.
    await save_payment_intent(db_session, uid, "GONE_404")
    await db_session.commit()
    _patch_payment_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", "")

    msg = _fake_photo_message(uid=992001)
    await handle_receipt_after_restart(msg)  # must not raise

    # The user got the restart-the-purchase reply, in their language.
    from app.services.i18n import t

    assert msg.answer.await_count == 1
    expected = t("payment_resume_failed", user_lang)
    assert msg.answer.await_args.args[0] == expected
    # The poison is gone: neither a live intent nor the row remains,
    # and no payment request was created.
    assert await get_recent_payment_intent(db_session, uid) is None
    assert await db_session.get(PaymentIntent, uid) is None
    reqs = (await db_session.execute(select(PaymentRequest))).scalars().all()
    assert reqs == []


@pytest.mark.asyncio
async def test_healthy_intent_still_resumes(db_session, monkeypatch):
    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=992002)
    await save_payment_intent(db_session, user.id, "MIN_10")
    await db_session.commit()
    _patch_payment_sessions(monkeypatch, db_session)
    monkeypatch.setattr(settings, "admin_ids_raw", "")

    msg = _fake_photo_message(uid=992002)
    await handle_receipt_after_restart(msg)

    reqs = (await db_session.execute(select(PaymentRequest))).scalars().all()
    assert len(reqs) == 1 and reqs[0].package_code == "MIN_10"
    assert await get_recent_payment_intent(db_session, user.id) is None
