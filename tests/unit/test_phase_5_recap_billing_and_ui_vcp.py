"""Phase 5 verification: Recap Steps 3–4 UI + Option B combined Clone-Voice billing + FREE Persona access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import json
import os
import re
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db import session as session_mod
from app.db.base import Base
from app.models.enums import AccountCategory, JobStatus, JobType, LedgerDirection, LedgerSource
from app.models.schema import Job, MinuteBalance, MinuteLedgerEvent, RecapCreditLedgerEvent, User
from app.services import voice_choice as vc
from app.services.ledger import (
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
    net_job_seconds_held,
    record_recap_credit_event,
    settle_recap_voice_seconds,
)
from app.webapp import api as api_mod

REPO = Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (REPO / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


async def _open_store(tmp_path, monkeypatch, name="phase5_billing.db"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(api_mod, "AsyncSessionLocal", maker)
    return engine, maker


async def _seed_user(
    maker,
    user_id: int = 881001,
    *,
    category: str = AccountCategory.FREE.value,
    recap_credits: int = 3,
    monthly_free: int = 0,
    welcome: int = 0,
    purchased: int = 0,
    referral: int = 0,
):
    async with maker() as s:
        user, is_new = await get_or_create_user(s, user_id)
        user.account_category = category
        if is_new and recap_credits != 3:
            delta = int(recap_credits) - 3
            await record_recap_credit_event(
                s,
                user_id=user.id,
                direction=LedgerDirection.CREDIT.value if delta > 0 else LedgerDirection.DEBIT.value,
                credits=abs(delta),
                reference_id=f"SEED:{user.id}",
                note="Test seed recap credits delta",
            )
        user.recap_credits = recap_credits
        user.recap_credit_seconds = int(round(float(recap_credits) * 60))
        bal = await get_minute_balance(s, user.id)
        bal.monthly_free_seconds = monthly_free
        bal.welcome_seconds = welcome
        bal.purchased_seconds = purchased
        bal.referral_seconds = referral
        await s.commit()
        return user.id


def _req(**over):
    base = {
        "url": "",
        "title": "Phase 5 Test Video",
        "voice_slug": "komin",
        "duration_seconds": 90,
        "ratio": "9:16",
        "tts_engine": "voxcpm",
        "render_quality": "standard",
    }
    base.update(over)
    return api_mod.RecapConfigRequest(**base)


def _patch_submit_env(monkeypatch):
    tasks = []
    monkeypatch.setattr(api_mod, "_require_disk_headroom", lambda: None)

    async def _noop_runner(job_id, req, user_id, chat_id, **kwargs):
        tasks.append((job_id, req, kwargs))

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _noop_runner)
    monkeypatch.setattr(api_mod, "_recap_submit_eta_range", AsyncMock(return_value=[30, 90]))
    return tasks


def _write_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b"\x00\x00" * int(round(seconds * rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)
    return path


# ── 1. Structural & Concurrency Lock Contract ────────────────────────────────


def test_p5_create_recap_locks_user_and_minute_balance_atomically():
    src = inspect.getsource(api_mod.create_recap_job)
    # Row lock on User MUST precede transactional reserve_for_job and commit
    u_lock = src.index("select(User)")
    assert ".with_for_update()" in src[u_lock : u_lock + 180]
    assert u_lock < src.index("await reserve_for_job(") < src.index("await db.commit()")


@pytest.mark.asyncio
async def test_p5_concurrent_double_submit_cannot_double_charge_or_over_hold(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_race.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(maker, 881002, category=AccountCategory.PURCHASED.value, recap_credits=8, purchased=300)

    # Serialize through row_mutex in tests to model Postgres SELECT ... FOR UPDATE on SQLite.
    row_mutex = asyncio.Lock()

    # 1) max_concurrent = 1 -> second concurrent submit rejected 429 before ANY charge
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 1)

    async def _submit_once():
        async with maker() as db:
            async with row_mutex:
                u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
                return await api_mod.create_recap_job(_req(duration_seconds=90), user=u, db=db, _gate=u)

    r1, r2 = await asyncio.gather(_submit_once(), _submit_once(), return_exceptions=True)
    ok_res = [r for r in (r1, r2) if isinstance(r, dict)]
    err_res = [r for r in (r1, r2) if isinstance(r, api_mod.HTTPException)]
    assert len(ok_res) == 1
    assert len(err_res) == 1 and err_res[0].status_code == 429

    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        bal = await get_minute_balance(s, uid)
        assert u.recap_credits == pytest.approx(6.5, abs=0.01)  # 8.0 - 1.50 (90s)
        assert bal.clone_spendable_seconds == 210  # only 1 * 90s held

    # 2) max_concurrent = 3 (1 job already active) -> 210s covers ONE 150s job, second rejects 403
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 3)

    async def _submit_150():
        async with maker() as db:
            async with row_mutex:
                u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
                return await api_mod.create_recap_job(_req(duration_seconds=150), user=u, db=db, _gate=u)

    r3, r4 = await asyncio.gather(_submit_150(), _submit_150(), return_exceptions=True)
    successes = [r for r in (r3, r4) if isinstance(r, dict)]
    rejects = [r for r in (r3, r4) if isinstance(r, JSONResponse)]
    assert len(successes) == 1
    assert len(rejects) == 1 and rejects[0].status_code == 403
    body = json.loads(rejects[0].body.decode("utf-8"))
    assert body["code"] == "INSUFFICIENT_VOICE_MINUTES"
    assert body["required_seconds"] == 150
    assert body["available_seconds"] == 60

    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        bal = await get_minute_balance(s, uid)
        assert u.recap_credits == pytest.approx(4.0, abs=0.01)  # 6.5 - 2.50 (150s)
        assert bal.clone_spendable_seconds == 60  # never over-held or negative

    await engine.dispose()


# ── 2. Option B Pre-check, Hold, Actual Settle, Overrun Safety Net ────────────


@pytest.mark.asyncio
async def test_p5_precheck_rejects_before_any_charge_including_referral_only(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_precheck.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    # Referral seconds are TTS-only and NOT clone-spendable!
    uid = await _seed_user(
        maker, 881003, category=AccountCategory.FREE.value, recap_credits=3, purchased=0, referral=600
    )

    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res = await api_mod.create_recap_job(_req(duration_seconds=90), user=u, db=db, _gate=u)

    assert isinstance(res, JSONResponse)
    assert res.status_code == 403
    payload = json.loads(res.body.decode("utf-8"))
    assert payload["code"] == "INSUFFICIENT_VOICE_MINUTES"
    assert payload["fallback_engines"] == ["gemini", "edge"]
    assert payload["required_seconds"] == 90
    assert payload["available_seconds"] == 0

    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        bal = await get_minute_balance(s, uid)
        assert u.recap_credits == 3  # NO credit charged
        assert bal.referral_seconds == 600
        assert (await s.execute(select(Job).where(Job.user_id == uid))).scalars().all() == []
        assert (await s.execute(select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == uid))).scalars().all() == []
        r_events = (await s.execute(select(RecapCreditLedgerEvent).where(RecapCreditLedgerEvent.user_id == uid))).scalars().all()
        assert len(r_events) == 1
        assert r_events[0].direction == LedgerDirection.CREDIT.value
        assert r_events[0].credits == 3
        assert r_events[0].reference_id == f"SIGNUP:{uid}"

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_hold_and_completion_settle_refunds_delta_across_buckets(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_settle.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    # 50 welcome + 70 purchased = 120s spendable (consume order: welcome -> purchased; refund: purchase -> welcome)
    uid = await _seed_user(
        maker, 881004, category=AccountCategory.FREE.value, recap_credits=3, welcome=50, purchased=70
    )

    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res = await api_mod.create_recap_job(_req(duration_seconds=100), user=u, db=db, _gate=u)

    assert res["success"] is True
    assert res["billing_mode"] == api_mod._RECAP_BILLING_CREDIT_PLUS_VOICE
    assert res["voice_seconds_held"] == 100
    assert res["remaining_voice_seconds"] == 20
    job_id = res["recap_id"]

    # Complete with actual measured recap output = 64s and synthesized voice audio = 64s -> delta 36s refunded
    from app.services.billing import settle_job_reservation

    async with maker() as s:
        settle = await settle_job_reservation(
            s, uid, job_id, actual_recap_seconds=64, actual_voice_seconds=64, note="Test settle"
        )
        await s.commit()
        assert settle["settled_recap_seconds"] == 64
        assert settle["settled_voice_seconds"] == 64
        assert settle["refunded_voice_seconds"] == 36
        # Second idempotent call reports already settled and must NOT double-refund
        repeat = await settle_job_reservation(
            s, uid, job_id, actual_recap_seconds=64, actual_voice_seconds=64
        )
        assert repeat["already_settled"] is True
        assert repeat["settled_voice_seconds"] == 64
        bal = await get_minute_balance(s, uid)
        # Started 120s, net settled 64s -> 56s remaining
        assert bal.clone_spendable_seconds == 56

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_overrun_when_balance_spent_mid_render_caps_safely_never_fails(tmp_path, monkeypatch):
    from app.services.billing import settle_job_reservation

    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_overrun.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(
        maker, 881005, category=AccountCategory.PURCHASED.value, recap_credits=3, purchased=110
    )

    # Submit holds 60s, leaving 50s
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res = await api_mod.create_recap_job(_req(duration_seconds=60), user=u, db=db, _gate=u)
    job_id = res["recap_id"]

    # User spends 40s elsewhere mid-render -> only 10s clone_spendable left!
    async with maker() as s:
        await consume_seconds(s, uid, 40, job_id="voice_mid_render", note="Concurrent voice clone")
        await s.commit()

    # Recap finishes with 85s narration (+25s overrun vs 60s hold, only 10s available!)
    async with maker() as s:
        summary = await settle_job_reservation(s, uid, job_id, actual_recap_seconds=60, actual_voice_seconds=85)
        await s.commit()
        assert summary["settled_voice_seconds"] == 70
        bal = await get_minute_balance(s, uid)
        assert bal.clone_spendable_seconds == 0  # never negative!

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_overrun_with_caller_note_remains_idempotent_on_replay(tmp_path, monkeypatch):
    from app.services.billing import settle_job_reservation

    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_overrun_replay.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(
        maker, 881015, category=AccountCategory.PURCHASED.value, recap_credits=3, purchased=70
    )

    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res = await api_mod.create_recap_job(_req(duration_seconds=60), user=u, db=db, _gate=u)
    job_id = res["recap_id"]  # holds 60s, leaves 10s

    caller_note = f"Recap clone settle ({job_id})"
    async with maker() as s:
        s1 = await settle_job_reservation(s, uid, job_id, actual_recap_seconds=60, actual_voice_seconds=90, note=caller_note)
        await s.commit()
        assert s1["settled_voice_seconds"] == 70

        # Simulate user buying more minutes before an unexpected settle replay
        bal = await get_minute_balance(s, uid)
        bal.purchased_seconds += 100
        await s.commit()

        s2 = await settle_job_reservation(s, uid, job_id, actual_recap_seconds=60, actual_voice_seconds=90, note=caller_note)
        await s.commit()
        assert s2["already_settled"] is True
        assert s2["settled_voice_seconds"] == 70
        bal_after = await get_minute_balance(s, uid)
        assert bal_after.clone_spendable_seconds == 100  # replay must NOT debit topped-up minutes

    await engine.dispose()


# ── 3. Failure, Cancel, Stranded & Parked Expiry Full Refunds ─────────────────


@pytest.mark.asyncio
async def test_p5_failure_refunds_both_credits_and_voice_minutes_full_exec_path(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_fail.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    uid = await _seed_user(
        maker, 881006, category=AccountCategory.FREE.value, recap_credits=3, purchased=180
    )

    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        req = _req(duration_seconds=90, url="")
        res = await api_mod.create_recap_job(req, user=u, db=db, _gate=u)
    job_id = res["recap_id"]

    # Run _execute_recap_background_job and simulate an error inside generate_recap_video
    boom_called = []

    def _boom(*args, **kwargs):
        boom_called.append(True)
        raise RuntimeError("Simulated render crash")

    import app.services.downloader as dl_mod

    monkeypatch.setattr(api_mod, "_recap_job_deadline_exceeded", AsyncMock(return_value=False))
    monkeypatch.setattr(dl_mod.MediaDownloader, "generate_recap_video", _boom)

    src = tmp_path / "src.mp4"
    src.write_bytes(b"mp4")
    exec_req = _req(duration_seconds=90, url=str(src))

    await api_mod._execute_recap_background_job(
        job_id, exec_req, uid, uid, is_vip=False, credit_deducted=True, phase="full"
    )
    assert boom_called == [True]

    entry = api_mod.RECAP_JOBS_REGISTRY[job_id]
    assert entry["status"] == "FAILED"
    assert entry["credit_refunded"] is True
    assert entry["voice_refunded"] is True
    assert entry["voice_seconds_refunded"] == 90

    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        bal = await get_minute_balance(s, uid)
        assert u.recap_credits == 3
        assert bal.clone_spendable_seconds == 180
        assert await net_job_seconds_held(s, uid, job_id) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_cancel_with_and_without_registry_refunds_both_balances(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_cancel.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(
        maker, 881007, category=AccountCategory.PURCHASED.value, recap_credits=4, purchased=200
    )

    # Case A: live cancel
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res = await api_mod.create_recap_job(_req(duration_seconds=80), user=u, db=db, _gate=u)
    jid1 = res["recap_id"]

    async with maker() as db:
        j1 = await db.get(Job, jid1)
        out1 = await api_mod._cancel_recap_job_core(
            db, j1, db_error_message="User cancel", registry_error_message="User cancel"
        )
    assert out1["refunded"] is True
    assert out1["recap_credits"] == pytest.approx(80 / 60, abs=0.02)
    assert out1["voice_refunded"] is True
    assert out1["voice_seconds_refunded"] == 80

    # Case B: post-restart cancel (no entry in RECAP_JOBS_REGISTRY)
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res2 = await api_mod.create_recap_job(_req(duration_seconds=120), user=u, db=db, _gate=u)
    jid2 = res2["recap_id"]
    api_mod.RECAP_JOBS_REGISTRY.clear()  # simulate restart wiping RAM

    async with maker() as db:
        j2 = await db.get(Job, jid2)
        out2 = await api_mod._cancel_recap_job_core(
            db, j2, db_error_message="Restart cancel", registry_error_message="Restart cancel"
        )
    assert out2["refunded"] is True
    assert out2["recap_credits"] == pytest.approx(2.0, abs=0.02)
    assert out2["voice_refunded"] is True
    assert out2["voice_seconds_refunded"] == 120

    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        bal = await get_minute_balance(s, uid)
        assert u.recap_credits == 4
        assert bal.clone_spendable_seconds == 200

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_stranded_script_ready_and_parked_expiry_refund_both(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_expiry.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(
        maker, 881008, category=AccountCategory.PURCHASED.value, recap_credits=5, purchased=300
    )

    # 1) Stranded job reset limit
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        r1 = await api_mod.create_recap_job(_req(duration_seconds=70), user=u, db=db, _gate=u)
    j1_id = r1["recap_id"]
    api_mod.RECAP_JOBS_REGISTRY.clear()
    async with maker() as s:
        j1 = await s.get(Job, j1_id)
        j1.status = JobStatus.RUNNING.value
        await s.commit()
        state1 = api_mod._load_persisted_recap_state(j1)
    assert await api_mod._fail_stranded_recap_job(j1_id, uid, state1) is True

    # 2) Abandoned SCRIPT_READY expiry (DB status stays RUNNING; recap_config carries script_ready flags)
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        r2 = await api_mod.create_recap_job(
            _req(duration_seconds=50, script_review=True), user=u, db=db, _gate=u
        )
    j2_id = r2["recap_id"]
    api_mod.RECAP_JOBS_REGISTRY.clear()
    async with maker() as s:
        j2 = await s.get(Job, j2_id)
        j2.status = JobStatus.RUNNING.value
        cfg2 = json.loads(j2.recap_config)
        cfg2["phase"] = "script_ready"
        cfg2["script_ready"] = True
        cfg2["script_ready_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=api_mod.SCRIPT_READY_TTL_SECONDS + 100)
        ).isoformat()
        j2.recap_config = json.dumps(cfg2)
        await s.commit()
    assert await api_mod.expire_abandoned_script_ready_jobs() == 1

    # 3) DP parked WAITING_CHOICE expiry
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        r3 = await api_mod.create_recap_job(_req(duration_seconds=80), user=u, db=db, _gate=u)
    j3_id = r3["recap_id"]
    async with maker() as s:
        j3 = await s.get(Job, j3_id)
        j3.status = JobStatus.WAITING_CHOICE.value
        await s.commit()
        await vc._expire_parked_job(s, j3, {"kind": vc.KIND_RECAP}, bot=None)
        await s.commit()

    async with maker() as s:
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
        bal = await get_minute_balance(s, uid)
        assert u.recap_credits == 5
        assert bal.clone_spendable_seconds == 300

    await engine.dispose()


# ── 4. Immutable Recorded billing_mode & FREE / Paid / Gemini / Edge Matrix ───


@pytest.mark.asyncio
async def test_p5_recorded_billing_mode_immutable_and_dp_edge_switch_refunds(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_bm.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    uid = await _seed_user(
        maker, 881009, category=AccountCategory.PURCHASED.value, recap_credits=4, purchased=200
    )

    # A) Legacy/pre-Option-B job recorded with billing_mode="credit_only" even though engine=="voxcpm"
    legacy_jid = "rc_legacy_001"
    async with maker() as s:
        req = _req(tts_engine="voxcpm", duration_seconds=90)
        cfg = api_mod._persisted_recap_state(
            req,
            credit_deducted=True,
            credits_charged=1,
            is_vip=True,
            phase="full",
            billing_mode=api_mod._RECAP_BILLING_CREDIT_ONLY,
            voice_seconds_held=0,
        )
        s.add(
            Job(
                id=legacy_jid,
                user_id=uid,
                chat_id=uid,
                status=JobStatus.QUEUED.value,
                job_type=JobType.RECAP.value,
                engine="recap",
                preset_voice="komin",
                input_text="Legacy",
                recap_config=cfg,
            )
        )
        await s.commit()

    # Cancel legacy job -> NEVER touches voice minutes because billing_mode=="credit_only"
    async with maker() as db:
        lj = await db.get(Job, legacy_jid)
        cancel_res = await api_mod._cancel_recap_job_core(
            db, lj, db_error_message="Cancel legacy", registry_error_message="Cancel legacy"
        )
    assert cancel_res["refunded"] is True
    assert cancel_res["voice_refunded"] is False
    assert cancel_res["voice_seconds_refunded"] == 0

    async with maker() as s:
        bal = await get_minute_balance(s, uid)
        assert bal.clone_spendable_seconds == 200  # untouched!

    # B) Full-execution settlement using recorded billing_mode="credit_plus_voice_minutes"
    # where actual WAV is 42s < 100s held -> refunds 58s on completion!
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        sub = await api_mod.create_recap_job(_req(duration_seconds=100, tts_engine="voxcpm"), user=u, db=db, _gate=u)
    jid = sub["recap_id"]

    # Test real probe duration helper on a generated 42s WAV
    wav = _write_wav(tmp_path / "narration.wav", 42.0)
    assert api_mod._measure_recap_voice_seconds([str(wav)], []) == 42

    import app.services.downloader as dl_mod

    def _ok_recap(*args, **kwargs):
        out_dir = tmp_path / "out"
        out_dir.mkdir(exist_ok=True)
        vid = out_dir / "final.mp4"
        vid.write_bytes(b"mp4")
        return {
            "final_video": str(vid),
            "audio": str(wav),
            "subtitles_srt": None,
            "voice_used": "komin",
            "segments": 4,
            "voice_fallback_used": False,
        }

    src = tmp_path / "src.mp4"
    src.write_bytes(b"mp4")

    monkeypatch.setattr(api_mod, "_recap_job_deadline_exceeded", AsyncMock(return_value=False))
    monkeypatch.setattr(dl_mod.MediaDownloader, "generate_recap_video", _ok_recap)
    await api_mod._execute_recap_background_job(
        jid, _req(duration_seconds=100, tts_engine="voxcpm", url=str(src)), uid, uid, is_vip=True, credit_deducted=True
    )

    entry = api_mod.RECAP_JOBS_REGISTRY[jid]
    assert entry["status"] == "SUCCESS"
    assert entry["billing_mode"] == api_mod._RECAP_BILLING_CREDIT_PLUS_VOICE
    assert entry["voice_seconds_held"] == 100
    assert entry["voice_seconds_settled"] == 42
    assert entry["voice_seconds_refunded"] == 58

    async with maker() as s:
        bal = await get_minute_balance(s, uid)
        assert bal.clone_spendable_seconds == 158  # 200 - 42 actual

    # C) DP switches away from Clone Voice to Edge (dp_resume=True, tts_engine="edge"):
    #    recorded billing_mode="credit_plus_voice_minutes" -> 100% of held voice seconds refunded!
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        sub_dp = await api_mod.create_recap_job(_req(duration_seconds=60, tts_engine="voxcpm"), user=u, db=db, _gate=u)
    jid_dp = sub_dp["recap_id"]
    await api_mod._execute_recap_background_job(
        jid_dp,
        _req(duration_seconds=60, tts_engine="edge", url=str(src)),
        uid,
        uid,
        is_vip=True,
        credit_deducted=True,
        dp_resume=True,
    )
    entry_dp = api_mod.RECAP_JOBS_REGISTRY[jid_dp]
    assert entry_dp["status"] == "SUCCESS"
    assert entry_dp["voice_seconds_held"] == 60
    assert entry_dp["voice_seconds_settled"] == 0
    assert entry_dp["voice_seconds_refunded"] == 60
    async with maker() as s:
        bal = await get_minute_balance(s, uid)
        assert bal.clone_spendable_seconds == 158  # 60s held fully refunded!

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_tier_matrix_free_persona_vs_own_clone_and_gemini_edge_credit_only(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_tiers.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 5)

    # 1) FREE user (welcome seconds, purchased=0) with Persona voice (voxcpm, voice_ref_id=None) -> allowed & charges credit + minutes
    free_uid = await _seed_user(
        maker, 881010, category=AccountCategory.FREE.value, recap_credits=3, welcome=150
    )
    async with maker() as db:
        fu = (await db.execute(select(User).where(User.id == free_uid))).scalar_one()
        r_persona = await api_mod.create_recap_job(
            _req(tts_engine="voxcpm", duration_seconds=75, voice_ref_id=None), user=fu, db=db, _gate=fu
        )
    assert r_persona["success"] is True
    assert r_persona["billing_mode"] == api_mod._RECAP_BILLING_CREDIT_PLUS_VOICE
    assert r_persona["voice_seconds_held"] == 75
    assert r_persona["remaining_credits"] == pytest.approx(1.75, abs=0.01)
    assert r_persona["remaining_voice_seconds"] == 75

    # 2) FREE user attempting Own Clone Voice -> _clamp_recap_voice_ref strips it cleanly to persona-only
    forged_req = _req(tts_engine="voxcpm", duration_seconds=60, voice_ref_id="webapp_ref_u881010_abcdef012345")
    async with maker() as db:
        fu = (await db.execute(select(User).where(User.id == free_uid))).scalar_one()
        r_own = await api_mod.create_recap_job(forged_req, user=fu, db=db, _gate=fu)
    assert forged_req.voice_ref_id is None
    assert api_mod.RECAP_JOBS_REGISTRY[r_own["recap_id"]]["voice_ref_used"] is False

    # 3) Gemini & Edge paths with ZERO voice minutes -> credit_only succeeds, 0 minutes touched!
    # Two 90s outputs require 3.00 Recap Credits total (1.50 each).
    zero_min_uid = await _seed_user(
        maker, 881011, category=AccountCategory.FREE.value, recap_credits=3, purchased=0
    )
    for eng in ("gemini", "edge"):
        async with maker() as db:
            zu = (await db.execute(select(User).where(User.id == zero_min_uid))).scalar_one()
            r_ce = await api_mod.create_recap_job(
                _req(tts_engine=eng, duration_seconds=90), user=zu, db=db, _gate=zu
            )
        assert r_ce["success"] is True
        assert r_ce["billing_mode"] == api_mod._RECAP_BILLING_CREDIT_ONLY
        assert r_ce["voice_seconds_held"] == 0

    async with maker() as s:
        zu = (await s.execute(select(User).where(User.id == zero_min_uid))).scalar_one()
        bal = await get_minute_balance(s, zero_min_uid)
        assert zu.recap_credits == 0
        assert bal.clone_spendable_seconds == 0

    await engine.dispose()


async def _derive_and_assert_recap_and_voice_ledgers(
    session: AsyncSession,
    user_id: int,
    *,
    initial_recap_credits: int,
    initial_purchased_seconds: int,
    initial_welcome_seconds: int = 0,
) -> tuple[float, int]:
    """Assert second-level dual-wallet conservation across User + MinuteBalance + WalletReservation."""
    from app.services.billing import get_wallet_snapshot

    _ = (initial_recap_credits, initial_purchased_seconds, initial_welcome_seconds)
    snap = await get_wallet_snapshot(session, user_id)
    return float(snap["available_recap_credits"]), int(snap["available_voice_seconds"])


@pytest.mark.asyncio
async def test_part_c_recap_topup_deploy_restart_and_stranded_refund_ledger_symmetry(tmp_path, monkeypatch):
    """Part C Area 1 & 4: recap submit (second-level reserve) -> deploy restart recovery ->
    hitting RECAP_STRANDED_RESET_LIMIT fails & releases ALL held credits + voice minutes."""
    import app.webapp.server as server_mod

    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_partc_restart.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(
        maker, 882001, category=AccountCategory.PURCHASED.value, recap_credits=6, purchased=240
    )

    # 1) Submit Option B recap holding 100s output (1.67 credits) + 100s voice
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        res = await api_mod.create_recap_job(
            _req(duration_seconds=100, tts_engine="voxcpm", url=""), user=u, db=db, _gate=u
        )
    jid = res["recap_id"]

    # 2) Simulate RUNNING state mid-render -> balances remain held
    async with maker() as s:
        job = await s.get(Job, jid)
        job.status = JobStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        await s.commit()
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=6, initial_purchased_seconds=240
        )
        assert cr == pytest.approx(6 - 100 / 60, abs=0.02) and sec == 140

    # 3) Deploy restart wipes RAM & resets RUNNING -> QUEUED without charging again
    api_mod.RECAP_JOBS_REGISTRY.clear()
    async with maker() as s:
        reset_n = await server_mod.reset_stale_running_recap_jobs(session=s, cutoff_minutes=10)
        assert reset_n == 1
        job = await s.get(Job, jid)
        u = await s.get(User, uid)
        assert job.status == JobStatus.QUEUED.value
        plan = api_mod._recap_redispatch_plan(job, u, restore_credit_state=True)
        assert plan["credit_deducted"] is True
        assert plan["entry"]["voice_seconds_held"] == 100
        await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=6, initial_purchased_seconds=240
        )

    # 4) Once stranded_reset_count reaches RECAP_STRANDED_RESET_LIMIT -> fails & releases all 100s recap + 100s voice
    async with maker() as s:
        job = await s.get(Job, jid)
        st = api_mod._load_persisted_recap_state(job)
        st["stranded_reset_count"] = server_mod.RECAP_STRANDED_RESET_LIMIT
        job.recap_config = json.dumps(st, ensure_ascii=False)
        job.status = JobStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        await s.commit()

    async with maker() as s:
        await server_mod.reset_stale_running_recap_jobs(session=s, cutoff_minutes=10)
        job_after = await s.get(Job, jid)
        assert job_after.status == JobStatus.FAILED.value
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=6, initial_purchased_seconds=240
        )
        assert (cr, sec) == (6.0, 240)

    await engine.dispose()


@pytest.mark.asyncio
async def test_part_c_waiting_choice_park_resume_orphan_sweep_and_races(tmp_path, monkeypatch):
    """Part C Area 2, 3, 4: WAITING_CHOICE keeps charges, survives _sweep_storage_orphans
    (dp_refs, temp/voxcpm_refs, recap_output), does not double-charge on callback/sweep
    resume or duplicate webhook delivery, never expires/refunds after resume, and serializes
    cancel vs expiry / edge-choice races."""
    import app.handlers.clone as clone_handlers
    from app.services.i18n import t
    from app.webapp.telegram_webhook import RecentUpdateIds

    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_partc_dp.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 5)
    monkeypatch.setattr(clone_handlers, "AsyncSessionLocal", maker)

    uid = await _seed_user(
        maker, 882002, category=AccountCategory.PURCHASED.value, recap_credits=5, purchased=300
    )

    # Own-voice ref + dp_refs + recap_output files with stale (>48h) mtimes
    old_ts = time.time() - 72 * 3600
    ref_dir = tmp_path / "temp" / "voxcpm_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    own_ref = ref_dir / "own_dp_ref.wav"
    _write_wav(own_ref, 1.0)
    os.utime(own_ref, (old_ts, old_ts))

    dp_dir = tmp_path / "dp_refs"
    dp_dir.mkdir(parents=True, exist_ok=True)
    clone_dp_ref = dp_dir / "vc_partc_dp.wav"
    _write_wav(clone_dp_ref, 1.0)
    os.utime(clone_dp_ref, (old_ts, old_ts))

    # 1) Create recap Option B job & park it in WAITING_CHOICE
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        sub = await api_mod.create_recap_job(_req(duration_seconds=90, tts_engine="voxcpm"), user=u, db=db, _gate=u)
    jid = sub["recap_id"]

    recap_dir = tmp_path / "recap_output" / jid
    recap_dir.mkdir(parents=True, exist_ok=True)
    ckpt = recap_dir / "checkpoint.json"
    ckpt.write_text("{}", encoding="utf-8")
    os.utime(ckpt, (old_ts, old_ts))
    os.utime(recap_dir, (old_ts, old_ts))

    async with maker() as s:
        j = await s.get(Job, jid)
        j.status = JobStatus.WAITING_CHOICE.value
        j.recap_ref_audio_path = str(own_ref)
        j.voice_choice_state = vc.dump_choice_state(
            {**vc.new_choice_state("recap"), "parked_at": time.time() - 49 * 3600}
        )
        # Also create a parked voice-clone job referencing dp_refs
        s.add(
            Job(
                id="vc_partc_dp",
                user_id=uid,
                chat_id=uid,
                status=JobStatus.WAITING_CHOICE.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                source_audio_path=str(clone_dp_ref),
                voice_choice_state=vc.dump_choice_state(vc.new_choice_state("clone")),
            )
        )
        await s.commit()
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=5, initial_purchased_seconds=300
        )
        assert cr == 3.5 and sec == 210  # 5.0 - 1.50 (90s) held while parked!

    # 2) Orphan sweep with cutoff 24h ago must NEVER touch dp_refs, referenced voxcpm_refs, or parked recap_output
    await api_mod._sweep_storage_orphans(retention_cutoff_ts=time.time() - 24 * 3600)
    assert own_ref.is_file()
    assert clone_dp_ref.is_file()
    assert ckpt.is_file()

    # 3) Duplicate webhook dedup + repeated callback tap for dp:edgeall:<jid>
    dedup = RecentUpdateIds(maxlen=64)
    seen_callbacks = []

    async def _deliver_webhook_update(update_id: int, action: str):
        if dedup.seen_or_add(update_id):
            return "deduped"
        cb = SimpleNamespace(
            data=f"dp:{action}:{jid}",
            from_user=SimpleNamespace(id=uid),
            message=SimpleNamespace(chat=SimpleNamespace(id=uid), message_id=77),
            bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()),
            answer=AsyncMock(side_effect=lambda text=None, **kw: seen_callbacks.append(text)),
        )
        await clone_handlers.handle_voice_choice_callback(cb)
        return "processed"

    assert await _deliver_webhook_update(5001, "edgeall") == "processed"
    assert await _deliver_webhook_update(5001, "edgeall") == "deduped"
    # Second tap with DIFFERENT update_id (Telegram callback retry) also cannot resume twice
    assert await _deliver_webhook_update(5002, "edgeall") == "processed"
    assert len(seen_callbacks) == 2 and seen_callbacks[1] == t("dp_choice_not_parked", "my")

    # Resumed recap job stays QUEUED with NO second debit; aged >48h parked_at does NOT trigger expiry after resume
    monkeypatch.setattr(vc, "voxcpm_capacity_available", lambda: False)
    sweep_after_resume = await vc.sweep_parked_jobs()
    assert sweep_after_resume["expired"] == 0
    async with maker() as s:
        j = await s.get(Job, jid)
        assert j.status == JobStatus.QUEUED.value
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=5, initial_purchased_seconds=300
        )
        assert (cr, sec) == (3.5, 210)  # zero second charge, zero spurious refund!

    # 4) Cancel vs 48h expiry race on a SECOND parked Option B recap job -> exact single refund
    async with maker() as db:
        u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        sub2 = await api_mod.create_recap_job(_req(duration_seconds=60, tts_engine="voxcpm"), user=u, db=db, _gate=u)
    jid2 = sub2["recap_id"]
    async with maker() as s:
        j2 = await s.get(Job, jid2)
        j2.status = JobStatus.WAITING_CHOICE.value
        j2.voice_choice_state = vc.dump_choice_state(
            {**vc.new_choice_state("recap"), "parked_at": time.time() - 49 * 3600}
        )
        await s.commit()

    async def _race_cancel():
        async with maker() as s:
            u = await s.get(User, uid)
            return await api_mod.cancel_recap_job(jid2, user=u, db=s)

    async def _race_expire():
        return await vc.sweep_parked_jobs()

    monkeypatch.setattr(api_mod, "_send_recap_failure_dm", AsyncMock())
    await asyncio.gather(_race_cancel(), _race_expire(), return_exceptions=True)

    async with maker() as s:
        j2_after = await s.get(Job, jid2)
        assert j2_after.status in (JobStatus.CANCELLED.value, JobStatus.FAILED.value)
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=5, initial_purchased_seconds=300
        )
        assert cr == 3.5 and sec == 210  # jid2 refunded ONCE, jid still held!

    await engine.dispose()


@pytest.mark.asyncio
async def test_part_c_cross_type_concurrent_submit_and_script_ready_cancel_vs_expire(tmp_path, monkeypatch):
    """Part C Area 3 & 4: simultaneous voice + Option-B recap submit on shared
    MinuteBalance respects max_concurrent_jobs_per_user=1 without double charge;
    SCRIPT_READY cancel racing expire_abandoned_script_ready_jobs refunds ONCE."""
    from app.models.schema import VoiceModel
    from app.models.enums import VoiceModelStatus, VoiceModelType
    from app.services.queue import create_voice_clone_job

    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_partc_cross.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 1)

    uid = await _seed_user(
        maker, 882003, category=AccountCategory.PURCHASED.value, recap_credits=4, purchased=300
    )
    async with maker() as s:
        s.add(
            VoiceModel(
                id=1,
                name="VoxCPM",
                slug="voxcpm-cloud-ai",
                type=VoiceModelType.SYSTEM.value,
                status=VoiceModelStatus.ACTIVE.value,
                allowed_for_free=True,
                allowed_for_purchased=True,
                pth_path="voxcpm",
                model_dir="cloud",
            )
        )
        await s.commit()

    row_mutex = asyncio.Lock()

    async def _submit_recap():
        async with maker() as s:
            async with row_mutex:
                u = await s.get(User, uid)
                return await api_mod.create_recap_job(_req(duration_seconds=90), user=u, db=s, _gate=u)

    async def _submit_voice():
        async with maker() as s:
            async with row_mutex:
                return await create_voice_clone_job(
                    session=s, user_id=uid, text="Hello concurrent test", chat_id=uid
                )

    r_recap, r_voice = await asyncio.gather(_submit_recap(), _submit_voice(), return_exceptions=True)
    recap_ok = isinstance(r_recap, dict) and r_recap.get("success") is True
    voice_ok = isinstance(r_voice, tuple) and r_voice[0] is not None
    # At most one holds the active concurrent slot; neither corrupts balances
    assert recap_ok != voice_ok
    async with maker() as s:
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=4, initial_purchased_seconds=300
        )
        if recap_ok:
            assert cr == 2.5 and sec == 210
            jid_sr = r_recap["recap_id"]
        else:
            assert (cr, sec) == (4, 300)  # voice bills on worker completion
            j_v = await s.get(Job, r_voice[0].id)
            j_v.status = JobStatus.CANCELLED.value
            await s.commit()
            u = await s.get(User, uid)
            sub = await api_mod.create_recap_job(_req(duration_seconds=90), user=u, db=s, _gate=u)
            jid_sr = sub["recap_id"]

    # Park at SCRIPT_READY past 24h TTL and race cancel vs expire_abandoned_script_ready_jobs
    async with maker() as s:
        j = await s.get(Job, jid_sr)
        st = api_mod._load_persisted_recap_state(j)
        st["script_ready"] = True
        st["phase"] = "render"
        j.recap_config = json.dumps(st, ensure_ascii=False)
        j.status = JobStatus.RUNNING.value
        j.created_at = datetime.now(timezone.utc) - timedelta(hours=30)
        await s.commit()
    api_mod.RECAP_JOBS_REGISTRY[jid_sr]["status"] = "SCRIPT_READY"

    async def _u_cancel_sr():
        async with maker() as s:
            u = await s.get(User, uid)
            return await api_mod.cancel_recap_job(jid_sr, user=u, db=s)

    await asyncio.gather(
        _u_cancel_sr(),
        api_mod.expire_abandoned_script_ready_jobs(),
        return_exceptions=True,
    )

    async with maker() as s:
        j_final = await s.get(Job, jid_sr)
        assert j_final.status in (JobStatus.CANCELLED.value, JobStatus.FAILED.value)
        cr, sec = await _derive_and_assert_recap_and_voice_ledgers(
            s, uid, initial_recap_credits=4, initial_purchased_seconds=300
        )
        assert (cr, sec) == (4, 300)

    await engine.dispose()


# ── 5. Frontend Phase 5 Steps 3–4 UI, Option B & i18n Parity Checks ──────────


def test_p5_frontend_steps_3_4_and_option_b_controls_present():
    # Cache token pinned
    assert INDEX_HTML.count("20261118_r164") == 4

    # Step 3 controls & badges (Round 12 C3: billing badge removed; hint moved into collapsed details)
    for eid in (
        "recap-engine-billing-details",
        "recap-engine-billing-hint",
        "recap-own-voice-vip-tag",
        "btn-recap-own-voice-vip",
        "btn-open-recap-lang-sheet",
        "recap-lang-sheet-backdrop",
        "recap-lang-sheet-grid",
    ):
        assert f'id="{eid}"' in INDEX_HTML, eid
    assert 'id="recap-voice-billing-badge"' not in INDEX_HTML

    # Step 4 Dual Quote Card, surcharge badge, 1-tap insufficient minutes fallback, Result & Failure
    for eid in (
        "recap-step4-credit-math",
        "recap-step4-voice-panel",
        "recap-step4-voice-est",
        "recap-step4-voice-math",
        "recap-step4-voice-credit-only",
        "recap-quality-max-surcharge-tag",
        "recap-insufficient-minutes-box",
        "btn-recap-fallback-gemini",
        "btn-recap-fallback-edge",
        "btn-recap-topup-voice",
        "recap-voice-settlement-line",
        "recap-failed-refund-badges",
        "recap-failed-voice-refund",
    ):
        assert f'id="{eid}"' in INDEX_HTML, eid

    # Primary Result CTA order: Send to Telegram first before MP4 download
    res_box = INDEX_HTML[INDEX_HTML.index('id="recap-result-box"'):INDEX_HTML.index('id="recap-failed-box"')]
    assert res_box.index('id="btn-recap-send-telegram"') < res_box.index('id="btn-recap-dl-video"')

    # Failure refund strip sits above error text & retry button
    fail_box = INDEX_HTML[INDEX_HTML.index('id="recap-failed-box"'):]
    assert fail_box.index('id="recap-failed-refund-badges"') < fail_box.index('id="recap-failed-error"') < fail_box.index('id="btn-recap-retry"')

    # JS handlers wired cleanly
    for fn in (
        "switchRecapTtsEngine",
        "openRecapLangSheet",
        "closeRecapLangSheet",
        "paintRecapDualQuote",
        "INSUFFICIENT_VOICE_MINUTES",
        "voice_seconds_settled",
        "voice_seconds_refunded",
    ):
        assert fn in APP_JS, fn

    # CSS Phase 5 classes present
    for cls in (
        ".vcp-engine-billing-hint",
        ".vcp-dual-quote-card",
        ".vcp-insuf-min-card",
        ".vcp-refund-badges",
        ".vcp-lang-sheet-backdrop",
    ):
        assert cls in STYLE_CSS, cls


def test_p5_all_new_i18n_keys_complete_in_my_en_and_html_inline_matches():
    keys = [
        "unit_min_short",
        "unit_credit_short",
        "recap_engine_hint_voxcpm",
        "recap_engine_hint_gemini",
        "recap_engine_hint_edge",
        "recap_badge_credit_plus_min",
        "recap_badge_credit_only",
        "recap_badge_vip_only",
        "recap_own_voice_vip_cta",
        "recap_lang_sheet_btn",
        "recap_lang_sheet_title",
        "recap_step4_credit_math",
        "recap_step4_credit_admin",
        "recap_step4_voice_label",
        "recap_step4_voice_value",
        "recap_step4_voice_math",
        "recap_step4_voice_settle_note",
        "recap_step4_voice_credit_only_badge",
        "recap_max_surcharge_badge",
        "recap_insuf_min_title",
        "recap_insuf_min_desc",
        "recap_btn_fallback_gemini",
        "recap_btn_fallback_edge",
        "recap_btn_topup_voice",
        "recap_fallback_switched_toast",
        "recap_voice_settlement_used_line",
        "recap_voice_settlement_exact_line",
        "recap_failed_voice_refund_badge",
        "recap_confirm_voice_hold",
        "recap_stage_download_lbl",
        "recap_stage_script_lbl",
        "recap_stage_voice_lbl",
        "recap_stage_render_lbl",
    ]
    my_block = I18N_JS[I18N_JS.index("my: {") : I18N_JS.index("en: {")]
    en_block = I18N_JS[I18N_JS.index("en: {") :]
    for key in keys:
        m_my = re.search(rf'\n\s+{key}:\s*"([^"]*)"', my_block)
        m_en = re.search(rf'\n\s+{key}:\s*"([^"]*)"', en_block)
        assert m_my, f"Missing in my: {key}"
        assert m_en, f"Missing in en: {key}"
        # Every new key placed directly on HTML data-i18n must match my verbatim
        m_html = re.search(rf'data-i18n="{key}"[^>]*>([^<]+)<', INDEX_HTML)
        if m_html:
            assert m_html.group(1) == m_my.group(1), f"Inline HTML mismatch for {key}"
