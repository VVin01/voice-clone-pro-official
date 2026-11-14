"""Phase 5 verification: Recap Steps 3–4 UI + Option B combined Clone-Voice billing + FREE Persona access."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import json
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
        user, _ = await get_or_create_user(s, user_id)
        user.account_category = category
        user.recap_credits = recap_credits
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
    # Row lock on User MUST precede minute-balance FOR UPDATE check and both charges
    u_lock = src.index("select(User)")
    assert ".with_for_update()" in src[u_lock : u_lock + 180]
    m_lock = src.index("select(MinuteBalance)")
    assert ".with_for_update()" in src[m_lock : m_lock + 180]
    assert u_lock < m_lock < src.index("update(User)") < src.index("await consume_seconds(")
    # Only one commit releases the lock after job row + credit debit + voice hold
    assert src.index("await consume_seconds(") < src.index("await db.commit()")


@pytest.mark.asyncio
async def test_p5_concurrent_double_submit_cannot_double_charge_or_over_hold(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch, "p5_race.db")
    api_mod.RECAP_JOBS_REGISTRY.clear()
    _patch_submit_env(monkeypatch)
    uid = await _seed_user(maker, 881002, category=AccountCategory.PURCHASED.value, recap_credits=5, purchased=300)

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
        assert u.recap_credits == 4
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
        assert u.recap_credits == 3
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
        assert (await s.execute(select(RecapCreditLedgerEvent).where(RecapCreditLedgerEvent.user_id == uid))).scalars().all() == []

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

    # Complete with actual synthesized duration = 64s -> delta 36s refunded
    async with maker() as s:
        settle = await settle_recap_voice_seconds(s, uid, job_id, actual_seconds=64, note="Test settle")
        await s.commit()
        assert settle["held_seconds"] == 100
        assert settle["settled_seconds"] == 64
        assert settle["refunded_seconds"] == 36
        # Second idempotent call reports already settled 36s and must NOT double-refund
        repeat = await settle_recap_voice_seconds(s, uid, job_id, actual_seconds=64)
        assert repeat["settled_seconds"] == 64
        assert repeat["refunded_seconds"] == 36
        bal = await get_minute_balance(s, uid)
        # Started 120s, net settled 64s -> 56s remaining
        assert bal.clone_spendable_seconds == 56
        assert await net_job_seconds_held(s, uid, job_id) == 64

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_overrun_when_balance_spent_mid_render_caps_safely_never_fails(tmp_path, monkeypatch):
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
        summary = await settle_recap_voice_seconds(s, uid, job_id, actual_seconds=85)
        await s.commit()
        assert summary["held_seconds"] == 60
        assert summary["overrun_charged_seconds"] == 10
        assert summary["overrun_uncovered_seconds"] == 15
        assert summary["settled_seconds"] == 70
        bal = await get_minute_balance(s, uid)
        assert bal.clone_spendable_seconds == 0  # never negative!

    await engine.dispose()


@pytest.mark.asyncio
async def test_p5_overrun_with_caller_note_remains_idempotent_on_replay(tmp_path, monkeypatch):
    """Part C fix pin: api._execute_recap_background_job passes
    note=f'Recap clone settle ({job_id})'. Overrun debits MUST retain the
    overrun prefix so replaying settle_recap_voice_seconds never inflates
    held_seconds or double-charges after a subsequent top-up."""
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
        s1 = await settle_recap_voice_seconds(s, uid, job_id, actual_seconds=90, note=caller_note)
        await s.commit()
        assert s1["held_seconds"] == 60
        assert s1["overrun_charged_seconds"] == 10
        assert s1["overrun_uncovered_seconds"] == 20
        assert s1["settled_seconds"] == 70

        # Simulate user buying more minutes before an unexpected settle replay
        bal = await get_minute_balance(s, uid)
        bal.purchased_seconds += 100
        await s.commit()

        s2 = await settle_recap_voice_seconds(s, uid, job_id, actual_seconds=90, note=caller_note)
        await s.commit()
        assert s2 == s1
        assert s2["held_seconds"] == 60
        assert s2["overrun_charged_seconds"] == 10
        assert s2["overrun_uncovered_seconds"] == 20
        assert s2["settled_seconds"] == 70
        bal_after = await get_minute_balance(s, uid)
        assert bal_after.clone_spendable_seconds == 100  # replay must NOT debit topped-up minutes
        ev_notes = (
            await s.execute(
                select(MinuteLedgerEvent.note).where(
                    MinuteLedgerEvent.user_id == uid,
                    MinuteLedgerEvent.reference_id == job_id,
                    MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
                )
            )
        ).scalars().all()
        assert any(
            (n or "").startswith(f"Recap clone settle overrun ({job_id})") and caller_note in (n or "")
            for n in ev_notes
        )

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
    assert out1["recap_credits"] == 1
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
    assert out2["recap_credits"] == 1
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
    assert r_persona["remaining_credits"] == 2
    assert r_persona["remaining_voice_seconds"] == 75

    # 2) FREE user attempting Own Clone Voice -> _clamp_recap_voice_ref strips it cleanly to persona-only
    forged_req = _req(tts_engine="voxcpm", duration_seconds=60, voice_ref_id="webapp_ref_u881010_abcdef012345")
    async with maker() as db:
        fu = (await db.execute(select(User).where(User.id == free_uid))).scalar_one()
        r_own = await api_mod.create_recap_job(forged_req, user=fu, db=db, _gate=fu)
    assert forged_req.voice_ref_id is None
    assert api_mod.RECAP_JOBS_REGISTRY[r_own["recap_id"]]["voice_ref_used"] is False

    # 3) Gemini & Edge paths with ZERO voice minutes -> credit_only succeeds, 0 minutes touched!
    zero_min_uid = await _seed_user(
        maker, 881011, category=AccountCategory.FREE.value, recap_credits=2, purchased=0
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


# ── 5. Frontend Phase 5 Steps 3–4 UI, Option B & i18n Parity Checks ──────────


def test_p5_frontend_steps_3_4_and_option_b_controls_present():
    # Cache token pinned
    assert INDEX_HTML.count("20261114_r149") == 4

    # Step 3 controls & badges
    for eid in (
        "recap-engine-billing-hint",
        "recap-voice-billing-badge",
        "recap-own-voice-vip-tag",
        "btn-recap-own-voice-vip",
        "btn-open-recap-lang-sheet",
        "recap-lang-sheet-backdrop",
        "recap-lang-sheet-grid",
    ):
        assert f'id="{eid}"' in INDEX_HTML, eid

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
