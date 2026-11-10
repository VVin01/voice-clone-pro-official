"""Batch M — final-audit fixes (2026-11-04).

One test group per audit finding:

1. S-1 (deploy blocker): the recap status cold path crashed with
   UnboundLocalError — a branch-local `AsyncSessionLocal` import made
   the name function-local, so every job missing from the registry
   (every job after a restart) got HTTP 500. Pinned by calling the
   handler the way production does: db NOT injected (db=None), empty
   registry, job present only in the DB.
2. Refund race (backend P1-1): the refund helper checked the
   credit_refunded flag, awaited, then set it — two concurrent
   callers refunded twice. Claim-before-await, rollback on failure,
   and the SCRIPT_READY expiry sweep claims through the persisted
   blob before issuing its refund.
3. Reaper (backend P1-2): the voice stale-job reaper failed recap
   rows (incl. parked SCRIPT_READY jobs) and kept their credits,
   because recap charges write no minute-ledger events. Recap rows
   are now excluded; voice rows are still recovered + refunded.
4. Unbounded ffmpeg (engine P1/P2): timeouts on the downloader's
   local-media calls, the Groq pre-compression passes (pinned at
   source level) and the audio normalize subprocess.
5. Poller CANCELLED branch (frontend P1): a job cancelled on another
   surface left this client polling to the 60-minute cap.
6. Group-AI limits write path (security P2-1): payload + GET +
   validation + admin UI fields; a POST changes what get_limits
   returns.
7. History "8500%" (visual MINOR-1): persisted sub_y/blur_y are
   integer percents; the renderer multiplied by 100 again.
8. i18n small items + cache r45: localized Step label, demo-asset
   label keys, localized fallback errors, cache-bust bump.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_mod
import app.services.audio as audio_mod
import app.services.downloader as dl_mod
import app.services.group_ai_admin as group_ai_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType, LedgerDirection
from app.models.schema import AuditLog, Job, MinuteLedgerEvent, User
from app.services.ledger import consume_seconds, get_minute_balance, get_or_create_user
from app.services.recovery import recover_stale_running_jobs
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

OWNER_TG = 880501

NEW_I18N_KEYS = [
    "voice_cancelled_toast", "recap_step_label",
    "settings_group_ai_cooldown", "settings_group_ai_cooldown_hint",
    "settings_group_ai_hourly_budget", "settings_group_ai_hourly_budget_hint",
    "settings_group_ai_daily_budget", "settings_group_ai_daily_budget_hint",
    "adm_recap_demo_asset_label", "adm_recap_demo_asset_ph",
]


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(api_mod.RECAP_JOBS_REGISTRY.keys())
    api_mod._RECAP_ETA_MEDIAN_CACHE.update({"at": 0.0, "seconds": None})
    yield
    for jid in set(api_mod.RECAP_JOBS_REGISTRY.keys()) - before:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
    api_mod._RECAP_ETA_MEDIAN_CACHE.update({"at": 0.0, "seconds": None})


async def _open_store(tmp_path, monkeypatch):
    """Point the API helpers' session factory at a real file DB
    (the batch K/L pattern: helpers open AsyncSessionLocal themselves)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_m.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


def _recap_state(**over):
    cfg = api_mod.RecapConfigRequest(
        url="https://example.com/movie",
        title="Batch M Movie",
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
        "credit_refunded": False,
        "credits_refunded": 0,
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
        input_text="Batch M Movie",
        source_audio_path=(state or {}).get("config", {}).get("url"),
        recap_config=json.dumps(state) if state is not None else None,
        created_at=datetime.now(timezone.utc),
    )
    for key, value in kw.items():
        setattr(job, key, value)
    return job


# ── (1) S-1: status cold path with db=None (production shape) ───────

def test_status_handler_import_is_unconditional():
    fn_src = API_SRC.split("async def get_recap_status")[1].split("\nasync def ")[0]
    # The import sits at the top of the function, before any branch…
    assert fn_src.index("from app.db.session import AsyncSessionLocal") < fn_src.index(
        "entry = RECAP_JOBS_REGISTRY.get(job_id)"
    )
    # …and the old branch-local import inside the FAILED try is gone.
    assert "try:\n                from app.db.session import AsyncSessionLocal" not in fn_src


