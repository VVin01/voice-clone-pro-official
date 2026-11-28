"""Unit tests for HF Hygiene Patch — D1-A (sweep blind-spot) + D2-A (ETA clamp & log tag).

Covers:
1. D1-A (`reconcile_from_probe_rows`):
   - `rem_val is None` on an existing `"ok"` row preserves `existing["updated_at"]`
     (does not reset `stale_drained` / `aged_partial` / `never_verified` age clocks).
   - New row with `rem_val is None` stamps `now`.
   - Existing `"auth_dead"` row transitioning to `"ok"` stamps `now`.
   - Real `rem_val` (`is not None`) stamps `now`.
2. D2-A (`estimate_refill_eta_label`):
   - `deficit <= 0` branch with a stored `resets_at` ~+10 h out clamps into `[+15 min, +60 min]`
     of `now` and preserves exact 5-char `"HH:MM"` format (`len == 5 and label[2] == ":"`).
   - `deficit <= 0` with `< +15 min` floors at `+15 min`; `deficit > 0` with `> +60 min` caps at `+60 min`.
   - Stored `resets_at` ~+25 min out (inside `[+15 min, +60 min]`) is returned unchanged.
3. D2-A 2b (`app.worker` Free fail-fast log tag):
   - Logs `reason=quota_exhausted` on `VoxCPMQuotaExhaustedError` and `reason=space_timeout`
     on pure Space `TimeoutError`, keeping identical user copy and 100% refund.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import worker as worker_mod
from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Base, Job, MinuteBalance, User
from app.services import billing as billing_mod
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_rotation as kr_mod
from app.services import voice_choice as vc_mod
from app.services import voxcpm_engine as vox_mod


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _isolated_kv(monkeypatch):
    kv: dict[str, str] = {}
    monkeypatch.setattr(kr_mod, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_delete",
        lambda k: (kv.pop(str(k), None) is not None) or True,
    )
    monkeypatch.setattr(hf_ledger, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        hf_ledger,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )
    monkeypatch.setattr(
        hf_ledger,
        "botsetting_sync_delete",
        lambda k: (kv.pop(str(k), None) is not None) or True,
    )
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    vox_mod.VoxCPMEngine._last_admission_price = None
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)


@pytest_asyncio.fixture
async def hygiene_db_env(tmp_path, monkeypatch):
    import app.db.session as db_session_mod

    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_root", storage)
    monkeypatch.setattr(settings, "admin_ids_raw", "999999")
    monkeypatch.setattr(settings, "bot_token", "123456:TEST_TOKEN")

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", maker)
    try:
        yield maker, storage
    finally:
        await eng.dispose()


def test_d1a_reconcile_preserves_updated_at_on_noop_ok_row_and_stamps_now_otherwise():
    tok_ok_noop = "hf_d1a_ok_noop"
    tok_auth_dead = "hf_d1a_auth_dead_recover"
    tok_real_rem = "hf_d1a_real_rem"
    tok_new_noop = "hf_d1a_brand_new_noop"

    t0 = 1_795_800_000.0
    old_ok_ts = t0 - 5 * 3600.0  # 5 hours old before Key Health runs
    old_ok_iso = _iso(old_ok_ts)
    old_dead_iso = _iso(t0 - 12 * 3600.0)
    old_real_iso = _iso(t0 - 8 * 3600.0)

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_ok_noop): {
                    "status": "ok",
                    "remaining_s": 22.0,  # drained < 45 s
                    "base_s": 300.0,
                    "resets_at": _iso(t0 + 18 * 3600.0),
                    "cooldown_until": "",
                    "updated_at": old_ok_iso,
                },
                hf_ledger.token_digest(tok_auth_dead): {
                    "status": "auth_dead",
                    "remaining_s": 0.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": _iso(t0 + 12 * 3600.0),
                    "updated_at": old_dead_iso,
                },
                hf_ledger.token_digest(tok_real_rem): {
                    "status": "ok",
                    "remaining_s": 210.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": old_real_iso,
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )

    probe_now = t0 + 600.0
    probe_now_iso = _iso(probe_now)
    tokens = [tok_ok_noop, tok_auth_dead, tok_real_rem, tok_new_noop]
    probe_rows = [
        {"index": 0, "status": "ok", "remaining_s": None, "base_s": None, "quota_error": "quota_scope_unsupported"},
        {"index": 1, "status": "ok", "remaining_s": None, "base_s": None, "quota_error": "quota_scope_unsupported"},
        {"index": 2, "status": "ok", "remaining_s": 175.0, "base_s": 300.0, "quota_error": None},
        {"index": 3, "status": "ok", "remaining_s": None, "base_s": None, "quota_error": "quota_scope_unsupported"},
    ]

    reconciled = hf_ledger.reconcile_from_probe_rows(tokens, probe_rows, now_ts=probe_now)
    tmap = reconciled["tokens"]

    # 1. Existing "ok" row with rem_val=None MUST preserve its prior updated_at
    assert tmap[hf_ledger.token_digest(tok_ok_noop)]["updated_at"] == old_ok_iso
    assert tmap[hf_ledger.token_digest(tok_ok_noop)]["remaining_s"] == pytest.approx(22.0)

    # 2. Existing "auth_dead" row transitioning to "ok" MUST stamp now
    assert tmap[hf_ledger.token_digest(tok_auth_dead)]["status"] == "ok"
    assert tmap[hf_ledger.token_digest(tok_auth_dead)]["updated_at"] == probe_now_iso

    # 3. Existing "ok" row with real rem_val MUST stamp now
    assert tmap[hf_ledger.token_digest(tok_real_rem)]["remaining_s"] == pytest.approx(175.0)
    assert tmap[hf_ledger.token_digest(tok_real_rem)]["updated_at"] == probe_now_iso

    # 4. Brand-new row with rem_val=None MUST stamp now
    assert tmap[hf_ledger.token_digest(tok_new_noop)]["updated_at"] == probe_now_iso

    # 5. Sweep classifier check: 1.5 h after the Key Health refresh (6.5 h after old_ok_ts >= 6 h),
    #    tok_ok_noop IS classified as `stale_drained` instead of being blinded!
    sweep_ts = old_ok_ts + 6.5 * 3600.0  # only (sweep_ts - probe_now) < 6h, so previously blinded!
    assert (sweep_ts - probe_now) < 6.0 * 3600.0
    cls_res = hf_ledger.classify_token_uncertainty(tok_ok_noop, now_ts=sweep_ts)
    assert cls_res.is_uncertain is True
    assert cls_res.reason == "stale_drained"


def test_d2a_eta_clamps_distant_morning_reset_into_15_to_60_min_window():
    mmt = ZoneInfo("Asia/Yangon")
    # Reproduce 2026-11-27 23:02 MMT failure where earliest future resets_at was next day 10:19 MMT (+11h 17m)
    now_dt = datetime(2026, 11, 27, 23, 2, 0, tzinfo=mmt)
    now_ts = now_dt.timestamp()
    distant_rst_dt = datetime(2026, 11, 28, 10, 19, 0, tzinfo=mmt)  # ~+11.3 h (> +10 h)

    hf_ledger.save_pool_state(
        {
            "tokens": {
                # Plenty of pool-wide available_gpu_s so `deficit <= 0`
                hf_ledger.token_digest("hf_rich_1"): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts - 600),
                },
                hf_ledger.token_digest("hf_morning_anchor"): {
                    "status": "ok",
                    "remaining_s": 40.0,
                    "base_s": 300.0,
                    "resets_at": distant_rst_dt.astimezone(timezone.utc).isoformat(),
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts - 3600),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now_ts,
    )

    label = hf_ledger.estimate_refill_eta_label(needed_gpu_s=90.0, tz_name="Asia/Yangon", now_ts=now_ts)
    assert len(label) == 5 and label[2] == ":"
    assert label != "10:19"

    allowed_labels = {
        (now_dt + timedelta(minutes=m)).strftime("%H:%M")
        for m in range(15, 61)
    }
    assert label in allowed_labels
    assert label == (now_dt + timedelta(minutes=60)).strftime("%H:%M")  # "00:02"

    # Also verify `deficit <= 0` with a tiny +2 min timestamp floors cleanly at +15 min
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_rich_1"): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts),
                },
                hf_ledger.token_digest("hf_soon"): {
                    "status": "ok",
                    "remaining_s": 40.0,
                    "base_s": 300.0,
                    "resets_at": _iso(now_ts + 120.0),
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now_ts,
    )
    label_floor = hf_ledger.estimate_refill_eta_label(needed_gpu_s=90.0, tz_name="Asia/Yangon", now_ts=now_ts)
    assert len(label_floor) == 5 and label_floor[2] == ":"
    assert label_floor == (now_dt + timedelta(minutes=15)).strftime("%H:%M")  # "23:17"

    # Also verify `deficit > 0` with a +10 h reset caps at +60 min
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_empty"): {
                    "status": "ok",
                    "remaining_s": 0.0,
                    "base_s": 300.0,
                    "resets_at": _iso(now_ts + 10 * 3600.0),
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now_ts,
    )
    label_def_cap = hf_ledger.estimate_refill_eta_label(needed_gpu_s=120.0, tz_name="Asia/Yangon", now_ts=now_ts)
    assert len(label_def_cap) == 5 and label_def_cap[2] == ":"
    assert label_def_cap == (now_dt + timedelta(minutes=60)).strftime("%H:%M")


def test_d2a_eta_25_min_inside_horizon_returned_unchanged():
    mmt = ZoneInfo("Asia/Yangon")
    now_dt = datetime(2026, 11, 27, 18, 6, 0, tzinfo=mmt)
    now_ts = now_dt.timestamp()
    rst_25m_dt = now_dt + timedelta(minutes=25)  # 18:31 MMT (inside [+15m, +60m])
    expected_label = rst_25m_dt.strftime("%H:%M")
    assert expected_label == "18:31"

    # 1. `deficit <= 0` branch (rich pool + one refilling token at +25 min)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_rich"): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts),
                },
                hf_ledger.token_digest("hf_mid_25m"): {
                    "status": "ok",
                    "remaining_s": 35.0,
                    "base_s": 300.0,
                    "resets_at": rst_25m_dt.astimezone(timezone.utc).isoformat(),
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now_ts,
    )
    got_le = hf_ledger.estimate_refill_eta_label(needed_gpu_s=60.0, tz_name="Asia/Yangon", now_ts=now_ts)
    assert got_le == expected_label

    # 2. `deficit > 0` branch (drained pool + refilling token at +25 min)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_drained_25m"): {
                    "status": "ok",
                    "remaining_s": 0.0,
                    "base_s": 300.0,
                    "resets_at": rst_25m_dt.astimezone(timezone.utc).isoformat(),
                    "cooldown_until": "",
                    "updated_at": _iso(now_ts),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now_ts,
    )
    got_gt = hf_ledger.estimate_refill_eta_label(needed_gpu_s=60.0, tz_name="Asia/Yangon", now_ts=now_ts)
    assert got_gt == expected_label


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("err_kind", "expected_reason"),
    [
        ("quota", "quota_exhausted"),
        ("timeout", "space_timeout"),
    ],
)
async def test_d2b_worker_free_fail_fast_logs_reason_tag_without_changing_user_copy(
    hygiene_db_env, monkeypatch, caplog, err_kind, expected_reason
):
    maker, _storage = hygiene_db_env
    uid = 9101 if err_kind == "quota" else 9102
    jid = f"vc_hygiene_{err_kind}"

    async with maker() as session:
        session.add(
            User(
                id=uid,
                username=f"u_{uid}",
                first_name="FreeUser",
                language_code="my",
                account_category=AccountCategory.FREE.value,
            )
        )
        session.add(MinuteBalance(user_id=uid, welcome_seconds=600))
        await session.commit()
        await billing_mod.reserve_for_job(
            session,
            user_id=uid,
            job_id=jid,
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=60,
        )
        session.add(
            Job(
                id=jid,
                user_id=uid,
                chat_id=uid,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                input_text="မင်္ဂလာပါ ခင်ဗျာ။",
                seconds_estimated=60,
            )
        )
        await session.commit()

    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)

    def _fail_synth(self_eng, text, output_wav, **kw):
        if err_kind == "quota":
            raise vox_mod.VoxCPMQuotaExhaustedError("ZeroGPU quota exhausted")
        raise TimeoutError("VoxCPM 2 Space 'awints001/voxcpm2-a' queue timed out after 45s")

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _fail_synth)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        async with maker() as session:
            job = await session.get(Job, jid)
            ok = await worker_mod.process_single_job(session, job, bot=None)
            await session.refresh(job)
            snap = await billing_mod.get_wallet_snapshot(session, uid)

    assert ok is False
    if err_kind == "timeout":
        assert job.status == JobStatus.WAITING_CHOICE.value
        assert snap["reserved_voice_seconds"] == 60
        assert snap["available_voice_seconds"] == 540
        park_logs = [
            r.getMessage()
            for r in caplog.records
            if "dp_park_event action=parked tier=free" in r.getMessage()
            and f"job={jid}" in r.getMessage()
        ]
        assert len(park_logs) == 1
        assert f"reason={expected_reason}" in park_logs[0]
    else:
        assert job.status == JobStatus.FAILED.value
        assert "မိနစ် လုံးဝ မဖြတ်ထားပါ" in (job.error_message or "")
        assert "10m" in (job.error_message or "")
        assert snap["reserved_voice_seconds"] == 0
        assert snap["available_voice_seconds"] == 600

        ff_logs = [
            r.getMessage()
            for r in caplog.records
            if f"Free voice job {jid} failed-fast on quota/congestion" in r.getMessage()
        ]
        assert len(ff_logs) == 1
        assert f"reason={expected_reason}" in ff_logs[0]

    # Confirm `vc_mod.quota_reset_estimate_label()` still honors exact 5-char HH:MM pin
    eta_lbl = vc_mod.quota_reset_estimate_label()
    assert len(eta_lbl) == 5 and eta_lbl[2] == ":"
