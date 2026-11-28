"""Unit & integration tests for FREE Congestion Parking, `in_flight_gpu_s` Self-Heal,
and Expiring-First Quota Walk (`hf_expiring_quota_first_enabled`) — 2026-11-28.

Covers design report Test Plan §1–5:
1. Classifier & split (`is_voxcpm_free_congestion_error` across pure timeout, failover
   budget cut, `"No GPU was available"`, pure `VoxCPMQuotaExhaustedError`, generic
   `"ZeroGPU runs limit"`, and mixed quota+timeout walks both with and without solvent tokens).
2. Multi-slice Free checkpoint reuse (`dp_refs/<job_id>/slice01.wav` + custom `ref_wav`),
   single reservation settlement on resume, and `0 s` net debit / `0` minted seconds across
   Bot `dp:cancel`, `/api/jobs/{id}/cancel`, and `2 h` expiry (incl. Asia/Yangon midnight rollover).
3. Fairness & leak guards (Paid-first sweep order, `MAX_FREE_RESUMES_PER_SWEEP = 2`,
   `virtual_committed_gpu_s`, `120 s` dwell floor, `space_cooldowns` hold, `<45 s` dust hold,
   `2 h` Free vs `24 h` Paid expiry, 1 active-or-parked job/Free user, daily-minute inclusion,
   `MAX_GLOBAL_FREE_PARKED_JOBS = 20`, and `order_voxcpm_fair_queue` `resumed_at` anchor).
4. `in_flight_gpu_s` self-heal (`1,122.0 + 39.6 = 1,161.6 -> 39.6` live / `0.0` idle,
   unblocking `hf_calibration.check_calibration_guards`, 2 h GC subtraction, crash recovery
   release) and `hf_expiring_quota_first_enabled` (`2,101 s` Pro token on Attempt 1 + `<=4 h`
   `resets_at` head ordering; default `False` preserves legacy Best-Fit).
5. Verbatim Burmese/English copy, congestion ETA `[+10m, +20m]` Yangon `HH:MM`, i18n parity,
   and Mini App `choice == "wait"` banner + chip state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import worker as worker_mod
from app.config import settings
from app.handlers import clone as clone_mod
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Base, Job, MinuteBalance, User, utc_now
from app.services import billing as billing_mod
from app.services import hf_calibration as hf_cal
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_rotation as kr_mod
from app.services import ledger as ledger_mod
from app.services import queue as queue_mod
from app.services import recovery as recovery_mod
from app.services import voice_choice as vc_mod
from app.services import voice_chunking as chunk_mod
from app.services import voxcpm_engine as vox_mod
from app.services.i18n import t
from app.webapp import api as api_mod

REPO_ROOT = Path(__file__).resolve().parents[2]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _make_speech_wav(path: Path, duration_s: float = 3.0, sr: int = 44100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(duration_s * sr))
    samples = bytearray()
    for i in range(n_frames):
        val = int(6000 * math.sin(2.0 * math.pi * 220.0 * (i / sr)))
        samples.extend(struct.pack("<h", val))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(samples))
    return path


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited_text: list[dict] = []
        self.edited_markup: list[dict] = []
        self._next_mid = 700
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **kw):
        self._next_mid += 1
        mid = self._next_mid
        self.sent.append(
            {"chat_id": chat_id, "message_id": mid, "text": text, "reply_markup": reply_markup}
        )
        return SimpleNamespace(message_id=mid)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None, **kw):
        self.edited_text.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}
        )
        return SimpleNamespace(message_id=message_id)

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup=None, **kw):
        self.edited_markup.append(
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
        )
        return True

    async def send_audio(self, chat_id: int, audio=None, **kw):
        self._next_mid += 1
        return SimpleNamespace(message_id=self._next_mid)

    async def send_document(self, chat_id: int, document=None, **kw):
        self._next_mid += 1
        return SimpleNamespace(message_id=self._next_mid)

    async def delete_message(self, chat_id: int, message_id: int, **kw):
        return True


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
    queue_mod.reset_fairness_for_tests()
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    vox_mod.VoxCPMEngine._last_admission_price = None
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    queue_mod.reset_fairness_for_tests()


@pytest_asyncio.fixture
async def park_db_env(tmp_path, monkeypatch):
    import app.db.session as db_session_mod

    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_root", storage)
    monkeypatch.setattr(settings, "admin_ids_raw", "999999")
    monkeypatch.setattr(settings, "bot_token", "123456:TEST_TOKEN")
    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(clone_mod, "AsyncSessionLocal", maker)
    try:
        yield maker, storage
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# §1 — Classifier & Congestion vs Quota Split
# ---------------------------------------------------------------------------


def test_1a_classifier_pure_congestion_vs_pure_quota_and_mixed_walk():
    now = 1795866000.0
    # Pure congestion markers -> True
    assert vox_mod.is_voxcpm_free_congestion_error(
        TimeoutError("VoxCPM 2 Space 'awints001/voxcpm2-a' queue timed out after 45s")
    ) is True
    assert vox_mod.is_voxcpm_free_congestion_error(
        vox_mod._FailoverBudgetExhaustedError("VoxCPM 2 total failover budget exhausted (120s)")
    ) is True
    assert vox_mod.is_voxcpm_free_congestion_error(
        RuntimeError("No GPU was available after waiting 60s")
    ) is True

    # Pure quota exhaustion -> False (even if wrapping a TimeoutError cause)
    wrapped_quota = vox_mod.VoxCPMQuotaExhaustedError("Pool exhausted")
    wrapped_quota.__cause__ = TimeoutError("earlier timeout")
    assert vox_mod.is_voxcpm_free_congestion_error(wrapped_quota) is False

    # Generic HF runs-limit refusal -> capacity error True, Free congestion False (fail-fast!)
    runs_limit_err = RuntimeError("You have reached the ZeroGPU runs limit for today")
    assert vox_mod.is_voxcpm_capacity_or_congestion_error(runs_limit_err) is True
    assert vox_mod.is_voxcpm_free_congestion_error(runs_limit_err) is False

    # Mixed walk #1: 2 quota refusals + 2 Space timeouts, >=1 solvent token at 45s door -> True (park!)
    tok_dead = "hf_dead_tok"
    tok_solvent = "hf_solvent_tok"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_dead): {
                    "status": "ok",
                    "remaining_s": 0.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": _iso(now + 1800),
                    "updated_at": _iso(now),
                },
                hf_ledger.token_digest(tok_solvent): {
                    "status": "ok",
                    "remaining_s": 210.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )
    mixed_eng_ok = SimpleNamespace(
        hf_tokens=[tok_dead, tok_solvent],
        quota_refusals=2,
        space_timeouts={"awints001/voxcpm2-a": 2},
        job_memory=None,
        pinned_space="awints001/voxcpm2-a",
    )
    mixed_err = RuntimeError("VoxCPM 2 speech synthesis failed: queue timed out after 45s")
    assert vox_mod.is_voxcpm_free_congestion_error(
        mixed_err, mixed_eng_ok, req_gate_s=60.0
    ) is True

    # Mixed walk #2: 2 quota refusals + 2 Space timeouts, ONLY <45s dust tokens left -> False (fail-fast!)
    tok_dust_a = "hf_dust_20s"
    tok_dust_b = "hf_dust_35s"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_dust_a): {
                    "status": "ok",
                    "remaining_s": 20.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
                hf_ledger.token_digest(tok_dust_b): {
                    "status": "ok",
                    "remaining_s": 35.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )
    mixed_eng_dust = SimpleNamespace(
        hf_tokens=[tok_dust_a, tok_dust_b],
        quota_refusals=2,
        space_timeouts={"awints001/voxcpm2-a": 2},
        job_memory=None,
        pinned_space="awints001/voxcpm2-a",
    )
    assert vox_mod.is_voxcpm_free_congestion_error(
        mixed_err, mixed_eng_dust, req_gate_s=45.0
    ) is False


@pytest.mark.asyncio
async def test_1b_worker_free_congestion_parks_while_runs_limit_and_mixed_exhausted_fail_fast(
    park_db_env, monkeypatch, caplog
):
    maker, _storage = park_db_env
    bot = _FakeBot()

    # Paid choice-state JSON stays byte-identical (exact 11 keys, omits new Free fields)
    paid_st = vc_mod.new_choice_state("clone", completed_units=1, total_units=2, needed_gpu_s=90.0)
    assert set(paid_st.keys()) == {
        "v",
        "kind",
        "choice",
        "parked_at",
        "prompt_chat_id",
        "prompt_message_id",
        "completed_units",
        "total_units",
        "needed_gpu_s",
        "pinned_space",
        "worker_host",
    }

    async def _seed_free_job(uid: int, jid: str) -> None:
        async with maker() as s:
            s.add(
                User(
                    id=uid,
                    username=f"u{uid}",
                    first_name="FreeUser",
                    language_code="my",
                    account_category=AccountCategory.FREE.value,
                )
            )
            s.add(
                MinuteBalance(
                    user_id=uid,
                    daily_free_seconds=300,
                    last_daily_grant_date=ledger_mod.get_current_daily_period(),
                )
            )
            await s.commit()
            await billing_mod.reserve_for_job(
                s,
                user_id=uid,
                job_id=jid,
                job_type=JobType.VOICE_CLONE.value,
                billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
                reserve_recap_seconds=0,
                reserve_voice_seconds=60,
            )
            s.add(
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
            await s.commit()

    # Case 1: "No GPU was available" -> WAITING_CHOICE + Free prompt + Wait/Cancel buttons only
    await _seed_free_job(3101, "vc_free_nogpu")
    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "synthesize",
        lambda self_e, *a, **kw: (_ for _ in ()).throw(
            RuntimeError("No GPU was available after 60s")
        ),
    )
    async with maker() as s:
        j1 = await s.get(Job, "vc_free_nogpu")
        await worker_mod.process_single_job(s, j1, bot=bot)
        await s.refresh(j1)
        snap1 = await billing_mod.get_wallet_snapshot(s, 3101)
    assert j1.status == JobStatus.WAITING_CHOICE.value
    assert snap1["reserved_voice_seconds"] == 60
    st1 = vc_mod.parse_choice_state(j1.voice_choice_state)
    assert st1["tier"] == "free"
    assert st1["park_reason"] == "congestion"
    assert len(bot.sent) == 1
    assert "အခမဲ့ မိနစ် လုံးဝ မဖြတ်ထားပါဘူး" in bot.sent[0]["text"]
    cb_datas = [
        btn.callback_data
        for row in bot.sent[0]["reply_markup"].inline_keyboard
        for btn in row
    ]
    btn_texts = [
        btn.text
        for row in bot.sent[0]["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert cb_datas == ["dp:wait:vc_free_nogpu", "dp:cancel:vc_free_nogpu"]
    assert btn_texts[1] == t("dp_btn_cancel_free", "my")

    # Case 2: Generic "ZeroGPU runs limit" -> FAILED + exact worker_error_quota_exhausted + 0s debit
    await _seed_free_job(3102, "vc_free_runslim")
    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "synthesize",
        lambda self_e, *a, **kw: (_ for _ in ()).throw(
            RuntimeError("Exceeded ZeroGPU runs limit for today")
        ),
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        async with maker() as s:
            j2 = await s.get(Job, "vc_free_runslim")
            await worker_mod.process_single_job(s, j2, bot=bot)
            await s.refresh(j2)
            snap2 = await billing_mod.get_wallet_snapshot(s, 3102)
    assert j2.status == JobStatus.FAILED.value
    assert j2.voice_choice_state is None
    assert "မိနစ် လုံးဝ မဖြတ်ထားပါ" in (j2.error_message or "")
    assert snap2["reserved_voice_seconds"] == 0
    assert snap2["available_voice_seconds"] == 300
    assert any(
        "Free voice job vc_free_runslim failed-fast" in r.getMessage()
        and "reason=quota_exhausted" in r.getMessage()
        for r in caplog.records
    )

    # Case 3 & 4: Mixed walk with solvent token parks (reason=mixed_congestion); with 0 solvent fails fast
    now = time.time()
    tok_ok = "hf_mix_ok"
    tok_dust = "hf_mix_dust"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_ok): {
                    "status": "ok",
                    "remaining_s": 180.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
                hf_ledger.token_digest(tok_dust): {
                    "status": "ok",
                    "remaining_s": 20.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )

    await _seed_free_job(3103, "vc_free_mix_park")

    def _mix_solvent_synth(self_e, *a, **kw):
        self_e.hf_tokens = [tok_dust, tok_ok]
        self_e.job_memory.record_quota_refusal(1.0, token=tok_dust)
        self_e.job_memory.record_space_timeout("awints001/voxcpm2-a")
        raise TimeoutError("queue timed out after 45s")

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _mix_solvent_synth)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        async with maker() as s:
            j3 = await s.get(Job, "vc_free_mix_park")
            await worker_mod.process_single_job(s, j3, bot=None)
            await s.refresh(j3)
    assert j3.status == JobStatus.WAITING_CHOICE.value
    assert any(
        "dp_park_event action=parked tier=free" in r.getMessage()
        and "job=vc_free_mix_park" in r.getMessage()
        and "reason=mixed_congestion" in r.getMessage()
        for r in caplog.records
    )

    await _seed_free_job(3104, "vc_free_mix_fail")

    def _mix_exhausted_synth(self_e, *a, **kw):
        self_e.hf_tokens = [tok_dust]
        self_e.job_memory.record_quota_refusal(1.0, token=tok_dust)
        self_e.job_memory.record_space_timeout("awints001/voxcpm2-a")
        raise TimeoutError("queue timed out after 45s")

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _mix_exhausted_synth)
    async with maker() as s:
        j4 = await s.get(Job, "vc_free_mix_fail")
        await worker_mod.process_single_job(s, j4, bot=None)
        await s.refresh(j4)
        snap4 = await billing_mod.get_wallet_snapshot(s, 3104)
    assert j4.status == JobStatus.FAILED.value
    assert snap4["reserved_voice_seconds"] == 0
    assert snap4["available_voice_seconds"] == 300


# ---------------------------------------------------------------------------
# §2 — Multi-Slice Free Checkpoint, Custom Ref & Billing Invariance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2a_multi_slice_free_checkpoint_custom_ref_preservation_and_single_settlement(
    park_db_env, tmp_path, monkeypatch, caplog
):
    maker, storage = park_db_env
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    from app.services.number_words import normalize_numbers_for_speech

    text_2slices = " ".join(
        f"အပိုင်း {i + 1:02d} ကျောင်းသားလူငယ်လေးများသည် တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်စွာ လမ်းလျှောက်လာကြပါသည်။"
        for i in range(15)
    )
    expected_slices = chunk_mod.plan_session_slices(normalize_numbers_for_speech(text_2slices))
    assert len(expected_slices) == 2

    # Custom user voice sample uploaded into a temp path
    temp_upload_dir = storage / "temp" / "upload_free_1"
    custom_ref = _make_speech_wav(temp_upload_dir / "custom_ref.wav", duration_s=6.0)

    uid = 4101
    jid = "vc_free_2slice"
    bot = _FakeBot()
    async with maker() as s:
        s.add(
            User(
                id=uid,
                username="free_2slice",
                first_name="FreeSlice",
                language_code="my",
                account_category=AccountCategory.FREE.value,
            )
        )
        s.add(
            MinuteBalance(
                user_id=uid,
                daily_free_seconds=300,
                last_daily_grant_date=ledger_mod.get_current_daily_period(),
            )
        )
        await s.commit()
        await billing_mod.reserve_for_job(
            s,
            user_id=uid,
            job_id=jid,
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=90,
        )
        s.add(
            Job(
                id=jid,
                user_id=uid,
                chat_id=uid,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                source_audio_path=str(custom_ref),
                input_text=text_2slices,
                seconds_estimated=90,
            )
        )
        await s.commit()

    run1_calls: list[tuple[bool, str]] = []

    def _run1_synth(self_e, text, output_wav, *, session_mode=False, reference_wav=None, **kw):
        assert reference_wav is not None and Path(reference_wav).is_file()
        run1_calls.append((bool(session_mode), text))
        if session_mode and text == expected_slices[0]:
            self_e.pinned_space = "awints001/voxcpm2-b"
            return _make_speech_wav(Path(output_wav), duration_s=28.0)
        raise TimeoutError("ZeroGPU queue timeout on slice 2")

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _run1_synth)

    async with maker() as s:
        j = await s.get(Job, jid)
        ok1 = await worker_mod.process_single_job(s, j, bot=bot)
        await s.refresh(j)
        snap_park = await billing_mod.get_wallet_snapshot(s, uid)

    # Simulate temp upload cleanup after Run 1
    custom_ref.unlink(missing_ok=True)

    assert ok1 is False
    assert j.status == JobStatus.WAITING_CHOICE.value
    assert snap_park["reserved_voice_seconds"] == 90
    assert hf_ledger.load_pool_state()["in_flight_gpu_s"] == 0
    assert (storage / "dp_refs" / jid / "slice01.wav").is_file()
    assert (storage / "dp_refs" / f"{jid}.wav").is_file()
    assert j.source_audio_path == str(storage / "dp_refs" / f"{jid}.wav")

    st_park = vc_mod.parse_choice_state(j.voice_choice_state)
    assert st_park["completed_units"] == 1
    assert st_park["total_units"] == 2
    assert st_park["tier"] == "free"
    assert st_park["pinned_space"] == "awints001/voxcpm2-b"

    # Run 2 (resumed): Slice 1 reused from dp_refs (0 re-synthesis of Slice 1), custom ref intact,
    # settles reservation ONCE (40s actual out of 90s reserved -> 260s left), cleans dp_refs!
    run2_calls: list[tuple[bool, str, str | None]] = []

    def _run2_synth(self_e, text, output_wav, *, session_mode=False, reference_wav=None, **kw):
        assert reference_wav is not None and Path(reference_wav).is_file()
        run2_calls.append((bool(session_mode), text, self_e.pinned_space))
        return _make_speech_wav(Path(output_wav), duration_s=28.0)

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _run2_synth)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        async with maker() as s:
            j = await s.get(Job, jid)
            j.status = JobStatus.QUEUED.value
            await s.commit()
            ok2 = await worker_mod.process_single_job(s, j, bot=bot)
            await s.refresh(j)
            snap_done = await billing_mod.get_wallet_snapshot(s, uid)

    assert ok2 is True
    assert j.status == JobStatus.SUCCESS.value
    assert run2_calls == [(True, expected_slices[1], "awints001/voxcpm2-b")]
    assert snap_done["reserved_voice_seconds"] == 0
    assert snap_done["available_voice_seconds"] == 300 - int(j.seconds_consumed)
    assert not (storage / "dp_refs" / jid).exists()
    assert not (storage / "dp_refs" / f"{jid}.wav").exists()
    assert len(bot.edited_markup) >= 1
    assert any(
        "dp_park_event action=completed_after_park tier=free kind=clone" in r.getMessage()
        and f"job={jid}" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_2b_billing_invariance_bot_cancel_api_cancel_and_midnight_rollover_expiry(
    park_db_env, monkeypatch
):
    maker, storage = park_db_env

    async def _create_parked_free_job(uid: int, jid: str, *, parked_at_ts: float) -> None:
        ckpt_dir = storage / "dp_refs" / jid
        _make_speech_wav(ckpt_dir / "slice01.wav", duration_s=2.0)
        ref_copy = _make_speech_wav(storage / "dp_refs" / f"{jid}.wav", duration_s=2.0)
        st = vc_mod.new_choice_state(
            vc_mod.KIND_CLONE,
            completed_units=1,
            total_units=2,
            needed_gpu_s=60.0,
            tier="free",
            park_reason="congestion",
            next_door_s=45.0,
        )
        st["parked_at"] = parked_at_ts
        st["prompt_chat_id"] = uid
        st["prompt_message_id"] = 888
        async with maker() as s:
            s.add(
                User(
                    id=uid,
                    username=f"u{uid}",
                    first_name="Free",
                    language_code="my",
                    account_category=AccountCategory.FREE.value,
                )
            )
            s.add(
                MinuteBalance(
                    user_id=uid,
                    daily_free_seconds=300,
                    last_daily_grant_date=ledger_mod.get_current_daily_period(),
                )
            )
            await s.commit()
            await billing_mod.reserve_for_job(
                s,
                user_id=uid,
                job_id=jid,
                job_type=JobType.VOICE_CLONE.value,
                billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
                reserve_recap_seconds=0,
                reserve_voice_seconds=60,
            )
            s.add(
                Job(
                    id=jid,
                    user_id=uid,
                    chat_id=uid,
                    status=JobStatus.WAITING_CHOICE.value,
                    job_type=JobType.VOICE_CLONE.value,
                    engine="voxcpm",
                    source_audio_path=str(ref_copy),
                    input_text="မင်္ဂလာပါ",
                    seconds_estimated=60,
                    voice_choice_state=vc_mod.dump_choice_state(st),
                )
            )
            await s.commit()

    # 1. Bot `dp:cancel:<job_id>` -> 0s net debit, Free cancel toast, dp_refs deleted, buttons retired
    await _create_parked_free_job(4201, "vc_cancel_bot", parked_at_ts=time.time() - 300)
    bot_cb = _FakeBot()
    answered: list[str] = []
    fake_callback = SimpleNamespace(
        data="dp:cancel:vc_cancel_bot",
        from_user=SimpleNamespace(id=4201),
        message=SimpleNamespace(chat=SimpleNamespace(id=4201), message_id=888),
        bot=bot_cb,
        answer=AsyncMock(side_effect=lambda txt, **k: answered.append(txt)),
    )
    await clone_mod.handle_voice_choice_callback(fake_callback)
    async with maker() as s:
        j_bot = await s.get(Job, "vc_cancel_bot")
        mb_bot = await ledger_mod.get_minute_balance(s, 4201)
        snap_bot = await billing_mod.get_wallet_snapshot(s, 4201)
    assert j_bot.status == JobStatus.CANCELLED.value
    assert answered == [t("dp_choice_cancel_answer_free", "my")]
    assert snap_bot["reserved_voice_seconds"] == 0
    assert mb_bot.daily_free_seconds == 300
    assert mb_bot.purchased_seconds == 0 and mb_bot.referral_voice_seconds == 0
    assert not (storage / "dp_refs" / "vc_cancel_bot").exists()
    assert len(bot_cb.edited_markup) == 1

    # 2. Generic `/api/jobs/{id}/cancel` refund/cleanup helper -> cleans dp_refs & retires Bot prompt
    await _create_parked_free_job(4202, "vc_cancel_api", parked_at_ts=time.time() - 200)
    api_bot = _FakeBot()
    monkeypatch.setattr(api_mod, "create_telegram_bot", lambda *a, **kw: api_bot)
    async with maker() as s:
        j_api = await s.get(Job, "vc_cancel_api")
        j_api.status = JobStatus.CANCELLED.value
        await s.commit()
        await api_mod._refund_cancelled_non_recap_job(s, j_api, actor="user")
        snap_api = await billing_mod.get_wallet_snapshot(s, 4202)
    assert snap_api["reserved_voice_seconds"] == 0
    assert snap_api["available_voice_seconds"] == 300
    assert not (storage / "dp_refs" / "vc_cancel_api").exists()
    assert not (storage / "dp_refs" / "vc_cancel_api.wav").exists()
    assert len(api_bot.edited_markup) == 1

    # 3. 2h expiry across Asia/Yangon midnight rollover -> 0 minted seconds, exact 300s daily free
    await _create_parked_free_job(
        4203, "vc_expire_midnight", parked_at_ts=time.time() - (vc_mod.FREE_PARK_SAFETY_VALVE_SECONDS + 30)
    )
    # Simulate midnight rollover in Asia/Yangon resetting daily_free_seconds to fresh daily grant
    async with maker() as s:
        mb_mid = await ledger_mod.get_minute_balance(s, 4203)
        mb_mid.last_daily_grant_date = "2026-11-27"
        await s.commit()
        await ledger_mod.grant_daily_free_allowance(s, 4203)
        await s.commit()
        await s.refresh(mb_mid)
        fresh_daily_s = int(mb_mid.daily_free_seconds)
    assert fresh_daily_s > 0

    exp_bot = _FakeBot()
    res_sweep = await vc_mod.sweep_parked_jobs(bot=exp_bot)
    assert res_sweep["expired"] == 1

    async with maker() as s:
        j_exp = await s.get(Job, "vc_expire_midnight")
        mb_exp = await ledger_mod.get_minute_balance(s, 4203)
        snap_exp = await billing_mod.get_wallet_snapshot(s, 4203)
    assert j_exp.status == JobStatus.FAILED.value
    assert j_exp.error_message == t("dp_park_expired_error_free", "my")
    assert snap_exp["reserved_voice_seconds"] == 0
    assert mb_exp.daily_free_seconds == fresh_daily_s
    assert mb_exp.purchased_seconds == 0
    assert mb_exp.referral_voice_seconds == 0
    assert mb_exp.welcome_seconds == 0
    assert not (storage / "dp_refs" / "vc_expire_midnight").exists()
    assert len(exp_bot.edited_markup) >= 1


# ---------------------------------------------------------------------------
# §3 — Fairness, Caps & Leak Guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_3a_sweep_paid_first_max_2_free_virtual_capacity_and_2h_vs_24h_valves(
    park_db_env, caplog
):
    maker, _storage = park_db_env
    now = time.time()

    # Seed pool with 400s usable capacity (enough for 1 Paid 60s*1.25=75s + 2 Free 60s*1.25=150s)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("tok_a"): {
                    "status": "ok",
                    "remaining_s": 250.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
                hf_ledger.token_digest("tok_b"): {
                    "status": "ok",
                    "remaining_s": 250.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                },
            },
            "space_gates": {"openbmb/VoxCPM-Demo": 60.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )

    async with maker() as s:
        s.add(User(id=5100, username="paid1", first_name="P", account_category=AccountCategory.PURCHASED.value))
        for uid in (5101, 5102, 5103):
            s.add(User(id=uid, username=f"f{uid}", first_name="F", account_category=AccountCategory.FREE.value))

        # Free jobs have OLDER parked_at than Paid, yet Paid wakes first and only 2 Free wake!
        for idx, uid in enumerate((5101, 5102, 5103), 1):
            fst = vc_mod.new_choice_state(
                vc_mod.KIND_CLONE, needed_gpu_s=60.0, tier="free", park_reason="congestion", next_door_s=45.0
            )
            fst["parked_at"] = now - (600 - idx * 10)
            s.add(
                Job(
                    id=f"vc_free_{idx}",
                    user_id=uid,
                    chat_id=uid,
                    status=JobStatus.WAITING_CHOICE.value,
                    job_type=JobType.VOICE_CLONE.value,
                    engine="voxcpm",
                    input_text="free",
                    voice_choice_state=vc_mod.dump_choice_state(fst),
                )
            )
        pst = vc_mod.new_choice_state(vc_mod.KIND_CLONE, needed_gpu_s=60.0)
        pst["parked_at"] = now - 30
        s.add(
            Job(
                id="vc_paid_1",
                user_id=5100,
                chat_id=5100,
                status=JobStatus.WAITING_CHOICE.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                input_text="paid",
                voice_choice_state=vc_mod.dump_choice_state(pst),
            )
        )
        await s.commit()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.voice_choice"):
        counts = await vc_mod.sweep_parked_jobs(bot=None)
    assert counts == {"resumed": 3, "expired": 0, "still_parked": 1}
    async with maker() as s:
        assert (await s.get(Job, "vc_paid_1")).status == JobStatus.QUEUED.value
        assert (await s.get(Job, "vc_free_1")).status == JobStatus.QUEUED.value
        assert (await s.get(Job, "vc_free_2")).status == JobStatus.QUEUED.value
        assert (await s.get(Job, "vc_free_3")).status == JobStatus.WAITING_CHOICE.value
    assert any(
        "dp_park_sweep_summary resumed_paid=1 resumed_free=2 expired_paid=0 expired_free=0 still_parked_paid=0 still_parked_free=1"
        in r.getMessage()
        for r in caplog.records
    )

    # Virtual capacity guard: 180s available; Paid wakes taking 120*1.25=150s -> 30s virtual left (< 60*1.25=75s) -> Free stays parked!
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("tok_tight"): {
                    "status": "ok",
                    "remaining_s": 180.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                }
            },
            "space_gates": {"openbmb/VoxCPM-Demo": 60.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )
    async with maker() as s:
        # Clear vc_free_3 first, then seed tight Paid + Free
        j_old = await s.get(Job, "vc_free_3")
        j_old.status = JobStatus.CANCELLED.value
        pst2 = vc_mod.new_choice_state(vc_mod.KIND_CLONE, needed_gpu_s=120.0)
        pst2["parked_at"] = now - 300
        fst_tight = vc_mod.new_choice_state(
            vc_mod.KIND_CLONE, needed_gpu_s=60.0, tier="free", park_reason="congestion", next_door_s=45.0
        )
        fst_tight["parked_at"] = now - 300
        s.add(Job(id="vc_paid_tight", user_id=5100, chat_id=5100, status=JobStatus.WAITING_CHOICE.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="p", voice_choice_state=vc_mod.dump_choice_state(pst2)))
        s.add(Job(id="vc_free_tight", user_id=5101, chat_id=5101, status=JobStatus.WAITING_CHOICE.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="f", voice_choice_state=vc_mod.dump_choice_state(fst_tight)))
        await s.commit()

    c_tight = await vc_mod.sweep_parked_jobs(bot=None)
    assert c_tight == {"resumed": 1, "expired": 0, "still_parked": 1}
    async with maker() as s:
        assert (await s.get(Job, "vc_paid_tight")).status == JobStatus.QUEUED.value
        assert (await s.get(Job, "vc_free_tight")).status == JobStatus.WAITING_CHOICE.value

    # 2h Free valve vs 24h Paid valve when capacity is 0: at 2h+10s, Free expires while Paid stays parked!
    hf_ledger.save_pool_state(
        {"tokens": {}, "space_gates": {}, "in_flight_gpu_s": 0.0}, now_ts=now
    )
    async with maker() as s:
        j_ft = await s.get(Job, "vc_free_tight")
        st_ft = vc_mod.parse_choice_state(j_ft.voice_choice_state)
        st_ft["parked_at"] = time.time() - 7210
        j_ft.voice_choice_state = vc_mod.dump_choice_state(st_ft)

        pst_2h = vc_mod.new_choice_state(vc_mod.KIND_CLONE, needed_gpu_s=60.0)
        pst_2h["parked_at"] = time.time() - 7210
        s.add(Job(id="vc_paid_2h", user_id=5100, chat_id=5100, status=JobStatus.WAITING_CHOICE.value, job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="p2h", voice_choice_state=vc_mod.dump_choice_state(pst_2h)))
        await s.commit()

    c_valve = await vc_mod.sweep_parked_jobs(bot=None)
    assert c_valve == {"resumed": 0, "expired": 1, "still_parked": 1}
    async with maker() as s:
        assert (await s.get(Job, "vc_free_tight")).status == JobStatus.FAILED.value
        assert (await s.get(Job, "vc_paid_2h")).status == JobStatus.WAITING_CHOICE.value


@pytest.mark.asyncio
async def test_3b_sweep_free_leak_guards_dwell_cooling_spaces_and_dust_tokens(
    park_db_env, monkeypatch
):
    maker, _storage = park_db_env
    now = time.time()
    monkeypatch.setattr(
        vox_mod, "get_configured_spaces", lambda: ["awints001/voxcpm2-a", "awints001/voxcpm2-b"]
    )

    async with maker() as s:
        s.add(User(id=5201, username="f5201", first_name="F", account_category=AccountCategory.FREE.value))
        fst = vc_mod.new_choice_state(
            vc_mod.KIND_CLONE, needed_gpu_s=60.0, tier="free", park_reason="congestion", next_door_s=45.0
        )
        # Guard 1: parked only 30s ago (< 120s dwell floor)
        fst["parked_at"] = now - 30.0
        s.add(
            Job(
                id="vc_guard_free",
                user_id=5201,
                chat_id=5201,
                status=JobStatus.WAITING_CHOICE.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                input_text="guard",
                voice_choice_state=vc_mod.dump_choice_state(fst),
            )
        )
        await s.commit()

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("tok_ok"): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                }
            },
            "space_gates": {"awints001/voxcpm2-a": 60.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )
    assert (await vc_mod.sweep_parked_jobs())["resumed"] == 0

    # Advance age past 120s dwell, but put BOTH configured GPU Spaces into cooldown -> still held!
    async with maker() as s:
        j = await s.get(Job, "vc_guard_free")
        st = vc_mod.parse_choice_state(j.voice_choice_state)
        st["parked_at"] = time.time() - 300.0
        j.voice_choice_state = vc_mod.dump_choice_state(st)
        await s.commit()

    hf_ledger.record_space_timeout("awints001/voxcpm2-a", cooldown_seconds=600.0)
    hf_ledger.record_space_timeout("awints001/voxcpm2-b", cooldown_seconds=600.0)
    assert (await vc_mod.sweep_parked_jobs())["resumed"] == 0

    # Clear Space cooldowns, but replace tokens with 50 x 35s = 1,750s dust (< 45s session door) -> still held!
    dust_tokens = {
        hf_ledger.token_digest(f"dust_{i}"): {
            "status": "ok",
            "remaining_s": 35.0,
            "base_s": 300.0,
            "resets_at": "",
            "cooldown_until": "",
            "updated_at": _iso(time.time()),
        }
        for i in range(50)
    }
    hf_ledger.save_pool_state(
        {
            "tokens": dust_tokens,
            "space_gates": {"awints001/voxcpm2-a": 30.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=time.time(),
    )
    assert (await vc_mod.sweep_parked_jobs())["resumed"] == 0

    # Add 1 solvent token >= 45s with non-cooling Space and dwell >= 120s -> wakes cleanly!
    dust_tokens[hf_ledger.token_digest("solvent_one")] = {
        "status": "ok",
        "remaining_s": 120.0,
        "base_s": 300.0,
        "resets_at": "",
        "cooldown_until": "",
        "updated_at": _iso(time.time()),
    }
    hf_ledger.save_pool_state(
        {
            "tokens": dust_tokens,
            "space_gates": {"awints001/voxcpm2-a": 45.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=time.time(),
    )
    assert (await vc_mod.sweep_parked_jobs())["resumed"] == 1


@pytest.mark.asyncio
async def test_3c_free_queue_caps_global_20_cap_and_resumed_anti_starvation_anchor(
    park_db_env, monkeypatch, caplog
):
    maker, _storage = park_db_env
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 2)  # 120s daily cap

    async with maker() as s:
        s.add(User(id=5301, username="f5301", first_name="F", account_category=AccountCategory.FREE.value))
        s.add(
            MinuteBalance(
                user_id=5301,
                daily_free_seconds=300,
                last_daily_grant_date=ledger_mod.get_current_daily_period(),
            )
        )
        s.add(User(id=5302, username="p5302", first_name="P", account_category=AccountCategory.PURCHASED.value))
        s.add(MinuteBalance(user_id=5302, purchased_seconds=900))
        # Seed 1 WAITING_CHOICE job for Free user 5301 and 1 for Paid user 5302
        s.add(
            Job(
                id="vc_f_parked",
                user_id=5301,
                chat_id=5301,
                status=JobStatus.WAITING_CHOICE.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                input_text="မ" * 1800,  # >100s estimate
            )
        )
        s.add(
            Job(
                id="vc_p_parked",
                user_id=5302,
                chat_id=5302,
                status=JobStatus.WAITING_CHOICE.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                input_text="မင်္ဂလာပါ",
            )
        )
        await s.commit()

        # 1. Free user blocked by CONCURRENT_JOB_LIMIT_EXCEEDED while 1 job is WAITING_CHOICE
        j_blocked, err_conc = await queue_mod.create_voice_clone_job(
            s, user_id=5301, text="မင်္ဂလာပါ", preset_voice="komin"
        )
        assert j_blocked is None
        assert err_conc == "CONCURRENT_JOB_LIMIT_EXCEEDED"

        # Paid user with 1 WAITING_CHOICE job is NOT blocked by concurrency check
        j_paid_ok, st_paid = await queue_mod.create_voice_clone_job(
            s, user_id=5302, text="မင်္ဂလာပါ", preset_voice="komin"
        )
        assert j_paid_ok is not None and st_paid == JobStatus.QUEUED.value

        # 2. Lift max_concurrent_jobs_per_user to 2 -> Free WAITING_CHOICE still counts against daily minutes!
        monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 2)
        j_min_blocked, err_min = await queue_mod.create_voice_clone_job(
            s, user_id=5301, text="မ" * 600, preset_voice="komin"
        )
        assert j_min_blocked is None
        assert err_min == "DAILY_MINUTE_LIMIT_EXCEEDED"

    # 3. Global cap MAX_GLOBAL_FREE_PARKED_JOBS = 20 -> 21st Free timeout fails fast (reason=free_park_cap)
    async with maker() as s:
        for i in range(19):
            uid_i = 5400 + i
            s.add(User(id=uid_i, username=f"f{uid_i}", first_name="F", account_category=AccountCategory.FREE.value))
            s.add(
                Job(
                    id=f"vc_global_{i}",
                    user_id=uid_i,
                    chat_id=uid_i,
                    status=JobStatus.WAITING_CHOICE.value,
                    job_type=JobType.VOICE_CLONE.value,
                    engine="voxcpm",
                    input_text="x",
                )
            )
        # Total Free WAITING_CHOICE is now 1 (vc_f_parked) + 19 = 20!
        s.add(User(id=5499, username="f5499", first_name="F", account_category=AccountCategory.FREE.value))
        s.add(
            MinuteBalance(
                user_id=5499,
                daily_free_seconds=300,
                last_daily_grant_date=ledger_mod.get_current_daily_period(),
            )
        )
        await s.commit()
        await billing_mod.reserve_for_job(
            s,
            user_id=5499,
            job_id="vc_global_21",
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=60,
        )
        s.add(
            Job(
                id="vc_global_21",
                user_id=5499,
                chat_id=5499,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                input_text="မင်္ဂလာပါ",
                seconds_estimated=60,
            )
        )
        await s.commit()
        assert await queue_mod.count_free_parked_clone_jobs(s) == 20

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "synthesize",
        lambda self_e, *a, **kw: (_ for _ in ()).throw(TimeoutError("Space queue timed out")),
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        async with maker() as s:
            j21 = await s.get(Job, "vc_global_21")
            await worker_mod.process_single_job(s, j21, bot=None)
            await s.refresh(j21)
            snap21 = await billing_mod.get_wallet_snapshot(s, 5499)
    assert j21.status == JobStatus.FAILED.value
    assert snap21["reserved_voice_seconds"] == 0
    assert any("reason=free_park_cap" in r.getMessage() for r in caplog.records)

    # 4. order_voxcpm_fair_queue: Free job created 40m ago & resumed 10s ago does NOT starve new Paid jobs!
    now_q = 1795870000.0
    dt_40m_ago = datetime.fromtimestamp(now_q - 2400.0, tz=timezone.utc)
    dt_5s_ago = datetime.fromtimestamp(now_q - 5.0, tz=timezone.utc)

    resumed_free_state = vc_mod.new_choice_state(
        vc_mod.KIND_CLONE, tier="free", park_reason="congestion"
    )
    resumed_free_state["resumed_at"] = now_q - 10.0

    j_free_woken = Job(
        id="j_free_woken",
        user_id=1,
        status="QUEUED",
        created_at=dt_40m_ago,
        voice_choice_state=vc_mod.dump_choice_state(resumed_free_state),
    )
    j_paid_a = Job(id="j_paid_a", user_id=2, status="QUEUED", created_at=dt_5s_ago)
    j_paid_b = Job(id="j_paid_b", user_id=3, status="QUEUED", created_at=dt_5s_ago)

    ordered = queue_mod.order_voxcpm_fair_queue(
        [j_free_woken, j_paid_a, j_paid_b],
        {"j_free_woken": False, "j_paid_a": True, "j_paid_b": True},
        {"j_free_woken": 50.0, "j_paid_a": 80.0, "j_paid_b": 90.0},
        paid_streak=0,
        now_ts=now_q,
    )
    assert [j.id for j in ordered] == ["j_paid_a", "j_paid_b", "j_free_woken"]

    # Once woken Free job waits >= 15 min AFTER `resumed_at`, anti-starvation kicks in
    ordered_after_16m = queue_mod.order_voxcpm_fair_queue(
        [j_free_woken, j_paid_a, j_paid_b],
        {"j_free_woken": False, "j_paid_a": True, "j_paid_b": True},
        {"j_free_woken": 50.0, "j_paid_a": 80.0, "j_paid_b": 90.0},
        paid_streak=0,
        now_ts=now_q + 960.0,
    )
    assert ordered_after_16m[0].id == "j_free_woken"


# ---------------------------------------------------------------------------
# §4 — `in_flight_gpu_s` Self-Heal, Crash Release & Expiring-First Walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4a_in_flight_ratchet_reproduction_reconcile_2h_gc_recovery_and_calibration_guard(
    park_db_env, monkeypatch
):
    maker, _storage = park_db_env
    now = 1795872000.0
    monkeypatch.setattr(settings, "hf_daily_calibration_enabled", True)

    # 1. Reproduce live incident state: leaked 1,122.0s floor in aj_hf:pool_state + 39.6s live job = 1,161.6s
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("tok_1"): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now),
                }
            },
            "space_gates": {"awints001/voxcpm2-a": 60.0},
            "in_flight_gpu_s": 1122.0,
        },
        now_ts=now,
    )
    tot_with_live = hf_ledger.acquire_in_flight_gpu("live_job_1", 39.6, now_ts=now)
    assert tot_with_live == pytest.approx(1161.6)

    # Calibration guard is blocked by leaked in_flight_gpu_s
    guard_blocked = await hf_cal.check_calibration_guards(
        scheduled=False, boot_age_s=1000.0, session_factory=maker, now_ts=now
    )
    assert guard_blocked.allowed is False
    assert guard_blocked.reason == "in_flight_gpu_nonzero"

    # Reconcile while `live_job_1` is RUNNING -> heals scalar from 1,161.6s to exact 39.6s!
    healed_live = hf_ledger.reconcile_in_flight_gpu(["live_job_1"], now_ts=now + 5)
    assert healed_live == pytest.approx(39.6)
    assert hf_ledger.load_pool_state(force=True)["in_flight_gpu_s"] == pytest.approx(39.6)

    # Reconcile once idle (`[]`) -> heals scalar to 0.0s AND unblocks calibration guard!
    healed_idle = hf_ledger.reconcile_in_flight_gpu([], now_ts=now + 10)
    assert healed_idle == 0.0
    assert hf_ledger.load_pool_state(force=True)["in_flight_gpu_s"] == 0.0
    guard_ok = await hf_cal.check_calibration_guards(
        scheduled=False, boot_age_s=1000.0, session_factory=maker, now_ts=now + 10
    )
    assert guard_ok.allowed is True
    assert guard_ok.reason == "ok"

    # 2. 2-hour GC subtraction in `_load_in_flight_map` kills the one-way ratchet automatically
    assert hf_ledger.acquire_in_flight_gpu("crashed_job_a", 150.0, now_ts=now) == pytest.approx(150.0)
    # 2h + 5s later, job B acquires 40.0s -> stale 150.0s is evicted AND subtracted -> 40.0s (not 190.0s!)
    after_gc = hf_ledger.acquire_in_flight_gpu(
        "fresh_job_b", 40.0, now_ts=now + hf_ledger.IN_FLIGHT_STALE_SECONDS + 5.0
    )
    assert after_gc == pytest.approx(40.0)
    hf_ledger.release_in_flight_gpu(
        "fresh_job_b", now_ts=now + hf_ledger.IN_FLIGHT_STALE_SECONDS + 6.0
    )
    assert hf_ledger.load_pool_state(force=True)["in_flight_gpu_s"] == 0.0

    # 3. `recover_stale_running_jobs` and `_fail_timed_out_job` release `in_flight_gpu_s`
    async with maker() as s:
        s.add(User(id=6101, username="u6101", first_name="U", account_category=AccountCategory.FREE.value))
        s.add(MinuteBalance(user_id=6101, daily_free_seconds=300))
        s.add(
            Job(
                id="vc_stale_crash",
                user_id=6101,
                chat_id=6101,
                status=JobStatus.RUNNING.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                input_text="stale",
                started_at=utc_now() - timedelta(hours=2),
            )
        )
        s.add(
            Job(
                id="vc_deadline_kill",
                user_id=6101,
                chat_id=6101,
                status=JobStatus.RUNNING.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                input_text="deadline",
                started_at=utc_now(),
            )
        )
        await s.commit()

    hf_ledger.acquire_in_flight_gpu("vc_stale_crash", 85.0)
    hf_ledger.acquire_in_flight_gpu("vc_deadline_kill", 65.0)
    assert hf_ledger.load_pool_state(force=True)["in_flight_gpu_s"] == pytest.approx(150.0)

    async with maker() as s:
        rec = await recovery_mod.recover_stale_running_jobs(s, stale_timeout_seconds=60)
    assert "vc_stale_crash" in rec
    assert hf_ledger.load_pool_state(force=True)["in_flight_gpu_s"] == pytest.approx(65.0)

    await worker_mod._fail_timed_out_job("vc_deadline_kill")
    assert hf_ledger.load_pool_state(force=True)["in_flight_gpu_s"] == 0.0


def test_4b_expiring_quota_first_dynamic_setting_pro_surplus_and_4h_resets_at(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    src_wav = _make_speech_wav(tmp_path / "pro.wav", duration_s=2.0)

    now = time.time()
    spaces = [f"awints001/voxcpm2-s{i}" for i in range(1, 7)]
    free_toks = [f"hf_free_{i}" for i in range(8)]
    tok_pro = "hf_yunaisbusy_pro"

    token_rows = {
        hf_ledger.token_digest(t_f): {
            "status": "ok",
            "remaining_s": 60.0 + i * 25.0,  # 60s .. 235s
            "base_s": 300.0,
            "resets_at": "",
            "cooldown_until": "",
            "updated_at": _iso(now),
        }
        for i, t_f in enumerate(free_toks)
    }
    token_rows[hf_ledger.token_digest(tok_pro)] = {
        "status": "ok",
        "remaining_s": 2101.0,
        "base_s": 2400.0,
        "resets_at": "",
        "cooldown_until": "",
        "updated_at": _iso(now),
    }
    hf_ledger.save_pool_state(
        {
            "tokens": token_rows,
            "space_gates": {sp: 45.0 for sp in spaces},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now,
    )

    attempts: list[tuple[str, str | None]] = []
    fail_first_space = {"on": False}

    class _FakeClient:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            attempts.append((self.sp, self.tok))
            if fail_first_space["on"] and len(attempts) == 1:
                raise TimeoutError("Space 1 busy")
            return SimpleNamespace(
                done=lambda: True, status=lambda: None, result=lambda: str(src_wav)
            )

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient(sp, hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w",
        spaces=spaces,
        hf_tokens=[*free_toks, tok_pro],
    )

    # 1. Default OFF (`hf_expiring_quota_first_enabled=False`): Best-Fit picks smallest sufficient free token
    assert settings.hf_expiring_quota_first_enabled is False
    attempts.clear()
    eng.synthesize(
        "က" * 100,
        tmp_path / "out_off.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert attempts[0][1] == free_toks[0]  # 60s Best-Fit token, NOT tok_pro!

    # 2. Flag ON (`hf_expiring_quota_first_enabled=True`): `tok_pro` (2,101s > 300s) runs on Attempt 1 & Attempt 2!
    monkeypatch.setattr(settings, "hf_expiring_quota_first_enabled", True)
    fail_first_space["on"] = True
    attempts.clear()
    eng2 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w2",
        spaces=spaces,
        hf_tokens=[*free_toks, tok_pro],
    )
    eng2.synthesize(
        "က" * 100,
        tmp_path / "out_on.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert len(attempts) == 2
    assert attempts[0][1] == tok_pro
    assert attempts[1][1] == tok_pro
    assert attempts[0][0] != attempts[1][0]

    # 3. Once `tok_pro` reaches <= 300.0s (and no <=4h resets_at), it drops back out of Expiring Head!
    fail_first_space["on"] = False
    token_rows[hf_ledger.token_digest(tok_pro)]["remaining_s"] = 300.0
    token_rows[hf_ledger.token_digest(free_toks[0])]["remaining_s"] = 70.0
    hf_ledger.save_pool_state(
        {
            "tokens": token_rows,
            "space_gates": {sp: 45.0 for sp in spaces},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=time.time(),
    )
    attempts.clear()
    eng3 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w3",
        spaces=[spaces[0]],
        hf_tokens=[*free_toks, tok_pro],
    )
    eng3.synthesize(
        "က" * 100,
        tmp_path / "out_300.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert attempts[0][1] == free_toks[0]

    # 4. Post-Dec-1 structural rule: `0 < resets_at - now <= 4h` runs earliest-reset-first ahead of +10h 300s token!
    now2 = time.time()
    tok_1h = "hf_exp_1h"
    tok_3h = "hf_exp_3h"
    tok_10h = "hf_fresh_10h"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_10h): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": _iso(now2 + 36000.0),
                    "cooldown_until": "",
                    "updated_at": _iso(now2),
                },
                hf_ledger.token_digest(tok_3h): {
                    "status": "ok",
                    "remaining_s": 180.0,
                    "base_s": 300.0,
                    "resets_at": _iso(now2 + 10800.0),
                    "cooldown_until": "",
                    "updated_at": _iso(now2),
                },
                hf_ledger.token_digest(tok_1h): {
                    "status": "ok",
                    "remaining_s": 120.0,
                    "base_s": 300.0,
                    "resets_at": _iso(now2 + 3600.0),
                    "cooldown_until": "",
                    "updated_at": _iso(now2),
                },
            },
            "space_gates": {spaces[0]: 45.0},
            "in_flight_gpu_s": 0.0,
        },
        now_ts=now2,
    )
    attempts_order: list[str | None] = []

    class _RefuseFirstClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            attempts_order.append(self.tok)
            if len(attempts_order) == 1:
                raise RuntimeError("ZeroGPU quota exceeded (45s requested vs 10s left)")
            return SimpleNamespace(
                done=lambda: True, status=lambda: None, result=lambda: str(src_wav)
            )

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _RefuseFirstClient(hf_token)),
    )
    eng4 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w4",
        spaces=[spaces[0]],
        hf_tokens=[tok_10h, tok_3h, tok_1h],
    )
    eng4.synthesize(
        "က" * 100,
        tmp_path / "out_exp.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert attempts_order == [tok_1h, tok_3h]


# ---------------------------------------------------------------------------
# §5 — Verbatim Copy, Congestion ETA Horizon & Mini App Banner
# ---------------------------------------------------------------------------


def test_5a_verbatim_copy_congestion_eta_horizon_and_miniapp_banner():
    my_json = json.loads((REPO_ROOT / "locales" / "my" / "messages.json").read_text(encoding="utf-8"))
    en_json = json.loads((REPO_ROOT / "locales" / "en" / "messages.json").read_text(encoding="utf-8"))
    js_i18n = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    js_app = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")

    expected_my = {
        "dp_park_prompt_clone_free": "⏸️ အသံ Clone လုပ်ခြင်းကို အပိုင်း {done}/{total} မှာ ခဏ ရပ်ဆိုင်းထားပါတယ် — လက်ရှိ ဆာဗာ အသုံးပြုသူ များနေလို့ပါ။ အခမဲ့ မိနစ် လုံးဝ မဖြတ်ထားပါဘူး။ ဆာဗာ အားလပ်တာနဲ့ အလိုအလျောက် ဆက်လုပ်ပေးပါမယ် (ခန့်မှန်း ~{eta})။",
        "dp_btn_cancel_free": "❌ အလုပ်ကို ပယ်ဖျက်မည် (မိနစ် မဖြတ်ပါ)",
        "dp_choice_cancel_answer_free": "အလုပ်ကို ပယ်ဖျက်ပြီးပါပြီ (အခမဲ့ မိနစ် လုံးဝ မဖြတ်ထားပါ)",
        "dp_park_expired_error_free": "ဆာဗာ ဆက်တိုက် အလုပ်များနေသဖြင့် ၂ နာရီအတွင်း ဆက်မလုပ်နိုင်သောကြောင့် အလုပ်ကို ရပ်နားလိုက်ပါတယ်။ အခမဲ့ မိနစ် လုံးဝ မဖြတ်ထားပါ။ ခဏနေမှ ပြန်စမ်းကြည့်ပေးပါရှင့်။",
        "live_job_status_waiting_capacity": "⏳ ဆာဗာ အားလပ်သည်နှင့် အလိုအလျောက် ဆက်လုပ်ပေးပါမည်",
    }
    expected_en = {
        "dp_park_prompt_clone_free": "⏸️ Your free voice generation is paused at part {done}/{total} because the voice servers are busy right now. Zero free minutes have been deducted. It will resume automatically as soon as a slot opens (estimated ~{eta}).",
        "dp_btn_cancel_free": "❌ Cancel Job (0 Minutes Used)",
        "dp_choice_cancel_answer_free": "Job cancelled (0 free minutes deducted)",
        "dp_park_expired_error_free": "The voice servers stayed busy for over 2 hours, so this job was stopped. Zero free minutes were deducted. Please try again shortly.",
        "live_job_status_waiting_capacity": "⏳ Queued to auto-resume as soon as capacity opens",
    }
    for k, v in expected_my.items():
        assert my_json[k] == v
        assert v in js_i18n
    for k, v in expected_en.items():
        assert en_json[k] == v
        assert v in js_i18n

    # Congestion ETA stays strictly inside [+10m, +20m] in Asia/Yangon HH:MM
    mmt = ZoneInfo("Asia/Yangon")
    base_dt = datetime(2026, 11, 28, 20, 0, 0, tzinfo=mmt)
    base_ts = base_dt.timestamp()
    assert hf_ledger.congestion_resume_eta_label(
        base_ts, state={"tokens": {}, "space_gates": {}, "in_flight_gpu_s": 0}
    ) == "20:10"
    assert hf_ledger.congestion_resume_eta_label(
        base_ts,
        state={
            "tokens": {},
            "space_gates": {},
            "in_flight_gpu_s": 0,
            "space_cooldowns": {"sp1": _iso(base_ts + 600.0)},
        },
    ) == "20:15"
    assert hf_ledger.congestion_resume_eta_label(
        base_ts,
        state={
            "tokens": {},
            "space_gates": {},
            "in_flight_gpu_s": 0,
            "space_cooldowns": {"sp1": _iso(base_ts + 3600.0)},
        },
    ) == "20:20"

    # `_live_job_payload` exposes `choice` when status == WAITING_CHOICE (omitted when QUEUED)
    now_utc = datetime.now(timezone.utc)
    st_wait = vc_mod.new_choice_state("clone", tier="free", park_reason="congestion")
    st_wait["choice"] = "wait"
    j_waiting = Job(
        id="vc_live_w",
        user_id=1,
        status=JobStatus.WAITING_CHOICE.value,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        created_at=now_utc,
        voice_choice_state=vc_mod.dump_choice_state(st_wait),
    )
    p_wait = api_mod._live_job_payload(j_waiting, None, now_utc)
    assert p_wait["choice"] == "wait"
    assert p_wait["tier"] == "free"

    j_queued = Job(
        id="vc_live_q",
        user_id=1,
        status=JobStatus.QUEUED.value,
        job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm",
        created_at=now_utc,
    )
    p_queued = api_mod._live_job_payload(j_queued, None, now_utc)
    assert "choice" not in p_queued

    # Mini App JS switches to `live_job_status_waiting_capacity` and hides Wait chip when choice === "wait"
    assert 'job.choice === "wait"' in js_app
    assert 't("live_job_status_waiting_capacity")' in js_app
    assert 'job.choice !== "wait"' in js_app