@pytest.mark.asyncio
async def test_status_cold_path_queued_db_none(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = OWNER_TG
        base = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
        async with maker() as s:
            for i, jid in enumerate(("rc_m_q_a", "rc_m_q_b", "rc_m_q_t")):
                s.add(_job(
                    jid, uid, _recap_state(),
                    status=JobStatus.QUEUED.value,
                    created_at=base + timedelta(minutes=i * 10),
                ))
            await s.commit()
        assert "rc_m_q_t" not in api_mod.RECAP_JOBS_REGISTRY
        res = await api_mod.get_recap_status("rc_m_q_t", user=User(id=uid), db=None)
        assert res["status"] == JobStatus.QUEUED.value
        assert res["queue_position"] == 2
        # No completed-job samples -> no fabricated ETA.
        assert res["eta_seconds"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_cold_path_success_db_none(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = OWNER_TG
        async with maker() as s:
            s.add(_job("rc_m_ok", uid, _recap_state(), status=JobStatus.SUCCESS.value))
            await s.commit()
        res = await api_mod.get_recap_status("rc_m_ok", user=User(id=uid), db=None)
        assert res["status"] == JobStatus.SUCCESS.value
        # No result file on disk -> honestly reported expired.
        assert res["is_expired"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_cold_path_failed_db_none(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = OWNER_TG
        async with maker() as s:
            s.add(_job("rc_m_fail", uid, _recap_state(), status=JobStatus.FAILED.value))
            await s.commit()
        res = await api_mod.get_recap_status("rc_m_fail", user=User(id=uid), db=None)
        assert res["status"] == JobStatus.FAILED.value
        assert res["can_retry"] is True
    finally:
        await engine.dispose()


# ── (2) refund race: claim-before-await ─────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_refund_calls_refund_exactly_once(monkeypatch):
    calls: list[int] = []

    async def fake_refund(user_id, amount, *, job_id=None):
        calls.append(amount)
        await asyncio.sleep(0.05)  # force the interleave the bug needed
        return True

    async def fake_persist(entry):
        return None

    monkeypatch.setattr(api_mod, "_refund_recap_credits", fake_refund)
    monkeypatch.setattr(api_mod, "_persist_recap_credits_charged", fake_persist)
    entry = {
        "id": "rc_m_race", "user_id": OWNER_TG,
        "credits_charged": 3, "credit_deducted": True,
        "credit_refunded": False, "credits_refunded": 0,
    }
    results = await asyncio.gather(
        api_mod._refund_recap_job_charge(OWNER_TG, entry),
        api_mod._refund_recap_job_charge(OWNER_TG, entry),
    )
    assert calls == [3]  # exactly ONE balance refund of the full charge
    assert sorted(results) == [False, True]
    assert entry["credit_refunded"] is True
    assert entry["credits_refunded"] == 3


@pytest.mark.asyncio
async def test_refund_claim_rolls_back_on_failure(monkeypatch):
    attempts = {"n": 0}

    async def flaky_refund(user_id, amount, *, job_id=None):
        attempts["n"] += 1
        return attempts["n"] > 1

    async def fake_persist(entry):
        return None

    monkeypatch.setattr(api_mod, "_refund_recap_credits", flaky_refund)
    monkeypatch.setattr(api_mod, "_persist_recap_credits_charged", fake_persist)
    entry = {
        "id": "rc_m_flaky", "user_id": OWNER_TG,
        "credits_charged": 1, "credit_deducted": True,
        "credit_refunded": False, "credits_refunded": 0,
    }
    assert await api_mod._refund_recap_job_charge(OWNER_TG, entry) is False
    # Claim released, so a later retry can still refund.
    assert entry["credit_refunded"] is False
    assert entry["credits_refunded"] == 0
    assert await api_mod._refund_recap_job_charge(OWNER_TG, entry) is True
    assert entry["credit_refunded"] is True


def _parked_state(hours_ago=25, **over):
    parked_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    base = _recap_state(
        phase="render",
        script_ready=True,
        script_ready_at=parked_at.isoformat(),
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_sweep_claims_persisted_refund_before_issuing(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = OWNER_TG
        async with maker() as s:
            s.add(User(id=uid, recap_credits=5))
            s.add(_job(
                "rc_m_sweep", uid, _parked_state(),
                status=JobStatus.RUNNING.value,
            ))
            await s.commit()

        seen: dict = {}

        async def fake_refund(user_id, amount, *, job_id=None):
            # At the moment the balance refund is issued, the claim
            # must ALREADY be persisted on the Job row.
            async with maker() as s:
                job = await s.get(Job, "rc_m_sweep")
                state = json.loads(job.recap_config)
                seen["claimed_at_refund"] = state.get("credit_refunded")
                seen["amount"] = amount
            return True

        async def fake_dm(**kwargs):
            seen["dm"] = True

        monkeypatch.setattr(api_mod, "_refund_recap_credits", fake_refund)
        monkeypatch.setattr(api_mod, "_send_recap_failure_dm", fake_dm)

        assert "rc_m_sweep" not in api_mod.RECAP_JOBS_REGISTRY
        expired = await api_mod.expire_abandoned_script_ready_jobs()
        assert expired == 1
        assert seen["claimed_at_refund"] is True
        assert seen["amount"] == 1

        # A second sweep run is a no-op: no second refund, no re-expire.
        expired2 = await api_mod.expire_abandoned_script_ready_jobs()
        assert expired2 == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_skips_refund_when_blob_already_refunded(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = OWNER_TG
        async with maker() as s:
            s.add(User(id=uid, recap_credits=5))
            s.add(_job(
                "rc_m_sweep_done", uid,
                _parked_state(credit_refunded=True, credits_refunded=1),
                status=JobStatus.RUNNING.value,
            ))
            await s.commit()

        calls: list[int] = []

        async def fake_refund(user_id, amount, *, job_id=None):
            calls.append(amount)
            return True

        async def fake_dm(**kwargs):
            return None

        monkeypatch.setattr(api_mod, "_refund_recap_credits", fake_refund)
        monkeypatch.setattr(api_mod, "_send_recap_failure_dm", fake_dm)

        expired = await api_mod.expire_abandoned_script_ready_jobs()
        assert expired == 1  # the job still expires…
        assert calls == []   # …but the proven refund is never doubled
        async with maker() as s:
            job = await s.get(Job, "rc_m_sweep_done")
            assert job.status == JobStatus.FAILED.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_with_live_entry_refunds_via_shared_entry(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = OWNER_TG
        async with maker() as s:
            s.add(User(id=uid, recap_credits=5))
            s.add(_job(
                "rc_m_sweep_live", uid, _parked_state(),
                status=JobStatus.RUNNING.value,
            ))
            await s.commit()

        calls: list[int] = []

        async def fake_refund(user_id, amount, *, job_id=None):
            calls.append(amount)
            await asyncio.sleep(0.01)
            return True

        async def fake_dm(**kwargs):
            return None

        monkeypatch.setattr(api_mod, "_refund_recap_credits", fake_refund)
        monkeypatch.setattr(api_mod, "_send_recap_failure_dm", fake_dm)

        live_entry = {
            "id": "rc_m_sweep_live", "user_id": uid,
            "credits_charged": 1, "credit_deducted": True,
            "credit_refunded": False, "credits_refunded": 0,
            "status": "SCRIPT_READY",
        }
        api_mod.RECAP_JOBS_REGISTRY["rc_m_sweep_live"] = live_entry
        expired = await api_mod.expire_abandoned_script_ready_jobs()
        assert expired == 1
        assert calls == [1]
        # The shared entry now carries the claim, so a racing caller
        # using the same entry can never refund a second time.
        assert live_entry["credit_refunded"] is True
        assert await api_mod._refund_recap_job_charge(uid, live_entry) is False
        assert calls == [1]
    finally:
        await engine.dispose()


# ── (3) reaper excludes recap rows ──────────────────────────────────

@pytest.mark.asyncio
async def test_reaper_leaves_stale_recap_rows_untouched(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=880511)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    parked = _parked_state()
    rows = [
        # Parked SCRIPT_READY recap job (RUNNING for up to 24h by design)
        _job("rc_m_parked", user.id, parked,
             status=JobStatus.RUNNING.value, started_at=stale),
        # Recap by type alone (no rc_ prefix, worker engine string)
        _job("job_m_typed", user.id, parked,
             status=JobStatus.RUNNING.value, started_at=stale, engine="voxcpm"),
        # rc_-prefixed id alone
        _job("rc_m_prefixed", user.id, None,
             status=JobStatus.RUNNING.value, started_at=stale,
             job_type="voice", engine="voxcpm"),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    recovered = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert recovered == []
    for row in rows:
        await db_session.refresh(row)
        assert row.status == JobStatus.RUNNING.value
        assert row.finished_at is None


@pytest.mark.asyncio
async def test_reaper_still_recovers_stale_voice_job_with_refund(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=880512)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = 300
    job_id = "voice-m-stale-1"
    db_session.add(Job(
        id=job_id,
        user_id=user.id,
        status=JobStatus.RUNNING.value,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=800),
    ))
    await db_session.commit()
    await consume_seconds(db_session, user.id, seconds_to_consume=75, job_id=job_id)

    recovered = await recover_stale_running_jobs(db_session, stale_timeout_seconds=600)
    assert recovered == [job_id]
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.welcome_seconds == 300
    refund_events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user.id,
                MinuteLedgerEvent.reference_id == f"REFUND:{job_id}",
                MinuteLedgerEvent.direction == LedgerDirection.CREDIT.value,
            )
        )
    ).scalars().all()
    assert len(refund_events) == 1
    assert refund_events[0].seconds == 75


# ── (4) bounded ffmpeg ──────────────────────────────────────────────

def _record_run(monkeypatch, behaviour):
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "kwargs": kwargs})
        return behaviour(cmd, kwargs)

    monkeypatch.setattr(dl_mod.subprocess, "run", fake_run)
    return calls


def test_thumbnail_call_is_time_bounded(monkeypatch, tmp_path):
    def ok(cmd, kwargs):
        return SimpleNamespace(returncode=0)

    calls = _record_run(monkeypatch, ok)
    out = dl_mod.extract_thumbnail_from_media(
        str(tmp_path / "in.mp4"), str(tmp_path / "thumb.jpg")
    )
    assert out is None  # fake ffmpeg wrote no file
    assert calls[0]["kwargs"].get("timeout") == dl_mod.THUMBNAIL_TIMEOUT_SECONDS


def test_thumbnail_timeout_fails_soft(monkeypatch, tmp_path):
    def boom(cmd, kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)

    _record_run(monkeypatch, boom)
    out = dl_mod.extract_thumbnail_from_media(
        str(tmp_path / "in.mp4"), str(tmp_path / "thumb.jpg")
    )
    assert out is None  # through the existing warning path, never a hang


def test_process_local_video_timeout_falls_back_to_copy(monkeypatch, tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"fake-video")
    dl = dl_mod.MediaDownloader(download_dir=str(tmp_path))
    monkeypatch.setattr(
        dl_mod.MediaDownloader, "get_local_info",
        lambda self, p: {"id": "m1", "title": "T", "duration": 120},
    )
    monkeypatch.setattr(
        dl_mod, "probe_local_media",
        lambda p: {"height": 720, "vcodec": "vp9"},
    )

    def boom(cmd, kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kwargs.get("timeout"))

    calls = _record_run(monkeypatch, boom)
    out_path, _info = dl.process_local_video(str(src), target_dir=str(tmp_path))
    assert Path(out_path).exists()  # copy fallback, as on a failed encode
    assert calls[0]["kwargs"].get("timeout") == dl_mod._local_media_timeout(120)


def test_extract_local_audio_timeout_uses_silent_fallback(monkeypatch, tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"fake-video")
    dl = dl_mod.MediaDownloader(download_dir=str(tmp_path))
    monkeypatch.setattr(
        dl_mod.MediaDownloader, "get_local_info",
        lambda self, p: {"id": "m2", "title": "T", "duration": 30},
    )

    def behaviour(cmd, kwargs):
        if "anullsrc" in " ".join(cmd):
            Path(cmd[-1]).write_bytes(b"mp3")
            return SimpleNamespace(returncode=0)
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=kwargs.get("timeout"))

    calls = _record_run(monkeypatch, behaviour)
    out_path, _info = dl.extract_local_audio(str(src), target_dir=str(tmp_path))
    assert Path(out_path).exists()
    assert len(calls) == 2
    for call in calls:
        assert call["kwargs"].get("timeout") == dl_mod._local_media_timeout(30)


@pytest.mark.asyncio
async def test_normalize_audio_to_wav_times_out(monkeypatch, tmp_path):
    class _HangingProc:
        returncode = None

        def __init__(self):
            self.killed = False

        async def communicate(self):
            await asyncio.sleep(30)
            return b"", b""

        def kill(self):
            self.killed = True

    proc = _HangingProc()

    async def fake_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(audio_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(audio_mod, "NORMALIZE_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(RuntimeError, match="timed out"):
        await audio_mod.normalize_audio_to_wav(
            tmp_path / "in.wav", tmp_path / "out.wav"
        )
    assert proc.killed is True


def test_groq_precompress_calls_are_time_bounded():
    transcriber_src = (ROOT / "app/services/transcriber.py").read_text(encoding="utf-8")
    whisper_src = (
        ROOT / "app/services/recap_engine/services/whisper_service.py"
    ).read_text(encoding="utf-8")
    assert "timeout=GROQ_PRECOMPRESS_TIMEOUT_SECONDS" in transcriber_src
    assert "timeout=GROQ_PRECOMPRESS_TIMEOUT_SECONDS" in whisper_src


# ── (5) poller CANCELLED branches (source pins) ─────────────────────

def test_recap_poller_has_cancelled_branch_with_proven_refund_only():
    poller = APP_JS.split("const recapPollTick")[1].split(
        "setInterval(recapPollTick, 2000)"
    )[0]
    assert 'data.status === "CANCELLED"' in poller
    branch = poller.split('data.status === "CANCELLED"')[1]
    assert "clearInterval(state.recapPollTimer)" in branch
    assert 'progressBox.classList.add("hidden")' in branch
    assert 't("recap_cancelled_toast")' in branch
    # The refund line rides ONLY on the payload's proof.
    assert 'if (data.credit_refunded)' in branch
    assert 'tf("recap_refunded_line"' in branch


def test_voice_poller_has_cancelled_branch():
    # Voice + recap pollers, plus the Batch AW TTS poller — every
    # polling loop stops honestly on a cancelled job.
    assert APP_JS.count('data.status === "CANCELLED"') == 3
    assert 't("voice_cancelled_toast")' in APP_JS


# ── (6) group-AI limits write path ──────────────────────────────────

def _admin(user_id=880599):
    return User(id=user_id)


def _snapshot_limits_settings():
    return {
        key: getattr(settings, key)
        for key in (
            "group_ai_user_cooldown_seconds",
            "group_ai_hourly_budget",
            "group_ai_daily_budget",
        )
    }


def _restore_limits_settings(snap):
    for key, val in snap.items():
        setattr(settings, key, val)


@pytest.mark.asyncio
async def test_group_ai_limits_roundtrip_changes_get_limits(db_session):
    admin = _admin()
    db_session.add(admin)
    await db_session.commit()
    snap = _snapshot_limits_settings()
    try:
        payload = api_mod.AdminSettingsPayload(
            group_ai_user_cooldown_seconds=0,
            group_ai_hourly_budget=7,
            group_ai_daily_budget=99,
        )
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["success"] is True
        assert res["updated"]["group_ai_user_cooldown_seconds"] == 0
        assert res["updated"]["group_ai_hourly_budget"] == 7
        assert res["updated"]["group_ai_daily_budget"] == 99

        out = await api_mod.get_admin_settings(admin, db_session)
        assert out["group_ai_user_cooldown_seconds"] == 0
        assert out["group_ai_hourly_budget"] == 7
        assert out["group_ai_daily_budget"] == 99

        limits = await group_ai_mod.get_limits(db_session)
        assert limits.user_cooldown_seconds == 0
        assert limits.hourly_budget == 7
        assert limits.daily_budget == 99

        log = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "settings.update",
                AuditLog.target_id == "group_ai_hourly_budget",
            ))).scalar_one()
        assert log.admin_id == admin.id
    finally:
        _restore_limits_settings(snap)


def test_group_ai_limits_validation_bounds():
    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates(
            {"group_ai_user_cooldown_seconds": -1}
        )
    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates(
            {"group_ai_user_cooldown_seconds": 86401}
        )
    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates({"group_ai_hourly_budget": 0})
    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates({"group_ai_daily_budget": 0})
    cleaned = api_mod._validate_admin_settings_updates(
        {
            "group_ai_user_cooldown_seconds": 0,
            "group_ai_hourly_budget": 1,
            "group_ai_daily_budget": 100000,
        }
    )
    assert cleaned["group_ai_user_cooldown_seconds"] == 0
    assert cleaned["group_ai_hourly_budget"] == 1
    assert cleaned["group_ai_daily_budget"] == 100000


def test_group_ai_limits_admin_ui_fields():
    for el_id in (
        "adm-set-group-ai-cooldown",
        "adm-set-group-ai-hourly",
        "adm-set-group-ai-daily",
    ):
        assert f'id="{el_id}"' in INDEX_HTML
    assert "group_ai_user_cooldown_seconds: groupAiCooldown" in APP_JS
    assert "group_ai_hourly_budget: groupAiHourly" in APP_JS
    assert "group_ai_daily_budget: groupAiDaily" in APP_JS
    assert "s.group_ai_user_cooldown_seconds" in APP_JS
    assert "s.group_ai_hourly_budget" in APP_JS
    assert "s.group_ai_daily_budget" in APP_JS


# ── (7) history percent rendering ───────────────────────────────────

def test_history_settings_summary_uses_integer_percents():
    assert "const pctOf = (v) => Math.round(v <= 1 ? v * 100 : v);" in APP_JS
    assert "pctOf(s.sub_y)" in APP_JS
    assert "pctOf(s.blur_y)" in APP_JS
    assert "Math.round(s.sub_y * 100)" not in APP_JS
    assert "Math.round(s.blur_y * 100)" not in APP_JS


# ── (8) i18n small items + cache ────────────────────────────────────

def test_new_i18n_keys_exist_in_both_languages():
    for key in NEW_I18N_KEYS:
        assert I18N_JS.count(f"{key}:") == 2, key


def test_step_label_is_localized():
    # Batch BR: the form's step line is composed by the store
    # renderer from the LiveJobs job (stage_index/stage_total) —
    # the same merged values the pill shows — not from the detail
    # poller's private data.progress_step any more.
    assert '`${t("recap_step_label")} ${job.stage_index || 0}/${job.stage_total || 4}`' in APP_JS
    assert '`${t("recap_step_label")} ${data.progress_step}`' not in APP_JS
    assert "`Step ${data.progress_step}`" not in APP_JS


def test_demo_asset_label_is_localized():
    assert 'data-i18n="adm_recap_demo_asset_label"' in INDEX_HTML
    assert 'data-i18n-ph="adm_recap_demo_asset_ph"' in INDEX_HTML
    assert (
        "Demo Asset — Telegram file_id or video URL (empty = demo slot hidden)</label>"
        not in INDEX_HTML
    )


def test_action_fallback_errors_are_localized():
    for literal in ('"Cancel failed"', '"Retry failed"', '"Demo failed"', '"Delete failed"'):
        assert literal not in APP_JS
    assert APP_JS.count('data.detail || t("err_generic")') >= 4


def test_cache_bust_version_is_r45():
    assert INDEX_HTML.count("20261104_r128") == 4
    assert "20261103_r44" not in INDEX_HTML
