"""Batch L — recap FEATURES (2026-11-04).

The five approved features, each pinned at the level it lives:

1. one-tap retry of a FAILED recap job — a NEW job built from the
   stored config, submitted through the normal path (create_recap_job)
   so it is charged at the normal price exactly like a fresh submit
   (the failed job was already refunded); owner-only; an upload whose
   file is gone is refused with the honest re-upload explanation,
   while URL jobs are always retryable;
2. user cancel of their OWN queued / processing / SCRIPT_READY recap
   jobs — the SAME machinery the admin console uses
   (_cancel_recap_job_core): row flip, registry marker, refund through
   _refund_recap_job_charge; SCRIPT_READY discards the held script
   state like the 24 h expiry sweep does;
3. job-aware progress info — queue_position counted from the live
   queue (registry on the hot path, Job table on the cold path) and
   eta_seconds derived from the median started→finished duration of
   recent SUCCESS recap jobs, reported only with >= 3 real samples
   (position-only otherwise; no fabricated numbers);
4. History actions — the history payload carries the rebuildable
   recap_config for the "re-run with these settings" prefill, and an
   owner-only DELETE endpoint removes the record first and then only
   the job's exclusively-owned files (the shared source upload is
   deliberately kept for the retention sweep's orphan pass);
5. demo slot — the recap_demo_asset dynamic setting (Telegram file_id
   or URL) rides /api/config and /api/user/me, empty hides the slot,
   a URL is handed back for the client to open, and a file_id is sent
   to the user's own chat by the bot. No media asset ships with the
   feature; the owner configures it later.

Backend behaviour is tested live against the endpoint functions;
frontend behaviour is pinned with source-level assertions, as in
prior batches.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aiogram
import app.db.session as session_mod
import app.services.downloader as downloader_mod
import app.services.url_safety as url_safety_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import BotSetting, Job, User
from app.services.dynamic_settings import set_dynamic_setting
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
MESSAGES_MY = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
MESSAGES_EN = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))

OWNER_TG = 880401
OTHER_TG = 880402

NEW_KEYS = [
    "recap_queue_ahead", "recap_eta_dynamic", "recap_cancel_confirm",
    "recap_cancelled_toast", "recap_failed_title", "recap_retry_charge_note",
    "recap_retry_confirm", "recap_retry_upload_missing",
    "recap_retry_started_toast", "btn_rerun_recap",
    "recap_rerun_prefilled_toast", "btn_delete_history",
    "history_delete_confirm", "history_deleted_toast", "recap_demo_title",
    "recap_demo_text", "recap_demo_watch", "recap_demo_sent_toast",
]


@pytest.fixture(autouse=True)
def _clean_registry_and_eta_cache():
    before = set(api_mod.RECAP_JOBS_REGISTRY.keys())
    api_mod._RECAP_ETA_MEDIAN_CACHE.update({"at": 0.0, "seconds": None})
    yield
    for jid in set(api_mod.RECAP_JOBS_REGISTRY.keys()) - before:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
    api_mod._RECAP_ETA_MEDIAN_CACHE.update({"at": 0.0, "seconds": None})


def _recap_state(url="https://example.com/movie", **over):
    cfg = api_mod.RecapConfigRequest(
        url=url,
        title="Batch L Movie",
        voice_slug="mahnin",
        duration_seconds=60,
        ratio="9:16",
    )
    state = {
        "config": cfg.model_dump(),
        "credit_deducted": True,
        "credits_charged": 1,
        "is_vip": False,
        "phase": "full",
        "credits_estimated": None,
        "credit_refunded": True,
        "credits_refunded": 1,
    }
    state.update(over)
    return state


def _job(job_id, user_id, state, status=JobStatus.FAILED.value, **kw):
    job = Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Batch L Movie",
        source_audio_path=state["config"]["url"],
        recap_config=json.dumps(state),
        created_at=datetime.now(timezone.utc),
    )
    for key, value in kw.items():
        setattr(job, key, value)
    return job


async def _open_store(tmp_path, monkeypatch):
    """Point the API helpers' own session factory at a real file DB
    (same pattern as batch K: refund / persist / aggregate helpers open
    AsyncSessionLocal themselves)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_l.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


# ── (3) pure helpers: source availability + ETA math ─────────────────

