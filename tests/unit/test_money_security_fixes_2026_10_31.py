"""Money/security fixes from the 2026-10-31 deep problem hunt.

F1  Mini App slip approval crashed (TypeError) for every minutes-bearing
    plan: the endpoint called ledger.credit_purchased_minutes() with
    keyword arguments it does not accept, so approvals from the Mini App
    NEVER credited the buyer. The endpoint (approve AND reject) now uses
    the same locked payments service as the bot path.

F2  Any PURCHASED user with 0 recap credits could run recaps free
    forever: the submit gate skipped VIPs and the deduction only ran
    when the balance was positive. Non-admins now need >= 1 credit and
    are always charged exactly 1 at submit.

F3  Recap config was never persisted, so a restart/deploy re-dispatch
    rebuilt jobs with default settings, lost the script-review phase,
    double-charged the per-part top-up and orphaned the submit credit's
    refund. The full config + credit state now lives on the Job row and
    re-dispatch restores it verbatim.

F4  WebApp initData never expired (no auth_date check): a leaked string
    was a forever-valid bearer credential. Older than 24h (or missing)
    is now rejected.

F7  /recap/frame had no ownership check: any signed-in user could pull
    frames from any file in storage. Files are now resolved to an owner
    (upload registry, owning job, job source) with owner-or-admin access
    and 404-style denial, mirroring the download endpoints.

F9  The media-retention sweep deleted by age alone, including QUEUED /
    RUNNING jobs. It now only sweeps terminal statuses.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory, JobStatus, JobType, PaymentStatus
from app.models.schema import Job, PaymentRequest, User
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.payments import create_payment_request
from app.webapp import api as api_mod


async def _make_user(db_session, telegram_id, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for key, value in fields.items():
        setattr(user, key, value)
    await db_session.commit()
    return user


# ── F1: Mini App slip approval / rejection ────────────────────────────────

@pytest.mark.asyncio
async def test_f1_approve_slip_minutes_plan_credits_exactly_once(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    buyer = await _make_user(db_session, 880101)
    admin = await _make_user(db_session, 880102)
    pay = await create_payment_request(
        session=db_session, user_id=buyer.id, package_code="MIN_10", receipt_file_id="slip-1"
    )
    await db_session.commit()

    resp = await api_mod.approve_payment_slip(
        api_mod.ApproveSlipRequest(request_id=pay.id), admin=admin, db=db_session
    )
    assert resp["success"] is True
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600  # 10 minutes, credited once
    stored = await db_session.get(PaymentRequest, pay.id)
    assert stored.status == PaymentStatus.APPROVED.value
    await db_session.refresh(buyer)
    assert buyer.account_category == AccountCategory.PURCHASED.value

    # Sequential double-approve: rejected as not-pending, no double credit.
    with pytest.raises(HTTPException) as exc:
        await api_mod.approve_payment_slip(
            api_mod.ApproveSlipRequest(request_id=pay.id), admin=admin, db=db_session
        )
    assert exc.value.status_code == 404
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 600


@pytest.mark.asyncio
async def test_f1_approve_slip_combined_plan_credits_minutes_and_recap(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    buyer = await _make_user(db_session, 880111)
    admin = await _make_user(db_session, 880112)
    pay = await create_payment_request(
        session=db_session, user_id=buyer.id, package_code="combined_starter", receipt_file_id="slip-2"
    )
    await db_session.commit()
    assert buyer.recap_credits == 3  # model default before approval

    resp = await api_mod.approve_payment_slip(
        api_mod.ApproveSlipRequest(request_id=pay.id), admin=admin, db=db_session
    )
    assert resp["success"] is True
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 30 * 60
    await db_session.refresh(buyer)
    assert buyer.recap_credits == 3 + 5
    assert buyer.account_category == AccountCategory.PURCHASED.value


@pytest.mark.asyncio
async def test_f1_approve_slip_recap_only_plan_still_works(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    buyer = await _make_user(db_session, 880121)
    admin = await _make_user(db_session, 880122)
    pay = await create_payment_request(
        session=db_session, user_id=buyer.id, package_code="recap_starter", receipt_file_id="slip-3"
    )
    await db_session.commit()
    assert pay.minutes == 0

    resp = await api_mod.approve_payment_slip(
        api_mod.ApproveSlipRequest(request_id=pay.id), admin=admin, db=db_session
    )
    assert resp["success"] is True
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 0  # no minutes on a recap-only plan
    await db_session.refresh(buyer)
    assert buyer.recap_credits == 3 + 5
    assert buyer.account_category == AccountCategory.PURCHASED.value
    stored = await db_session.get(PaymentRequest, pay.id)
    assert stored.status == PaymentStatus.APPROVED.value


@pytest.mark.asyncio
async def test_f1_reject_slip_uses_locked_service(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    buyer = await _make_user(db_session, 880131)
    admin = await _make_user(db_session, 880132)
    pay = await create_payment_request(
        session=db_session, user_id=buyer.id, package_code="MIN_30", receipt_file_id="bad-slip"
    )
    await db_session.commit()

    resp = await api_mod.reject_payment_slip(
        api_mod.RejectSlipRequest(request_id=pay.id, reason="ပြေစာ မမှန်ကန်ပါ"),
        admin=admin, db=db_session,
    )
    assert resp["success"] is True
    stored = await db_session.get(PaymentRequest, pay.id)
    assert stored.status == PaymentStatus.REJECTED.value
    assert stored.rejection_reason == "ပြေစာ မမှန်ကန်ပါ"
    bal = await get_minute_balance(db_session, buyer.id)
    assert bal.purchased_seconds == 0

    with pytest.raises(HTTPException) as exc:
        await api_mod.reject_payment_slip(
            api_mod.RejectSlipRequest(request_id=pay.id), admin=admin, db=db_session
        )
    assert exc.value.status_code == 404


# ── F2: recap credit gate for non-admins ─────────────────────────────────

@pytest.fixture
def recap_runner(monkeypatch):
    """Capture background-job dispatches instead of running the pipeline."""
    calls = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        calls.append({"job_id": job_id, "req": req, "user_id": user_id, "kwargs": kwargs})

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)
    yield calls
    api_mod.RECAP_JOBS_REGISTRY.clear()


def _recap_req(**over):
    base = {"url": "", "title": "Test Movie", "voice_slug": "komin", "duration_seconds": 60}
    base.update(over)
    return api_mod.RecapConfigRequest(**base)


@pytest.mark.asyncio
async def test_f2_purchased_user_with_zero_credits_is_rejected(db_session, recap_runner):
    user = await _make_user(
        db_session, 880201,
        account_category=AccountCategory.PURCHASED.value, recap_credits=0,
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_recap_job(_recap_req(), user=user, db=db_session, _gate=user)
    assert exc.value.status_code == 403
    assert recap_runner == []  # nothing was dispatched


@pytest.mark.asyncio
async def test_f2_purchased_user_with_one_credit_is_charged(db_session, recap_runner):
    user = await _make_user(
        db_session, 880202,
        account_category=AccountCategory.PURCHASED.value, recap_credits=1,
    )
    resp = await api_mod.create_recap_job(_recap_req(), user=user, db=db_session, _gate=user)
    await asyncio.sleep(0)
    assert resp["success"] is True
    assert resp["remaining_credits"] == 0
    await db_session.refresh(user)
    assert user.recap_credits == 0
    assert len(recap_runner) == 1
    assert recap_runner[0]["kwargs"]["credit_deducted"] is True


@pytest.mark.asyncio
async def test_f2_free_user_with_zero_credits_still_rejected(db_session, recap_runner):
    user = await _make_user(
        db_session, 880203,
        account_category=AccountCategory.FREE.value, recap_credits=0,
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_recap_job(_recap_req(), user=user, db=db_session, _gate=user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_f2_admin_is_never_charged(db_session, recap_runner, monkeypatch):
    admin = await _make_user(db_session, 880204, recap_credits=0)
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin.id))
    resp = await api_mod.create_recap_job(_recap_req(), user=admin, db=db_session, _gate=admin)
    await asyncio.sleep(0)
    assert resp["success"] is True
    await db_session.refresh(admin)
    assert admin.recap_credits == 0
    assert recap_runner[0]["kwargs"]["credit_deducted"] is False


# ── F3: recap config persistence & re-dispatch ────────────────────────────

def _persisted_job(job_id="rc_f3_a", user_id=880301, **state_over):
    cfg = api_mod.RecapConfigRequest(
        url="https://example.com/movie",
        title="Persisted Movie",
        voice_slug="mahnin",
        ratio="16:9",
        tts_engine="gemini",
        gemini_voice="Charon",
        split_video=True,
        script_review=True,
    )
    state = {
        "config": cfg.model_dump(),
        "credit_deducted": True,
        "credits_charged": 1,
        "is_vip": True,
        "phase": "script",
        "credits_estimated": None,
    }
    state.update(state_over)
    return Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Persisted Movie",
        source_audio_path="https://example.com/movie",
        recap_config=json.dumps(state),
    )


@pytest.mark.asyncio
async def test_f3_submit_persists_full_config_and_credit_state(db_session, recap_runner):
    user = await _make_user(
        db_session, 880302,
        account_category=AccountCategory.PURCHASED.value, recap_credits=5,
    )
    req = _recap_req(
        ratio="16:9", story_style="dramatic", tts_engine="gemini", gemini_voice="Charon",
        split_video=True, script_review=True, voice_slug="mahnin",
    )
    resp = await api_mod.create_recap_job(req, user=user, db=db_session, _gate=user)
    job = await db_session.get(Job, resp["recap_id"])
    assert job is not None and job.recap_config
    state = json.loads(job.recap_config)
    cfg = state["config"]
    assert cfg["ratio"] == "16:9"
    assert cfg["tts_engine"] == "gemini"
    assert cfg["gemini_voice"] == "Charon"
    assert cfg["split_video"] is True
    assert cfg["script_review"] is True
    assert cfg["voice_slug"] == "mahnin"
    assert state["phase"] == "script"
    assert state["credit_deducted"] is True
    assert state["credits_charged"] == 1
    assert state["is_vip"] is True


def test_f3_redispatch_restores_config_phase_and_credit_state():
    job = _persisted_job()
    plan = api_mod._recap_redispatch_plan(
        job, SimpleNamespace(account_category=AccountCategory.PURCHASED.value)
    )
    cfg = plan["cfg"]
    assert cfg.ratio == "16:9"
    assert cfg.tts_engine == "gemini"
    assert cfg.gemini_voice == "Charon"
    assert cfg.split_video is True
    assert cfg.script_review is True
    assert cfg.voice_slug == "mahnin"
    assert plan["phase"] == "script"
    assert plan["is_vip"] is True
    assert plan["credit_deducted"] is True
    entry = plan["entry"]
    assert entry["credits_charged"] == 1
    assert entry["credit_deducted"] is True
    assert entry["script_review"] is True
    assert entry["split_video"] is True


def test_f3_redispatch_legacy_row_falls_back_to_reconstruction():
    job = _persisted_job()
    job.recap_config = None
    plan = api_mod._recap_redispatch_plan(
        job, SimpleNamespace(account_category=AccountCategory.FREE.value)
    )
    assert plan["cfg"].ratio == "9:16"  # model default, as before persistence
    assert plan["cfg"].voice_slug == "mahnin"  # from the row's preset_voice
    assert plan["phase"] == "full"
    assert plan["credit_deducted"] is False
    assert plan["entry"]["credits_charged"] == 0


class _CreditStore:
    """Real-SQLite stand-in for the credit helpers' session factory.

    Since hardening F5 the helpers mutate balances with conditional
    SQL UPDATEs, so a get()/commit()-only dict fake can no longer
    stand in for a session. This handle points AsyncSessionLocal at a
    real file-backed SQLite database — one short-lived engine per
    session, so the asyncio.run() calls in these tests never share an
    engine across event loops — while every behavioural assertion in
    the tests below is unchanged.
    """

    def __init__(self, db_file: str):
        self._db_file = db_file

    def make_session(self):
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._db_file}", future=True
        )
        maker = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        return maker()

    def add_user(self, uid, credits):
        async def _seed():
            async with self.make_session() as s:
                s.add(User(id=uid, recap_credits=credits))
                await s.commit()

        asyncio.run(_seed())

    def credits(self, uid) -> int:
        async def _read():
            async with self.make_session() as s:
                user = await s.get(User, uid)
                return user.recap_credits if user else None

        return asyncio.run(_read())


@pytest.fixture
def fake_credit_store(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    db_file = str(tmp_path / "f3_credits.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    store = _CreditStore(db_file)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", store.make_session)
    monkeypatch.setattr(settings, "admin_ids_raw", "313131")
    return store


def test_f3_refund_after_redispatch_returns_submit_credit(fake_credit_store):
    uid = 880401
    fake_credit_store.add_user(uid, 4)  # submit already took 1 of 5
    plan = api_mod._recap_redispatch_plan(
        _persisted_job(user_id=uid), SimpleNamespace(account_category="PURCHASED")
    )
    assert asyncio.run(api_mod._refund_recap_job_charge(uid, plan["entry"])) is True
    assert fake_credit_store.credits(uid) == 5  # exactly whole again


def test_f3_topup_after_redispatch_charges_n_minus_1_total(fake_credit_store):
    uid = 880402
    fake_credit_store.add_user(uid, 4)  # submit already took 1 of 5
    plan = api_mod._recap_redispatch_plan(
        _persisted_job(user_id=uid), SimpleNamespace(account_category="PURCHASED")
    )
    entry = plan["entry"]
    total = asyncio.run(api_mod._top_up_recap_segment_charge(uid, entry, 3))
    assert total == 3
    assert entry["credits_charged"] == 3
    assert fake_credit_store.credits(uid) == 2  # only N-1 = 2 more were charged, not N


@pytest.mark.asyncio
async def test_f3_topup_persists_new_total_to_job_row(tmp_path, monkeypatch):
    import app.db.session as session_mod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/f3.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)

    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=880501)
        uid = user.id
        user.recap_credits = 4  # submit already took 1 of 5
        job = _persisted_job(user_id=uid)
        s.add(job)
        await s.commit()

    entry = api_mod._recap_redispatch_plan(job, user)["entry"]
    total = await api_mod._top_up_recap_segment_charge(uid, entry, 3)
    assert total == 3

    async with maker() as s:
        stored_user = await s.get(User, uid)
        assert stored_user.recap_credits == 2
        stored_job = await s.get(Job, job.id)
        state = json.loads(stored_job.recap_config)
        assert state["credits_charged"] == 3  # a later restart restores N, not 1
    await engine.dispose()


# ── F4: initData freshness ────────────────────────────────────────────────

_BOT_TOKEN = "123456:test-token-for-init-data"

def _signed_init_data(fields, token=_BOT_TOKEN):
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def _init_fields(auth_date="fresh"):
    fields = {"user": json.dumps({"id": 880601, "first_name": "T"}), "query_id": "AAE"}
    if auth_date == "fresh":
        fields["auth_date"] = str(int(time.time()))
    elif auth_date == "stale":
        fields["auth_date"] = str(int(time.time()) - 25 * 3600)
    return fields


def test_f4_fresh_init_data_verifies():
    from app.webapp.auth import parse_and_verify_init_data

    out = parse_and_verify_init_data(_signed_init_data(_init_fields("fresh")), bot_token=_BOT_TOKEN)
    assert out is not None
    assert out["id"] == 880601


def test_f4_stale_init_data_rejected():
    from app.webapp.auth import parse_and_verify_init_data

    out = parse_and_verify_init_data(_signed_init_data(_init_fields("stale")), bot_token=_BOT_TOKEN)
    assert out is None


def test_f4_missing_auth_date_rejected():
    from app.webapp.auth import parse_and_verify_init_data

    out = parse_and_verify_init_data(_signed_init_data(_init_fields("missing")), bot_token=_BOT_TOKEN)
    assert out is None


def test_f4_tampered_init_data_still_rejected():
    from app.webapp.auth import parse_and_verify_init_data

    forged = _signed_init_data(_init_fields("fresh"), token="999999:attacker-token")
    assert parse_and_verify_init_data(forged, bot_token=_BOT_TOKEN) is None


# ── F7: /recap/frame ownership ────────────────────────────────────────────

@pytest.fixture
def frame_storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    (root / "temp" / "recap_uploads").mkdir(parents=True)
    monkeypatch.setattr(settings, "storage_root", root)
    api_mod.UPLOADED_VIDEO_OWNERS.clear()
    yield root
    api_mod.UPLOADED_VIDEO_OWNERS.clear()


@pytest.mark.asyncio
async def test_f7_owner_can_view_own_upload_thumb(db_session, frame_storage):
    thumb = frame_storage / "temp" / "recap_uploads" / "up_abc1234567_thumb.jpg"
    thumb.write_bytes(b"\xff\xd8fake-jpeg")
    api_mod.UPLOADED_VIDEO_OWNERS[thumb.name] = 880701

    resp = await api_mod.get_recap_frame(
        path=thumb.name, t=None, user=SimpleNamespace(id=880701), db=db_session
    )
    assert Path(resp.path) == thumb


@pytest.mark.asyncio
async def test_f7_other_user_denied_upload_thumb(db_session, frame_storage):
    thumb = frame_storage / "temp" / "recap_uploads" / "up_abc1234567_thumb.jpg"
    thumb.write_bytes(b"\xff\xd8fake-jpeg")
    api_mod.UPLOADED_VIDEO_OWNERS[thumb.name] = 880701

    with pytest.raises(HTTPException) as exc:
        await api_mod.get_recap_frame(
            path=thumb.name, t=None, user=SimpleNamespace(id=880702), db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_f7_admin_can_view_any_upload(db_session, frame_storage, monkeypatch):
    thumb = frame_storage / "temp" / "recap_uploads" / "up_abc1234567_thumb.jpg"
    thumb.write_bytes(b"\xff\xd8fake-jpeg")
    api_mod.UPLOADED_VIDEO_OWNERS[thumb.name] = 880701
    monkeypatch.setattr(settings, "admin_ids_raw", "880799")

    resp = await api_mod.get_recap_frame(
        path=thumb.name, t=None, user=SimpleNamespace(id=880799), db=db_session
    )
    assert Path(resp.path) == thumb


@pytest.mark.asyncio
async def test_f7_ownership_resolves_from_job_source(db_session, frame_storage):
    video = frame_storage / "temp" / "recap_uploads" / "up_def7654321.mp4"
    video.write_bytes(b"video-bytes")
    # No in-memory owner record: ownership must come from the Job row.
    owner = await _make_user(db_session, 880703)
    db_session.add(Job(
        id="rc_f7_a", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap", source_audio_path=str(video),
    ))
    await db_session.commit()

    resp = await api_mod.get_recap_frame(
        path=str(video), t=None, user=SimpleNamespace(id=880703), db=db_session
    )
    assert Path(resp.path) == video

    with pytest.raises(HTTPException) as exc:
        await api_mod.get_recap_frame(
            path=str(video), t=None, user=SimpleNamespace(id=880704), db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_f7_other_user_denied_job_output_frame(db_session, frame_storage):
    out_dir = frame_storage / "recap_output" / "rc_f7_b"
    out_dir.mkdir(parents=True)
    frame = out_dir / "preview.jpg"
    frame.write_bytes(b"img")
    owner = await _make_user(db_session, 880705)
    db_session.add(Job(
        id="rc_f7_b", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
    ))
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await api_mod.get_recap_frame(
            path=str(frame), t=1.0, user=SimpleNamespace(id=880706), db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_f7_path_outside_storage_still_rejected(db_session, frame_storage, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_recap_frame(
            path=str(outside), t=1.0, user=SimpleNamespace(id=880707), db=db_session
        )
    assert exc.value.status_code == 403


# ── F9: retention sweep only touches terminal jobs ────────────────────────

@pytest.mark.asyncio
async def test_f9_sweep_never_deletes_active_jobs(tmp_path, monkeypatch):
    import app.db.session as session_mod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/f9.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)

    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    monkeypatch.setattr(settings, "storage_root", storage_root)

    now = datetime.datetime.now(datetime.timezone.utc)
    old = now - datetime.timedelta(days=8)
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=880801)
        uid = user.id
        for jid, status in [
            ("rc_f9_q", JobStatus.QUEUED.value),
            ("rc_f9_r", JobStatus.RUNNING.value),
            ("rc_f9_s", JobStatus.SUCCESS.value),
            ("rc_f9_f", JobStatus.FAILED.value),
        ]:
            out_file = storage_root / "recap_output" / jid / "final.mp4"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(b"\x00" * 100)
            s.add(Job(
                id=jid, user_id=uid, status=status, job_type=JobType.RECAP.value,
                engine="recap", result_path=str(out_file), created_at=old,
            ))
        await s.commit()

    deleted = await api_mod.cleanup_expired_jobs()
    assert deleted == 2  # SUCCESS + FAILED only

    async with maker() as s:
        remaining = {j.id for j in (await s.execute(select(Job))).scalars().all()}
    assert remaining == {"rc_f9_q", "rc_f9_r"}
    assert (storage_root / "recap_output" / "rc_f9_q" / "final.mp4").is_file()
    assert (storage_root / "recap_output" / "rc_f9_r" / "final.mp4").is_file()
    assert not (storage_root / "recap_output" / "rc_f9_s").exists()
    assert not (storage_root / "recap_output" / "rc_f9_f").exists()

    await engine.dispose()