def test_source_available_rules(tmp_path):
    assert api_mod._recap_source_available("https://example.com/v.mp4") is True
    assert api_mod._recap_source_available("http://example.com/v.mp4") is True
    assert api_mod._recap_source_available("") is False
    assert api_mod._recap_source_available(None) is False
    assert api_mod._recap_source_available("/no/such/up_dead.mp4") is False
    real = tmp_path / "up_real.mp4"
    real.write_bytes(b"x")
    assert api_mod._recap_source_available(str(real)) is True


def test_eta_seconds_math_is_grounded_in_the_median():
    # No samples -> no ETA at all (the payload degrades to position).
    assert api_mod._recap_eta_seconds(0, "QUEUED", 0, None) is None
    assert api_mod._recap_eta_seconds(2, "RUNNING", 40, None) is None
    # QUEUED: two renders drain in parallel; position 0 is first wave.
    assert api_mod._recap_eta_seconds(0, "QUEUED", 0, 600.0) == 600
    assert api_mod._recap_eta_seconds(2, "QUEUED", 0, 600.0) == 1200
    assert api_mod._recap_eta_seconds(4, "QUEUED", 0, 600.0) == 1800
    # RUNNING: the median scaled by the engine's remaining fraction.
    assert api_mod._recap_eta_seconds(0, "RUNNING", 50, 600.0) == 300
    assert api_mod._recap_eta_seconds(0, "RUNNING", None, 600.0) == 600
    # Terminal / unknown states never get an ETA.
    assert api_mod._recap_eta_seconds(0, "SUCCESS", 100, 600.0) is None


def test_registry_queue_position_counts_only_active_earlier_jobs():
    api_mod.RECAP_JOBS_REGISTRY.update({
        "rc_l_pos_a": {"user_id": 1, "status": "QUEUED", "created_at": 100.0},
        "rc_l_pos_b": {"user_id": 2, "status": "RUNNING", "created_at": 200.0},
        "rc_l_pos_old_failed": {"user_id": 1, "status": "FAILED", "created_at": 50.0},
        "rc_l_pos_later": {"user_id": 1, "status": "QUEUED", "created_at": 400.0},
    })
    entry = {"user_id": 1, "status": "QUEUED", "created_at": 300.0}
    assert api_mod._recap_registry_queue_position("rc_l_pos_t", entry) == 2
    # No trustworthy timestamp -> no position (never a guessed one).
    assert api_mod._recap_registry_queue_position("rc_l_pos_t", {}) is None


# ── (1) retry ────────────────────────────────────────────────────────

@pytest.fixture
def _no_network(monkeypatch):
    """create_recap_job unshortens + SSRF-validates http(s) sources
    through module-level imports; neutralise the network, and keep the
    background runner from starting."""
    monkeypatch.setattr(downloader_mod, "extract_first_url", lambda u: u)
    monkeypatch.setattr(downloader_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: None)

    async def _noop_runner(*args, **kwargs):
        return None

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _noop_runner)


@pytest.mark.asyncio
async def test_retry_creates_new_job_same_config_and_charges_anew(db_session, _no_network):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    owner.recap_credits = 2
    state = _recap_state()
    db_session.add(_job("rc_l_retry_src", owner.id, state))
    await db_session.commit()

    res = await api_mod.retry_recap_job(
        "rc_l_retry_src", user=owner, db=db_session, _gate=owner
    )
    assert res["success"] is True
    assert res["retried_from"] == "rc_l_retry_src"
    new_id = res["recap_id"]
    assert new_id != "rc_l_retry_src"
    # A retry is a NEW charge at the normal price (the failed job was
    # already refunded — the UI copy says exactly that).
    assert res["credits_charged"] == 1
    await db_session.refresh(owner)
    assert owner.recap_credits == 1

    new_job = await db_session.get(Job, new_id)
    assert new_job is not None
    assert new_job.status == JobStatus.QUEUED.value
    new_state = api_mod._load_persisted_recap_state(new_job)
    assert new_state["config"] == state["config"]
    # The failed job itself is untouched.
    old_job = await db_session.get(Job, "rc_l_retry_src")
    assert old_job.status == JobStatus.FAILED.value


@pytest.mark.asyncio
async def test_retry_forbidden_for_another_users_job(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    other, _ = await get_or_create_user(db_session, telegram_id=OTHER_TG)
    db_session.add(_job("rc_l_retry_foreign", owner.id, _recap_state()))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await api_mod.retry_recap_job(
            "rc_l_retry_foreign", user=other, db=db_session, _gate=other
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_retry_non_failed_job_rejected(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job(
        "rc_l_retry_done", owner.id, _recap_state(),
        status=JobStatus.SUCCESS.value,
    ))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await api_mod.retry_recap_job(
            "rc_l_retry_done", user=owner, db=db_session, _gate=owner
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_retry_upload_source_gone_is_refused_honestly(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    owner.recap_credits = 2
    gone = "/storage/temp/recap_uploads/up_gone1234.mp4"
    db_session.add(_job("rc_l_retry_gone", owner.id, _recap_state(url=gone)))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await api_mod.retry_recap_job(
            "rc_l_retry_gone", user=owner, db=db_session, _gate=owner
        )
    assert exc.value.status_code == 409
    assert "ပြန်တင်" in exc.value.detail  # says: upload it again
    # Nothing was charged for a job that cannot run.
    await db_session.refresh(owner)
    assert owner.recap_credits == 2


@pytest.mark.asyncio
async def test_retry_upload_source_present_succeeds(db_session, tmp_path, monkeypatch, _no_network):
    storage = tmp_path / "storage"
    upload = storage / "temp" / "recap_uploads" / "up_keep99.mp4"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"fake video bytes")
    monkeypatch.setattr(settings, "storage_root", storage)

    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    owner.recap_credits = 1
    db_session.add(_job(
        "rc_l_retry_upload", owner.id, _recap_state(url=str(upload))
    ))
    await db_session.commit()

    res = await api_mod.retry_recap_job(
        "rc_l_retry_upload", user=owner, db=db_session, _gate=owner
    )
    assert res["success"] is True
    new_job = await db_session.get(Job, res["recap_id"])
    new_state = api_mod._load_persisted_recap_state(new_job)
    assert new_state["config"]["url"] == str(upload)
    await db_session.refresh(owner)
    assert owner.recap_credits == 0


# ── (2) user cancel (+ the shared admin core) ────────────────────────

async def _seed_cancel_case(maker, *, jid, uid, job_status, entry_status,
                            script_ready=False, credits_charged=1):
    state = _recap_state(
        credit_refunded=False,
        credits_refunded=0,
        credits_charged=credits_charged,
        script_ready=script_ready,
        phase="render" if script_ready else "full",
    )
    async with maker() as s:
        s.add(User(id=uid, language_code="my", recap_credits=1))
        s.add(_job(jid, uid, state, status=job_status))
        await s.commit()
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "id": jid, "user_id": uid, "status": entry_status,
        "credit_deducted": True, "credits_charged": credits_charged,
        "credit_refunded": False, "credits_refunded": 0,
        "error_message": None, "created_at": time.time(),
    }


async def _balance_and_job(maker, uid, jid):
    async with maker() as s:
        user = await s.get(User, uid)
        job = await s.get(Job, jid)
        return (user.recap_credits if user else None), job


@pytest.mark.asyncio
async def test_cancel_queued_job_refunds_owner(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid, jid = 880411, "rc_l_cancel_q"
        await _seed_cancel_case(
            maker, jid=jid, uid=uid,
            job_status=JobStatus.QUEUED.value, entry_status="QUEUED",
        )
        async with maker() as s:
            res = await api_mod.cancel_recap_job(
                jid, user=User(id=uid), db=s
            )
        assert res["success"] is True
        assert res["credit_refunded"] is True
        assert res["credits_refunded"] == 1
        balance, job = await _balance_and_job(maker, uid, jid)
        assert balance == 2  # 1 remaining + 1 refunded
        assert job.status == JobStatus.CANCELLED.value
        assert job.error_message == "Cancelled by user via Mini App"
        entry = api_mod.RECAP_JOBS_REGISTRY[jid]
        assert entry["status"] == "FAILED"
        assert entry["error_message"] == "Cancelled by user"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_running_job_refunds(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid, jid = 880412, "rc_l_cancel_r"
        await _seed_cancel_case(
            maker, jid=jid, uid=uid,
            job_status=JobStatus.RUNNING.value, entry_status="RUNNING",
        )
        async with maker() as s:
            res = await api_mod.cancel_recap_job(jid, user=User(id=uid), db=s)
        assert res["credit_refunded"] is True
        balance, job = await _balance_and_job(maker, uid, jid)
        assert balance == 2
        assert job.status == JobStatus.CANCELLED.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_script_ready_discards_state_and_refunds(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid, jid = 880413, "rc_l_cancel_sr"
        await _seed_cancel_case(
            maker, jid=jid, uid=uid,
            job_status=JobStatus.RUNNING.value, entry_status="SCRIPT_READY",
            script_ready=True,
        )
        async with maker() as s:
            res = await api_mod.cancel_recap_job(jid, user=User(id=uid), db=s)
        assert res["credit_refunded"] is True
        balance, job = await _balance_and_job(maker, uid, jid)
        assert balance == 2
        assert job.status == JobStatus.CANCELLED.value
        # The held script state is discarded (expiry-sweep hygiene), so
        # the restart restore can never resurrect the parked job.
        state = api_mod._load_persisted_recap_state(job)
        assert state["script_ready"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_terminal_job_rejected(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job(
        "rc_l_cancel_done", owner.id, _recap_state(),
        status=JobStatus.SUCCESS.value,
    ))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await api_mod.cancel_recap_job(
            "rc_l_cancel_done", user=owner, db=db_session
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_cancel_is_owner_only(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid, jid = 880414, "rc_l_cancel_foreign"
        await _seed_cancel_case(
            maker, jid=jid, uid=uid,
            job_status=JobStatus.QUEUED.value, entry_status="QUEUED",
        )
        async with maker() as s:
            with pytest.raises(HTTPException) as exc:
                await api_mod.cancel_recap_job(
                    jid, user=User(id=OTHER_TG), db=s
                )
        assert exc.value.status_code == 404
        balance, job = await _balance_and_job(maker, uid, jid)
        assert balance == 1  # no refund leaked to a stranger's action
        assert job.status == JobStatus.QUEUED.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_cancel_recap_uses_the_same_core(tmp_path, monkeypatch):
    """The admin console endpoint was refactored onto the shared core:
    same row flip, registry marker, refund and audit behaviour."""
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid, jid = 880415, "rc_l_cancel_admin"
        await _seed_cancel_case(
            maker, jid=jid, uid=uid,
            job_status=JobStatus.QUEUED.value, entry_status="QUEUED",
        )
        async with maker() as s:
            res = await api_mod.cancel_queued_job(
                jid, User(id=999001), db=s
            )
        assert res["success"] is True
        assert res["refund"]["refunded"] is True
        assert res["refund"]["recap_credits"] == 1
        balance, job = await _balance_and_job(maker, uid, jid)
        assert balance == 2
        assert job.status == JobStatus.CANCELLED.value
        assert job.error_message == "Cancelled by Administrator via Admin Console"
        entry = api_mod.RECAP_JOBS_REGISTRY[jid]
        assert entry["error_message"] == "Cancelled by Administrator"
    finally:
        await engine.dispose()


# ── (3) status payload: queue position + ETA ─────────────────────────

async def _seed_success_durations(maker, uid, durations=(600, 600, 600)):
    base = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    async with maker() as s:
        if await s.get(User, uid) is None:
            s.add(User(id=uid, language_code="my", recap_credits=0))
        for i, secs in enumerate(durations):
            s.add(Job(
                id=f"rc_l_done_{uid}_{i}", user_id=uid, chat_id=uid,
                status=JobStatus.SUCCESS.value,
                job_type=JobType.RECAP.value, engine="recap",
                preset_voice="mahnin", input_text="done",
                created_at=base + timedelta(minutes=i * 30),
                started_at=base + timedelta(minutes=i * 30),
                finished_at=base + timedelta(minutes=i * 30, seconds=secs),
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_status_registry_branch_position_and_eta(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880421
        await _seed_success_durations(maker, uid)
        api_mod.RECAP_JOBS_REGISTRY.update({
            "rc_l_q_a": {"user_id": 1, "status": "QUEUED", "created_at": 100.0},
            "rc_l_q_b": {"user_id": 2, "status": "RUNNING", "created_at": 200.0},
            "rc_l_q_t": {"user_id": uid, "status": "QUEUED", "created_at": 300.0},
        })
        async with maker() as s:
            res = await api_mod.get_recap_status(
                "rc_l_q_t", user=User(id=uid), db=s
            )
        assert res["queue_position"] == 2
        # Median 600 s; two ahead drain two-at-a-time -> 2 waves.
        assert res["eta_seconds"] == 1200
        assert res["can_retry"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_position_without_samples_has_no_eta(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880422
        async with maker() as s:
            s.add(User(id=uid, language_code="my", recap_credits=0))
            await s.commit()
        api_mod.RECAP_JOBS_REGISTRY.update({
            "rc_l_q2_a": {"user_id": 1, "status": "RUNNING", "created_at": 100.0},
            "rc_l_q2_t": {"user_id": uid, "status": "QUEUED", "created_at": 300.0},
        })
        async with maker() as s:
            res = await api_mod.get_recap_status(
                "rc_l_q2_t", user=User(id=uid), db=s
            )
        # Position is countable; with < 3 completed samples there is
        # no honest ETA, so the field is null — not an invented number.
        assert res["queue_position"] == 1
        assert res["eta_seconds"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_db_branch_position_and_eta(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880423
        await _seed_success_durations(maker, uid)
        base = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
        async with maker() as s:
            for i, jid in enumerate(("rc_l_dq_a", "rc_l_dq_b", "rc_l_dq_t")):
                s.add(_job(
                    jid, uid, _recap_state(),
                    status=JobStatus.QUEUED.value,
                    created_at=base + timedelta(minutes=i * 10),
                ))
            await s.commit()
        async with maker() as s:
            res = await api_mod.get_recap_status(
                "rc_l_dq_t", user=User(id=uid), db=s
            )
        assert res["queue_position"] == 2
        assert res["eta_seconds"] == 1200
        assert res["can_retry"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_db_branch_failed_can_retry_flag(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job("rc_l_st_retry", owner.id, _recap_state()))
    db_session.add(_job(
        "rc_l_st_gone", owner.id,
        _recap_state(url="/storage/temp/recap_uploads/up_gone.mp4"),
    ))
    await db_session.commit()
    res_ok = await api_mod.get_recap_status(
        "rc_l_st_retry", user=owner, db=db_session
    )
    assert res_ok["can_retry"] is True
    assert res_ok["queue_position"] is None
    assert res_ok["eta_seconds"] is None
    res_gone = await api_mod.get_recap_status(
        "rc_l_st_gone", user=owner, db=db_session
    )
    assert res_gone["can_retry"] is False


# ── (4) history: config payload + delete ─────────────────────────────

@pytest.mark.asyncio
async def test_history_carries_recap_config_and_retry_flags(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    db_session.add(_job("rc_l_hist_url", owner.id, _recap_state()))
    db_session.add(_job(
        "rc_l_hist_gone", owner.id,
        _recap_state(url="/storage/temp/recap_uploads/up_gone.mp4"),
    ))
    db_session.add(_job(
        "rc_l_hist_ok", owner.id, _recap_state(),
        status=JobStatus.SUCCESS.value,
    ))
    await db_session.commit()
    res = await api_mod.get_user_history(user=owner, db=db_session)
    items = {item["id"]: item for item in res["history"]}

    url_item = items["rc_l_hist_url"]
    assert url_item["recap_config"]["url"] == "https://example.com/movie"
    assert url_item["recap_config"]["ratio"] == "9:16"
    assert url_item["source_available"] is True
    assert url_item["can_retry"] is True

    gone_item = items["rc_l_hist_gone"]
    assert gone_item["recap_config"] is not None
    assert gone_item["source_available"] is False
    assert gone_item["can_retry"] is False

    ok_item = items["rc_l_hist_ok"]
    assert ok_item["recap_config"] is not None
    assert ok_item["can_retry"] is False  # not FAILED -> no retry offer


@pytest.mark.asyncio
async def test_history_delete_owner_only_and_active_guard(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    other, _ = await get_or_create_user(db_session, telegram_id=OTHER_TG)
    db_session.add(_job(
        "rc_l_del_done", owner.id, _recap_state(),
        status=JobStatus.SUCCESS.value,
    ))
    db_session.add(_job(
        "rc_l_del_active", owner.id, _recap_state(),
        status=JobStatus.QUEUED.value,
    ))
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await api_mod.delete_history_item(
            "rc_l_del_done", user=other, db=db_session
        )
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await api_mod.delete_history_item(
            "rc_l_del_active", user=owner, db=db_session
        )
    assert exc.value.status_code == 409

    res = await api_mod.delete_history_item(
        "rc_l_del_done", user=owner, db=db_session
    )
    assert res["success"] is True
    assert await db_session.get(Job, "rc_l_del_done") is None


@pytest.mark.asyncio
async def test_history_delete_removes_record_and_own_files_only(
    db_session, tmp_path, monkeypatch
):
    storage = tmp_path / "storage"
    out_dir = storage / "recap_output" / "rc_l_del_files"
    out_dir.mkdir(parents=True)
    result_file = out_dir / "final.mp4"
    result_file.write_bytes(b"video")
    upload = storage / "temp" / "recap_uploads" / "up_shared.mp4"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"shared upload")
    monkeypatch.setattr(settings, "storage_root", storage)

    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    job = _job(
        "rc_l_del_files", owner.id,
        _recap_state(url=str(upload)),
        status=JobStatus.SUCCESS.value,
    )
    job.result_path = str(result_file)
    db_session.add(job)
    await db_session.commit()

    res = await api_mod.delete_history_item(
        "rc_l_del_files", user=owner, db=db_session
    )
    assert res["success"] is True
    assert await db_session.get(Job, "rc_l_del_files") is None
    # The job's own output directory is gone...
    assert not out_dir.exists()
    # ...but the source upload is NOT deleted: it can be shared with
    # other jobs, and the retention sweep's orphan pass reclaims it
    # once no surviving row references it.
    assert upload.exists()


# ── (5) demo slot ────────────────────────────────────────────────────

class _FakeBotSession:
    async def close(self):
        pass


class _FakeDemoBot:
    sent_videos: list = []
    sent_docs: list = []

    def __init__(self, *args, **kwargs):
        self.session = _FakeBotSession()

    async def send_video(self, chat_id=None, video=None, **kwargs):
        _FakeDemoBot.sent_videos.append((chat_id, video))

    async def send_document(self, chat_id=None, document=None, **kwargs):
        _FakeDemoBot.sent_docs.append((chat_id, document))


@pytest.mark.asyncio
async def test_demo_slot_hidden_when_unset(db_session, monkeypatch):
    monkeypatch.setattr(settings, "recap_demo_asset", "")
    cfg = await api_mod.get_public_app_config(db=db_session)
    assert cfg["recap_demo_asset"] == ""


@pytest.mark.asyncio
async def test_demo_asset_setting_roundtrip(db_session):
    old = settings.recap_demo_asset
    try:
        await set_dynamic_setting(
            db_session, "recap_demo_asset", "https://example.com/demo.mp4"
        )
        # Writable through the dynamic-settings registry (not a secret,
        # not rejected) and applied in-memory like every other setting.
        assert settings.recap_demo_asset == "https://example.com/demo.mp4"
        cfg = await api_mod.get_public_app_config(db=db_session)
        assert cfg["recap_demo_asset"] == "https://example.com/demo.mp4"
        admin_view = await api_mod.get_admin_settings(
            admin=User(id=999002), db=db_session
        )
        assert admin_view["recap_demo_asset"] == "https://example.com/demo.mp4"
    finally:
        settings.recap_demo_asset = old


def test_demo_asset_key_is_not_treated_as_a_telegram_link():
    # The settings validator forces every *_link value to be a t.me
    # URL; the demo key deliberately avoids that suffix so a Telegram
    # file_id — the primary asset form — validates cleanly.
    cleaned = api_mod._validate_admin_settings_updates(
        {"recap_demo_asset": "BAADBQAD_demo_file_id"}
    )
    assert cleaned["recap_demo_asset"] == "BAADBQAD_demo_file_id"
    assert "recap_demo_asset" in api_mod.AdminSettingsPayload.model_fields


@pytest.mark.asyncio
async def test_demo_send_refused_when_unset(db_session, monkeypatch):
    monkeypatch.setattr(settings, "recap_demo_asset", "")
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    with pytest.raises(HTTPException) as exc:
        await api_mod.send_recap_demo(user=owner, db=db_session, _gate=owner)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_demo_send_url_asset_returns_url(db_session):
    db_session.add(BotSetting(
        key="recap_demo_asset", value="https://example.com/demo.mp4"
    ))
    await db_session.commit()
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    res = await api_mod.send_recap_demo(user=owner, db=db_session, _gate=owner)
    assert res == {"success": True, "url": "https://example.com/demo.mp4"}


@pytest.mark.asyncio
async def test_demo_send_file_id_goes_through_the_bot(db_session, monkeypatch):
    _FakeDemoBot.sent_videos = []
    _FakeDemoBot.sent_docs = []
    monkeypatch.setattr(aiogram, "Bot", _FakeDemoBot)
    monkeypatch.setattr(settings, "bot_token", "123456:batch-l-test")
    db_session.add(BotSetting(key="recap_demo_asset", value="DEMO_FILE_ID"))
    await db_session.commit()
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    res = await api_mod.send_recap_demo(user=owner, db=db_session, _gate=owner)
    assert res == {"success": True, "sent": True}
    assert _FakeDemoBot.sent_videos == [(owner.id, "DEMO_FILE_ID")]
    assert _FakeDemoBot.sent_docs == []


# ── i18n: every new string in all four files ─────────────────────────

def _i18n_js_keys(lang: str) -> set:
    marker = "    " + lang + ": {"
    start = I18N_JS.index(marker) + len(marker)
    end = I18N_JS.index("\n    }", start)
    return set(re.findall(r"(?m)^        (\w+):", I18N_JS[start:end]))


def test_batch_l_keys_exist_in_all_four_i18n_files():
    my_js, en_js = _i18n_js_keys("my"), _i18n_js_keys("en")
    for key in NEW_KEYS:
        assert key in my_js, f"i18n.js my missing {key}"
        assert key in en_js, f"i18n.js en missing {key}"
        assert key in MESSAGES_MY, f"messages my missing {key}"
        assert key in MESSAGES_EN, f"messages en missing {key}"
    # The pre-existing button keys batch L wires up live in i18n.js
    # (both languages) — they predate the batch and are UI-only.
    for key in ("btn_retry_job", "btn_cancel_job"):
        assert key in my_js and key in en_js


def test_i18n_key_sets_stay_symmetric():
    assert _i18n_js_keys("my") == _i18n_js_keys("en")
    assert set(MESSAGES_MY) == set(MESSAGES_EN)


# ── frontend / route pins ────────────────────────────────────────────

def test_batch_l_frontend_wiring_pins():
    for element_id in (
        "recap-demo-card", "btn-recap-demo",
        "recap-failed-box", "recap-failed-error", "btn-recap-retry",
        "recap-retry-note", "recap-retry-upload-missing",
        "btn-recap-cancel", "btn-script-cancel", "adm-recap-demo-asset",
    ):
        assert f'id="{element_id}"' in INDEX_HTML, element_id
    # Static assets moved to the batch's cache version.
    assert "20261104_r61" in INDEX_HTML
    assert "20261103_r43" not in INDEX_HTML

    # Poller renders the server's job-aware position/ETA.
    assert "queue_position" in APP_JS
    assert "eta_seconds" in APP_JS
    assert 'tf("recap_queue_ahead"' in APP_JS
    assert 'tf("recap_eta_dynamic"' in APP_JS
    # Action helpers call the batch L endpoints.
    assert "/cancel`" in APP_JS and "cancelRecapJobById" in APP_JS
    assert "/retry`" in APP_JS and "retryRecapJobById" in APP_JS
    assert "/api/recap/demo/send" in APP_JS
    assert "/api/history/${jobId}" in APP_JS
    assert 'method: "DELETE"' in APP_JS
    # History actions + recap-tab prefill (never auto-submits).
    assert "window.prefillRecapFromHistory" in APP_JS
    assert "window.rerunRecapFromHistory" in APP_JS
    assert "window.deleteHistoryItem" in APP_JS
    assert "recap_config" in APP_JS
    # Failed card is shown from the poller's FAILED branch.
    assert 'getElementById("recap-failed-box")' in APP_JS
    assert "can_retry" in APP_JS
    # Demo visibility is driven by the /me payload field.
    assert "recap_demo_asset" in APP_JS
    assert "watchRecapDemo" in APP_JS


def test_batch_l_routes_and_shared_cancel_core_pins():
    assert '@api_router.post("/recap/{job_id}/retry")' in API_SRC
    assert '@api_router.post("/recap/{job_id}/cancel")' in API_SRC
    assert '@api_router.post("/recap/demo/send")' in API_SRC
    assert '@api_router.delete("/history/{job_id}")' in API_SRC
    # One cancel machinery: the core is defined once and called by
    # BOTH the admin endpoint and the user endpoint.
    assert API_SRC.count("_cancel_recap_job_core") >= 3
    # Retry goes through the normal submission path (new charge).
    assert "create_recap_job(cfg, user=user, db=db, _gate=user)" in API_SRC
    # Status + history payloads carry the batch L fields.
    assert '"queue_position"' in API_SRC
    assert '"eta_seconds"' in API_SRC
    assert '"can_retry"' in API_SRC
    assert '"recap_config": _hist_cfg' in API_SRC
    assert '"recap_demo_asset"' in API_SRC
